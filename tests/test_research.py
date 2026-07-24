"""Contract for the corpus-BLIND research plane (``aisr/research.py``).

PRIVACY-CRITICAL (Phase-4). The research plane is the ONLY component allowed to
feed conversation data to a cloud LLM, so it must be corpus-blind BY
CONSTRUCTION: it reads ONLY the allowlisted ``MetadataView`` fields
(conversation_id, provider, account, title, created_at/updated_at, turn_count,
char_count, thread_id, tags, aliases, notes + aggregate stats) and NEVER a raw
body, a preview, a rollout path, or any PII -- even when the object it is handed
also carries one.

SYNTHETIC fixtures ONLY -- nothing here is a real conversation. Two poison tokens
are embedded in NON-allowlisted fields of the adversarial fixtures and must never
surface in a prompt / backend input (the leak-hunt oracle):

  * ``RAWBODY_SECRET_DO_NOT_LEAK`` -- stands in for a raw message body.
  * ``123-45-6789``                -- stands in for PII (a fake SSN).
"""
from types import SimpleNamespace

from aisr import research

RAWBODY = "RAWBODY_SECRET_DO_NOT_LEAK"
SSN = "123-45-6789"

# The full contract allowlist, stated INDEPENDENTLY of the module so a drift in
# either direction (a new leaky field, a dropped one) is caught.
ALLOWLIST = {
    "conversation_id", "provider", "account", "thread_id",
    "created_at", "updated_at", "turn_count", "char_count",
    "title", "notes", "tags", "aliases",
}


def _view(**kw):
    """A synthetic MetadataView-shaped object (duck-typed by attribute name)."""
    base = dict(
        conversation_id="c1", provider="claude", account="alice",
        thread_id="t1", created_at="2026-01-01", updated_at="2026-01-02",
        turn_count=3, char_count=100, title="Weekly sync", notes="follow up",
        tags=["work", "sync"], aliases=["standup"],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _poison_view(**kw):
    """A view whose ALLOWLISTED fields are clean but which ALSO carries a raw
    body / preview / PII in NON-allowlisted attributes -- exactly the shape the
    plane must be blind to."""
    v = _view(**kw)
    v.body = RAWBODY
    v.raw_body = RAWBODY
    v.preview = RAWBODY
    v.rollout_path = RAWBODY
    v.cwd = SSN
    v.git_branch = RAWBODY
    v.pii = SSN
    v.turns = [SimpleNamespace(blocks=[SimpleNamespace(text=SSN)])]
    return v


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


def test_summary_prompt_contains_allowlisted_values():
    mb = research.MockBackend()
    research.synthesize_over_metadata([
        _view(conversation_id="cid9", provider="claude", account="alice",
              thread_id="th9", title="Weekly sync", notes="ping the team",
              tags=["work", "sync"], aliases=["standup"], turn_count=7,
              char_count=4242),
    ], mb)
    p = mb.prompts[0]
    for token in ("cid9", "claude", "alice", "th9", "Weekly sync",
                  "ping the team", "work", "sync", "standup", "7", "4242"):
        assert token in p


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
    assert RAWBODY not in p and SSN not in p
    assert "Weekly sync" in p                          # a real prompt was built


# ------------------------------------------------- leak-hunt / adversarial (crux)

def test_no_raw_body_or_pii_in_summary_prompt():
    mb = research.MockBackend()
    research.synthesize_over_metadata([_poison_view()], mb)
    p = mb.prompts[0]
    assert RAWBODY not in p
    assert SSN not in p
    # ...yet the allowlisted metadata IS present, proving a real prompt was built.
    assert "Weekly sync" in p and "claude" in p


def test_projection_keys_are_exactly_the_allowlist_and_carry_no_secret():
    proj = research._project(_poison_view())
    assert set(proj) == ALLOWLIST
    blob = repr(proj)
    assert RAWBODY not in blob
    assert SSN not in blob


def test_conversation_like_object_leaks_no_raw_content():
    # A full conversation-shaped object (turns/blocks/body) that also happens to
    # expose the allowlisted metadata must still project to metadata ONLY.
    convo = SimpleNamespace(
        conversation_id="c1", provider="claude", account="alice", thread_id="t1",
        created_at="2026-01-01", updated_at="2026-01-02", turn_count=2,
        char_count=50, title="Sync", notes="", tags=[], aliases=[],
        body=RAWBODY,
        turns=[SimpleNamespace(blocks=[SimpleNamespace(text=RAWBODY + " " + SSN)])],
    )
    mb = research.MockBackend()
    research.synthesize_over_metadata([convo], mb)
    assert RAWBODY not in mb.prompts[0]
    assert SSN not in mb.prompts[0]


# ------------------------------------------------------- robustness / coercion

def test_missing_optional_fields_default_safely():
    sparse = SimpleNamespace(conversation_id="only-id", provider="claude")
    mb = research.MockBackend(response="OK")
    assert research.synthesize_over_metadata([sparse], mb) == "OK"
    p = mb.prompts[0]
    assert "only-id" in p and "claude" in p
    assert "turn_count: 0" in p and "char_count: 0" in p


def test_free_text_is_sanitized_at_the_boundary():
    # A zero-width space (U+200B) hidden in a title must be STRIPPED before it
    # can cross to the cloud agent-feed surface.
    mb = research.MockBackend()
    research.synthesize_over_metadata([_view(title="Plan​ning")], mb)
    p = mb.prompts[0]
    assert "​" not in p
    assert "Planning" in p


def test_non_string_free_text_becomes_empty():
    mb = research.MockBackend(response="OK")
    out = research.synthesize_over_metadata([_view(title=None, notes=None)], mb)
    assert out == "OK"
    assert "title: \n" in mb.prompts[0]                # empty, no crash


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


def test_tags_tuple_supported_and_non_list_becomes_empty():
    mb = research.MockBackend()
    research.synthesize_over_metadata([
        _view(tags=("alpha", "beta"), aliases=None),
    ], mb)
    p = mb.prompts[0]
    assert "tags: alpha, beta" in p
    assert p.endswith("aliases: ")                     # None -> empty list (last line)


def test_empty_view_set_aggregates_to_zero():
    mb = research.MockBackend(response="EMPTY")
    out = research.synthesize_over_metadata([], mb)
    assert out == "EMPTY"
    p = mb.prompts[0]
    assert "conversations: 0" in p
    assert "total_turns: 0" in p and "total_chars: 0" in p
    assert len(mb.prompts) == 1                          # backend still called once
