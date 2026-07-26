"""Contract for the corpus-BLIND research plane (``llm_anthology/research.py``).

PRIVACY-CRITICAL (Phase-4). The research plane is the ONLY component allowed to
feed conversation data to a cloud LLM, so it must be corpus-blind BY
CONSTRUCTION: it reads ONLY the allowlisted STRUCTURAL ``MetadataView`` fields
(conversation_id, provider, account, thread_id, created_at/updated_at,
turn_count, char_count + aggregate stats) and NEVER a raw body, a title, any
free text (notes/tags/aliases), a preview, a rollout path, or any PII -- even
when the object it is handed also carries one.

NO FREE TEXT CROSSES. This is stricter than "sanitize the free text" and it is
deliberate (owner decision 2026-07-25): a conversation ``title`` is derived from
raw message content, and free text can carry PII no regex reliably strips. An
earlier iteration allowed sanitized free text and was PROVEN in a live probe to
carry an SSN, an email, a freeform patient name and a drug name into the cloud
prompt.

SYNTHETIC fixtures ONLY -- nothing here is a real conversation. The poison tokens
are embedded BOTH in non-allowlisted fields AND in the free-text fields that used
to cross, and must never surface in a prompt / backend input (the leak-hunt
oracle):

  * ``RAWBODY_SECRET_DO_NOT_LEAK`` -- stands in for a raw message body.
  * ``123-45-6789``                -- PII: a fake SSN.
  * ``victim@example.com``         -- PII: a fake email.
  * ``Jane Q. Patient``            -- PII: a freeform name no regex catches.
  * ``Zynflaxen-250``              -- sensitive non-PII content (a drug name).
"""
from types import SimpleNamespace

from llm_anthology import research

RAWBODY = "RAWBODY_SECRET_DO_NOT_LEAK"
SSN = "123-45-6789"
EMAIL = "victim@example.com"
NAME = "Jane Q. Patient"
COMPOUND = "Zynflaxen-250"

SENSITIVE = (RAWBODY, SSN, EMAIL, NAME, COMPOUND)

# The full contract allowlist, stated INDEPENDENTLY of the module so a drift in
# either direction (a new leaky field, a dropped one) is caught. STRUCTURAL ONLY.
ALLOWLIST = {
    "conversation_id", "provider", "account", "thread_id",
    "created_at", "updated_at", "turn_count", "char_count",
}


