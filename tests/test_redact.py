"""Contract + airtight leak proof for the metadata allowlist projection.

SAFETY CORE (Phase-4 privacy model). The ONLY data that may cross to any cloud /
network path is a STRUCTURAL METADATA/AGGREGATE projection built BY CONSTRUCTION from a
STRICT ALLOWLIST. Raw message bodies (Block.text / Turn content), any PII, AND ALL FREE
TEXT (title / notes / tags / aliases) are FORBIDDEN to cross, ever (owner decision
2026-07-25: `title` is derived from raw content, and free text can carry PII no regex
strips — so the cloud plane gets ONLY opaque ids + timestamps + counts + aggregate).

These tests are load-bearing:
  * the field-set test proves `MetadataView` carries ONLY the allowlisted STRUCTURAL
    fields (an added `body`/`rollout_path`/`preview`/`title` field turns this red);
  * the airtight tests embed KNOWN sensitive tokens (a fake body secret + a fake SSN +
    a fake email + a local username) in the WORST places a projection could accidentally
    read them — INCLUDING the allowlisted free-text fields title/notes/tags/aliases
    (the finding-#3 blind spot the old tests never exercised) — and assert those tokens
    appear NOWHERE in the serialized projection;
  * the mutation test proves the airtight detector actually FIRES on a known
    body-passthrough (the both-states discipline) — otherwise a green airtight test
    would be vacuous.
"""
import json
from dataclasses import asdict, fields

from aisr import corpus, ir
from aisr.redact import (
    MetadataView,
    aggregate_stats,
    corpus_metadata,
    metadata_payload,
    to_metadata_view,
)

# --- known sensitive tokens (synthetic — never real corpus data) ------------------
SECRET = "RAWBODY_SECRET_DO_NOT_LEAK"
SSN = "123-45-6789"
EMAIL = "victim@example.com"
USERNAME = "Prekzursil"                       # a local path component = PII to keep out
NAME = "Jane Q. Patient"                      # freeform PII no regex reliably catches

# exact block bodies so char_count is deterministic
BODY1 = f"My SSN is {SSN} and secret {SECRET}"
BODY2 = f"Reach me at {EMAIL} urgently"

# the full allowlist — the ONLY fields permitted to cross a cloud boundary. STRUCTURAL
# ONLY: no title, no notes/tags/aliases, no body.
ALLOWLIST = {
    "conversation_id", "provider", "account", "created_at", "updated_at",
    "turn_count", "char_count", "thread_id",
}

# The free-text families that USED to cross (sanitized) and now must NOT cross at all.
FREE_TEXT_FIELDS = ("title", "notes", "tags", "aliases")


def _secret_conv(cid="conv-1", provider="claude", account="acct-a"):
    """A Conversation whose bodies (and a tool block's data payload) embed every
    sensitive token, AND whose allowlisted free-text fields (a content-derived title,
    tags/aliases/notes) also carry canaries — the projection must read none of them."""
    return ir.Conversation(
        id=cid,
        title=f"Patient intake {NAME} SSN {SSN} {SECRET}",   # content-derived + poisoned
        provider=provider,
        account=account,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
        turns=[
            ir.Turn(role="human", blocks=[ir.Block("text", text=BODY1)]),
            ir.Turn(
                role="assistant",
                blocks=[
                    ir.Block("text", text=BODY2),
                    # secret also hidden in a tool block's structured payload
                    ir.Block("tool_use", text="", data={"raw": SECRET, "ssn": SSN}),
                ],
            ),
        ],
        meta={
            "thread_id": "th-1",
            "tags": [f"mrn-{SSN}"],            # free text -> must NOT cross
            "aliases": [NAME],                 # free text -> must NOT cross
            "notes": f"contact {EMAIL}",       # free text -> must NOT cross
            # a body snippet parked in meta must NOT be read by the projection
            "preview": f"user said {SECRET}",
        },
    )


# --------------------------------------------------------------- structural allowlist

