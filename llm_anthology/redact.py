"""Metadata allowlist projection — the Phase-4 privacy SAFETY CORE.

Private medical/pharma conversation data lives in this corpus, so the cloud research
plane may only ever see a SANITIZED METADATA/AGGREGATE projection. This module is the
one boundary that produces it.

The projection is a STRICT ALLOWLIST built BY CONSTRUCTION: `MetadataView` names every
field that is permitted to cross, and `to_metadata_view` populates each one from an
explicitly-named source. There is deliberately NO `MetadataView(**row)` /
`asdict(conversation)` passthrough anywhere — a passthrough could silently carry a new
field (a body, a title, a file path, a preview snippet) across the boundary the day
someone adds one. Extra columns a row happens to carry (`title`, `rollout_path`,
`preview`, `rowid`, `tags`, ...) are simply never read, so they cannot cross even by
accident. This is verified adversarially in tests/test_redact.py.

ALLOWLIST — STRUCTURAL / AGGREGATE ONLY (the ONLY fields permitted to cross):
    conversation_id, provider, account, created_at, updated_at, turn_count,
    char_count, thread_id
plus the aggregate counts/histograms from `aggregate_stats`.

FORBIDDEN to cross, ever: raw message bodies (Block.text / Turn content), ANY PII, AND
ALL FREE TEXT — `title`, `notes`, `tags`, `aliases` included. This is stricter than a
"sanitize the free text" model, and it is deliberate (owner decision 2026-07-25, after
an adversarial leak-hunt): a conversation `title` is DERIVED FROM RAW CONTENT — the
Codex adapter builds it from the first line of the first user message
(adapters/codex_rollout.py `title = _first_line(first_user, 80)`), and ChatGPT/Claude/
Gemini titles are provider auto-summaries of the content — so letting `title` cross
would systematically leak raw content, and free text can carry PII (names, SSNs,
compound names, clinical questions) that no regex reliably strips. A live probe of the
previous iteration carried an SSN, an email, a freeform patient name and a drug name
into the cloud prompt. The cloud research plane therefore receives ONLY opaque
identifiers, timestamps, counts, and aggregate histograms. Per-conversation,
content-aware synthesis exists only on the on-box LOCAL tier (llm_anthology/sidecar.py
`_research_local`), which never egresses.

`char_count` is a COUNT of body text (an int), never the text itself — the bodies are
read only to measure their length, never copied into a field.

NOTE for the future csm metadata port: aliases/tags/notes are an app-owned LOCAL
metadata layer (shown in the cockpit UI, stored on-box). They are LOCAL by design and
must NOT be re-added to this cloud projection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from llm_anthology.ir import Conversation

__all__ = [
    "MetadataView",
    "to_metadata_view",
    "corpus_metadata",
    "aggregate_stats",
    "metadata_payload",
]


@dataclass(frozen=True)
class MetadataView:
    """The allowlisted, STRUCTURAL projection of ONE conversation.

    Frozen (immutable) and carrying ONLY structural/opaque fields: no body, no title,
    no notes/tags/aliases, no rollout_path, no preview — nothing derived from raw
    content, and no free text at all. Its field set IS the wire contract to the cloud
    research plane; adding a field here is the only way to widen what crosses, and that
    is a reviewable one-line change.
    """
    conversation_id: str
    provider: str
    account: str
    created_at: str
    updated_at: str
    turn_count: int
    char_count: int                     # a COUNT of body chars, never the body
    thread_id: str


def _from_conversation(conv):
    """Project an `ir.Conversation`. Bodies are read ONLY to count characters; the text
    itself never lands in a field. `title` and the free-text `meta` (tags/aliases/notes)
    are deliberately NOT read — see the module docstring (owner decision 2026-07-25)."""
    return MetadataView(
        conversation_id=conv.id,
        provider=conv.provider,
        account=conv.account,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        turn_count=len(conv.turns),
        char_count=sum(len(b.text) for t in conv.turns for b in t.blocks),
        thread_id=conv.meta.get("thread_id", ""),
    )


def _from_row(row):
    """Project a conversations-table row (a sqlite3.Row or a mapping). `dict(row)` is
    only a uniform accessor; each allowlisted field is then read by NAME, so any extra
    column the row carries (`title` / `rollout_path` / `preview` / `rowid` / `tags` /
    ...) is dropped."""
    d = dict(row)
    return MetadataView(
        conversation_id=d.get("conversation_id", ""),
        provider=d.get("provider", ""),
        account=d.get("account", ""),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
        turn_count=d.get("turn_count", 0),
        char_count=d.get("char_count", 0),
        thread_id=d.get("thread_id", ""),
    )


def to_metadata_view(conv_or_row):
    """Project an `ir.Conversation` OR a conversations-table row into a `MetadataView`.

    The single entry point for the boundary: whatever the caller has (a live IR object
    or a persisted row), the result is the same strict, structural allowlist.
    """
    if isinstance(conv_or_row, Conversation):
        return _from_conversation(conv_or_row)
    return _from_row(conv_or_row)


def corpus_metadata(corpus):
    """Every conversation in `corpus` as a list of `MetadataView` (order preserved)."""
    return [to_metadata_view(conv) for conv in corpus.conversations]


def aggregate_stats(corpus):
    """Corpus-level counts and histograms — the aggregate side of the allowlist.

    Only counts and allowlisted labels (provider / account) leave; thread `preview`,
    `title` and `rollout_path` are never read, so no body snippet, title, or local path
    can ride an aggregate across the boundary.
    """
    views = corpus_metadata(corpus)
    providers = {}
    accounts = {}
    total_turns = 0
    total_chars = 0
    for v in views:
        providers[v.provider] = providers.get(v.provider, 0) + 1
        accounts[v.account] = accounts.get(v.account, 0) + 1
        total_turns += v.turn_count
        total_chars += v.char_count
    return {
        "conversation_count": len(views),
        "thread_count": len(corpus.threads),
        "edge_count": len(corpus.edges),
        "providers": providers,
        "accounts": accounts,
        "total_turns": total_turns,
        "total_chars": total_chars,
    }


def metadata_payload(corpus):
    """The complete JSON-able object the research plane may send: the per-conversation
    structural projections plus the aggregate stats. This is the whole crossing surface;
    if a token is absent here it cannot reach the cloud through this module.
    """
    return {
        "conversations": [asdict(v) for v in corpus_metadata(corpus)],
        "aggregate": aggregate_stats(corpus),
    }
