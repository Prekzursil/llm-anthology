"""Auto-discovery of AI session data already on this machine.

The app should not make its owner hunt for a SQLite index in a file picker: it should
look where these things actually live and offer what it finds. This module is the
scanner behind that. It answers one question — *what session data is on this box, and
what could the app do with it?* — and returns structured, sorted findings.

Three KINDS, because each leads to a different user action:

  ``built_index``    an existing corpus index (the schema ``corpus.INDEX_SCHEMA``
                     writes) that the app can OPEN immediately.
  ``session_store``  a live on-disk store that can be INGESTED — a Codex home with a
                     ``state_5.sqlite`` and a date-nested rollout tree, or a Claude
                     Code ``projects/`` directory of ``.jsonl`` transcripts.
  ``export_file``    a downloaded export the app can RENDER — a ChatGPT/Claude
                     ``conversations.json``, a Codex ``codex.json``, a Google Takeout
                     Gemini ``transcript.json``.

Four properties are load-bearing, and each is enforced by a test:

  * READ-ONLY. Nothing here opens a file for writing, and every SQLite probe uses the
    ``mode=ro&immutable=1`` URI (the same discipline as ``adapters/codex_state.py``),
    so a live store being written by a running agent is read without taking a lock and
    this process cannot mutate it. ``test_scan_mutates_nothing`` snapshots size+mtime
    of the whole fixture tree across a scan and asserts it is unchanged.

  * NO CONVERSATION CONTENT IS READ. Detection works from directory shapes, filenames,
    extensions, sizes and mtimes, plus exactly two structural sniffs that never look at
    a record: the first bytes of a file (is the first non-space character ``[`` or
    ``{``; is the header the SQLite magic) and one ``sqlite_master`` table-name query
    plus a ``COUNT(*)``. No message text is parsed, returned, or logged.
    ``test_never_returns_file_content`` plants a canary inside a fixture and asserts it
    cannot appear in the result.

  * BOUNDED. A naive recursive walk of a home directory takes minutes and hangs a UI.
    The search is therefore fixed candidate roots only (never ``C:\\``, never a bare
    home), a hard per-walk DEPTH cap, pruning of known-huge directory names, and a
    global cap on files examined. Measured on the author's machine (428 entries in
    Downloads, 107 in Documents, 196 on Desktop): a full default scan is well under a
    second.

  * TOTAL. A missing root, an unreadable directory, a permission error, a SYMLINK, or a
    corrupt SQLite file is recorded and skipped — never fatal. (A Windows JUNCTION is
    traversed, not skipped; see ``_is_dir`` for the measurement and why that is left as-is.)
    Recursion cannot run away: SYMLINKED directories are not descended
    (``os.DirEntry.is_dir(follow_symlinks=False)`` is False for a directory symlink), and
    independently of that the depth cap bounds any cycle — which is what bounds a JUNCTION,
    because a junction is NOT excluded by that check (see ``_walk``).

Adding a provider is a TABLE EDIT, not new scan code: append an ``ExportSpec`` or a
``StoreSpec`` to ``PROVIDERS``. Only providers whose on-disk shape this repository
actually grounds are shipped — a filename pattern is never invented for a provider
whose export format is unknown.

Paths in the result are ABSOLUTE and real, because a finding the caller cannot act on
is useless. They embed the local filesystem layout (and therefore the username), so a
caller that puts them on a wire is responsible for reducing them exactly as
``sidecar._build_error`` does for ingest errors.
"""
import dataclasses
import fnmatch
import os
import sqlite3
import time

# ------------------------------------------------------------------- vocabulary

KIND_BUILT_INDEX = "built_index"
KIND_SESSION_STORE = "session_store"
KIND_EXPORT_FILE = "export_file"

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"

# Kinds are ordered by how immediately actionable they are, so the default ordering is
# also the sensible presentation order: something openable now, then something
# ingestable, then something renderable.
_KIND_RANK = {KIND_BUILT_INDEX: 0, KIND_SESSION_STORE: 1, KIND_EXPORT_FILE: 2}

# ---------------------------------------------------------------------- bounding

#: How deep to descend below a user landing directory.
#:
#: 4, and the exact value is empirical rather than tidy. A real multi-account export
#: lands as ``Downloads/<batch>/<provider>/<account>/conversations.json`` and a
#: converted Takeout as ``Downloads/<batch>/Gemini Apps/_converted/transcript.json`` —
#: both put the file FOUR levels below the landing directory. Depth 3 was tried first
#: and silently missed eight real exports on the author's machine while every synthetic
#: test passed, because the fixtures encoded the assumed shape rather than the real one.
#:
#: Measured over the author's Downloads + Documents + Desktop, warm cache (cold in
#: brackets): depth 3 = 795 dirs / 13.7k files / 0.15 s [0.21 s]; depth 4 = 2188 dirs /
#: 39.5k files / 0.35 s [1.10 s]; depth 5 = 5069 dirs / 68.7k files / 0.99 s [1.94 s].
#: Depth 5 buys no known real shape for triple the work, so 4 is the stopping point.
FILE_SCAN_DEPTH = 4

