"""Codex physical-copy -> logical-session collapse (a VIEW, never a delete).

Codex writes the same session into more than one place — the live store
(`<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*.jsonl`), a `sessions_backup` mirror, plus
whatever the user's own copies/syncs leave behind — so the same conversation shows up
several times in the browser. This module collapses those PHYSICAL COPIES into one
LOGICAL SESSION while keeping every copy attached as evidence.

Ported from codex-session-manager (C#, being retired):
`Core/Sessions/SessionDeduplicator.Consolidate` + `SessionPhysicalCopy` + `LogicalSession`,
with `Storage/Discovery/KnownStoreLocator` for the store roots. There is no shared code —
only the behaviour, whose spec is `SessionDeduplicatorTests.cs`.

THE IDENTITY RULE (exact, and deliberately the only one):

    two physical files are the same logical session IFF their derived session_id strings
    are BYTE-EQUAL — Python `==` on str, i.e. code-point-exact, which is what the C#
    `GroupBy(SessionId, StringComparer.Ordinal)` does. No case folding, no whitespace
    stripping, no path/mtime/size/title/cwd similarity, no fuzzy content match.

`session_id` is whatever `adapters.codex_rollout` derives, and nothing else:
`session_meta.session_id` -> `session_meta.id` -> the UUID embedded in the rollout
FILENAME -> `""` (that module's `_assemble`, and its trap 6). That chain is what makes the
rule work in practice: a truncated or resumed copy that lost its `session_meta` header is
STILL identified by its filename UUID, so it merges with its complete sibling instead of
masquerading as a separate conversation. `scan_store` adds exactly one check to that
chain — the derived id must be UUID-SHAPED, the same gate the filename link already
applies — because the `session_meta.id` link accepts any string and a shared non-unique
value there would merge two unrelated rollouts (see `_trusted_id`).

Why nothing softer: a FALSE MERGE silently hides one of the owner's conversations behind
another, while a false split only shows a cosmetic duplicate. Every judgement call is
therefore resolved toward splitting — ids differing only in case, or by a trailing space,
stay separate (matching Ordinal), and two runs of the same prompt (same cwd, same title,
byte-identical size, same mtime, adjacent filenames) never merge because only the id is
consulted.

UNIDENTIFIED COPIES. The C# `Consolidate` DROPS copies whose SessionId is blank
(`.Where(copy => !string.IsNullOrWhiteSpace(copy.SessionId))`). This port does not: a drop
is a deletion from the view, and grouping the blanks TOGETHER would be the largest false
merge available. Each blank-id copy instead becomes its own singleton keyed by its path,
survives in the output, and reports `is_identified == False` so the UI can flag it. That
holds end to end: a consumer mapping sessions onto corpus threads MUST key that map by
PATH for a blank id, because keying it by session_id re-merges every one of them at the end.

CANONICAL-COPY CHOICE — deterministic, total, and never "first one found". Copies are
ordered by this key and `copies[0]` becomes `canonical`:

  1. store rank        live = 0, everything else = 1        (C#: `StoreKind is Live ? 0 : 1`)
  2. size_bytes DESC   the most complete copy wins          (ADDED - see below)
  3. last_write_ms DESC  newest wins; an unknown mtime sorts last  (C#: ThenByDescending)
  4. file_path ASC, case-insensitive                        (C#: ThenBy OrdinalIgnoreCase)
  5. file_path ASC, raw — so paths differing only in case still get a TOTAL order

Key 2 is a deliberate divergence. The C# captured `FileSizeBytes` on every copy and then
never used it in the ordering, which makes a truncated copy win whenever its mtime is
newer — e.g. a backup half-written by an interrupted copy. A rollout is an APPEND-ONLY
event log (see `codex_rollout`'s module docstring), so two copies of one session are
prefixes of each other and the larger one CONTAINS everything the smaller one has: size is
the direct measure of completeness that mtime only proxies. Store rank still outranks it,
so the authoritative live file is never demoted behind a stale mirror.

That ordering has one honest cost: ACROSS stores a crash-truncated live copy still beats a
complete backup, and a consumer that reads only the canonical never sees the fuller copy in
the rendered view. The copy is never lost (`duplicate_paths` keeps it), but "never hide one
of the owner's conversations" means the loss of DETAIL cannot be silent either, so
`LogicalSession.has_larger_copy` reports it instead of the choice being reversed.

STORAGE. `ensure_schema` creates this module's OWN table (`session_physical_copies`); it
does not touch `corpus.py`'s schema, and it is safe to call on a live corpus index.
`save_sessions` is INSERT-OR-REPLACE keyed on file_path, so replays are no-ops and a later
scan that sees fewer stores never erases the evidence of a copy it no longer finds.

PRIVACY: local-only. Every entry point takes an EXPLICIT root or connection — nothing here
resolves `$CODEX_HOME`, `~/.codex` or AppData, nothing opens a socket, and only file paths,
sizes and mtimes are recorded. Tests use synthetic fixtures.
"""
import os
from dataclasses import dataclass, field
from typing import Optional

