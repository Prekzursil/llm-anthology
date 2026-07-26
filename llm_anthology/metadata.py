"""App-owned session metadata: ALIAS / TAGS / NOTES (the csm metadata port).

codex-session-manager let the user pin a friendly ALIAS, a set of TAGS and free-form
NOTES onto a session, kept in the APP's own store and never written back into the session
files. This module is that layer for LLM Anthology, ported from
`Storage/Indexing/SessionCatalogRepository.cs` (SaveMetadataAsync / SelectMetadataSql /
MergeExistingMetadataAsync / UpdateMetadataSql) with `SessionMetadataRepositoryTests` as
the behavioural spec.

THE HEADLINE INVARIANT — NON-MUTATING. A session file on disk is byte-identical after any
sequence of metadata writes. It holds BY CONSTRUCTION, not by discipline: nothing here
opens a file. Every entry point takes a caller-supplied sqlite connection and the only
thing ever written is a row in `conversation_metadata`. Proven in
tests/test_metadata.py::test_session_file_is_byte_identical_after_metadata_writes, which
ingests a synthetic rollout through the REAL loader, runs the full write battery, and
re-hashes every byte under the sessions tree.

KEYED BY conversation_id — decided from the data model, not by preference:
  * `conversations.conversation_id` is UNIQUE and is the only CROSS-PROVIDER identity in
    the index. `conversations.thread_id` DEFAULTs to '' and is populated only on the
    Codex path (loaders.load_corpus sets `conv.meta["thread_id"]`, index.build_index
    copies it in), so keying on thread_id would leave ChatGPT / Claude / Gemini
    conversations un-annotatable — three quarters of an "anthology".
  * On the Codex path the choice costs nothing: codex_rollout._assemble builds BOTH
    `Conversation.id` and `ThreadMeta.id` from the same session id, so for a rollout
    conversation_id == thread_id already. `find_by_thread` covers the general case with a
    join through `conversations.thread_id` instead of a second key, so there is exactly
    ONE key and no chance of the two drifting.
  * csm keyed on `session_id`, which is the same notion (one logical Codex session).

SCHEMA — MY OWN TABLE, in the SAME index DB. `ensure_schema` is idempotent and additive;
corpus.py's schema is not touched. Two deliberate departures from csm:
  * csm stores alias/tags/notes as COLUMNS ON THE `sessions` ROW, next to the derived
    search document. That is why it needs MergeExistingMetadataAsync — a re-upsert
    rewrites the whole row and would wipe hand-authored metadata. Here they live in a
    separate table, so `corpus.add_conversation` structurally cannot reach them.
  * there is deliberately NO foreign key to `conversations(conversation_id)`: metadata
    must OUTLIVE a rebuilt index (an ON DELETE CASCADE would turn "reindex the corpus"
    into "silently destroy every note the owner ever wrote), and metadata may be set for
    a conversation that has not been ingested yet.

DURABILITY — THE WRITE PATH COMMITS. Every write commits before it returns, and returns
the stored Metadata as a receipt. That receipt has to be TRUE. A caller who opens a
connection, writes and exits would otherwise lose everything: sqlite rolls back an
uncommitted transaction on close, and a read-back on the same connection still sees the
new value from inside that transaction, so nothing short of reopening the file can
detect the loss. `index.build_index` owns durability the same way (`do(conn.commit)` per
chunk). Pinned by tests that CLOSE and REOPEN the database with no caller commit.

THE KEY IS VALIDATED, NOT COERCED. `conversation_id` must be a non-blank str; anything
else raises ValueError. That is csm's SaveMetadataAsync guard
(`IsNullOrWhiteSpace(sessionId)` -> ArgumentException), and it carries more weight here
than in C#: SQLite does not enforce NOT NULL on a non-INTEGER PRIMARY KEY and NULLs
compare DISTINCT in the implied unique index, so a None key APPENDS a row on every write
instead of upserting. See `_check_id`.

PRIVACY — LOCAL-ONLY, and NOT part of the cloud projection. `redact.py`'s allowlist
forbids ALL free text from crossing to the cloud research plane (owner decision
2026-07-25, after a probe carried an SSN, an email and a patient name into a prompt), and
its docstring names this port explicitly: "aliases/tags/notes are an app-owned LOCAL
metadata layer ... must NOT be re-added to this cloud projection." This module therefore
does NOT add alias / tags / notes to `redact.MetadataView`, does not import redact, and
does not read the environment or the filesystem. Two tests pin that: a field-set
disjointness check on MetadataView, and an end-to-end probe that stores distinctive
tokens and proves they appear nowhere in `redact.metadata_payload`.

Free text is SANITIZED on the way in with `sanitize.sanitize_for_copy`, which STRIPS the
flagged hidden codepoints (zero-width / bidi / TAG-block / variation selectors). The
corpus is known to carry that payload class, and an alias or tag is re-displayed and
re-fed to models, so it is exactly the "copy / agent-feed surface" that function exists
for.
"""
from dataclasses import dataclass

