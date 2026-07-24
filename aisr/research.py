"""Corpus-BLIND research plane (Phase-4 privacy model).

This is the ONLY component permitted to feed conversation data to a cloud LLM, so
it is corpus-blind BY TYPE and BY CONSTRUCTION:

  * BY TYPE -- every public function takes ONLY ``MetadataView`` objects (the
    sanitized metadata projection built by :mod:`aisr.redact`). It never imports
    or accepts a ``Corpus``, an ``ir.Conversation``, a sqlite connection, or raw
    text. The sole reference to ``MetadataView`` is a typing-only import, so this
    module has NO runtime dependency on the redaction layer and cannot reach back
    into the raw corpus even by accident.

  * BY CONSTRUCTION -- the prompt handed to the backend is assembled from a STRICT
    ALLOWLIST of attribute names (:data:`_ID_FIELDS`, :data:`_TEXT_FIELDS`,
    :data:`_LIST_FIELDS`, :data:`_COUNT_FIELDS`) plus aggregate counts. The
    projection reads EXACTLY those names and nothing else, so a raw message body,
    a preview, a rollout path, or any PII the handed object might ALSO carry can
    never reach a prompt. It is a positive emit-list, not a blocklist that could
    miss a field.

Free text that crosses to the cloud (title, notes, tags, aliases) is passed
through :func:`aisr.sanitize.sanitize_for_copy` at this boundary -- the agent-feed
surface -- so a hidden-unicode smuggle in a label cannot re-inject downstream.

The backend is a pluggable :class:`LLMBackend` (one ``synthesize(prompt) -> str``
method); :class:`MockBackend` is a deterministic, no-network implementation that
records every prompt, so a test can inspect EXACTLY what would cross.

The prompt is fully deterministic -- histograms are key-sorted and views keep
input order; there is no clock, randomness, or corpus access anywhere here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from aisr.sanitize import sanitize_for_copy

if TYPE_CHECKING:  # pragma: no cover - typing-only; keeps the plane corpus-blind
    from collections.abc import Iterable

    from aisr.redact import MetadataView


# --- the STRICT allowlist: the ONLY MetadataView attributes that may cross -----
# Opaque identifiers + timestamps (emitted as text, never parsed or split).
_ID_FIELDS = ("conversation_id", "provider", "account", "thread_id",
              "created_at", "updated_at")
# Human/model-authored free text (sanitized for the cloud agent-feed surface).
_TEXT_FIELDS = ("title", "notes")
# Lists of free-text labels (each element sanitized).
_LIST_FIELDS = ("tags", "aliases")
# Integer aggregates.
_COUNT_FIELDS = ("turn_count", "char_count")

_SUMMARY_INSTRUCTION = (
    "Summarize the themes and activity across the following AI-chat "
    "conversations."
)
_ENTITY_INSTRUCTION = (
    "List the salient topics/entities across the following AI-chat "
    "conversations, one per line."
)


@runtime_checkable
class LLMBackend(Protocol):
    """A pluggable synthesis backend -- the research plane's ONLY egress point.

    A concrete backend receives a prompt string already built from allowlisted
    metadata and returns the model's text response.
    """

    def synthesize(self, prompt: str) -> str: ...


class MockBackend:
    """Deterministic, no-network :class:`LLMBackend` for tests and offline dev.

    Records every prompt in :attr:`prompts` so a caller (or a leak-hunt test) can
    assert EXACTLY what would be sent to a real cloud backend. Returns
    ``response`` unless a ``responder`` callable is supplied, in which case it
    returns ``responder(prompt)``.
    """

    def __init__(self, response="", responder=None):
        self.response = response
        self.responder = responder
        self.prompts = []

    def synthesize(self, prompt):
        self.prompts.append(prompt)
        if self.responder is not None:
            return self.responder(prompt)
        return self.response


# --- coercion helpers (all total: never raise, never leak) ---------------------

def _as_text(value):
    """A metadata identifier/timestamp as display text."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _as_int(value):
    """An integer count, or 0 for anything non-integer."""
    if isinstance(value, int):
        return value
    return 0


def _as_list(value):
    """A list/tuple of labels, or an empty tuple for anything else."""
    if isinstance(value, (list, tuple)):
        return value
    return ()


def _clean(value):
    """Sanitize one free-text value for the cloud agent-feed surface."""
    return sanitize_for_copy(value if isinstance(value, str) else "")