#: Global cap on files examined in one scan. A real machine's full scan examines ~42k,
#: so this is roughly a 5x headroom ceiling for a pathological tree rather than a limit
#: normal use meets — at the measured rate it bounds the walk at about two seconds.
#: Hitting it sets ``ScanStats.budget_exhausted`` so a caller can say "partial results"
#: instead of presenting a truncated scan as an exhaustive one.
DEFAULT_FILE_BUDGET = 200000

#: Per (provider, kind) cap on emitted findings. A user with 200 saved chat exports
#: needs the newest handful offered, not a 200-row list; the rest are counted, and the
#: group is named in ``ScanStats.truncated_groups``.
DEFAULT_MAX_PER_GROUP = 25

#: Directory names never descended into. Every one is either enormous, machine-
#: generated, or a place a session export does not live. Pruning them is what keeps a
#: depth-3 walk of a real Downloads tree under a second.
PRUNED_DIR_NAMES = frozenset({
    "node_modules", ".git", ".hg", ".svn", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".tox", ".nox", "venv", ".venv", "env",
    "site-packages", "dist-info", "target", "build", "dist", "out",
    "AppData", "Library", "System Volume Information", "$RECYCLE.BIN",
    ".cache", ".gradle", ".m2", ".nuget", ".cargo", ".rustup", ".conda",
    "Windows", "Program Files", "Program Files (x86)",
    # this app's own rendered output. loaders.py:64-66 already refuses to INGEST a
    # `_site` tree ("never ingest our own output"); offering one back as a discovery
    # would recreate that bug from the other end.
    "_site", "_codex_site",
})

_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")

#: How many leading bytes the JSON sniff reads. Enough to clear a BOM and any
#: whitespace; far too few to contain a conversation.
_SNIFF_BYTES = 64


# --------------------------------------------------------------------- structures

@dataclasses.dataclass(frozen=True)
class Roots:
    """The candidate locations a scan is allowed to look in.

    Injected rather than discovered so a test can point a whole scan at a temporary
    tree, and so a UI can offer "also look in ..." without this module growing a
    second search strategy. Every non-empty entry must be an absolute local path.
    """
    codex_home: str = ""            # $CODEX_HOME, else ~/.codex
    claude_home: str = ""           # ~/.claude
    grok_home: str = ""             # $GROK_HOME, else ~/.grok
    user_dirs: tuple = ()           # Downloads / Documents / Desktop — export landing
    index_dirs: tuple = ()          # where an already-built corpus index might sit


@dataclasses.dataclass(frozen=True)
class Finding:
    """One discovered thing, and enough about it to act on and to rank it.

    ``count`` is the number of on-disk items the finding covers: sessions for a store,
    conversations for a built index, and 1 for a single export file (counting the
    conversations inside one would mean parsing it, which this module must not do).
    ``newest_mtime`` is epoch seconds of the newest item, or ``0.0`` when nothing datable
    was seen — never ``None``. This said None, while the scan has always fallen back to
    zero, so a caller written against the docstring would have guarded with ``is not
    None`` and let that zero through as a real timestamp — dating every empty store to
    1970 and sorting it oldest. The cockpit gets this right for the wrong reason:
    ``types.ts`` documents 0.0-and-never-null because someone read the code instead of
    this sentence. ``detail`` carries small structured non-content facts and is
    provider-specific.

    (The fallback expression is described rather than quoted on purpose: the citation gate
    pins it as a unique token, and repeating it verbatim here made it match two lines and
    silently disabled that pin.)
    """
    provider: str
    kind: str
    path: str
    count: int
    newest_mtime: float
    confidence: str
    detail: dict

    def as_dict(self):
        """A JSON-ready copy — tuples inside ``detail`` become lists."""
        return {"provider": self.provider, "kind": self.kind, "path": self.path,
                "count": self.count, "newest_mtime": self.newest_mtime,
                "confidence": self.confidence,
                "detail": {k: list(v) if isinstance(v, tuple) else v
                           for k, v in sorted(self.detail.items())}}


@dataclasses.dataclass(frozen=True)
class ScanStats:
    """What the scan actually cost and whether it saw everything.

    A caller must be able to tell a complete scan from a truncated one: silently
    returning partial results as if they were exhaustive is how "I found 0" becomes a
    false negative.
    """
    elapsed_seconds: float
    roots_scanned: int
    dirs_visited: int
    files_examined: int
    budget_exhausted: bool
    truncated_groups: tuple
    errors: tuple

    def as_dict(self):
        return {"elapsed_seconds": self.elapsed_seconds,
                "roots_scanned": self.roots_scanned,
                "dirs_visited": self.dirs_visited,
                "files_examined": self.files_examined,
                "budget_exhausted": self.budget_exhausted,
                "truncated_groups": list(self.truncated_groups),
                "errors": list(self.errors)}