from llm_anthology import corpus
from llm_anthology.sanitize import sanitize_for_copy

__all__ = [
    "METADATA_SCHEMA",
    "Metadata",
    "ensure_schema",
    "open_metadata",
    "clean_text",
    "clean_tags",
    "get_metadata",
    "get_alias",
    "get_tags",
    "get_notes",
    "set_metadata",
    "set_alias",
    "set_tags",
    "set_notes",
    "add_tags",
    "remove_tags",
    "clear_alias",
    "clear_tags",
    "clear_notes",
    "clear_metadata",
    "merge_metadata",
    "find_by_tag",
    "find_by_thread",
    "search_metadata",
    "search_conversations",
    "tag_counts",
]

# csm's wire format for a tag list is `string.Join('\n', tags)`, read back with a
# '\n' split under RemoveEmptyEntries | TrimEntries. Keeping that exact encoding means a
# csm export and this store are the same bytes, and it is also the separator the
# match-key columns below are delimited by.
_SEP = "\n"

_COLS = ("conversation_id", "alias", "tags", "notes", "tags_key", "search_key")

METADATA_SCHEMA = """
-- App-owned per-conversation annotations. `alias`, `tags` and `notes` are the DISPLAY
-- values (tags newline-joined, csm wire-compatible). `tags_key` and `search_key` are
-- derived MATCH columns, recomputed on every write — the analogue of csm recomputing
-- `combined_text` in UpdateMetadataSql, which is what makes a freshly-annotated session
-- findable. They are stored casefolded because SQLite's own lower() is ASCII-only and
-- would silently fail to match 'Ä' against 'ä'.
--
-- An all-blank annotation is stored as the ABSENCE of a row, so a cleared conversation
-- reads identically to one that was never annotated and the table keeps no residue.
-- No FOREIGN KEY on purpose: this metadata must survive a rebuilt index (see the module
-- docstring).
CREATE TABLE IF NOT EXISTS conversation_metadata (
    conversation_id TEXT PRIMARY KEY,
    alias           TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    tags_key        TEXT NOT NULL DEFAULT '',
    search_key      TEXT NOT NULL DEFAULT ''
);
"""


# ------------------------------------------------------------------- dataclass

@dataclass(frozen=True)
class Metadata:
    """One conversation's annotations. Frozen, and `tags` is a TUPLE, so a value handed
    to a caller (or held by a UI) cannot be mutated behind the store's back."""
    conversation_id: str
    alias: str = ""
    tags: tuple = ()
    notes: str = ""

    @property
    def is_empty(self):
        """True when nothing is annotated — what `absence of a row` reads back as, and
        the signal the UI uses to skip rendering a badge."""
        return not (self.alias or self.tags or self.notes)


# ---------------------------------------------------------------------- schema

def ensure_schema(conn):
    """Create MY table if it is absent; return the connection. Idempotent and additive —
    re-running against a live index is a no-op and corpus.py's tables are untouched."""
    conn.executescript(METADATA_SCHEMA)
    return conn