def test_metadata_view_carries_only_allowlisted_fields():
    """BY CONSTRUCTION: the dataclass exposes EXACTLY the STRUCTURAL allowlist — no body,
    no rollout_path, no preview, and no free text (title/notes/tags/aliases). Adding a
    forbidden field turns this red immediately."""
    assert {f.name for f in fields(MetadataView)} == ALLOWLIST
    # the free-text families are gone from the wire contract entirely
    names = {f.name for f in fields(MetadataView)}
    assert names.isdisjoint(FREE_TEXT_FIELDS)


# ------------------------------------------------------------- Conversation input

def test_to_metadata_view_from_conversation_maps_named_fields():
    view = to_metadata_view(_secret_conv())
    assert isinstance(view, MetadataView)
    assert view.conversation_id == "conv-1"
    assert view.provider == "claude"
    assert view.account == "acct-a"
    assert view.created_at == "2026-01-01T00:00:00Z"
    assert view.updated_at == "2026-01-02T00:00:00Z"
    assert view.turn_count == 2
    # char_count is a COUNT of body text, never the text itself
    assert view.char_count == len(BODY1) + len(BODY2)
    assert view.thread_id == "th-1"
    # free text (incl. the content-derived title) is NOT part of the structural view
    for gone in FREE_TEXT_FIELDS:
        assert not hasattr(view, gone)


def test_conversation_without_meta_uses_safe_defaults():
    conv = ir.Conversation(id="c0", title="t", provider="gemini")
    view = to_metadata_view(conv)
    assert view.thread_id == ""
    assert view.turn_count == 0
    assert view.char_count == 0
    for gone in FREE_TEXT_FIELDS:
        assert not hasattr(view, gone)


def test_char_count_counts_body_across_turns_and_empty_blocks():
    """char_count sums block-text LENGTHS across turns; a turn with no blocks
    contributes zero (and exercises the inner no-block path)."""
    conv = ir.Conversation(
        id="c-cc", title="t", provider="claude",
        turns=[
            ir.Turn("human", blocks=[]),                        # no blocks -> +0
            ir.Turn("assistant", blocks=[
                ir.Block("text", text="abc"),
                ir.Block("text", text="de"),
            ]),
        ],
    )
    view = to_metadata_view(conv)
    assert view.turn_count == 2
    assert view.char_count == 5


# ---------------------------------------------------------------------- row input

def _full_row():
    return {
        "conversation_id": "row-1",
        "provider": "chatgpt",
        "account": "acct-b",
        "title": f"Row {SSN}",
        "created_at": "2026-02-01",
        "updated_at": "2026-02-02",
        "turn_count": 5,
        "char_count": 4242,
        "thread_id": "th-2",
        # forbidden columns that exist on the row but MUST NOT cross:
        "rollout_path": rf"C:\Users\{USERNAME}\sessions\x.jsonl",
        "preview": f"assistant said {SECRET}",
        "extra_pii": SSN,
        # free-text metadata the row may carry — now dropped entirely:
        "tags": [f"mrn-{SSN}"],
        "aliases": [NAME],
        "notes": f"note {EMAIL}",
    }


def test_to_metadata_view_from_row_maps_named_fields():
    view = to_metadata_view(_full_row())
    assert view.conversation_id == "row-1"
    assert view.provider == "chatgpt"
    assert view.account == "acct-b"
    assert view.created_at == "2026-02-01"
    assert view.updated_at == "2026-02-02"
    assert view.turn_count == 5
    assert view.char_count == 4242
    assert view.thread_id == "th-2"
    for gone in FREE_TEXT_FIELDS:
        assert not hasattr(view, gone)


def test_row_extra_and_forbidden_columns_never_cross():
    """A row may carry title / rollout_path / preview / tags / arbitrary extra columns.
    None of them may appear in the projection — the allowlist is by construction, not
    passthrough, and free text is dropped whole."""
    blob = json.dumps(asdict(to_metadata_view(_full_row())))
    assert USERNAME not in blob        # rollout_path username + aliases
    assert SECRET not in blob          # preview body snippet
    assert SSN not in blob             # extra_pii column + title + tags
    assert EMAIL not in blob           # notes
    assert NAME not in blob            # aliases (freeform name)
    for col in ("rollout_path", "preview", "title", "tags", "aliases", "notes"):
        assert col not in blob