@dataclasses.dataclass(frozen=True)
class ScanResult:
    findings: tuple
    stats: ScanStats

    def as_dict(self):
        return {"findings": [f.as_dict() for f in self.findings],
                "stats": self.stats.as_dict()}


# ----------------------------------------------------------------- the registry

@dataclasses.dataclass(frozen=True)
class ItemPattern:
    """One counted item shape inside a session store.

    ``ingestable`` is deliberately explicit: a store can hold items this repository
    cannot currently read, and reporting the total as if it were all loadable would
    promise an ingest that yields nothing.
    """
    name: str                       # the ``detail`` counter key
    glob: str                       # fnmatch, on the basename
    ingestable: bool = True


@dataclasses.dataclass(frozen=True)
class StoreSpec:
    """A live on-disk session store: a DIRECTORY shape."""
    provider: str
    root: str                       # which Roots field supplies the base
    subdir: str = ""                # where the items live, relative to the base
    item_patterns: tuple = ()       # ItemPattern, or a bare glob string
    item_depth: int = 5
    markers: tuple = ()             # (detail_key, filename) relative to the base
    report: str = "base"            # which path the finding names: "base" | "subdir"
    dir_counter: str = ""           # if set, count distinct item-bearing child dirs
    confidence: str = CONF_HIGH
    kind: str = KIND_SESSION_STORE


@dataclasses.dataclass(frozen=True)
class ExportSpec:
    """A downloaded export FILE, matched by name in a user landing directory."""
    provider: str
    patterns: tuple                 # fnmatch on the basename, case-insensitive
    dir_any: tuple = ()             # containing-dir names that CONFIRM the provider
    sibling_any: tuple = ()         # sibling entry names that CONFIRM the provider
    require_json: bool = True       # gate on the structural first-byte sniff
    confidence: str = CONF_MEDIUM
    kind: str = KIND_EXPORT_FILE


@dataclasses.dataclass(frozen=True)
class IndexSpec:
    """An already-built corpus index, identified by its SCHEMA rather than its name."""
    provider: str
    required_tables: tuple
    count_table: str = ""           # optional COUNT(*) for a useful size signal
    confidence: str = CONF_HIGH
    kind: str = KIND_BUILT_INDEX