def open_metadata(path):
    """Open the index at `path` (corpus.py's schema) with the metadata table ensured.

    Deliberately the SAME database file, not a second store: alias/tags/notes join
    against `conversations` for every UI listing, and one file keeps a backup, a WAL
    checkpoint and a "delete the index" action atomic across both.
    """
    return ensure_schema(corpus.open_index(path))


# ---------------------------------------------------------------- normalisation

def _check_id(conversation_id):
    """Reject a `conversation_id` that cannot be a usable key. csm's dropped guard —
    `SaveMetadataAsync`: `if (string.IsNullOrWhiteSpace(sessionId)) throw new
    ArgumentException(...)`.

    Load-bearing in a way it is not in C#. SQLite does not enforce NOT NULL on a
    non-INTEGER PRIMARY KEY and NULLs compare DISTINCT in the implied unique index, so a
    None key does not upsert — every write APPENDS another row, and those rows are
    unreachable through get_metadata (which reads blanks), undeletable through
    clear_metadata (which affects zero rows) and still visible to search_metadata: the
    owner's own text becomes permanent, un-prunable residue under a key no UI can
    resolve. '' is milder — it round-trips and deletes — but still names a conversation
    that can never join `conversations`.

    RAISES where `clean_text` coerces, because there is no safe identity to coerce TO:
    the realistic source is an RPC handler doing `params.get("conversation_id")` on a
    payload that omits the field, and silently annotating some fallback conversation
    would be worse than failing. Applied on the READ side too, which csm did not do — a
    Metadata(conversation_id=None) handed to a UI is nonsense however it was reached.

    VALIDATES ONLY, never trims: trimming an id would silently change an identity.
    """
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("conversation_id must be a non-blank string")


def clean_text(value):
    """A free-text field on the way IN: hidden codepoints STRIPPED, then trimmed.

    `sanitize_for_copy` coerces a non-str to '' before stripping, so a bad RPC payload
    cannot land a non-text value in a TEXT NOT NULL column. Sanitize runs BEFORE the
    trim, because `str.strip()` does not treat U+200B and friends as whitespace — a
    leading zero-width space would survive the other order.
    """
    return sanitize_for_copy(value).strip()


def clean_tags(tags):
    """A raw tag iterable -> the canonical, deduped, deterministically ordered tuple.

    * a BARE STRING is one tag, not a character sequence (`set_tags(conn, c,
      "important")` must not become 9 single-letter tags);
    * each tag is sanitized, then every whitespace RUN collapses to a single space —
      which trims it, drops a blank, and neutralises an embedded newline in one step.
      The newline part is load-bearing: '\\n' is the csm wire separator, so a tag
      carrying one would split into two on the round trip;
    * dedup is CASE-INSENSITIVE keeping the FIRST-seen casing, so a tag facet can never
      show 'Renderer' and 'renderer' as two different tags, and re-adding an existing tag
      in other casing does not rewrite what the user typed;
    * the order comes from the tag TEXT (casefold, then the exact string as tiebreak) and
      so is independent of insertion history — two callers who add the same tags in
      different orders store identical bytes.
    """
    if isinstance(tags, str):
        tags = [tags]
    seen = {}
    for raw in tags:
        tag = " ".join(clean_text(raw).split())
        if not tag:
            continue
        key = tag.casefold()
        if key not in seen:
            seen[key] = tag
    return tuple(sorted(seen.values(), key=lambda t: (t.casefold(), t)))


def _tags_key(tags):
    """The exact-membership match column: casefolded tags, newline-delimited with a
    LEADING AND TRAILING sentinel, so `instr(tags_key, '\\ntag\\n')` is a whole-tag test
    that cannot match a prefix ('important' must not match 'importantly'). '' when there
    are no tags — a bare '\\n\\n' would match the needle built for an empty tag."""
    if not tags:
        return ""
    return _SEP + _SEP.join(t.casefold() for t in tags) + _SEP


def _search_key(alias, tags, notes):
    """The free-text match column: casefolded alias + tags + notes.

    The analogue of csm's UpdateMetadataSql rebuilding `combined_text` as
    `... || $alias || char(10) || $tags || char(10) || $notes` on every metadata write.
    """
    return _SEP.join((alias, _SEP.join(tags), notes)).casefold()