# --- projection: read ONLY the allowlist ---------------------------------------

def _project(view: "MetadataView") -> dict:
    """Project ONE view onto the strict allowlist.

    Reads EXACTLY the allowlisted attribute names; nothing else on ``view`` is
    accessed, so a raw body / preview / PII field it may also carry cannot reach
    the result. A missing attribute defaults to empty (a leaner view still
    projects cleanly), and free text is sanitized.
    """
    projected = {}
    for name in _ID_FIELDS:
        projected[name] = _as_text(getattr(view, name, ""))
    for name in _TEXT_FIELDS:
        projected[name] = _clean(getattr(view, name, ""))
    for name in _LIST_FIELDS:
        projected[name] = [_clean(item) for item in _as_list(getattr(view, name, ()))]
    for name in _COUNT_FIELDS:
        projected[name] = _as_int(getattr(view, name, 0))
    return projected


def _aggregate(projections):
    """Counts + histograms over the projected views (allowlisted fields only)."""
    by_provider = {}
    by_account = {}
    total_turns = 0
    total_chars = 0
    for p in projections:
        by_provider[p["provider"]] = by_provider.get(p["provider"], 0) + 1
        by_account[p["account"]] = by_account.get(p["account"], 0) + 1
        total_turns += p["turn_count"]
        total_chars += p["char_count"]
    return {
        "conversations": len(projections),
        "total_turns": total_turns,
        "total_chars": total_chars,
        "by_provider": by_provider,
        "by_account": by_account,
    }


def _hist(counts):
    """A deterministic ``k=v, k=v`` rendering of a histogram (key-sorted)."""
    return ", ".join("%s=%d" % (k, counts[k]) for k in sorted(counts))


def _render_view(p):
    """Render one projected view as allowlisted ``key: value`` lines."""
    return "\n".join((
        "- conversation_id: %s" % p["conversation_id"],
        "  provider: %s" % p["provider"],
        "  account: %s" % p["account"],
        "  thread_id: %s" % p["thread_id"],
        "  created_at: %s" % p["created_at"],
        "  updated_at: %s" % p["updated_at"],
        "  turn_count: %d" % p["turn_count"],
        "  char_count: %d" % p["char_count"],
        "  title: %s" % p["title"],
        "  notes: %s" % p["notes"],
        "  tags: %s" % ", ".join(p["tags"]),
        "  aliases: %s" % ", ".join(p["aliases"]),
    ))


def _build_prompt(views: "Iterable[MetadataView]", instruction: str) -> str:
    """Build the FULL prompt from ONLY allowlisted metadata + aggregate stats."""
    projections = [_project(v) for v in views]
    stats = _aggregate(projections)
    lines = [
        instruction,
        "",
        "You are given ONLY sanitized conversation METADATA -- no message bodies, "
        "no personal data. Base your answer solely on the fields below.",
        "",
        "## Aggregate",
        "conversations: %d" % stats["conversations"],
        "total_turns: %d" % stats["total_turns"],
        "total_chars: %d" % stats["total_chars"],
        "by_provider: %s" % _hist(stats["by_provider"]),
        "by_account: %s" % _hist(stats["by_account"]),
        "",
        "## Conversations",
    ]
    lines.extend(_render_view(p) for p in projections)
    return "\n".join(lines)


def _parse_entities(raw):
    """Parse a backend response into an ordered, de-duplicated entity list."""
    text = raw if isinstance(raw, str) else ""
    entities = []
    for piece in text.replace(",", "\n").split("\n"):
        item = piece.strip(" -\t")
        if item and item not in entities:
            entities.append(item)
    return entities


# --- public API: consumes ONLY MetadataView ------------------------------------

def synthesize_over_metadata(views: "Iterable[MetadataView]",
                             backend: LLMBackend) -> str:
    """Summarize a set of conversations from ONLY their sanitized metadata."""
    return backend.synthesize(_build_prompt(views, _SUMMARY_INSTRUCTION))


def extract_entities(views: "Iterable[MetadataView]",
                     backend: LLMBackend) -> "list[str]":
    """Extract salient entities/topics from ONLY sanitized metadata.

    Returns the ordered, de-duplicated list of entity strings the backend
    produced from the metadata-only prompt.
    """
    raw = backend.synthesize(_build_prompt(views, _ENTITY_INSTRUCTION))
    return _parse_entities(raw)