#: The shipped provider table.
#:
#: Every entry is grounded in this repository, cited below. Providers the owner wants
#: next — Copilot, DeepSeek, Perplexity, Cursor — are deliberately ABSENT: their
#: on-disk export shapes are not established here, and a guessed filename pattern is a
#: detector that silently never fires. Add one by appending a row once its real shape
#: is known — Grok/xAI joined the table that way, once `.scratch/GROK-SCHEMA.md`
#: measured its store and `adapters/grok.py` could read it.
PROVIDERS = (
    # corpus.py:179,197 — the index schema; conversations + conversations_fts identify
    # an index this app wrote, regardless of what the file is called.
    IndexSpec(provider="anthology",
              required_tables=("conversations", "conversations_fts"),
              count_table="conversations"),

    # adapters/codex_state.py:45 (state_5.sqlite) and adapters/codex_rollout.py:1
    # (sessions/YYYY/MM/DD/rollout-*.jsonl).
    #
    # COUPLED TO THE ENGINE — re-check this flag when codex_rollout changes.
    #
    # `.zst` WAS marked not-ingestable, correctly at the time: codex_rollout globbed
    # "rollout-*.jsonl" only, while the measured live store held 2024 `.zst` and ZERO plain
    # `.jsonl`, so reporting them as loadable would have promised an ingest that yields
    # nothing. That reader is now fixed — ingest_sessions globs both suffixes and
    # transparently decompresses, measured on the same store as docs=2042 where it had
    # returned 0 — so the flag is flipped, as the note here predicted it would be.
    #
    # Leaving it False after the fix was NOT harmless: the shipped discovery panel showed
    # "2,024 sessions · ingestable 0" for a store that had just become fully readable,
    # i.e. it told the user their entire Codex history could not be imported. A stale
    # capability flag understates the product as confidently as a wrong one overstates it.
    # ~/.codex/archived_sessions is deliberately NOT counted: `subdir` is the single
    # tree an ingest is pointed at, and the archive is a sibling of it.
    StoreSpec(provider="codex", root="codex_home", subdir="sessions",
              item_patterns=(ItemPattern("rollouts_jsonl", "rollout-*.jsonl"),
                             ItemPattern("rollouts_zst", "rollout-*.jsonl.zst")),
              markers=(("state_db", "state_5.sqlite"),),
              item_depth=5, report="base"),

    # Claude Code writes one .jsonl transcript per session under ~/.claude/projects/<slug>/.
    # This finding IS openable: `adapters/claude_code.py:1-6` reads that shape and
    # `sidecar.py:258` wires it in. (Said "no adapter reads it yet" until that adapter landed.)
    StoreSpec(provider="claude-code", root="claude_home", subdir="projects",
              item_patterns=("*.jsonl",), item_depth=2, report="subdir",
              dir_counter="project_dirs"),

    # adapters/grok.py:1 — Grok Build keeps a LIVE store at
    # <grok_home>/sessions/<percent-encoded-cwd>/<session-id>/, one DIRECTORY per
    # session. `updates.jsonl` is the counted item because it is the conversation;
    # counting `summary.json` too would double every session.
    #
    # report="subdir", so `path` is the sessions root that grok.ingest_sessions()
    # takes directly — no caller has to derive it.
    #
    # item_depth=3 is exact, not slack: it reaches files in <enc-cwd>/<session-id>/
    # and stops ABOVE `subagents/<id>/`, so a subagent's bookkeeping directory can
    # never inflate the session count this reports (grok.py:_is_subagent_dir makes
    # the same exclusion on the ingest side).
    #
    # No markers: an empty ~/.grok must report nothing, and the item count is the
    # only honest evidence a store is there.
    StoreSpec(provider="grok", root="grok_home", subdir="sessions",
              item_patterns=(ItemPattern("session_updates", "updates.jsonl"),),
              item_depth=3, report="subdir", dir_counter="session_dirs"),

    # adapters/chatgpt.py:1 and cli.py:8 — the native export is conversations.json.
    # Claude's export uses the SAME filename, so neither claims it alone; `chat.html`
    # is the disambiguating sibling (OBSERVED beside a real ChatGPT export on this
    # machine, not read from a specification).
    #
    # `conversations-NNN.json` is the CHUNKED form a large history arrives in. It is
    # observed on disk rather than documented, and it collides with nothing (Claude's
    # file carries no `-NNN`), so it is unambiguous and needs no sibling to confirm.
    ExportSpec(provider="chatgpt",
               patterns=("conversations.json", "conversations-*.json"),
               sibling_any=("chat.html",)),

    # adapters/claude.py:1 and cli.py:7. loaders.py:46-47 names the real sibling set
    # that a Claude export directory carries — that is what disambiguates it from a
    # ChatGPT export of the same filename, without opening either file.
    ExportSpec(provider="claude", patterns=("conversations.json",),
               sibling_any=("users.json", "memories.json", "design_chats",
                            "reflections", "projects.json")),

    # adapters/codex.py:1 and cli.py:9 — the task export. A unique filename.
    ExportSpec(provider="codex", patterns=("codex.json",), confidence=CONF_HIGH),

    # adapters/gemini.py:1,8 — Google Takeout "Gemini Apps" activity, probed from the
    # real transcript.json. transcript.json is a generic name, so it is only LOW on its
    # own and is confirmed by the Takeout folder shape around it.
    ExportSpec(provider="gemini", patterns=("transcript.json",),
               dir_any=("Gemini Apps", "Takeout", "*Gemini*"),
               confidence=CONF_LOW),
)


# ------------------------------------------------------------------ public entry

def default_roots(home=None, env=None):
    """The candidate roots for this machine.

    ``$CODEX_HOME`` wins over ``~/.codex`` because that is the precedence
    ``adapters/codex_state.py:129`` already uses — a discovery that disagreed with the
    loader would point at a store the ingest then ignores. ``$GROK_HOME`` is the same
    shape one level over: the override names the HOME, and the sessions tree hangs off
    it, so the default resolves to ``~/.grok/sessions``. UNVERIFIED whether Grok reads
    ``$GROK_HOME`` as the home or as the sessions directory itself; the settling check
    is the ``grok_home`` value inside any ``summary.json``, which is a path and not
    conversation content.
    """
    home = home if home is not None else os.path.expanduser("~")
    env = env if env is not None else os.environ
    user_dirs = tuple(os.path.join(home, name)
                      for name in ("Downloads", "Documents", "Desktop"))
    return Roots(codex_home=env.get("CODEX_HOME") or os.path.join(home, ".codex"),
                 claude_home=os.path.join(home, ".claude"),
                 grok_home=env.get("GROK_HOME") or os.path.join(home, ".grok"),
                 user_dirs=user_dirs,
                 index_dirs=user_dirs)