def _tag_needle(tag):
    """The exact-match needle for one tag, normalised by the SAME `clean_tags` that
    normalises a STORED tag — so a probe and a stored value can never disagree. '' for a
    blank tag, which callers turn into "matches nothing" rather than "matches all"."""
    cleaned = clean_tags(tag)
    if not cleaned:
        return ""
    return _SEP + cleaned[0].casefold() + _SEP


# ------------------------------------------------------------------------ read

def _to_metadata(conversation_id, row):
    """(alias, tags, notes) -> Metadata; a MISSING row -> an EMPTY Metadata.

    Absence reading back as blanks is csm's semantics: SelectMetadataSql plus
    `if (!reader.Read()) return current` treats a missing row as "nothing to merge", and
    ListSessionsSql reads the NOT NULL columns as ''. Splitting the stored tags with a
    plain `if t` filter is csm's SplitLines (RemoveEmptyEntries); the stored form is
    already canonical, so the round trip is lossless.
    """
    if row is None:
        return Metadata(conversation_id=conversation_id)
    return Metadata(conversation_id=conversation_id, alias=row[0],
                    tags=tuple(t for t in row[1].split(_SEP) if t), notes=row[2])


def get_metadata(conn, conversation_id):
    """The annotations for `conversation_id`. An un-annotated conversation yields an
    EMPTY Metadata — never None, never an error. An UNUSABLE conversation_id is a
    different thing from an un-annotated one and does raise (`_check_id`)."""
    _check_id(conversation_id)
    row = conn.execute(
        "SELECT alias, tags, notes FROM conversation_metadata WHERE conversation_id=?",
        (conversation_id,)).fetchone()
    return _to_metadata(conversation_id, row)


def get_alias(conn, conversation_id):
    """The alias, or '' when unset."""
    return get_metadata(conn, conversation_id).alias


def get_tags(conn, conversation_id):
    """The canonical tag tuple, or () when unset."""
    return get_metadata(conn, conversation_id).tags


def get_notes(conn, conversation_id):
    """The notes, or '' when unset."""
    return get_metadata(conn, conversation_id).notes


# ----------------------------------------------------------------------- write

def _write(conn, conversation_id, alias, tags, notes):
    """Upsert one already-normalised annotation, or DELETE it when it holds nothing, then
    COMMIT. Returns the stored Metadata so a caller never has to re-read to see the
    result.

    The SINGLE write chokepoint — `clear_metadata` routes its DELETE through here too —
    so the key guard and the commit are stated once and no write entry point can reach
    the table around them.

    The commit is part of the contract, not a convenience: that returned Metadata reads
    as a success receipt, so the write it reports must have actually landed on disk.
    """
    _check_id(conversation_id)
    meta = Metadata(conversation_id=conversation_id, alias=alias, tags=tags, notes=notes)
    if meta.is_empty:
        conn.execute("DELETE FROM conversation_metadata WHERE conversation_id=?",
                     (conversation_id,))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO conversation_metadata(%s) VALUES (?,?,?,?,?,?)"
            % ",".join(_COLS),
            (conversation_id, alias, _SEP.join(tags), notes,
             _tags_key(tags), _search_key(alias, tags, notes)))
    conn.commit()
    return meta


def set_metadata(conn, conversation_id, alias=None, tags=None, notes=None):
    """Write the named fields and return the stored Metadata.

    `None` means LEAVE THIS FIELD UNCHANGED; an explicit '' (or an empty tag list) CLEARS
    it. csm cannot express a partial edit — SaveMetadataAsync always writes all three —
    but the cockpit edits one field at a time, and a per-field call must not silently
    blank the other two. Passing all three reproduces SaveMetadataAsync exactly, blanks
    included, which is how the UI clears a field.
    """
    cur = get_metadata(conn, conversation_id)
    return _write(conn, conversation_id,
                  cur.alias if alias is None else clean_text(alias),
                  cur.tags if tags is None else clean_tags(tags),
                  cur.notes if notes is None else clean_text(notes))