from llm_anthology.adapters import codex_rollout


# Store kinds (the C# `SessionStoreKind` enum). Only LIVE is privileged; the rest tie, so
# an unrecognised kind degrades to "not live" rather than raising.
STORE_LIVE = "live"
STORE_BACKUP = "backup"
STORE_MIRROR = "mirror"
STORE_OTHER = "other"
STORE_UNKNOWN = "unknown"

COPIES_TABLE = "session_physical_copies"


def store_rank(store_kind):
    """Preference rank of a store: the live store is 0, every other kind is 1."""
    return 0 if store_kind == STORE_LIVE else 1


def known_store_roots(codex_home):
    """[(root, store_kind)] for the two stores Codex is known to write — a port of
    `KnownStoreLocator.GetKnownStores`. PURE path arithmetic: it does not stat, list or
    open anything, so naming a home directory never reads the owner's real sessions."""
    return [(os.path.join(codex_home, "sessions"), STORE_LIVE),
            (os.path.join(codex_home, "sessions_backup"), STORE_BACKUP)]


# ------------------------------------------------------------------- dataclasses

@dataclass(frozen=True)
class PhysicalCopy:
    """One session file found on disk. Frozen: a copy is EVIDENCE, so consolidation can
    reorder references to it but can never edit or drop the record."""
    session_id: str
    file_path: str
    store_kind: str = STORE_UNKNOWN
    last_write_ms: Optional[int] = None
    size_bytes: int = 0


@dataclass(frozen=True)
class LogicalSession:
    """One conversation, plus every physical copy of it.

    `copies` is the full set in preference order and `copies[0] is canonical`; the tuple
    (not a list) is what makes "dedup never deletes" structural rather than a promise.
    """
    session_id: str
    canonical: PhysicalCopy
    copies: tuple = field(default_factory=tuple)

    @property
    def copy_count(self):
        """How many physical files back this one conversation (>= 1)."""
        return len(self.copies)

    @property
    def duplicate_paths(self):
        """The non-canonical copies' paths, still fully retained as evidence."""
        return tuple(c.file_path for c in self.copies[1:])

    @property
    def is_identified(self):
        """False when no session id could be recovered from the file OR its name, in
        which case this session is a path-keyed singleton that was NOT merged."""
        return bool(self.session_id.strip())

    @property
    def has_larger_copy(self):
        """True when the canonical copy is SMALLER than one it demoted — i.e. the copy
        this view puts forward is a truncated prefix of a sibling.

        Store rank outranks size (key 1 beats key 2), which is right — the live store is
        authoritative and must never be demoted behind a stale mirror — but it means a
        crash-truncated live rollout can win over a complete backup of the same session.
        A consumer that reads only the canonical then leaves the fuller copy out of the
        rendered view. Nothing is deleted (`duplicate_paths` still lists it), yet a
        conversation the owner can see in one file and not the other is exactly what
        this module promises never to hide, so the condition is REPORTED rather than
        silently resolved. A UI can offer the larger copy; the canonical rule stays
        single-implementation."""
        return self.canonical.size_bytes < max(
            (c.size_bytes for c in self.copies), default=self.canonical.size_bytes)


# ----------------------------------------------------------------- the two rules

def _is_blank(session_id):
    """Blank in the C# `string.IsNullOrWhiteSpace` sense (whitespace counts as blank)."""
    return not session_id.strip()


def _group_key(copy):
    """The IDENTITY RULE. A real id groups (byte-exact); a blank id falls back to the
    file's own path, which is unique per copy, so unidentifiable files can never be
    merged into each other yet are never dropped either."""
    if _is_blank(copy.session_id):
        return ("path", copy.file_path)
    return ("id", copy.session_id)


def _preference_key(copy):
    """The CANONICAL-COPY RULE: live store, then most complete, then newest, then path.
    Total by construction — the raw path is the final key — so the winner is independent
    of input order."""
    ms = copy.last_write_ms
    return (store_rank(copy.store_kind),
            -copy.size_bytes,
            (0, -ms) if ms is not None else (1, 0),
            copy.file_path.casefold(),
            copy.file_path)


def _session_key(session):
    """Result ordering: by session id (ordinal, like the C#), then canonical path."""
    return (session.session_id, session.canonical.file_path.casefold(),
            session.canonical.file_path)