def test_row_without_optional_fields_uses_safe_defaults():
    row = {
        "conversation_id": "row-2",
        "provider": "gemini",
        "account": "",
        "created_at": "",
        "updated_at": "",
        "turn_count": 0,
        "char_count": 0,
        "thread_id": "",
    }
    view = to_metadata_view(row)
    assert view.thread_id == ""
    assert view.turn_count == 0
    assert view.char_count == 0


def test_to_metadata_view_from_real_sqlite_row():
    """The production row shape: a sqlite3.Row from the conversations table. dict(row)
    carries rowid + rollout_path + title; none may cross."""
    conn = corpus.open_index(":memory:")
    try:
        conv = _secret_conv("conv-db")
        corpus.add_conversation(
            conn, conv, thread_id="th-9",
            rollout_path=rf"C:\Users\{USERNAME}\s.jsonl",
        )
        row = conn.execute(
            "SELECT * FROM conversations WHERE conversation_id=?", (conv.id,)
        ).fetchone()
    finally:
        conn.close()
    view = to_metadata_view(row)     # sqlite3.Row retains its values after close
    assert view.conversation_id == "conv-db"
    assert view.thread_id == "th-9"
    assert view.turn_count == 2
    blob = json.dumps(asdict(view))
    assert USERNAME not in blob        # rollout_path must not cross
    assert "rowid" not in blob         # sqlite artefact must not cross
    assert "title" not in blob         # the content-derived title must not cross
    assert SECRET not in blob
    assert SSN not in blob


# --------------------------------------------------------------------- no free text

def test_free_text_fields_never_cross():
    """Owner decision 2026-07-25 (Phase-4 grill): NO free text crosses. `title` (which is
    RAW-CONTENT-DERIVED), notes, tags and aliases are dropped from the projection BY
    CONSTRUCTION, so PII placed in them — which the old 'sanitize only' model let through
    — cannot reach the cloud. This is the finding-#3 blind spot made explicit: poison in
    an ALLOWLISTED field, not merely in a body / preview / rollout_path."""
    conv = ir.Conversation(
        id="c-ft", provider="claude", account="a",
        title=f"Chemo plan for {NAME} SSN {SSN}",          # hostile content-derived title
        meta={
            "thread_id": "t",
            "tags": [f"mrn-{SSN}"],
            "aliases": [NAME],
            "notes": f"reach {EMAIL}",
        },
    )
    view = to_metadata_view(conv)
    # the fields themselves are gone from the wire contract ...
    for gone in FREE_TEXT_FIELDS:
        assert not hasattr(view, gone)
    # ... so no token placed in them can appear anywhere in the serialization.
    blob = json.dumps(asdict(view))
    for token in (SSN, NAME, EMAIL):
        assert token not in blob, f"LEAK: {token!r} crossed via a free-text field"
    assert "title" not in blob and "tags" not in blob


def test_hidden_unicode_in_free_text_cannot_ride_across_either():
    """Belt-and-braces: even a hidden-unicode smuggle in a free-text field is moot now,
    because the whole field is dropped. (The old model relied on sanitize_for_copy here;
    dropping the field is strictly stronger.)"""
    conv = ir.Conversation(
        id="c-zw", title="Onc\u200bology", provider="claude",
        meta={"tags": ["ta\u200bg"], "aliases": ["ali\u202eas"], "notes": "no\u200bte"},
    )
    blob = json.dumps(asdict(to_metadata_view(conv)))
    for ch in ("\u200b", "\u202e", "Oncology", "note"):
        assert ch not in blob


# ------------------------------------------------------------------- corpus_metadata

def test_corpus_metadata_projects_each_conversation_in_order():
    cor = corpus.Corpus(conversations=[_secret_conv("a"), _secret_conv("b")])
    views = corpus_metadata(cor)
    assert [v.conversation_id for v in views] == ["a", "b"]
    assert all(isinstance(v, MetadataView) for v in views)


def test_corpus_metadata_empty():
    assert corpus_metadata(corpus.Corpus()) == []


# -------------------------------------------------------------------- aggregate_stats