def discover(roots=None, specs=None, file_budget=DEFAULT_FILE_BUDGET,
             max_per_group=DEFAULT_MAX_PER_GROUP):
    """Scan the candidate roots and return everything found, sorted and bounded.

    Read-only and content-blind (see the module docstring). Raises ``ValueError`` for a
    UNC/network or non-absolute root — the same discipline as
    ``sidecar._reject_nonlocal_path``, which the RPC layer should translate into its own
    ``-32602``.
    """
    roots = roots if roots is not None else default_roots()
    specs = specs if specs is not None else PROVIDERS
    for path in _all_roots(roots):
        _reject_nonlocal(path)

    state = _Scan(file_budget)
    findings = []
    findings.extend(_scan_stores(roots, specs, state))
    findings.extend(_scan_files(roots, specs, state))
    return ScanResult(findings=_finalize(findings, max_per_group, state),
                      stats=state.stats())


# ------------------------------------------------------------------- path guard

def _all_roots(roots):
    """Every configured root path, in a stable order, skipping unset ones."""
    return [p for p in (roots.codex_home, roots.claude_home, roots.grok_home)
            + tuple(roots.user_dirs) + tuple(roots.index_dirs) if p]


def _reject_nonlocal(path):
    """Reject a UNC / network path and any non-absolute path BEFORE touching the disk.

    A crafted ``\\\\host\\share`` target coerces an outbound SMB/NTLM authentication —
    the Windows hash-leak class — so it is refused rather than stat'ed. Mirrors
    ``sidecar._reject_nonlocal_path`` (sidecar.py:609).
    """
    if path.replace("/", "\\").startswith("\\\\"):
        raise ValueError("root must be a local path, not a UNC/network path: %s" % path)
    if not os.path.isabs(path):
        raise ValueError("root must be an absolute local path: %s" % path)


# ------------------------------------------------------------------ scan state

class _Scan:
    """Mutable counters shared by every walk in one scan."""

    def __init__(self, file_budget):
        self.file_budget = file_budget
        self.started = time.monotonic()
        self.roots_scanned = 0
        self.dirs_visited = 0
        self.files_examined = 0
        self.truncated_groups = []
        self.errors = []

    @property
    def exhausted(self):
        return self.files_examined >= self.file_budget

    def note_error(self, path, exc):
        self.errors.append("%s: %s" % (path, exc))

    def stats(self):
        return ScanStats(elapsed_seconds=round(time.monotonic() - self.started, 4),
                         roots_scanned=self.roots_scanned,
                         dirs_visited=self.dirs_visited,
                         files_examined=self.files_examined,
                         budget_exhausted=self.exhausted,
                         truncated_groups=tuple(sorted(self.truncated_groups)),
                         errors=tuple(self.errors))


# ------------------------------------------------------------------- the walker

def _walk(base, max_depth, state):
    """Yield ``(dirpath, entry, sibling_names)`` for every FILE at most ``max_depth``
    below ``base``.

    The single bounded traversal every scan uses. It never descends into a directory
    SYMLINK and never READS THROUGH a symlink of any kind (see ``_is_link``), never
    descends into a pruned name, never exceeds ``max_depth``, and stops entirely once the
    file budget is spent. Those three bounds are independent, so a directory cycle
    terminates even if
    one of them were wrong. An unreadable directory is recorded and skipped; it can
    never abort the walk.
    """
    stack = [(base, 0)]
    while stack:
        path, depth = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError as exc:
            state.note_error(path, exc)
            continue
        state.dirs_visited += 1
        names = frozenset(e.name for e in entries)
        for entry in entries:
            if _is_dir(entry, state):
                if depth + 1 < max_depth and entry.name not in PRUNED_DIR_NAMES:
                    stack.append((entry.path, depth + 1))
                continue
            # A SYMLINK IS NEVER READ THROUGH. `_is_dir` already refuses to DESCEND one,
            # but a link to a FILE used to fall through to here and be yielded — and every
            # consumer opens BY PATH, which follows: measured, `discover()` returned
            # export_file findings for a link whose real content sat outside every scanned
            # root, and `_head` on it returned that outside file's bytes.
            #
            # The sharp edge is not confinement but EGRESS: a link named
            # `conversations.json` aimed at `\\host\share\x` turns that open() into an
            # outbound SMB/NTLM authentication — a credential leak out of a first-run scan
            # the user never pointed anywhere. The `except OSError` in the readers prevents
            # the crash, not the connection.
            #
            # Skipped HERE rather than in `_head` because `_sqlite_shape` also opens by
            # path, so a link to a `.db` would otherwise stay reachable through the index
            # route (both were live in the measurement). One skip where entries are
            # produced covers every consumer, including future ones — and it makes the file
            # side agree with the dir side, which had already decided not to traverse links.
            if _is_link(entry, state):
                continue
            state.files_examined += 1
            if state.exhausted:
                return
            yield path, entry, names