def consolidate(copies):
    """[PhysicalCopy] -> [LogicalSession], deterministically ordered.

    Every input copy appears in exactly one output session; nothing is filtered, nothing
    is written, and the caller's sequence is not mutated.
    """
    groups = {}
    for copy in copies:
        groups.setdefault(_group_key(copy), []).append(copy)

    sessions = []
    for members in groups.values():
        ordered = tuple(sorted(members, key=_preference_key))
        sessions.append(LogicalSession(session_id=ordered[0].session_id,
                                       canonical=ordered[0], copies=ordered))
    sessions.sort(key=_session_key)
    return sessions


# ---------------------------------------------------------------------- scanning

def _trusted_id(derived_id):
    """A scan-derived id, or `""` when it does not look like a Codex session id.

    `codex_rollout` derives the id from `session_meta.session_id` -> `session_meta.id`
    -> the UUID in the FILENAME. Only the third link is shape-checked: the second
    accepts ANY string, so two unrelated rollouts that both carry, say,
    `"id": "default"` would arrive here with byte-equal ids and MERGE — one of the
    owner's conversations hidden behind another, the exact failure the identity rule
    exists to prevent. Requiring the same UUID shape the filename fallback already
    requires closes that without inventing a new rule (hence `codex_rollout`'s own
    regex, not a second copy of the pattern that could drift from it).

    A real session id that is not UUID-shaped therefore SPLITS into per-path singletons
    instead of merging. That is the deliberate direction: a false split shows a cosmetic
    duplicate, a false merge hides a conversation. The gate lives here, at the
    derivation boundary, and not in `consolidate` — `consolidate`'s contract is that the
    CALLER supplies identity and byte-equal ids group, which is what makes it a pure,
    testable rule.
    """
    return derived_id if codex_rollout._UUID.fullmatch(derived_id) else ""


def scan_store(sessions_root, store_kind=STORE_UNKNOWN):
    """One store root -> ([PhysicalCopy], errors).

    Identity comes from `codex_rollout.ingest_sessions` (see `_trusted_id` for the one
    thing this does NOT take on faith), so this inherits its recursive date-nested walk,
    its skip-and-log of a torn last line, and its filename-UUID fallback. A missing root
    is an empty result, not an error. `errors` is passed through verbatim so a partial
    parse is visible without costing the copy.
    """
    docs, errors = codex_rollout.ingest_sessions(sessions_root)
    copies = []
    for doc in docs:
        # Only files ingest_sessions could already READ reach here, so stat cannot fail.
        stat = os.stat(doc.rollout_path)
        copies.append(PhysicalCopy(session_id=_trusted_id(doc.thread_id),
                                   file_path=doc.rollout_path,
                                   store_kind=store_kind,
                                   last_write_ms=int(stat.st_mtime * 1000),
                                   size_bytes=stat.st_size))
    return copies, errors


def scan_stores(roots):
    """[(root, store_kind)] -> (copies, errors) across every store, ready to consolidate."""
    copies, errors = [], []
    for root, store_kind in roots:
        found, errs = scan_store(root, store_kind)
        copies.extend(found)
        errors.extend(errs)
    return copies, errors


# ----------------------------------------------------------------------- storage

# This module's OWN table. file_path is the PK because one path is one physical copy;
# is_canonical is materialised so a SQL consumer can filter without re-running the rule.
SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
    file_path     TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL DEFAULT '',
    store_kind    TEXT NOT NULL DEFAULT '{unknown}',
    last_write_ms INTEGER,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    is_canonical  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dedup_session ON {t}(session_id);
""".format(t=COPIES_TABLE, unknown=STORE_UNKNOWN)

_COPY_COLS = ("session_id", "file_path", "store_kind", "last_write_ms", "size_bytes")


def ensure_schema(conn):
    """Create this module's table/index if absent and return `conn`. Idempotent, and
    additive: it never issues DDL against `corpus.py`'s tables."""
    conn.executescript(SCHEMA)
    return conn


def save_sessions(conn, sessions):
    """Record every physical copy of every session. INSERT OR REPLACE on file_path, so a
    re-scan updates a copy in place and NEVER deletes one it did not see this time."""
    sql = ("INSERT OR REPLACE INTO %s(%s, is_canonical) VALUES (?,?,?,?,?,?)"
           % (COPIES_TABLE, ",".join(_COPY_COLS)))
    for session in sessions:
        for copy in session.copies:
            conn.execute(sql, tuple(getattr(copy, c) for c in _COPY_COLS)
                         + (1 if copy is session.canonical else 0,))


def load_sessions(conn):
    """Rebuild the dedup view from the table. The stored `is_canonical` flag is NOT
    trusted — `consolidate` re-derives it, so the rule has exactly one implementation."""
    rows = conn.execute("SELECT %s FROM %s" % (",".join(_COPY_COLS), COPIES_TABLE))
    return consolidate([PhysicalCopy(*tuple(row)) for row in rows])