def _mixed_corpus():
    c1 = _secret_conv("conv-1", provider="claude", account="acct-a")   # 2 turns
    c2 = ir.Conversation(
        id="conv-2", title="Follow up", provider="claude", account="acct-a",
        turns=[ir.Turn("human", [ir.Block("text", text="hi")])],       # 1 turn, 2 chars
    )
    c3 = ir.Conversation(
        id="conv-3", title="Other", provider="chatgpt", account="acct-b", turns=[],
    )
    cor = corpus.Corpus(conversations=[c1, c2, c3])
    cor.add_thread(corpus.ThreadMeta(
        id="th-1", preview=f"leak {SECRET}", rollout_path=rf"C:\Users\{USERNAME}\x"))
    cor.add_thread(corpus.ThreadMeta(id="th-2"))
    cor.add_edge(corpus.SpawnEdge("th-1", "th-2"))
    return cor


def test_aggregate_stats_counts_and_histograms():
    stats = aggregate_stats(_mixed_corpus())
    assert stats["conversation_count"] == 3
    assert stats["thread_count"] == 2
    assert stats["edge_count"] == 1
    assert stats["providers"] == {"claude": 2, "chatgpt": 1}
    assert stats["accounts"] == {"acct-a": 2, "acct-b": 1}
    assert stats["total_turns"] == 3
    assert stats["total_chars"] == (len(BODY1) + len(BODY2)) + len("hi")


def test_aggregate_stats_never_leaks_thread_preview_or_path():
    blob = json.dumps(aggregate_stats(_mixed_corpus()))
    assert SECRET not in blob
    assert USERNAME not in blob


def test_aggregate_stats_empty_corpus():
    stats = aggregate_stats(corpus.Corpus())
    assert stats["conversation_count"] == 0
    assert stats["thread_count"] == 0
    assert stats["edge_count"] == 0
    assert stats["providers"] == {}
    assert stats["accounts"] == {}
    assert stats["total_turns"] == 0
    assert stats["total_chars"] == 0


# ---------------------------------------------------- AIRTIGHT: nothing sensitive crosses

def test_metadata_payload_is_airtight():
    """The full object the research plane may send. No body text, no PII, no free text,
    no local path component appears anywhere in its JSON serialization."""
    cor = _mixed_corpus()
    blob = json.dumps(metadata_payload(cor))
    for token in (SECRET, SSN, EMAIL, USERNAME, NAME):
        assert token not in blob, f"LEAK: {token!r} crossed the boundary"
    # sanity: the payload is non-trivial (it really did project the conversations)
    payload = metadata_payload(cor)
    assert len(payload["conversations"]) == 3
    assert payload["aggregate"]["conversation_count"] == 3


def test_single_view_json_is_airtight():
    blob = json.dumps(asdict(to_metadata_view(_secret_conv())))
    for token in (SECRET, SSN, EMAIL, USERNAME, NAME):
        assert token not in blob


def test_metadata_payload_empty_corpus():
    payload = metadata_payload(corpus.Corpus())
    assert payload["conversations"] == []
    assert payload["aggregate"]["conversation_count"] == 0


# ------------------------------------------------- MUTATION / both-states detector proof

def _leaky_projection(conv):
    """A DELIBERATELY broken projection that passes body text AND the content-derived
    title through — the mutant `to_metadata_view` the airtight test MUST be able to
    catch."""
    d = asdict(to_metadata_view(conv))
    d["body"] = "\n".join(b.text for t in conv.turns for b in t.blocks)
    d["title"] = conv.title
    return d


def test_mutation_body_and_title_passthrough_is_detected():
    """Both-states proof: the airtight detector is silent on the real projection AND
    FIRES on a known body/title passthrough. A detector silent in both states proves
    nothing.
    """
    conv = _secret_conv()
    real = json.dumps(asdict(to_metadata_view(conv)))
    leaky = json.dumps(_leaky_projection(conv))
    # real projection is clean ...
    assert SECRET not in real
    assert SSN not in real
    assert NAME not in real            # freeform name in the title does NOT cross
    # ... and the SAME detector catches the mutant that leaks the body + title
    assert SECRET in leaky             # body secret
    assert SSN in leaky                # body/title SSN
    assert NAME in leaky               # title freeform name