def _is_dir(entry, state):
    """True for a directory that is descended.

    A directory SYMLINK reports False here and is therefore never descended. A WINDOWS
    JUNCTION DOES NOT — measured on this box (Python 3.14, `_winapi.CreateJunction`):
    a junction reports ``is_dir(follow_symlinks=False)`` True, ``is_symlink()`` False and
    ``os.path.islink()`` False, so it is traversed and only the depth cap bounds it. An
    earlier version of this docstring claimed False "for both, verified against the real
    ``~/.claude/skills`` junction" — but that artifact measures as ``is_symlink()`` True,
    i.e. it is a directory SYMLINK, not a junction, so the verification was performed
    against the wrong reparse type and the generalisation to junctions never held.

    The consequence is scope, not egress: a junction's target must be a local volume path,
    so it cannot aim a read at ``\\\\host\\share`` the way a symlink can (which is why
    ``_is_link`` guards the egress case and this does not). It does mean a junction inside
    a scanned root widens the scan past that root — left as-is deliberately, because
    someone may junction a store in on purpose and changing it is a behaviour decision,
    not a bug fix.

    An entry that cannot be stat'ed is treated as a non-directory so a single bad entry
    cannot abort the walk.
    """
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError as exc:                       # pragma: no cover - needs a race
        state.note_error(entry.path, exc)
        return False


def _is_link(entry, state):
    """True for a symlink — which this scanner SKIPS rather than reads through.

    The skip is recorded via ``note_error`` rather than dropped, because the panel already
    surfaces a count of skipped locations: a link that is silently ignored looks identical
    to a link that was never there, and the operator would have no way to learn why their
    file was not found.

    An entry that cannot be stat'ed is treated AS A LINK, i.e. skipped. That is the
    opposite of ``_is_dir``'s tolerance, and deliberately so: ``_is_dir`` fails open because
    mis-classifying a directory only costs some traversal, whereas here the thing being
    avoided is opening a path whose real target is unknown. Skipping cannot abort the walk.
    """
    try:
        if not entry.is_symlink():
            return False
    except OSError as exc:                       # a race, or a vanished entry
        state.note_error(entry.path, exc)
        return True
    state.note_error(entry.path, "skipped: a symbolic link is never read through")
    return True


def _existing_dir(path):
    return bool(path) and os.path.isdir(path)


# ------------------------------------------------------------------ store scan

def _item_patterns(spec):
    """``StoreSpec.item_patterns`` normalised — a bare glob string becomes an
    ``ItemPattern`` named after itself, so adding a store provider can stay a one-line
    table edit when it needs no per-shape breakdown."""
    return tuple(p if isinstance(p, ItemPattern) else ItemPattern(p, p)
                 for p in spec.item_patterns)


def _scan_stores(roots, specs, state):
    """Every StoreSpec whose base exists, resolved into at most one Finding each."""
    findings = []
    for spec in specs:
        if not isinstance(spec, StoreSpec):
            continue
        base = getattr(roots, spec.root, "")
        if not _existing_dir(base):
            continue
        state.roots_scanned += 1
        finding = _scan_one_store(spec, base, state)
        if finding is not None:
            findings.append(finding)
    return findings


def _scan_one_store(spec, base, state):
    """One store: count its items by shape, find its markers, or report nothing.

    A directory that merely sits at the right path is NOT a match — it must carry a
    marker file or at least one item, or every user with an empty ``~/.codex`` would be
    told they have a Codex store.
    """
    patterns = _item_patterns(spec)
    counts = dict((p.name, 0) for p in patterns)
    mtimes = []
    item_dirs = set()
    scan_root = os.path.join(base, spec.subdir) if spec.subdir else base

    if _existing_dir(scan_root):
        for dirpath, entry, _names in _walk(scan_root, spec.item_depth, state):
            low = entry.name.lower()
            hit = next((p for p in patterns if _matches_low(low, p.glob)), None)
            if hit is None:
                continue
            counts[hit.name] += 1
            mtimes.append(_mtime(entry, state))
            item_dirs.add(dirpath)

    detail = dict(counts)
    for key, filename in spec.markers:
        marker = os.path.join(base, filename)
        found = os.path.isfile(marker)
        detail[key] = marker if found else ""
        if found:
            mtimes.append(os.path.getmtime(marker))

    total = sum(counts.values())
    if not total and not any(detail.get(k) for k, _ in spec.markers):
        return None

    detail["ingestable"] = sum(counts[p.name] for p in patterns if p.ingestable)
    if spec.subdir:
        # `path` and the counted items are NOT always the same directory:
        # loaders.load_corpus(sessions_root, index_path, codex_home) takes both, and for
        # Codex they differ (~/.codex vs ~/.codex/sessions). Naming the item root
        # explicitly stops a caller wiring the count to the wrong parameter.
        detail["items_root"] = scan_root
    if spec.dir_counter:
        detail[spec.dir_counter] = len(item_dirs)
    return Finding(provider=spec.provider, kind=spec.kind,
                   path=scan_root if spec.report == "subdir" else base,
                   count=total, newest_mtime=max(mtimes) if mtimes else 0.0,
                   confidence=spec.confidence, detail=detail)


