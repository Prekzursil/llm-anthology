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

SECOND BOUNDARY IN THIS FILE — the LOCAL EXPORT (decisions G-5 / G-6, bottom half):
`scan_credential_shapes` / `scrub_credential_shapes` / `relativize_home` /
`shareable_thread` shape a FILE the owner deliberately hands to someone else. Nothing
there egresses, and its policy is deliberately WEAKER than the cloud allowlist above
(titles and repo/branch are kept, because the owner accepted that trade for a file they
choose to send). They live here so the project has ONE redaction module instead of a
third implementation — not because the two policies are the same. The credential scanner
is a SHAPE matcher only and says so in its own coverage statement; it is not, and must
not be presented as, a personal-data or medical-content detector.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass

from llm_anthology.corpus import ThreadMeta
from llm_anthology.ir import Conversation

__all__ = [
    "MetadataView",
    "to_metadata_view",
    "corpus_metadata",
    "aggregate_stats",
    "metadata_payload",
    # --- the LOCAL export boundary (DECISIONS G-5 / G-6), a different boundary from
    # the cloud projection above: nothing here egresses; it shapes a FILE the owner
    # chooses to hand to someone else.
    "CREDENTIAL_SHAPE_COVERAGE_LIMIT",
    "CREDENTIAL_SHAPE_NAMES",
    "scan_credential_shapes",
    "scrub_credential_shapes",
    "relativize_home",
    "shareable_thread",
    "SHAREABLE_DECIDED_FIELDS",
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


# =============================================================== the EXPORT boundary
#
# DECISIONS G-5 and G-6. Everything above shapes what may cross to a CLOUD plane;
# everything below shapes a LOCAL FILE the owner deliberately hands to someone else.
# Nothing here egresses, and nothing here runs unless an export runs.
#
# The two boundaries deliberately share this module rather than growing a third
# redaction implementation, because the allowlist discipline is the same: name every
# field explicitly, never pass a record through wholesale. They are NOT the same
# policy — the cloud projection forbids all free text; a shareable export keeps titles
# and repo/branch because the owner accepted that trade for a file they choose to send.


CREDENTIAL_SHAPE_COVERAGE_LIMIT = (
    "This scan detects credential SHAPES only: API-key, token, bearer-header and "
    "private-key text patterns. It is BLIND to personal and medical content — names, "
    "dates of birth, diagnoses, medication names, addresses, phone numbers and account "
    "numbers are NOT detected and are exported verbatim. A result with no findings "
    "does not mean this export is safe to share; read what you are sending."
)
"""The sentence every credential-shape warning MUST carry, findings or not.

This constant is the point of the whole feature, not a footnote to it. A scanner that
reports "clean" over a corpus of private medical conversations is *factually correct and
dangerously misleading*: it answers a question the reader did not ask (are there API keys
in here) in a way that sounds like the question they care about (is this safe to send).
Stating the blind spot in the same breath as the verdict is what stops a green result
from lowering their guard.

Deliberately NOT described as a personal-data or privacy scrubber anywhere: it is a
credential-shape matcher, and calling it anything wider would re-create the exact
false-assurance this constant exists to prevent.
"""


# Pinned shapes, widest-first where two can overlap. Each is a SHAPE, not a validator:
# no checksum is verified and no key is contacted, so a match means "this looks like a
# credential and a human should look", never "this is a live credential".
_CREDENTIAL_SHAPES = (
    ("private-key-block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    # `Bearer <token>` is listed before the token shapes so a bearer header carrying a
    # JWT reports ONCE, as the header (the wider, more actionable finding).
    ("bearer-token", re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{16,}")),
    ("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    # The negative lookahead keeps an Anthropic key from ALSO matching here; without it
    # every `sk-ant-` hit would be reported twice under two different shape names.
    ("openai-api-key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_-]{20,}")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{12,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")),
)

#: The reportable shape labels, in scan order. A finding always carries one of these.
CREDENTIAL_SHAPE_NAMES = tuple(name for name, _ in _CREDENTIAL_SHAPES)


def _mask(value):
    """A locatable but non-disclosing preview: the first 4 characters and the length.

    The whole matched run never travels, not even inside the process's own report — a
    report is rendered in a UI, logged, and pasted into bug threads, and a credential is
    compromised by being copied anywhere, not only by being committed.
    """
    return "%s… (%d chars)" % (value[:4], len(value))


def _credential_spans(text):
    """Non-overlapping ``(start, end, shape)`` spans in document order.

    At a given start the WIDEST match wins, and a shape nested inside an already-reported
    span is skipped — so one credential is one finding regardless of how many patterns it
    happens to satisfy. Deterministic: the sort key is (start, -length, shape).
    """
    if not isinstance(text, str):
        return []
    raw = sorted((m.start(), -(m.end() - m.start()), name)
                 for name, pattern in _CREDENTIAL_SHAPES
                 for m in pattern.finditer(text))
    spans, end_of_last = [], -1
    for start, neg_len, name in raw:
        if start < end_of_last:
            continue
        end_of_last = start - neg_len
        spans.append((start, end_of_last, name))
    return spans


def scan_credential_shapes(text):
    """Report every credential-shaped run in ``text`` WITHOUT changing it.

    Returns ``[{"shape", "offset", "preview"}, ...]`` in document order: the pinned shape
    label, the character offset (the location a reader needs to go look), and a masked
    preview. Reading is the whole job — mutation is a separate, opt-in call
    (:func:`scrub_credential_shapes`), because an archive of record must not be altered
    behind the owner's back.

    COVERAGE: shapes only. See :data:`CREDENTIAL_SHAPE_COVERAGE_LIMIT` — an empty result
    is not a safety verdict, and personal and medical content is invisible to this.
    """
    return [{"shape": name, "offset": start, "preview": _mask(text[start:end])}
            for start, end, name in _credential_spans(text)]


def scrub_credential_shapes(text):
    """``text`` with every credential-shaped run replaced by ``[redacted:<shape>]``.

    OPT-IN ONLY. Callers reach for this after a human has seen the scan and asked for it;
    no code path calls it by default. The placeholder names the shape so the reader can
    still tell what was there, and re-scanning the result yields nothing (the both-states
    property the tests pin).

    COVERAGE: identical to the scan's — it removes what the shapes match and nothing
    else, so it does NOT make a transcript safe to share.
    """
    if not isinstance(text, str):
        return ""
    out, cursor = [], 0
    for start, end, name in _credential_spans(text):
        out.append(text[cursor:start])
        out.append("[redacted:%s]" % name)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def relativize_home(path, home=None):
    """An absolute local path -> ``~``-prefixed with ``/`` separators when it lies inside
    the user's home directory; returned UNCHANGED otherwise.

    This is the G-6 path treatment: ``C:\\Users\\<name>\\.codex\\sessions\\r.jsonl``
    becomes ``~/.codex/sessions/r.jsonl``, so the OS username stops riding along in every
    ``cwd`` and ``rollout_path``. The prefix test is case-insensitive (Windows paths are)
    and requires a SEPARATOR after the home root, so a sibling directory
    (``C:\\Users\\bob2`` against home ``C:\\Users\\bob``) is not silently rewritten.

    KNOWN RESIDUAL, stated here rather than in a footnote: a path OUTSIDE the home
    directory (``D:\\work\\client-x\\repo``) is returned verbatim. It carries no
    username, but it can carry a project or client name. Settling experiment if that
    becomes unacceptable: drop everything but the basename for non-home paths and
    measure how many exported nodes lose their only locating field.
    """
    if not isinstance(path, str) or not path:
        return ""
    root = (os.path.expanduser("~") if home is None else home)
    root = root.replace("\\", "/").rstrip("/")
    if not root:
        # No home to relativize against (an explicitly empty root). Rewriting would
        # turn every absolute path into `~/...`, which would be a lie.
        return path
    norm = path.replace("\\", "/").rstrip("/")
    if norm.lower() == root.lower():
        return "~"
    if norm.lower().startswith(root.lower() + "/"):
        return "~/" + norm[len(root) + 1:]
    return path


def scrub_home_mentions(text, home=None):
    """Replace every MENTION of the home directory inside free text with ``~``.

    SEPARATE FROM :func:`relativize_home` ON PURPOSE, and the difference is the whole reason
    this exists. `relativize_home` answers "is this string a path under home?" — it rewrites
    only when the WHOLE value is home-rooted, and returns anything else verbatim. That is
    exactly right for `cwd` and `rollout_path`, which ARE paths, and changing it would change
    them, so it is left alone.

    A `title` is not a path. It is `_first_line(first_user, 80)` for the Codex adapter — the
    user's opening sentence — so the realistic leak is a path EMBEDDED in prose:
    ``see C:/Users/<name>/notes.md``. MEASURED: `relativize_home` returns that untouched,
    because it does not start at the home root. Applying it to `title` would therefore have
    looked like a fix while leaving the demonstrated case leaking, which is worse than not
    fixing it — the honest UI warning next to this would have been weakened on false evidence.

    CONSERVATIVE BY CONSTRUCTION. It substitutes the home root and nothing else: no
    username-guessing, no heuristics about what "looks personal". Both separator spellings are
    matched, case-insensitively, because Windows paths reach here either way. A string
    containing no home mention is returned byte-identical, so an ordinary prose title is
    untouched — pinned by a test, since silently mangling titles would be its own defect.

    KNOWN RESIDUAL, stated rather than footnoted: a path OUTSIDE home
    (``D:/work/client-x``) still passes, exactly as `relativize_home` lets it, and a BARE
    username (``someone@desktop`` in `agent_nickname`) is not a path at all so nothing here
    can help it. Both are the owner's call, not this function's.
    """
    if not isinstance(text, str) or not text:
        return ""
    root = (os.path.expanduser("~") if home is None else home).rstrip("/\\")
    if not root:
        return text
    out = text
    for spelling in {root.replace("\\", "/"), root.replace("/", "\\")}:
        idx = out.lower().find(spelling.lower())
        while idx != -1:
            out = out[:idx] + "~" + out[idx + len(spelling):]
            idx = out.lower().find(spelling.lower(), idx + 1)
    return out


#: Every ThreadMeta field name, spelled out so :func:`shareable_thread` cannot silently
#: pass through a field added later. A test asserts this equals ThreadMeta's real field
#: set, so adding a field turns that test red and forces a keep / drop / relativize call.
SHAREABLE_DECIDED_FIELDS = frozenset({
    "id", "title", "model_provider", "tokens_used", "created_at_ms", "updated_at_ms",
    "git_branch", "cwd", "agent_role", "agent_nickname", "preview", "rollout_path",
    "adapter",
})


def shareable_thread(meta, home=None):
    """A NEW ThreadMeta projected for the G-6 SHAREABLE export mode. Pure: ``meta`` is
    never mutated.

    Built by construction, field by field — the same discipline as :class:`MetadataView`:

      * ``preview`` is DROPPED ENTIRELY (set to ""). It is populated as
        ``first_user[:200]`` by every adapter, so it is a verbatim 200-character excerpt
        of each conversation's opening user message — frequently the most sensitive
        sentence in a private medical conversation, and the single worst field to hand to
        someone else. Dropped, not truncated: a shorter excerpt is the same leak.
      * ``cwd`` and ``rollout_path`` are relativized to ``~`` (see
        :func:`relativize_home`) — absolute local paths carry the OS username.
      * structure (``id``, timestamps, ``tokens_used``), ``title``, ``model_provider``,
        ``adapter``, ``agent_role``/``agent_nickname`` and ``git_branch`` are KEPT.

    ACCEPTED RESIDUAL, inline and deliberate: ``title`` is derived from raw content for
    the Codex adapter (see this module's cloud-projection docstring above), and repo /
    branch names are kept because the owner explicitly accepted that trade. "Shareable"
    therefore means MEASURABLY LESS LEAKY, not safe — the mode still hands over titles.
    """
    return ThreadMeta(
        id=meta.id,
        title=scrub_home_mentions(meta.title, home=home),
        model_provider=meta.model_provider,
        tokens_used=meta.tokens_used,
        created_at_ms=meta.created_at_ms,
        updated_at_ms=meta.updated_at_ms,
        git_branch=scrub_home_mentions(meta.git_branch, home=home),
        cwd=relativize_home(meta.cwd, home=home),
        agent_role=meta.agent_role,
        agent_nickname=meta.agent_nickname,
        preview="",
        rollout_path=relativize_home(meta.rollout_path, home=home),
        adapter=meta.adapter,
    )
