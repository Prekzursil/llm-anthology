"""Metadata allowlist projection — the Phase-4 privacy SAFETY CORE.

Private medical/pharma conversation data lives in this corpus, so the cloud research
plane may only ever see a SANITIZED METADATA/AGGREGATE projection. This module is the
one boundary that produces it.

The projection is a STRICT ALLOWLIST built BY CONSTRUCTION: `MetadataView` names every
field that is permitted to cross, and `to_metadata_view` populates each one from an
explicitly-named source. There is deliberately NO `MetadataView(**row)` /
`asdict(conversation)` passthrough anywhere — a passthrough could silently carry a new
field (a body, a file path, a preview snippet) across the boundary the day someone adds
one. Extra columns a row happens to carry (`rollout_path`, `preview`, `rowid`, ...) are
simply never read, so they cannot cross even by accident. This is verified adversarially
in tests/test_redact.py.

ALLOWLIST (the ONLY fields permitted to cross):
    conversation_id, provider, account, title*, created_at, updated_at, turn_count,
    char_count, thread_id, tags*, aliases*, notes*        (* = free text, sanitized)
plus the aggregate counts/histograms from `aggregate_stats`.

FORBIDDEN to cross, ever: raw message bodies (Block.text / Turn content) and any PII.
`char_count` is a COUNT of body text (an int), never the text itself — the bodies are
read only to measure their length, never copied into a field.

Every free-text field that crosses (title / notes / aliases, and tags as
defence-in-depth) is passed through `aisr.sanitize.sanitize_for_copy` first, so a
hidden-unicode prompt-injection payload cannot ride a label across to the next model.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from aisr.ir import Conversation
from aisr.sanitize import sanitize_for_copy

__all__ = [
    "MetadataView",
    "to_metadata_view",
    "corpus_metadata",
    "aggregate_stats",
    "metadata_payload",
]


@dataclass(frozen=True)
class MetadataView:
    """The sanitized, allowlisted projection of ONE conversation.

    Frozen (immutable) and carrying ONLY the allowlisted fields: no body, no
    rollout_path, no preview. Its field set IS the wire contract — adding a field here
    is the only way to widen what crosses, and that is a reviewable one-line change.
    """
    conversation_id: str
    provider: str
    account: str
    title: str                          # sanitized free text
    created_at: str
    updated_at: str
    turn_count: int
    char_count: int                     # a COUNT of body chars, never the body
    thread_id: str
    tags: tuple[str, ...] = ()          # sanitized (defence-in-depth)
    aliases: tuple[str, ...] = ()       # sanitized free text
    notes: str = ""                     # sanitized free text


def _sanitize_seq(seq):
    """Sanitize every element of a free-text sequence into an immutable tuple."""
    return tuple(sanitize_for_copy(x) for x in seq)


def _from_conversation(conv):
    """Project an `ir.Conversation`. Bodies are read ONLY to count characters; the
    text itself never lands in a field. tags/aliases/notes/thread_id come from the
    conversation's extensibility `meta` dict by NAMED key, not by dumping `meta`."""
    meta = conv.meta
    return MetadataView(
        conversation_id=conv.id,
        provider=conv.provider,
        account=conv.account,
        title=sanitize_for_copy(conv.title),
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        turn_count=len(conv.turns),
        char_count=sum(len(b.text) for t in conv.turns for b in t.blocks),
        thread_id=meta.get("thread_id", ""),
        tags=_sanitize_seq(meta.get("tags", ())),
        aliases=_sanitize_seq(meta.get("aliases", ())),
        notes=sanitize_for_copy(meta.get("notes", "")),
    )


def _from_row(row):
    """Project a conversations-table row (a sqlite3.Row or a mapping). `dict(row)` is
    only a uniform accessor; each allowlisted field is then read by NAME, so any extra
    column the row carries (rollout_path / preview / rowid / ...) is dropped."""
    d = dict(row)
    return MetadataView(
        conversation_id=d.get("conversation_id", ""),
        provider=d.get("provider", ""),
        account=d.get("account", ""),
        title=sanitize_for_copy(d.get("title", "")),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
        turn_count=d.get("turn_count", 0),
        char_count=d.get("char_count", 0),
        thread_id=d.get("thread_id", ""),
        tags=_sanitize_seq(d.get("tags", ())),
        aliases=_sanitize_seq(d.get("aliases", ())),
        notes=sanitize_for_copy(d.get("notes", "")),
    )


def to_metadata_view(conv_or_row):
    """Project an `ir.Conversation` OR a conversations-table row into a `MetadataView`.

    The single entry point for the boundary: whatever the caller has (a live IR object
    or a persisted row), the result is the same strict, sanitized allowlist.
    """
    if isinstance(conv_or_row, Conversation):
        return _from_conversation(conv_or_row)
    return _from_row(conv_or_row)


def corpus_metadata(corpus):
    """Every conversation in `corpus` as a list of `MetadataView` (order preserved)."""
    return [to_metadata_view(conv) for conv in corpus.conversations]


def aggregate_stats(corpus):
    """Corpus-level counts and histograms — the aggregate side of the allowlist.

    Only counts and allowlisted labels (provider / account) leave; thread `preview`
    and `rollout_path` are never read, so no body snippet or local path can ride an
    aggregate across the boundary.
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
    allowlist projections plus the aggregate stats. This is the whole crossing surface;
    if a token is absent here it cannot reach the cloud through this module.
    """
    return {
        "conversations": [asdict(v) for v in corpus_metadata(corpus)],
        "aggregate": aggregate_stats(corpus),
    }