# ------------------------------------------------------------------- file scan

def _scan_files(roots, specs, state):
    """One bounded walk per user/index root, offering each file to every file spec.

    Export detection and built-index detection share the walk deliberately: they look in
    the same places, and walking those directories twice would double the only cost that
    matters here.
    """
    export_specs = tuple(s for s in specs if isinstance(s, ExportSpec))
    index_specs = tuple(s for s in specs if isinstance(s, IndexSpec))
    findings = []
    for base in _unique(tuple(roots.user_dirs) + tuple(roots.index_dirs)):
        if not _existing_dir(base):
            continue
        state.roots_scanned += 1
        for dirpath, entry, names in _walk(base, FILE_SCAN_DEPTH, state):
            findings.extend(_match_index(entry, index_specs, state))
            findings.extend(_match_exports(dirpath, entry, names, export_specs, state))
    return findings


def _unique(paths):
    """De-duplicate while preserving order — user_dirs and index_dirs usually overlap,
    and walking the same tree twice is pure cost."""
    seen, out = set(), []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _match_exports(dirpath, entry, names, specs, state):
    """Every ExportSpec matching this file, with the ambiguity between same-named
    exports resolved from SIBLING FILENAMES rather than from file contents.

    ChatGPT and Claude both ship ``conversations.json``. Opening one to tell them apart
    would mean reading conversation data, so instead: if a spec's grounded sibling set
    is present in the same directory it WINS outright at high confidence; if nothing
    disambiguates, every candidate is emitted at medium confidence carrying
    ``ambiguous_with``, and the caller can ask.
    """
    low = entry.name.lower()
    hits = [s for s in specs if any(_matches_low(low, p) for p in s.patterns)]
    if not hits:
        return []
    if any(s.require_json for s in hits) and not _looks_like_json(entry.path, state):
        return []

    confirmed = [s for s in hits
                 if s.sibling_any and _any_match(names, s.sibling_any)]
    if confirmed:
        hits, forced = confirmed, True
    else:
        forced = False

    ancestry = _components(dirpath)
    out = []
    for spec in hits:
        others = tuple(sorted(s.provider for s in hits if s is not spec))
        detail = {"size_bytes": _size(entry, state)}
        if others:
            detail["ambiguous_with"] = others
        out.append(Finding(
            provider=spec.provider, kind=spec.kind, path=entry.path, count=1,
            newest_mtime=_mtime(entry, state),
            confidence=_export_confidence(spec, forced, ancestry, others),
            detail=detail))
    return out


def _components(path):
    """Every directory NAME on `path`, root and drive letter excluded.

    Used to look for a confirming folder ABOVE a file rather than only beside it: the
    real converted Takeout puts its transcript in ``Gemini Apps/_converted/``, so the
    immediate parent is uninformative and only an ancestor identifies the provider.
    """
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    return tuple(p for p in rest.replace("\\", "/").split("/") if p)


def _export_confidence(spec, forced, ancestry, others):
    """HIGH when a sibling set confirmed it or a Takeout-style folder encloses it;
    MEDIUM while a same-named twin is still in play; otherwise the spec's own rating."""
    if forced:
        return CONF_HIGH
    if spec.dir_any and _any_match(ancestry, spec.dir_any):
        return CONF_HIGH
    return CONF_MEDIUM if others else spec.confidence


def _match_index(entry, specs, state):
    """A built corpus index, identified by SCHEMA not by filename.

    Cheap gates first: the suffix, then the 16-byte SQLite magic, and only then an
    actual connection. That ordering matters — a Downloads tree holds plenty of .db
    files, and opening each one would be the slowest thing this module does.
    """
    if not specs or not entry.name.lower().endswith(_SQLITE_SUFFIXES):
        return []
    if _head(entry.path, len(_SQLITE_MAGIC), state) != _SQLITE_MAGIC:
        return []
    tables, counts = _sqlite_shape(entry.path, specs, state)
    if tables is None:
        return []
    out = []
    for spec in specs:
        if not set(spec.required_tables) <= tables:
            continue
        detail = {"tables": tuple(sorted(spec.required_tables))}
        if spec.count_table:
            detail[spec.count_table] = counts.get(spec.count_table, 0)
        out.append(Finding(provider=spec.provider, kind=spec.kind, path=entry.path,
                           count=counts.get(spec.count_table, 0),
                           newest_mtime=_mtime(entry, state),
                           confidence=spec.confidence, detail=detail))
    return out