def set_alias(conn, conversation_id, alias):
    """Set the alias, leaving tags and notes alone."""
    return set_metadata(conn, conversation_id, alias=alias)


def set_tags(conn, conversation_id, tags):
    """REPLACE the tag set (canonicalised by clean_tags), leaving alias and notes alone."""
    return set_metadata(conn, conversation_id, tags=tags)


def set_notes(conn, conversation_id, notes):
    """Set the notes, leaving alias and tags alone."""
    return set_metadata(conn, conversation_id, notes=notes)


def add_tags(conn, conversation_id, tags):
    """UNION the given tags into the stored set — adding one twice is a no-op and the
    result stays deterministically ordered. Stored tags are listed first, so the
    first-seen-casing rule in clean_tags preserves the casing already on disk."""
    return set_metadata(conn, conversation_id,
                        tags=list(get_tags(conn, conversation_id)) + list(clean_tags(tags)))


def remove_tags(conn, conversation_id, tags):
    """DIFFERENCE the given tags out of the stored set. Case-insensitive, matching the
    case-insensitive dedup; removing a tag that is not applied is a no-op."""
    drop = {t.casefold() for t in clean_tags(tags)}
    return set_metadata(conn, conversation_id,
                        tags=[t for t in get_tags(conn, conversation_id)
                              if t.casefold() not in drop])


def clear_alias(conn, conversation_id):
    """Clear the alias only."""
    return set_metadata(conn, conversation_id, alias="")


def clear_tags(conn, conversation_id):
    """Clear every tag only."""
    return set_metadata(conn, conversation_id, tags=())


def clear_notes(conn, conversation_id):
    """Clear the notes only."""
    return set_metadata(conn, conversation_id, notes="")


def clear_metadata(conn, conversation_id):
    """Drop the whole annotation and COMMIT. Absent row -> a silent no-op, mirroring csm,
    whose UPDATE ... WHERE session_id=? simply affects zero rows for an unknown session.

    Expressed as an all-blank `_write` rather than as its own DELETE. An all-blank
    annotation is ALREADY stored as the absence of a row, so this issues byte-for-byte
    the same statement — and it is what gives `_write`'s key guard TEETH: every OTHER
    write path validates during its leading `get_metadata` read, so a guard in `_write`
    would be unreachable, un-assertable dead code (measured: its mutant survived a green
    100% suite) until one caller reaches it without reading first. This is that caller.
    """
    return _write(conn, conversation_id, "", (), "")


def merge_metadata(conn, conversation_id, alias="", tags=(), notes=""):
    """The RE-INGEST / IMPORT path: a BLANK incoming value yields to what is stored.

    This is csm's MergeExistingMetadataAsync — `IsNullOrWhiteSpace(incoming) ? stored :
    incoming` per field, with an EMPTY incoming tag list also yielding — so re-indexing a
    session cannot wipe hand-authored metadata. `clean_text` trims, so a whitespace-only
    incoming value is blank here exactly as it is under IsNullOrWhiteSpace.

    csm NEEDS this because alias/tags/notes are columns on the row an upsert rewrites;
    here they are in a separate table and `corpus.add_conversation` cannot touch them, so
    the invariant already holds. It is ported for the case the merge genuinely serves: an
    IMPORTED annotation set (a backup restore, a second machine, a csm export) that must
    not blank a field it has nothing to say about.
    """
    cur = get_metadata(conn, conversation_id)
    return _write(conn, conversation_id,
                  clean_text(alias) or cur.alias,
                  clean_tags(tags) or cur.tags,
                  clean_text(notes) or cur.notes)


# --------------------------------------------------------------- search / filter

def _select(conn, where, params):
    """Annotation rows matching `where` -> a Metadata list, ordered by conversation_id so
    a UI listing is deterministic."""
    rows = conn.execute(
        "SELECT conversation_id, alias, tags, notes FROM conversation_metadata "
        "WHERE %s ORDER BY conversation_id" % where, params).fetchall()
    return [_to_metadata(r[0], (r[1], r[2], r[3])) for r in rows]


