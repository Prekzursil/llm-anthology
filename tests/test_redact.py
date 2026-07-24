"""Contract + airtight leak proof for the metadata allowlist projection.

SAFETY CORE (Phase-4 privacy model). The ONLY data that may cross to any cloud /
network path is a SANITIZED METADATA/AGGREGATE projection built BY CONSTRUCTION from a
STRICT ALLOWLIST. Raw message bodies (Block.text / Turn content) and any PII are
FORBIDDEN to cross, ever.

These tests are load-bearing:
  * the field-set test proves `MetadataView` carries ONLY the allowlisted fields (an
    added `body`/`rollout_path`/`preview` field turns this red);
  * the airtight tests embed KNOWN sensitive tokens (a fake body secret + a fake SSN +
    a fake email + a local username in a path) in the WORST places a projection could
    accidentally read them (block text, block data, a row's extra columns, a thread
    preview) and assert those tokens appear NOWHERE in the serialized projection;
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

# exact block bodies so char_count is deterministic
BODY1 = f"My SSN is {SSN} and secret {SECRET}"
BODY2 = f"Reach me at {EMAIL} urgently"

# the full allowlist — the ONLY fields permitted to cross a cloud boundary
ALLOWLIST = {
    "conversation_id", "provider", "account", "title", "created_at", "updated_at",
    "turn_count", "char_count", "thread_id", "tags", "aliases", "notes",
}


def _secret_conv(cid="conv-1", provider="claude", account="acct-a"):
    """A Conversation whose bodies (and a tool block's data payload) embed every
    sensitive token, in the places a careless projection might slurp them."""
    return ir.Conversation(
        id=cid,
        title="Patient Intake",
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
            "tags": ["oncology"],
            "aliases": ["case-42"],
            "notes": "clinician note",
            # a body snippet parked in meta must NOT be read by the projection
            "preview": f"user said {SECRET}",
        },
    )


# --------------------------------------------------------------- structural allowlist

def test_metadata_view_carries_only_allowlisted_fields():
    """BY CONSTRUCTION: the dataclass exposes EXACTLY the allowlist — no body, no
    rollout_path, no preview. Adding a forbidden field turns this red immediately."""
    assert {f.name for f in fields(MetadataView)} == ALLOWLIST


# ------------------------------------------------------------- Conversation input

def test_to_metadata_view_from_conversation_maps_named_fields():
    view = to_metadata_view(_secret_conv())
    assert isinstance(view, MetadataView)
    assert view.conversation_id == "conv-1"
    assert view.provider == "claude"
    assert view.account == "acct-a"
    assert view.title == "Patient Intake"
    assert view.created_at == "2026-01-01T00:00:00Z"
    assert view.updated_at == "2026-01-02T00:00:00Z"
    assert view.turn_count == 2
    # char_count is a COUNT of body text, never the text itself
    assert view.char_count == len(BODY1) + len(BODY2)
    assert view.thread_id == "th-1"
    assert view.tags == ("oncology",)
    assert view.aliases == ("case-42",)
    assert view.notes == "clinician note"


def test_conversation_without_meta_uses_safe_defaults():
    conv = ir.Conversation(id="c0", title="t", provider="gemini")
    view = to_metadata_view(conv)
    assert view.thread_id == ""
    assert view.tags == ()
    assert view.aliases == ()
    assert view.notes == ""
    assert view.turn_count == 0
    assert view.char_count == 0


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
        "title": "Row Title",
        "created_at": "2026-02-01",
        "updated_at": "2026-02-02",
        "turn_count": 5,
        "char_count": 4242,
        "thread_id": "th-2",
        # forbidden columns that exist on the row but MUST NOT cross:
        "rollout_path": rf"C:\Users\{USERNAME}\sessions\x.jsonl",
        "preview": f"assistant said {SECRET}",
        "extra_pii": SSN,
        # forward-looking optional metadata the row may carry:
        "tags": ["t1", "t2"],
        "aliases": ["alias-1"],
        "notes": "row note",
    }


def test_to_metadata_view_from_row_maps_named_fields():
    view = to_metadata_view(_full_row())
    assert view.conversation_id == "row-1"
    assert view.provider == "chatgpt"
    assert view.account == "acct-b"
    assert view.title == "Row Title"
    assert view.created_at == "2026-02-01"
    assert view.updated_at == "2026-02-02"
    assert view.turn_count == 5
    assert view.char_count == 4242
    assert view.thread_id == "th-2"
    assert view.tags == ("t1", "t2")
    assert view.aliases == ("alias-1",)
    assert view.notes == "row note"


def test_row_extra_and_forbidden_columns_never_cross():
    """A row may carry rollout_path / preview / arbitrary extra columns. None of them
    may appear in the projection — the allowlist is by construction, not passthrough."""
    blob = json.dumps(asdict(to_metadata_view(_full_row())))
    assert USERNAME not in blob        # rollout_path username
    assert SECRET not in blob          # preview body snippet
    assert SSN not in blob             # extra_pii column
    assert "rollout_path" not in blob
    assert "preview" not in blob


def test_row_without_optional_fields_uses_safe_defaults():
    row = {
        "conversation_id": "row-2",
        "provider": "gemini",
        "account": "",
        "title": "",
        "created_at": "",
        "updated_at": "",
        "turn_count": 0,
        "char_count": 0,
        "thread_id": "",
    }
    view = to_metadata_view(row)
    assert view.tags == ()
    assert view.aliases == ()
    assert view.notes == ""


def test_to_metadata_view_from_real_sqlite_row():
    """The production row shape: a sqlite3.Row from the conversations table. dict(row)
    carries rowid + rollout_path; neither may cross."""
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
    assert SECRET not in blob


# --------------------------------------------------------------------- sanitization

def test_free_text_fields_are_sanitized():
    """title / notes / aliases / tags cross as free text, so hidden-unicode
    smuggling payloads must be stripped (aisr.sanitize.sanitize_for_copy)."""
    conv = ir.Conversation(
        id="c-s",
        title="Onc\u200bology",                       # zero-width space
        provider="claude",
        meta={
            "tags": ["ta\u200bg"],
            "aliases": ["ali\u202eas"],               # RTL override
            "notes": "no\u200bte",
        },
    )
    view = to_metadata_view(conv)
    assert view.title == "Oncology"
    assert view.tags == ("tag",)
    assert view.aliases == ("alias",)
    assert view.notes == "note"
    # no flagged invisible survives anywhere in the serialized view
    blob = json.dumps(asdict(view))
    for ch in ("\u200b", "\u202e"):
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
    """The full object the research plane may send. No body text, no PII, no local
    path component appears anywhere in its JSON serialization."""
    cor = _mixed_corpus()
    blob = json.dumps(metadata_payload(cor))
    for token in (SECRET, SSN, EMAIL, USERNAME):
        assert token not in blob, f"LEAK: {token!r} crossed the boundary"
    # sanity: the payload is non-trivial (it really did project the conversations)
    payload = metadata_payload(cor)
    assert len(payload["conversations"]) == 3
    assert payload["aggregate"]["conversation_count"] == 3


def test_single_view_json_is_airtight():
    blob = json.dumps(asdict(to_metadata_view(_secret_conv())))
    for token in (SECRET, SSN, EMAIL):
        assert token not in blob


def test_metadata_payload_empty_corpus():
    payload = metadata_payload(corpus.Corpus())
    assert payload["conversations"] == []
    assert payload["aggregate"]["conversation_count"] == 0


# ------------------------------------------------- MUTATION / both-states detector proof

def _leaky_projection(conv):
    """A DELIBERATELY broken projection that passes body text through — the mutant
    `to_metadata_view` the airtight test MUST be able to catch."""
    d = asdict(to_metadata_view(conv))
    d["body"] = "\n".join(b.text for t in conv.turns for b in t.blocks)
    return d


def test_mutation_body_passthrough_is_detected():
    """Both-states proof: the airtight detector is silent on the real projection AND
    FIRES on a known body-passthrough. A detector silent in both states proves nothing.
    """
    conv = _secret_conv()
    real = json.dumps(asdict(to_metadata_view(conv)))
    leaky = json.dumps(_leaky_projection(conv))
    # real projection is clean ...
    assert SECRET not in real
    assert SSN not in real
    # ... and the SAME detector catches the mutant that leaks the body
    assert SECRET in leaky
    assert SSN in leaky