def _sqlite_shape(path, specs, state):
    """``(table names, {table: row count})`` for a SQLite file, or ``(None, {})``.

    Opened ``mode=ro&immutable=1`` for the same reason ``adapters/codex_state.py:11-13``
    does: a live store being written by a running agent is read without taking a lock,
    without triggering journal recovery, and with no possibility of mutating it. Only
    table NAMES and row COUNTS are read — never a row.
    """
    # `%` FIRST, and the order is the whole correctness argument: URI mode DECODES `%HH`,
    # so an un-escaped `%` means the path SQLite resolves is not the path on disk. Measured:
    # `Chat%20Export.sqlite` — exactly what a browser download produces — resolved to
    # `Chat Export.sqlite`, which does not exist, and `a%41b.sqlite` to `aAb.sqlite`. The
    # failure was SILENT and total: the suffix and 16-byte magic gates in `_match_index`
    # both use a plain `open()` (no decoding), so only this connection failed, `tables`
    # came back None, the finding was dropped, and first-run discovery reported "No AI
    # session data found in the usual places" with the index sitting in the scanned folder.
    # Escaping `%` LAST would re-escape the `%` of `%3f`/`%23` into `%253f`/`%2523` and
    # break the two cases this line already handled.
    esc = (path.replace("%", "%25").replace("?", "%3f").replace("#", "%23"))
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro&immutable=1" % esc, uri=True)
        tables = set(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"))
        counts = {}
        for spec in specs:
            name = spec.count_table
            if name and name in tables and name not in counts:
                counts[name] = conn.execute(
                    "SELECT COUNT(*) FROM \"%s\"" % name.replace('"', '""')).fetchone()[0]
        return tables, counts
    except sqlite3.Error as exc:
        state.note_error(path, exc)
        return None, {}
    finally:
        if conn is not None:
            conn.close()


# ------------------------------------------------------------------- tiny probes

def _matches_low(low_name, pattern):
    """Case-insensitive fnmatch against an ALREADY-LOWERED basename.

    The hot path: it runs once per candidate pattern per file, ~300k times in a real
    scan. ``fnmatch.fnmatch`` was measured at 0.88 s over that many calls because it
    re-runs ``os.path.normcase`` on both arguments every time; ``fnmatchcase`` on a name
    the caller lowered ONCE per file measures 0.22 s for the same work, about a third of
    the whole scan. Hence the slightly awkward "caller lowers" contract.
    """
    return fnmatch.fnmatchcase(low_name, pattern.lower())


def _matches(name, pattern):
    """Case-insensitive fnmatch on a basename — Windows paths are case-insensitive and
    an export folder is as likely to be ``Conversations.json`` as ``conversations.json``."""
    return _matches_low(name.lower(), pattern)


def _any_match(names, patterns):
    return any(_matches(n, p) for n in names for p in patterns)


def _head(path, count, state):
    """The first ``count`` bytes of a file, or ``b""`` if it cannot be read."""
    try:
        with open(path, "rb") as fh:
            return fh.read(count)
    except OSError as exc:
        state.note_error(path, exc)
        return b""


def _looks_like_json(path, state):
    """Is the first non-whitespace character ``[`` or ``{``?

    The entire structural sniff. It reads a few dozen bytes to reject an HTML page or an
    empty file that happens to carry a JSON name; it cannot see a record, a key, or a
    message, and its result is a single boolean that is never logged.
    """
    head = _head(path, _SNIFF_BYTES, state).lstrip(b"\xef\xbb\xbf").lstrip()
    return head[:1] in (b"[", b"{")


def _mtime(entry, state):
    try:
        return entry.stat(follow_symlinks=False).st_mtime
    except OSError as exc:                       # pragma: no cover - needs a race
        state.note_error(entry.path, exc)
        return 0.0


def _size(entry, state):
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError as exc:                       # pragma: no cover - needs a race
        state.note_error(entry.path, exc)
        return 0


# --------------------------------------------------------------------- assembly

def _finalize(findings, max_per_group, state):
    """Cap each (provider, kind) group NEWEST-FIRST, then sort the survivors.

    Two different orderings, deliberately: the cap must keep the most recent items
    (an old export is the least interesting thing in a full Downloads folder), while the
    presentation order must be stable across runs so a UI never reshuffles. Selection by
    mtime, presentation by (kind, provider, path).
    """
    groups = {}
    for f in findings:
        groups.setdefault((f.provider, f.kind), []).append(f)

    kept = []
    for key, items in groups.items():
        if len(items) > max_per_group:
            state.truncated_groups.append("%s/%s" % key)
            items = sorted(items, key=lambda f: (-f.newest_mtime, f.path))[:max_per_group]
        kept.extend(items)
    return tuple(sorted(kept, key=lambda f: (_KIND_RANK.get(f.kind, 9),
                                             f.provider, f.path)))