def _view(**kw):
    """A synthetic MetadataView-shaped object (duck-typed by attribute name),
    carrying ONLY the structural allowlist -- the real crossing surface."""
    base = dict(
        conversation_id="c1", provider="claude", account="alice",
        thread_id="t1", created_at="2026-01-01", updated_at="2026-01-02",
        turn_count=3, char_count=100,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _poison_view(**kw):
    """A view whose STRUCTURAL fields are clean but which ALSO carries a raw
    body / preview / PII in non-allowlisted attributes AND in the free-text
    family (title/notes/tags/aliases) that must no longer cross -- exactly the
    shape the plane must be blind to."""
    v = _view(**kw)
    # the leak class that actually shipped: free text, incl. a content-derived title
    v.title = f"Chemo plan {COMPOUND} for {NAME} SSN {SSN}"
    v.notes = f"reach {EMAIL}"
    v.tags = [f"mrn-{SSN}", COMPOUND]
    v.aliases = [NAME]
    # and the raw-body / path family
    v.body = RAWBODY
    v.raw_body = RAWBODY
    v.preview = RAWBODY
    v.rollout_path = RAWBODY
    v.cwd = SSN
    v.git_branch = RAWBODY
    v.pii = SSN
    v.turns = [SimpleNamespace(blocks=[SimpleNamespace(text=SSN)])]
    return v


def _leaks_in(haystack):
    """The ONE oracle: the set of canaries present in a prompt/response."""
    blob = haystack if isinstance(haystack, str) else repr(haystack)
    return {t for t in SENSITIVE if t in blob}


# --------------------------------------------------------------- LLM backend

def test_llmbackend_is_runtime_checkable_protocol():
    assert isinstance(research.MockBackend(), research.LLMBackend)
    assert not isinstance(object(), research.LLMBackend)


def test_mockbackend_records_prompt_and_returns_default():
    mb = research.MockBackend(response="CANNED")
    assert mb.synthesize("hello") == "CANNED"
    assert mb.prompts == ["hello"]


def test_mockbackend_responder_overrides_default():
    mb = research.MockBackend(response="CANNED", responder=lambda p: "R:" + p)
    assert mb.synthesize("x") == "R:x"
    assert mb.prompts == ["x"]


# ------------------------------------------------------- synthesize_over_metadata

def test_synthesize_returns_backend_output():
    mb = research.MockBackend(response="THE SUMMARY")
    assert research.synthesize_over_metadata([_view()], mb) == "THE SUMMARY"


def test_summary_prompt_contains_allowlisted_structural_values():
    mb = research.MockBackend()
    research.synthesize_over_metadata([
        _view(conversation_id="cid9", provider="claude", account="alice",
              thread_id="th9", created_at="2026-05-05", updated_at="2026-05-06",
              turn_count=7, char_count=4242),
    ], mb)
    p = mb.prompts[0]
    for token in ("cid9", "claude", "alice", "th9", "2026-05-05", "2026-05-06",
                  "7", "4242"):
        assert token in p


def test_summary_prompt_declares_no_free_text_and_emits_none():
    """The prompt must neither claim nor carry free text: no title/notes/tags/
    aliases key is rendered at all."""
    mb = research.MockBackend()
    research.synthesize_over_metadata([_poison_view()], mb)
    p = mb.prompts[0]
    for key in ("title:", "notes:", "tags:", "aliases:"):
        assert key not in p, f"free-text key {key!r} rendered into the cloud prompt"


def test_summary_prompt_contains_aggregate_stats():
    mb = research.MockBackend()
    research.synthesize_over_metadata([
        _view(conversation_id="a", provider="claude", account="alice",
              turn_count=3, char_count=10),
        _view(conversation_id="b", provider="chatgpt", account="bob",
              turn_count=5, char_count=90),
    ], mb)
    p = mb.prompts[0]
    assert "conversations: 2" in p
    assert "total_turns: 8" in p
    assert "total_chars: 100" in p
    assert "chatgpt=1" in p and "claude=1" in p       # key-sorted histogram
    assert "alice=1" in p and "bob=1" in p


def test_summary_accepts_a_generator_of_views():
    mb = research.MockBackend(response="OK")
    out = research.synthesize_over_metadata((v for v in [_view(), _view()]), mb)
    assert out == "OK"
    assert "conversations: 2" in mb.prompts[0]


# ----------------------------------------------------------- extract_entities

def test_extract_entities_parses_newline_list_dedup_and_empties():
    # Alpha(add) Beta(add) Alpha(dup) ''(empty) Gamma(add)
    mb = research.MockBackend(response="Alpha\nBeta\nAlpha\n\nGamma")
    assert research.extract_entities([_view()], mb) == ["Alpha", "Beta", "Gamma"]


def test_extract_entities_parses_comma_and_dash_bullets():
    mb = research.MockBackend(response="- Alpha, Beta ,Alpha")
    assert research.extract_entities([_view()], mb) == ["Alpha", "Beta"]


def test_extract_entities_non_string_response_is_empty():
    mb = research.MockBackend(response=12345)          # not a str
    assert research.extract_entities([_view()], mb) == []


def test_extract_entities_uses_metadata_only_prompt():
    mb = research.MockBackend(response="Alpha")
    research.extract_entities([_poison_view()], mb)
    p = mb.prompts[0]
    assert _leaks_in(p) == set()
    assert "c1" in p                                   # a real prompt was built


# ------------------------------------------------- leak-hunt / adversarial (crux)

def test_no_raw_body_pii_or_free_text_in_summary_prompt():
    mb = research.MockBackend()
    research.synthesize_over_metadata([_poison_view()], mb)
    p = mb.prompts[0]
    assert _leaks_in(p) == set(), "a canary crossed to the cloud research plane"
    # ...yet the structural metadata IS present, proving a real prompt was built.
    assert "c1" in p and "claude" in p


def test_projection_keys_are_exactly_the_allowlist_and_carry_no_secret():
    proj = research._project(_poison_view())
    assert set(proj) == ALLOWLIST
    assert _leaks_in(proj) == set()


def test_conversation_like_object_leaks_no_raw_content():
    # A full conversation-shaped object (turns/blocks/body/title) that also happens
    # to expose the allowlisted metadata must still project to structural data ONLY.
    convo = SimpleNamespace(
        conversation_id="c1", provider="claude", account="alice", thread_id="t1",
        created_at="2026-01-01", updated_at="2026-01-02", turn_count=2,
        char_count=50,
        title=f"{NAME} — {COMPOUND}", notes=EMAIL, tags=[SSN], aliases=[NAME],
        body=RAWBODY,
        turns=[SimpleNamespace(blocks=[SimpleNamespace(text=RAWBODY + " " + SSN)])],
    )
    mb = research.MockBackend()
    research.synthesize_over_metadata([convo], mb)
    assert _leaks_in(mb.prompts[0]) == set()


def test_both_states_control_detector_fires_on_a_leaky_render():
    """Both-states discipline: the SAME oracle that reads zero on every real path
    MUST fire when free text actually crosses. A detector that can never fire makes
    every airtight assertion above unciteable."""
    v = _poison_view()
    clean_mb = research.MockBackend()
    research.synthesize_over_metadata([v], clean_mb)
    # a DELIBERATELY leaky renderer: the pre-fix behaviour (free text emitted)
    leaky_prompt = "\n".join((
        "  title: %s" % v.title,
        "  notes: %s" % v.notes,
        "  tags: %s" % ", ".join(v.tags),
        "  aliases: %s" % ", ".join(v.aliases),
        "  body: %s" % v.body,
    ))
    assert _leaks_in(clean_mb.prompts[0]) == set()      # silent on the real path
    assert _leaks_in(leaky_prompt) == set(SENSITIVE)    # fires on the leak


# ------------------------------------------------------- robustness / coercion

def test_missing_optional_fields_default_safely():
    sparse = SimpleNamespace(conversation_id="only-id", provider="claude")
    mb = research.MockBackend(response="OK")
    assert research.synthesize_over_metadata([sparse], mb) == "OK"
    p = mb.prompts[0]
    assert "only-id" in p and "claude" in p
    assert "turn_count: 0" in p and "char_count: 0" in p


def test_non_integer_counts_become_zero():
    mb = research.MockBackend()
    research.synthesize_over_metadata([_view(turn_count="lots", char_count=None)], mb)
    p = mb.prompts[0]
    assert "turn_count: 0" in p and "char_count: 0" in p
    assert "total_turns: 0" in p


def test_timestamp_integer_is_stringified():
    mb = research.MockBackend()
    research.synthesize_over_metadata([_view(created_at=1700000000000)], mb)
    assert "created_at: 1700000000000" in mb.prompts[0]


def test_none_timestamp_becomes_empty():
    mb = research.MockBackend(response="OK")
    out = research.synthesize_over_metadata([_view(updated_at=None)], mb)
    assert out == "OK"
    assert "updated_at: \n" in mb.prompts[0]


def test_empty_view_set_aggregates_to_zero():
    mb = research.MockBackend(response="EMPTY")
    out = research.synthesize_over_metadata([], mb)
    assert out == "EMPTY"
    p = mb.prompts[0]
    assert "conversations: 0" in p
    assert "total_turns: 0" in p and "total_chars: 0" in p
    assert "by_provider: \n" in p                        # empty histogram renders ""
    assert len(mb.prompts) == 1                          # backend still called once