def find_by_tag(conn, tag):
    """Every annotation carrying EXACTLY `tag`, case-insensitively.

    Matched with `instr()` against the sentinel-delimited `tags_key`, never LIKE: under
    LIKE a tag of '%' would match every row and '100%_done' would match far too much,
    because '%' and '_' are wildcards there. A blank tag matches NOTHING.
    """
    needle = _tag_needle(tag)
    if not needle:
        return []
    return _select(conn, "instr(tags_key, ?) > 0", (needle,))


def search_metadata(conn, text):
    """Substring search over alias + tags + notes, case-insensitive, blank -> [].

    csm's analogue is SearchAsync over the FTS `combined_text`. This is a literal
    substring match on `search_key` instead of an FTS query, deliberately:
    `conversations_fts` is CONTENTLESS with `detail=none` (corpus.py), so its rows cannot
    be updated in place to fold metadata in without re-supplying the original body — and
    this module must not edit corpus.py's schema. One row per ANNOTATED conversation is a
    small exact scan with no FTS-sync state to drift out of date and no wildcard-escaping
    hazard, and it still finds annotations for conversations the index has not ingested.
    """
    needle = clean_text(text).casefold()
    if not needle:
        return []
    return _select(conn, "instr(search_key, ?) > 0", (needle,))


def search_conversations(conn, text="", tag=""):
    """The UI listing entry point: matching annotations JOINED to their display columns.

    An INNER JOIN, like csm's SearchSql joining `sessions`, so only conversations the
    index actually knows come back; an annotation with no indexed conversation is still
    reachable through get_metadata / search_metadata. `text` and `tag` are ANDed. With
    NEITHER filter the result is [] — csm's SearchAsync also returns an empty list for a
    blank query rather than dumping the whole catalogue into the UI.
    """
    clauses, params = [], []
    needle = _tag_needle(tag)
    if needle:
        clauses.append("instr(m.tags_key, ?) > 0")
        params.append(needle)
    text_needle = clean_text(text).casefold()
    if text_needle:
        clauses.append("instr(m.search_key, ?) > 0")
        params.append(text_needle)
    if not clauses:
        return []
    return conn.execute(
        "SELECT c.conversation_id, c.provider, c.account, c.title, c.created_at, "
        "c.updated_at, c.turn_count, c.thread_id, m.alias, m.tags, m.notes "
        "FROM conversation_metadata m "
        "JOIN conversations c ON c.conversation_id = m.conversation_id "
        "WHERE %s ORDER BY c.conversation_id" % " AND ".join(clauses), params).fetchall()


def find_by_thread(conn, thread_id):
    """Every annotation on a conversation linked to `thread_id`, via the join column.

    The thread-scoped lookup that lets conversation_id stay the ONE key: on the Codex
    path `Conversation.id` and `ThreadMeta.id` are the same session id anyway, and
    index.build_index copies `conv.meta['thread_id']` into the conversations row. A blank
    thread_id matches nothing — `conversations.thread_id` DEFAULTs to '' for every
    non-Codex provider, so treating '' as a query would return the whole non-Codex
    corpus.
    """
    if not thread_id:
        return []
    return _select(
        conn,
        "conversation_id IN (SELECT conversation_id FROM conversations WHERE thread_id=?)",
        (thread_id,))


def tag_counts(conn):
    """tag -> how many conversations carry it, for the UI's tag facet.

    Counts collapse case-insensitively (two conversations tagged 'Beta' and 'beta' are
    one tag with count 2) and the label shown is the lexicographically-first display form
    among them, so the facet is deterministic and never lists the same tag twice. Ordered
    by the casefolded tag.
    """
    counts, display = {}, {}
    for (tags,) in conn.execute("SELECT tags FROM conversation_metadata "
                                "ORDER BY conversation_id"):
        for tag in (t for t in tags.split(_SEP) if t):
            key = tag.casefold()
            counts[key] = counts.get(key, 0) + 1
            display[key] = min(tag, display.get(key, tag))
    return {display[k]: v for k, v in sorted(counts.items())}
