"""Pin the `<engine-file>:<line>` citations in the cockpit IPC layer to real code.

WHY THIS EXISTS. `cockpit/src/ipc/mock.ts` and `types.ts` document the engine surface by
citing exact lines. Those anchors DRIFT, and nothing else catches it. Two measured sweeps:

  * `maintenance.py` — 26 of 35 citations landed on unrelated code, four on blank lines.
  * `sidecar.py` · `dedup.py` · `discover.py` · `metadata.py` ·
    `test_sidecar_maintenance.py` — 102 citations audited, **21 wrong**, of which
    **14 of 14 `discover.py` anchors** needed correcting (ten landed on unrelated code or
    a blank line). The other four engine files were clean: `metadata.py` 9/9 and
    `test_sidecar_maintenance.py` 8/8 correct, `dedup.py` 10/10 correct.

The claims were almost all true; the anchors had rotted. That is not cosmetic. A reader who
follows `discover.py:673` to check what a built-index `detail` carries lands on a blank line
and concludes the comment is nonsense, so the next person re-derives by hand what the
citation was supposed to save them. This test makes that drift a red build instead of a slow
decay.

WHAT IT ASSERTS. For each pinned citation: the cited line must still CONTAIN a token
belonging to the construct the claim is about. Not an exact-text match — that would break on
a reword — and not a line-number equality against a pinned constant, which is
self-falsifying (an equality-to-a-pinned-value check reports a legitimate edit as a broken
detector). A token is the cheapest thing that survives formatting and dies on a real move.

TOKEN STRENGTH, DISCLOSED. Where a cited RANGE opens on a generic line the token is
necessarily weak: `return {` (4 rows), `return [`, `targets = []`, `patterns = [`,
`@property` (3 rows), `def as_dict` (`discover.py` has two). Those rows still catch the
common drift — an insertion above shifts the line and the token vanishes — but a shift that
happens to land on ANOTHER identical generic line would pass. Re-anchoring a correct
citation purely to buy a nicer token was rejected as dishonest, so the residual is named
here instead of hidden. Rows marked `# weak` below are that set.

WHY IN pytest AND NOT vitest. It reads Python source; the Python rail already runs on every
CI leg; and a vitest reaching into `llm_anthology/` needs a `?raw` import that only works
because vitest ignores the Vite `fs.allow` boundary — a coupling not worth a second instance
of.

COVERAGE BOUNDARY — what is now pinned, and what is not.
  * COVERED: every `.py` citation in `mock.ts` and `types.ts`, across all ELEVEN engine
    files they cite (see ENGINES). A citation to an engine file NOT in ENGINES is a
    VIOLATION, not a silent pass, so adding a twelfth file forces a deliberate edit here.
  * NOT COVERED: the other cockpit sources. `ipc/real.ts`, `ui/**` and `graph/**` carry
    their own `<engine-file>:<line>` citations, audited separately and NOT pinned by this
    file. Absence of a pin there is not evidence they are correct — a sweep of
    `discover.py`/`sidecar.py` anchors in one such file found ~11 of 18 wrong.
  * NOT COVERED: `.rs` citations (e.g. `src-tauri/src/lib.rs:91-107`) — the scraper matches
    `.py` only.
  * QUARANTINED: one citation is deliberately left BROKEN, because the sentence it anchors
    is FALSE and rewording a false claim into a true one would destroy the evidence. See
    QUARANTINE.

This file adds no `llm_anthology` code, so it does not move the 100% coverage gate.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Every engine file the two IPC sources cite, keyed by the BASENAME a citation spells.
ENGINES = {
    "maintenance.py": REPO / "llm_anthology" / "maintenance.py",
    "sidecar.py": REPO / "llm_anthology" / "sidecar.py",
    "discover.py": REPO / "llm_anthology" / "discover.py",
    "dedup.py": REPO / "llm_anthology" / "dedup.py",
    "metadata.py": REPO / "llm_anthology" / "metadata.py",
    "research.py": REPO / "llm_anthology" / "research.py",
    "corpus.py": REPO / "llm_anthology" / "corpus.py",
    "codex_rollout.py": REPO / "llm_anthology" / "adapters" / "codex_rollout.py",
    "test_sidecar_maintenance.py": REPO / "tests" / "test_sidecar_maintenance.py",
    "test_sidecar_metadata.py": REPO / "tests" / "test_sidecar_metadata.py",
    "test_sidecar_dedup.py": REPO / "tests" / "test_sidecar_dedup.py",
}

TS_FILES = {
    "mock.ts": REPO / "cockpit" / "src" / "ipc" / "mock.ts",
    "types.ts": REPO / "cockpit" / "src" / "ipc" / "types.ts",
}

#: A citation naming its file. The leading `[\w./\\-]*` is GREEDY on purpose: that is what
#: makes `tests/test_sidecar_maintenance.py:194` resolve to the TEST file instead of being
#: read as a `maintenance.py:194` citation. A naive `\bmaintenance\.py:(\d+)` produced eight
#: false readings during the audit; `test_the_scraper_tells_presence_from_absence` locks the
#: fix in both directions.
#: The trailing `((?:,\d+…)*)` group catches the COMMA-LIST spelling — `corpus.py:179,197`
#: and `adapters/gemini.py:1,8`, both real in `discover.py`. Without it the second number is
#: invisible and unpinnable. Neither IPC source uses that shape (measured), so adding it
#: cannot disturb the TS rows.
_PRIMARY = re.compile(r"([\w./\\-]*\.py):(\d+)(?:-(\d+))?((?:,\d+(?:-\d+)?)*)")
_EXTRA = re.compile(r",(\d+)(?:-(\d+))?")

#: A SECONDARY anchor in the same sentence, e.g. "`:645-648` runs ahead of `:669`". It is
#: attributed to the NEAREST primary ON THE SAME LINE by character distance — nearest in
#: EITHER direction, because `types.ts:787` writes the secondary BEFORE its own primary
#: ("schema at `:823-836`). ... (`maintenance.py:870-871`)"). A preceding-only rule orphans
#: it. Nearest-on-the-same-line is also what stops `metadata.py:243-269, :456-463` being
#: charged to a different engine file mentioned earlier in the comment.
#:
#: TWO SPELLINGS, not one. The backticked form is what the IPC sources use; the ENGINE
#: sources also write it BARE after a comma — `(codex_rollout.py:343, :347, :435)`. A
#: backtick-only pattern silently dropped two of those three anchors, and both were wrong,
#: so the detector was hiding exactly the drift it exists to find.
_SECONDARY = re.compile(r"`:(\d+)(?:-(\d+))?|,\s*:(\d+)(?:-(\d+))?")


def _citations(text, engines=None):
    """-> (rows, problems). rows are (engine_basename, cited_line) pairs.

    `problems` collects the two shapes that must never pass silently: a secondary anchor on
    a line with no primary to own it (so its file is unknowable), and a citation to a `.py`
    file absent from ENGINES (so nothing verifies it).
    """
    engines = ENGINES if engines is None else engines
    rows, problems = [], []
    for lineno, line in enumerate(text.split("\n"), 1):
        prims = []
        for m in _PRIMARY.finditer(line):
            base = m.group(1).replace("\\", "/").rsplit("/", 1)[-1]
            prims.append((m.start(), base, int(m.group(2))))
            for extra in _EXTRA.finditer(m.group(4) or ""):
                prims.append((m.start(), base, int(extra.group(1))))
            if base not in engines:
                problems.append(
                    "line %d cites %s:%s, which is not in ENGINES — add it (with its pin "
                    "rows) or remove the citation; an unlisted engine file is verified by "
                    "nothing" % (lineno, base, m.group(2)))
        for m in _SECONDARY.finditer(line):
            anchor = m.group(1) or m.group(3)
            if not prims:
                problems.append(
                    "line %d carries the secondary anchor `%s` but no `<file>.py:<line>` "
                    "citation on the SAME line to own it, so its engine file is unknowable. "
                    "Keep a secondary on its primary's line." % (lineno, m.group(0).strip()))
                continue
            nearest = min(prims, key=lambda p: abs(p[0] - m.start()))
            rows.append((nearest[1], int(anchor)))
        rows.extend((base, cited) for _pos, base, cited in prims)
    return rows, problems


#: (ts file, engine file, cited line, token that line must contain, what the claim is about).
#:
#: The token is chosen from the FIRST line of each cited range. Where a claim cites a range,
#: only its opening line is pinned — enough to catch a move, cheap enough to stay honest
#: about what it proves.
PINS = [
    # ---------------------------------------------------------------- maintenance.py (40)
    ("mock.ts", "maintenance.py", 295, "PROTECTED_PATH_MARKERS", "the protected-store markers, ported verbatim"),
    ("mock.ts", "maintenance.py", 384, "def _spells_protected", "the protected-path matcher this ports"),
    ("mock.ts", "maintenance.py", 452, "def _effective_destination_root", "the effective destination root"),
    ("mock.ts", "maintenance.py", 463, "def _confirmation_phrase", "the phrase the operator must type"),
    ("mock.ts", "maintenance.py", 345, "def _classify", "the lexical outside-store-root half"),
    ("mock.ts", "maintenance.py", 536, "Checked last", "duplicate checked last, so protected wins"),
    ("mock.ts", "maintenance.py", 549, "warnings.append", "the blocked-target DANGEROUS warning"),
    ("mock.ts", "maintenance.py", 529, "planned_sources", "duplicate-target detection"),
    ("mock.ts", "maintenance.py", 419, "def _measured_size", "size measured from disk, not the caller"),
    ("mock.ts", "maintenance.py", 558, "warnings.append", "a DANGEROUS warning per ALLOWED target"),
    ("mock.ts", "maintenance.py", 472, "def _unique_name", "the deterministic -N suffix"),
    ("mock.ts", "maintenance.py", 457, '"deleted"', "a delete quarantines under the checkpoint root"),
    ("mock.ts", "maintenance.py", 645, "requires_typed_confirmation or not confirmation", "the confirmation gate"),
    ("mock.ts", "maintenance.py", 669, "if not apply", "the apply branch the gate precedes"),
    ("mock.ts", "maintenance.py", 670, "moves=preview.plan", "a dry run returns the planned moves"),
    ("mock.ts", "maintenance.py", 750, 'status = doc.get("status"', "restore's first check"),
    ("mock.ts", "maintenance.py", 784, "os.path.lexists(relocated)", "the per-entry restore rule"),
    ("mock.ts", "maintenance.py", 776, "seen_originals", "the duplicate-entry restore guards"),
    ("types.ts", "maintenance.py", 141, "class MaintenanceAction", "the action enum"),
    ("types.ts", "maintenance.py", 158, "class SessionStoreKind", "the store-kind enum"),
    ("types.ts", "maintenance.py", 182, "class SessionCopy", "one session file inside a plan"),
    ("types.ts", "maintenance.py", 206, "last_write_ms", "epoch ms, or null"),
    ("types.ts", "maintenance.py", 561, "is_hot", "a hot target raises a REVIEW warning"),
    ("types.ts", "maintenance.py", 558, "warnings.append", "a DANGEROUS warning per ALLOWED target"),
    ("types.ts", "maintenance.py", 576, "warnings.append", "the closing INFO summary"),
    ("types.ts", "maintenance.py", 150, "class MaintenanceWarningSeverity", "the severity enum"),
    ("types.ts", "maintenance.py", 452, "def _effective_destination_root", "the EFFECTIVE destination"),
    ("types.ts", "maintenance.py", 587, "requires_checkpoint=True", "always true"),
    ("types.ts", "maintenance.py", 588, "requires_typed_confirmation=True", "always true"),
    ("types.ts", "maintenance.py", 463, "def _confirmation_phrase", "the exact phrase, from the ALLOWED count"),
    ("types.ts", "maintenance.py", 794, "skip_unaccounted", "without it, the whole batch is refused"),
    ("types.ts", "maintenance.py", 670, 'manifest_path=""', "an empty manifest path on a dry run"),
    ("types.ts", "maintenance.py", 285, "unaccounted", "recorded originals a restore could not account for"),
    ("types.ts", "maintenance.py", 849, "def record_run", "one row of the audit ledger"),
    ("types.ts", "maintenance.py", 823, "MAINTENANCE_SCHEMA", "the ledger schema"),
    ("types.ts", "maintenance.py", 870, "ORDER BY recorded_at_ms DESC, manifest_path", "newest first, path breaks ties"),
    ("types.ts", "maintenance.py", 715, '"pending"', "the pending status"),
    ("types.ts", "maintenance.py", 720, '"executed"', "the executed status"),
    ("types.ts", "maintenance.py", 808, '"restored"', "the restored status"),
    ("types.ts", "maintenance.py", 626, "def read_checkpoint", "opens the manifest directly"),

    # -------------------------------------------------------------------- discover.py (15)
    # Every one of these was re-anchored in the 2026-08-08 sweep; ten of the fourteen
    # originals landed on unrelated code or a blank line.
    ("mock.ts", "discover.py", 108, "DEFAULT_MAX_PER_GROUP", "the per-group cap the census equals"),
    ("mock.ts", "discover.py", 248, 'report: str = "base"', "the field the two store shapes differ in"),
    ("mock.ts", "discover.py", 653, 'spec.report == "subdir"', "where report decides the named path"),
    ("mock.ts", "discover.py", 654, "max(mtimes) if mtimes else 0.0", "0.0 and never null when nothing is datable"),
    ("types.ts", "discover.py", 152, "class Finding:", "the Finding DTO this mirrors"),  # weak
    ("types.ts", "discover.py", 654, "max(mtimes) if mtimes else 0.0", "0.0 and never null when nothing is datable"),
    ("types.ts", "discover.py", 770, '"tables": tuple(sorted', "a built index's detail keys"),
    ("types.ts", "discover.py", 309, 'StoreSpec(provider="codex"', "a Codex store's detail keys"),
    ("types.ts", "discover.py", 720, '"size_bytes": _size', "an export file's detail keys"),
    ("types.ts", "discover.py", 50, "Adding a provider is a TABLE EDIT", "why kind/confidence stay plain strings"),
    ("types.ts", "discover.py", 68, "KIND_BUILT_INDEX", "the kind vocabulary"),
    ("types.ts", "discover.py", 72, "CONF_HIGH", "the confidence vocabulary"),
    ("types.ts", "discover.py", 189, "class ScanStats", "what one scan cost"),
    ("types.ts", "discover.py", 108, "DEFAULT_MAX_PER_GROUP", "the cap that names a truncated group"),
    ("types.ts", "discover.py", 897, "truncated_groups.append", "where a group is recorded as truncated"),

    # ----------------------------------------------------------------------- dedup.py (10)
    ("mock.ts", "dedup.py", 149, "@property", "has_larger_copy, the truncated-canonical flag"),  # weak
    ("mock.ts", "dedup.py", 118, "last_write_ms: Optional[int]", "epoch ms, or None"),
    ("mock.ts", "dedup.py", 244, "def scan_store", "one store root -> copies + errors"),
    ("mock.ts", "dedup.py", 249, "A missing root", "a missing root is empty, not an error"),
    ("types.ts", "dedup.py", 117, "store_kind: str = STORE_UNKNOWN", "store_kind is a plain str here"),
    ("types.ts", "dedup.py", 87, "STORE_LIVE", "the store-kind string vocabulary"),
    ("types.ts", "dedup.py", 118, "last_write_ms: Optional[int]", "epoch ms, or null"),
    ("types.ts", "dedup.py", 138, "@property", "duplicate_paths, retained as evidence"),  # weak
    ("types.ts", "dedup.py", 149, "@property", "has_larger_copy, the truncated-canonical flag"),  # weak

    # -------------------------------------------------------------------- metadata.py (8)
    ("mock.ts", "metadata.py", 529, "def tag_counts", "the case-collapsing tag facet"),
    ("mock.ts", "metadata.py", 214, "def clean_tags", "the engine's tag canonicalisation"),
    ("mock.ts", "metadata.py", 501, "if not clauses", "neither filter -> empty, not the catalogue"),
    ("mock.ts", "metadata.py", 243, "def _tags_key", "the sentinel-delimited whole-tag column"),
    ("mock.ts", "metadata.py", 456, "never LIKE", "whole-tag instr(), never a LIKE"),
    ("mock.ts", "metadata.py", 470, "instead of an FTS query", "free text is a substring match"),
    ("types.ts", "metadata.py", 501, "if not clauses", "neither filter -> empty, not the catalogue"),
    ("types.ts", "metadata.py", 529, "def tag_counts", "the case-collapsing tag facet"),

    # --------------------------------------------------------------------- sidecar.py (65)
    ("mock.ts", "sidecar.py", 299, "JSON-RPC error envelope", "the {code, message} envelope"),
    # These five arrived in mock.ts AFTER the engine-side sweep, from a concurrent change
    # outside it. They are listed here rather than left alone because an unpinned citation
    # is the state this file exists to refuse — `test_every_citation_is_pinned` went red on
    # them, which is the gate working, not a defect.
    ("mock.ts", "sidecar.py", 781, '_reject_nonlocal_path(index_path', "the index_path guard the mock reproduces"),
    ("mock.ts", "sidecar.py", 834, "Returns immediately", "the build reply means ACCEPTED, not finished"),
    ("mock.ts", "sidecar.py", 869, "if not sessions_root and not grok_root", "at least one source root must be named"),
    ("mock.ts", "sidecar.py", 878, '(sessions_root, "sessions_root")', "each named root refused if UNC or relative"),
    ("mock.ts", "sidecar.py", 1009, "if snapshot is None", "no job yet is a state, not an error"),
    ("mock.ts", "sidecar.py", 1014, 'requested is not None and requested != snapshot["job_id"]', "a stale job_id is refused, not answered"),
    ("mock.ts", "sidecar.py", 1018, '"sessions_root": _clean(snapshot["sessions_root"])', "build_status reports the root the job started with"),
    # The corrected `corpus.build` params note, formerly the QUARANTINE row below. The two
    # anchors are the whole correction: 856 shows the param IS `_opt_str`, 858 shows what
    # the engine actually refuses (neither root named) — which is not "codex_home missing".
    ("types.ts", "sidecar.py", 866, 'sessions_root = _opt_str(params, "sessions_root")', "every corpus.build param is optional"),
    ("types.ts", "sidecar.py", 869, "if not sessions_root and not grok_root", "the only refusal: NEITHER root named"),
    ("mock.ts", "sidecar.py", 1013, '"state": "idle"', "idle before any build, rather than erroring"),
    ("mock.ts", "sidecar.py", 587, "self.research_backend", "the MockBackend fallback"),
    ("mock.ts", "sidecar.py", 1366, "Partial update: an OMITTED field", "the metadata.set tri-state"),
    ("mock.ts", "sidecar.py", 1460, "Scan the known Codex stores under an EXPLICIT", "dedup.scan requires codex_home"),
    ("mock.ts", "sidecar.py", 1587, "SessionStoreKind.UNKNOWN", "the edge forces every target to UNKNOWN"),
    ("mock.ts", "sidecar.py", 1621, "result.executed and result.manifest_path", "only an applied run is recorded"),
    ("types.ts", "sidecar.py", 790, '"index_path": _clean(index_path)', "corpus.create returns {index_path, created}"),
    ("types.ts", "sidecar.py", 306, "def _clean", "the hidden-unicode strip, via _sanitize_tree"),
    ("types.ts", "sidecar.py", 834, "Returns immediately", "the build reply means ACCEPTED, not finished"),
    ("types.ts", "sidecar.py", 990, "def _corpus_build_status", "the poll-safe status surface"),
    ("types.ts", "sidecar.py", 622, '"research.synthesize"', "the research methods exist on the engine"),
    ("types.ts", "sidecar.py", 587, "self.research_backend", "the MockBackend fallback"),
    ("types.ts", "sidecar.py", 1317, "research.extract_entities(views, self.research_backend)", "extraction routes through the same backend"),
    ("types.ts", "sidecar.py", 1350, "return {", "the annotation DTO"),  # weak
    ("types.ts", "sidecar.py", 1338, "the absorbed csm annotation layer", "annotations are local-only by design"),
    ("types.ts", "sidecar.py", 1359, "Un-annotated reads back as an EMPTY", "is_empty rather than an error"),
    ("types.ts", "sidecar.py", 1365, "def _metadata_set", "the metadata.set surface"),
    ("types.ts", "sidecar.py", 1366, "Partial update: an OMITTED field", "the tri-state is the whole point"),
    ("types.ts", "sidecar.py", 1372, "if tags is not None and not isinstance", "a non-list tags is -32602"),
    ("types.ts", "sidecar.py", 1396, "def _metadata_search", "the metadata.search surface"),
    ("types.ts", "sidecar.py", 1398, "With neither filter the result is empty", "a blank query dumps nothing"),
    ("types.ts", "sidecar.py", 1408, "return [", "the search row DTO"),  # weak
    ("types.ts", "sidecar.py", 1428, '{"tag": _clean(tag)', "one entry of the tag facet"),
    ("types.ts", "sidecar.py", 1431, "Codex physical copies -> one logical session", "dedup is a view, never a delete"),
    ("types.ts", "sidecar.py", 1434, "The paths it returns are LOCAL filesystem paths", "display-sensitive, absent from MetadataView"),
    ("types.ts", "sidecar.py", 1447, "return {", "the DedupSession DTO"),  # weak
    ("types.ts", "sidecar.py", 1473, "return {", "the dedup.scan result"),  # weak
    ("types.ts", "sidecar.py", 1488, "the ONLY destructive surface", "why the client never sends a preview back"),
    ("types.ts", "sidecar.py", 1504, '"session_id": copy.session_id', "one session file inside a plan"),
    ("types.ts", "sidecar.py", 1505, "copy.store_kind.value", "a real enum serialized via .value"),
    ("types.ts", "sidecar.py", 1508, "def _preview_dto", "the plan preview DTO"),
    ("types.ts", "sidecar.py", 1516, '"blocked": [{"target"', "a refused target and its reason"),
    ("types.ts", "sidecar.py", 1518, '"warnings": [{"severity"', "the warning DTO"),
    ("types.ts", "sidecar.py", 1520, '"plan": [{"session_id"', "the planned moves"),
    ("types.ts", "sidecar.py", 1529, "return {", "the execute/restore result"),  # weak
    ("types.ts", "sidecar.py", 1551, "def _maintenance_plan", "maintenance.plan is pure"),
    ("types.ts", "sidecar.py", 1561, '(("store_root", store_root)', "both roots refused if UNC or relative"),
    ("types.ts", "sidecar.py", 1567, '_req_str(params, "action")', "an unknown action is -32602"),
    ("types.ts", "sidecar.py", 1575, "raw_targets, list) or not raw_targets", "targets must be a non-empty array"),
    ("types.ts", "sidecar.py", 1577, "targets = []", "how the edge builds each target"),  # weak
    ("types.ts", "sidecar.py", 1581, 'item.get("file_path")', "file_path is required per target"),
    ("types.ts", "sidecar.py", 1585, 'item.get("session_id"', "session_id defaults to empty"),
    ("types.ts", "sidecar.py", 1587, "SessionStoreKind.UNKNOWN", "the edge forces every target to UNKNOWN"),
    ("types.ts", "sidecar.py", 1588, 'item.get("size_bytes", 0) or 0', "size_bytes defaults to 0"),
    ("types.ts", "sidecar.py", 1600, "def _maintenance_execute", "the execute surface"),
    ("types.ts", "sidecar.py", 1618, "Consumed only once the engine ACCEPTED it", "a refused confirmation is correctable"),
    ("types.ts", "sidecar.py", 1625, "def _maintenance_restore", "the restore surface"),
    ("types.ts", "sidecar.py", 1630, "_reject_nonlocal_path(manifest_path", "manifest_path refused if UNC or relative"),
    ("types.ts", "sidecar.py", 1460, "Scan the known Codex stores under an EXPLICIT", "dedup.scan requires codex_home"),
    ("types.ts", "sidecar.py", 299, "JSON-RPC error envelope", "the {code, message} envelope"),
    ("types.ts", "sidecar.py", 684, "params must be an object", "-32602, wrong params"),
    ("types.ts", "sidecar.py", 677, "Internal error", "-32603, an unhandled engine fault"),
    ("types.ts", "sidecar.py", 252, "CORPUS_NOT_INDEXED", "-32000, no corpus attached"),
    ("types.ts", "sidecar.py", 253, "THREAD_NOT_FOUND", "-32001"),
    ("types.ts", "sidecar.py", 254, "DB_BUSY", "-32002, sqlite lock/busy"),
    ("types.ts", "sidecar.py", 255, "the safety model REFUSED", "the maintenance-refused code"),
    ("types.ts", "sidecar.py", 259, "A second corpus.build while one is still running", "the build-in-progress code"),
    ("types.ts", "sidecar.py", 262, "cannot run against THIS engine", "the build-unavailable code"),
    ("types.ts", "sidecar.py", 265, "where a file already exists", "the corpus-exists code"),

    # ------------------------------------------------- test_sidecar_maintenance.py (8)
    ("mock.ts", "test_sidecar_maintenance.py", 103, "def test_a_parent_traversal_root_is_caught_by_the_ENGINE", "traversal is the engine's refusal, not the edge's"),
    ("mock.ts", "test_sidecar_maintenance.py", 194, "def test_a_refused_confirmation_leaves_the_handle_usable", "a typo is correctable without a re-plan"),
    ("mock.ts", "test_sidecar_maintenance.py", 305, "def test_a_dry_run_does_not_enter_the_ledger", "only an applied run enters the ledger"),
    ("types.ts", "test_sidecar_maintenance.py", 154, "def test_plan_is_pure_and_touches_nothing", "plan mutates no filesystem"),
    ("types.ts", "test_sidecar_maintenance.py", 209, "def test_execute_defaults_to_a_dry_run", "apply defaults to False"),
    ("types.ts", "test_sidecar_maintenance.py", 194, "def test_a_refused_confirmation_leaves_the_handle_usable", "a typo is correctable without a re-plan"),
    ("types.ts", "test_sidecar_maintenance.py", 216, 'assert out["executed"] is False', "a dry run reports executed False"),
    ("types.ts", "test_sidecar_maintenance.py", 305, "def test_a_dry_run_does_not_enter_the_ledger", "only an applied run enters the ledger"),

    # ------------------------------------------- the remaining four engine files (12)
    ("mock.ts", "codex_rollout.py", 357, "MEASURED on a live store", "2043 .zst and zero plain .jsonl"),
    ("mock.ts", "codex_rollout.py", 416, "patterns = [", "ingest_sessions globs both forms"),  # weak
    ("mock.ts", "research.py", 88, 'response="", responder=None', "MockBackend returns '' by default"),
    ("types.ts", "research.py", 88, 'response="", responder=None', "MockBackend returns '' by default"),
    ("types.ts", "research.py", 76, "-> str: ...", "the Protocol stub, the only other synthesize"),
    ("mock.ts", "corpus.py", 183, "account", "conversations.account is NOT NULL DEFAULT ''"),
    ("types.ts", "corpus.py", 179, "CREATE TABLE IF NOT EXISTS conversations", "every column NOT NULL, so no nulls"),
    ("mock.ts", "test_sidecar_metadata.py", 124, "def test_metadata_set_whitespace_only_id", "a whitespace-only id is -32602"),
    ("types.ts", "test_sidecar_metadata.py", 84, "the store orders tags deterministically", "tags come back sorted"),
    ("types.ts", "test_sidecar_metadata.py", 89, "def test_metadata_set_is_partial", "an omitted field is untouched"),
    ("mock.ts", "test_sidecar_dedup.py", 101, "def test_dedup_scan_of_a_missing_home_is_empty_not_an_error", "a missing home is empty, not an error"),
    ("mock.ts", "test_sidecar_dedup.py", 170, "def test_dedup_sessions_is_empty_before_any_scan", "no scan yet -> empty"),
    ("types.ts", "test_sidecar_dedup.py", 101, "def test_dedup_scan_of_a_missing_home_is_empty_not_an_error", "a missing home is empty, not an error"),
]

#: (ts file, engine file, cited line, the FALSE sentence, why it is not simply re-anchored).
#:
#: A wrong anchor is drift and gets fixed. A FALSE CLAIM is a different and worse defect, and
#: re-pointing its anchor would launder it: there is no engine line that supports a claim the
#: engine contradicts. So the citation is left exactly as the audit found it and recorded
#: here instead. `test_a_quarantined_claim_has_not_been_quietly_reworded` asserts the false
#: sentence is still present VERBATIM, so editing it is a red build — which is the point:
#: the decision about what the sentence should say belongs to a human, not to whoever next
#: runs a sweep.
#: EMPTY, and that is a RESOLVED state rather than an unused table. The one row this list
#: ever held — types.ts saying `corpus.build` parameters "BOTH are required" — was resolved
#: by a human exactly as the row demanded, and the resolution is recorded in
#: FALSE_PREMISES_FIXED. `test_a_quarantined_claim_has_not_been_quietly_reworded` refuses to
#: pass on an empty list unless that record exists, so emptying this table cannot become a
#: silent way to switch the check off.
QUARANTINE = []


# ---------------------------------------------------------------- the ENGINE-SIDE surface
#
# Everything above pins the citations the COCKPIT makes about the engine. The engine also
# cites ITSELF — `sidecar.py` explains its threading model by pointing at `corpus.py` and
# `loaders.py`, `claude_code.py` justifies its edge shape against `grok.py`, and so on.
# Those anchors rot the same way and were unpinned entirely. A 2026-08-08 sweep of all 63:
# 16 were wrong, of which THREE were not merely mis-anchored but stated something the code
# CONTRADICTS (see FALSE_PREMISES_FIXED). One landed on a blank line.
#
# Kept as separate tables rather than folded into ENGINES/PINS because the two surfaces have
# different membership: `test_the_scrape_finds_citations_at_all` asserts every ENGINES entry
# is still cited BY THE IPC SOURCES, and `build.py` / `index.py` / `chatgpt.py` are cited
# only from inside the engine. Merging the maps would make that test red for a false reason.

#: The engine files that CARRY citations. A file with none does not belong here.
PY_SOURCES = {
    "sidecar.py": REPO / "llm_anthology" / "sidecar.py",
    "discover.py": REPO / "llm_anthology" / "discover.py",
    "cli.py": REPO / "llm_anthology" / "cli.py",
    "claude_code.py": REPO / "llm_anthology" / "adapters" / "claude_code.py",
    "grok.py": REPO / "llm_anthology" / "adapters" / "grok.py",
}

#: Every engine file an engine-side citation NAMES. Same fail-closed rule as ENGINES: a
#: citation to a file absent from here is a VIOLATION, not a silent pass.
PY_TARGETS = {
    "build.py": REPO / "llm_anthology" / "build.py",
    "chatgpt.py": REPO / "llm_anthology" / "adapters" / "chatgpt.py",
    "claude.py": REPO / "llm_anthology" / "adapters" / "claude.py",
    "claude_code.py": REPO / "llm_anthology" / "adapters" / "claude_code.py",
    "cli.py": REPO / "llm_anthology" / "cli.py",
    "codex.py": REPO / "llm_anthology" / "adapters" / "codex.py",
    "codex_rollout.py": REPO / "llm_anthology" / "adapters" / "codex_rollout.py",
    "codex_state.py": REPO / "llm_anthology" / "adapters" / "codex_state.py",
    "corpus.py": REPO / "llm_anthology" / "corpus.py",
    "discover.py": REPO / "llm_anthology" / "discover.py",
    "gemini.py": REPO / "llm_anthology" / "adapters" / "gemini.py",
    "grok.py": REPO / "llm_anthology" / "adapters" / "grok.py",
    "index.py": REPO / "llm_anthology" / "index.py",
    "loaders.py": REPO / "llm_anthology" / "loaders.py",
    "render_html.py": REPO / "llm_anthology" / "render_html.py",
    "sidecar.py": REPO / "llm_anthology" / "sidecar.py",
}

#: (source engine file, cited engine file, cited line, token that line must contain, claim).
#: Same token-containment contract as PINS, and the same honesty rule about weak tokens.
PY_PINS = [
    # ------------------------------------------------------------------- sidecar.py (16)
    ("sidecar.py", "codex_rollout.py", 343, '"file": rollout_path', "a producer of the `file` key"),  # weak
    ("sidecar.py", "codex_rollout.py", 347, '"file": rollout_path', "a producer of the `file` key"),  # weak
    ("sidecar.py", "codex_rollout.py", 435, '"file": path, "stage": "read"', "the third producer of `file`"),
    ("sidecar.py", "corpus.py", 303, "sqlite3.connect(path)", "the DEFAULT check_same_thread=True"),
    ("sidecar.py", "corpus.py", 292, "PRAGMA journal_mode=WAL", "WAL is what lets both connections share the file"),
    ("sidecar.py", "loaders.py", 452, "corpus.open_index(index_path)", "the worker opens its OWN connection"),
    ("sidecar.py", "loaders.py", 463, "conn.close()", "...and closes it, inside the worker thread"),
    ("sidecar.py", "index.py", 171, "if progress is not None", "the only cooperative abort point in the stack"),
    ("sidecar.py", "loaders.py", 336, "def load_corpus", "load_corpus DOES accept a progress callback"),
    ("sidecar.py", "loaders.py", 461, "progress=progress", "...and DOES forward it — the old premise is dead"),
    ("sidecar.py", "sidecar.py", 945, "loaders.load_corpus(", "the worker call that passes none"),
    ("sidecar.py", "sidecar.py", 890, 'codex_home = _opt_str(params, "codex_home")', "codex_home is read OPTIONAL, never required"),
    ("sidecar.py", "index.py", 168, "corpus.set_checkpoint", "the per-chunk commit + checkpoint"),
    ("sidecar.py", "codex_state.py", 127, "def _db_path", "the LIVE Codex store fallback"),
    ("sidecar.py", "codex_rollout.py", 425, "glob.glob(pattern", "ingest_sessions globs, so a typo'd root is silent"),
    ("sidecar.py", "codex_state.py", 96, "is retried then", "a missing/busy state DB is skipped by design"),
    ("sidecar.py", "loaders.py", 454, "_persist_graph(conn, result)", "the graph commits BEFORE the long ingest"),
    ("sidecar.py", "loaders.py", 394, "result = corpus.Corpus()", "load_corpus starts from a FRESH Corpus"),
    ("sidecar.py", "loaders.py", 788, "upsert_thread(conn", "the run is UPSERTed into a previous build's tables"),

    # ------------------------------------------------------------------ discover.py (19)
    ("discover.py", "loaders.py", 64, "never ingest our own output", "loaders already refuses its own output tree"),
    ("discover.py", "corpus.py", 179, "CREATE TABLE IF NOT EXISTS conversations", "the index schema"),
    ("discover.py", "corpus.py", 197, "CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts", "its FTS half"),
    ("discover.py", "codex_state.py", 45, '_DB_NAME = "state_5.sqlite"', "the state-DB marker filename"),
    ("discover.py", "codex_rollout.py", 1, "Codex CLI session rollout logs", "the rollout store shape"),
    ("discover.py", "sidecar.py", 246, '"claude-code": (claude_code', "the reparser that makes the finding openable"),
    ("discover.py", "claude_code.py", 1, "Claude Code transcripts", "the adapter that makes this store openable"),
    ("discover.py", "grok.py", 1, "Grok Build session store", "the Grok store shape"),
    ("discover.py", "chatgpt.py", 1, "ChatGPT native export", "the ChatGPT export shape"),
    ("discover.py", "cli.py", 8, "llm-anthology chatgpt", "the CLI verb for it"),
    ("discover.py", "claude.py", 1, "Claude native account-export", "the Claude export shape"),
    ("discover.py", "cli.py", 7, "llm-anthology claude", "the CLI verb for it"),
    ("discover.py", "loaders.py", 46, "users.json, memories.json", "the real sibling set of an export dir"),
    ("discover.py", "codex.py", 1, "Codex task export", "the task-export shape"),
    ("discover.py", "cli.py", 9, "llm-anthology codex", "the CLI verb for it"),
    ("discover.py", "gemini.py", 1, 'Google Takeout "Gemini Apps"', "the Takeout activity shape"),
    ("discover.py", "gemini.py", 8, "probed from the real transcript.json", "its probed schema"),
    ("discover.py", "codex_state.py", 129, 'os.environ.get("CODEX_HOME")', "the same home resolution discovery uses"),
    ("discover.py", "sidecar.py", 500, "def _reject_nonlocal_path", "the path refusal this mirrors"),
    ("discover.py", "codex_state.py", 11, "READ-ONLY + IMMUTABLE", "why the probe opens mode=ro&immutable=1"),

    # ------------------------------------------------------------------------ cli.py (4)
    ("cli.py", "loaders.py", 336, "def load_corpus", "load_corpus DOES accept a progress callback"),
    ("cli.py", "loaders.py", 461, "progress=progress", "...and forwards it after every committed chunk"),
    ("cli.py", "codex_state.py", 129, 'os.environ.get("CODEX_HOME")', "where the disclosed path resolves from, even when it is NOT read"),
    ("cli.py", "build.py", 107, "carries NO turns is a silent", "the silent false-success this was bitten by"),

    # ----------------------------------------------------------------- claude_code.py (11)
    # Converted from a FUNCTION-qualified citation (`corpus.add_conversation:278-281`) to
    # the file form, which is the only shape the scraper recognises. As written it was
    # outside the gate entirely: the anchor pointed at `init_index`, and the CLAIM beside
    # it described the pre-reindex early-return — the same dead premise that let
    # `loaders._admit` destroy 47% of the Codex store. Nothing could catch either, because
    # an unrecognised shape is not an unpinned citation, it is an invisible one.
    ("claude_code.py", "corpus.py", 347, "def add_conversation", "a duplicate id OVERWRITES, it does not drop"),
    ("claude_code.py", "discover.py", 318, 'StoreSpec(provider="claude-code"', "the StoreSpec that finds this store"),
    ("claude_code.py", "discover.py", 315, "Claude Code writes one .jsonl transcript", "the corrected openable note"),
    ("claude_code.py", "grok.py", 626, "def _read_subagents", "grok has the PARENT read `subagents/`"),
    ("claude_code.py", "claude.py", 175, "def _active_path", "the DAG walk this deliberately does NOT do"),
    ("claude_code.py", "grok.py", 632, "glob.escape", "the percent/bracket-bearing slug hazard"),
    ("claude_code.py", "sidecar.py", 1665, '"provider": meta.adapter', "the adapter label surfaced as `provider`"),
    ("claude_code.py", "render_html.py", 191, 'if b.type == "media"', "the renderer needs a LOCAL relative path"),
    ("claude_code.py", "render_html.py", 163, "body = content if isinstance(content, str)", "a result is JSON-dumped"),
    ("claude_code.py", "claude.py", 238, 'for item in (m.get("content")', "the claude.ai block vocabulary this matches"),
    ("claude_code.py", "render_html.py", 161, 'or "tool"', "the renderer already falls back to \"tool\""),
    ("claude_code.py", "codex_rollout.py", 31, "THE DEVELOPER ENVELOPE", "the shape codex_rollout drops"),
    ("claude_code.py", "grok.py", 516, "if not parent or not child", "half an edge is never emitted"),

    # ------------------------------------------------------------------------ grok.py (3)
    ("grok.py", "codex_rollout.py", 156, 'ir.Block("thinking"', "what codex does with a reasoning summary"),
    ("grok.py", "render_html.py", 148, 'if b.type == "thinking"', "a thinking block renders in a collapsed <details>"),
    ("grok.py", "sidecar.py", 1665, '"provider": meta.adapter', "the adapter label surfaced as `provider`"),
]

#: Secondary anchors in the engine that belong to a NON-`.py` primary written on an EARLIER
#: line — all three point into `.scratch/CLAUDE-CODE-SCHEMA.md`, cited at
#: `claude_code.py:30`. The scraper cannot attribute them (it only understands `.py`
#: primaries), so they are listed rather than swallowed: a NEW orphan still fails.
#:
#: DISCLOSED DEFECT, not fixed here: `.scratch/` is gitignored (`.gitignore:43`), so those
#: three anchors point at a file that does not exist in a fresh clone. Every reader outside
#: this machine follows them nowhere. Moving the spec into the repo is a call for the owner,
#: not a citation sweep, so it is recorded and left.
#:
#: THE SPELLING IS THE KEY, and it must be the WHOLE anchor as the scraper reports it —
#: `` `:116-122 ``, not `` `:116 ``. The first version of this set recorded three truncated
#: spellings and every one of them went RED, which is the behaviour to keep: an exemption
#: that does not match is an exemption that does not fire, so the orphan is reported. It
#: fails CLOSED. The alternative fixes were both rejected. Inventing a `.py` primary for
#: these lines would be a lie — they anchor into a MARKDOWN spec (the `.md` primary is at
#: `claude_code.py:31`), and rewording honest prose to please a `.py`-only scraper is the
#: dishonesty this file's header already refuses. Dropping the exemption entirely would
#: make the suite permanently red for three citations that are correct.
#: KEYED BY (file, spelling) — NOT by line number, which is what it used to be.
#:
#: The line number was pure maintenance cost with no verification value. It pinned nothing
#: about the citation; it only recorded where the citation happened to sit, so ANY edit
#: above one of these lines reddened the gate for a reason that had nothing to do with
#: citations. That is not theoretical — it fired three times in a single session, twice on
#: edits to the very docstring being corrected, and each time the "fix" was to renumber a
#: waiver that had never been wrong. A gate whose false alarms outnumber its catches is a
#: gate somebody eventually deletes.
#:
#: Dropping the line does NOT widen the waiver, because the exactly-one-site check below
#: replaces what the line was doing: a waiver may cover ONE orphan, and a second occurrence
#: of the same spelling in the same file is reported rather than silently absorbed. So the
#: set still cannot grow coverage by accident — it just stops caring where the line moved.
PY_ORPHAN_SECONDARIES = {
    ("claude_code.py", "`:116-122"),
    ("claude_code.py", "`:120-122"),
    ("claude_code.py", "`:102"),
}

#: Claims found FALSE — not mis-anchored, but contradicted by the code they cite — and
#: rewritten in place. Recorded so the correction is not silently re-reverted. Unlike
#: QUARANTINE these ARE fixed, because each had a true replacement sentence available.
FALSE_PREMISES_FIXED = (
    "sidecar.py:745-758 and cli.py:88-93 both said `loaders.load_corpus` does not forward "
    "a `progress` callback, so there was 'no hook to honour a cancel through'. It forwards "
    "one at loaders.py:428 and has taken the parameter since loaders.py:319 — and the "
    "comment at loaders.py:381-386 names sidecar.py as the passage its change invalidated, "
    "so the staleness was known and left. What is actually true is narrower and is now "
    "what the comments say: the two CALL SITES pass none.",
    "discover.py:311-313 told a caller 'no adapter in this repository reads that shape "
    "yet', of the Claude Code store. adapters/claude_code.py reads exactly that shape and "
    "sidecar.py:246 wires it into _REPARSERS, so the finding IS openable; the adapter's own "
    "docstring quoted the note and announced it obsolete without editing it.",
    "types.ts said `corpus.build` parameters 'BOTH are required and neither is defaulted by "
    "the engine', and the engine's own docstring said ``codex_home`` is REQUIRED. Both were "
    "FALSE: every one of the three params is read with `_opt_str` (sidecar.py:856-876) and "
    "the only refusal is when NEITHER root is named (sidecar.py:858-861). This was the sole "
    "QUARANTINE row — held broken on purpose because the fix was a human's call, not a "
    "sweep's — and it is now resolved on BOTH sides. The DECISION it justified survives "
    "intact and is what the corrected text says: never DEFAULT codex_home, because "
    "defaulting it once let an automated probe read the owner's live store. Optional is not "
    "lax; 'omitted' means no state graph, not go and find one.",
)


def _read(path):
    """Read a source file, FAILING LOUDLY if it has moved.

    Not `pytest.skip`: a skip on a missing input is the vacuous-pass shape this whole file
    exists to prevent. If a path moved, the pin must go red so someone re-points it.
    """
    if not path.exists():
        pytest.fail(
            "cannot find %s — this pin cannot verify anything until the path is re-pointed "
            "(a skip here would be a green build proving nothing)" % path
        )
    return path.read_text(encoding="utf-8")


def _engine_lines(name):
    return _read(ENGINES[name]).split("\n")


# ----------------------------------------------------------------- the detector's own control

def test_the_scraper_tells_presence_from_absence():
    """The both-states control: the scraper must FIRE on a real citation and stay SILENT on
    the spellings that look like one. Extended with the new engine files, because a control
    that only exercises `maintenance.py` says nothing about the other ten."""
    fire, problems = _citations("see (`llm_anthology/maintenance.py:645`) for the gate")
    assert fire == [("maintenance.py", 645)] and problems == []
    assert _citations("bare (`maintenance.py:295`)")[0] == [("maintenance.py", 295)]

    # THE TRAP: this filename ENDS IN `maintenance.py` but is a different file. A naive
    # `\bmaintenance\.py:(\d+)` graded eight test-file citations against the engine.
    assert _citations("(`tests/test_sidecar_maintenance.py:194-206`)")[0] == [
        ("test_sidecar_maintenance.py", 194)]
    # Same trap, one directory deeper, and for a file whose basename is not a suffix of another.
    assert _citations("(`llm_anthology/adapters/codex_rollout.py:357-359`)")[0] == [
        ("codex_rollout.py", 357)]
    # ...and the windows spelling, since these citations are authored on this box.
    assert _citations(r"(`llm_anthology\sidecar.py:995`)")[0] == [("sidecar.py", 995)]

    # A secondary is charged to the NEAREST primary on its line, in either direction.
    assert _citations("(`maintenance.py:645` ahead of `:669`)")[0] == [
        ("maintenance.py", 669), ("maintenance.py", 645)]
    assert _citations("schema at `:823`). ties (`maintenance.py:870`).")[0] == [
        ("maintenance.py", 823), ("maintenance.py", 870)]
    # ...so a secondary beside `metadata.py` is NOT charged to a file named earlier elsewhere.
    assert _citations("(`metadata.py:243`, `:456`)")[0] == [
        ("metadata.py", 456), ("metadata.py", 243)]

    # Both failure shapes must be REPORTED, not swallowed.
    assert "no `<file>.py:<line>` citation on the SAME line" in _citations("stray `:785`")[1][0]
    assert "not in ENGINES" in _citations("(`llm_anthology/render.py:12`)")[1][0]


def test_the_scrape_finds_citations_at_all():
    """Fail closed on an empty scrape, per TS file AND per engine file.

    Deliberately `> 0` rather than a pinned total: an exact-count assertion is
    self-falsifying — removing one legitimate citation would report "the scrape is broken",
    sending the next reader after a phantom parser bug instead of at their own edit.

    The per-ENGINE leg also catches a dead ENGINES entry: if every citation to a file is
    legitimately removed, this goes red and forces that entry to be deleted deliberately
    rather than left behind as decoration.
    """
    per_engine = {}
    for name, path in TS_FILES.items():
        rows, problems = _citations(_read(path))
        assert not problems, "%s: %s" % (name, "\n  ".join(problems))
        assert rows, (
            "found ZERO engine citations in %s. Either the file no longer documents the "
            "engine (then delete this pin deliberately) or the scraper broke (then fix it) "
            "— but a green build here would prove nothing." % name
        )
        for engine, _line in rows:
            per_engine[engine] = per_engine.get(engine, 0) + 1
    unused = sorted(set(ENGINES) - set(per_engine))
    assert not unused, (
        "ENGINES lists %s, which nothing in mock.ts/types.ts cites any more. Remove the "
        "entry (and its PINS rows) deliberately." % ", ".join(unused)
    )


# ------------------------------------------------------------------------------- the pin

def test_every_pinned_citation_lands_on_its_construct():
    """Each cited engine line must still contain a token from the construct it is cited for."""
    problems = []
    for name, engine, cited, token, about in PINS:
        lines = _engine_lines(engine)
        if cited > len(lines):
            problems.append(
                "%s cites %s:%d for %s, but that file now has only %d lines"
                % (name, engine, cited, about, len(lines))
            )
            continue
        actual = lines[cited - 1]
        if token not in actual:
            problems.append(
                "%s cites %s:%d for %s\n"
                "      expected that line to contain: %r\n"
                "      the line actually contains:    %r\n"
                "      -> the code moved; find %s in %s and re-point the citation"
                % (name, engine, cited, about, token, actual.strip(), token, engine)
            )
    assert not problems, "citation anchors have drifted:\n  " + "\n  ".join(problems)


def test_every_citation_is_pinned():
    """A citation may not be added or re-anchored without also being pinned here.

    Without this, the pin protects only the anchors that happened to be wrong once, and the
    next drift lands in whatever was never listed. Two unpinned anchors were caught this way
    while the maintenance table was being written.
    """
    pinned = {(n, e, c) for n, e, c, _t, _a in PINS}
    pinned |= {(n, e, c) for n, e, c, _s, _w in QUARANTINE}
    unpinned = []
    for name, path in TS_FILES.items():
        rows, _problems = _citations(_read(path))
        for engine, anchor in sorted(set(rows)):
            if (name, engine, anchor) not in pinned:
                unpinned.append(
                    "%s cites %s:%d but no PINS row verifies it — add "
                    '("%s", "%s", %d, "<token on that line>", "<what the claim is about>")'
                    % (name, engine, anchor, name, engine, anchor)
                )
    assert not unpinned, "unverified citations:\n  " + "\n  ".join(unpinned)


def test_every_pinned_engine_citation_lands_on_its_construct():
    """The engine-side twin of the pin above: each cited line must still contain a token
    from the construct it is cited for."""
    problems = []
    for src, target, cited, token, about in PY_PINS:
        lines = _read(PY_TARGETS[target]).split("\n")
        if cited > len(lines):
            problems.append(
                "%s cites %s:%d for %s, but that file now has only %d lines"
                % (src, target, cited, about, len(lines))
            )
            continue
        actual = lines[cited - 1]
        if token not in actual:
            problems.append(
                "%s cites %s:%d for %s\n"
                "      expected that line to contain: %r\n"
                "      the line actually contains:    %r\n"
                "      -> the code moved; find %s in %s and re-point the citation"
                % (src, target, cited, about, token, actual.strip(), token, target)
            )
    assert not problems, "engine citation anchors have drifted:\n  " + "\n  ".join(problems)


#: A token SHAPED like a citation: a dotted identifier followed by `:<line>`. Every segment
#: must begin with a letter or underscore, which is what keeps `192.0.2.1:445` and
#: `127.0.0.1:8812` — both real in these files — out of the match.
_CITATION_SHAPED = re.compile(r"\b([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+):(\d+)")

#: The suffixes `_PRIMARY` can actually resolve. Anything else ending a citation-shaped
#: token is a spelling the scraper will silently skip.
_READABLE_SUFFIXES = ("py", "ts", "rs", "json", "html", "md", "toml", "jsonl", "mjs", "yml",
                      "yaml", "ps1", "sh", "lock", "cfg", "ini", "txt")


def test_no_citation_uses_a_shape_the_scraper_cannot_SEE():
    """The gate's own blind spot, which is the one thing it cannot report on itself.

    `_PRIMARY` requires a `.py` filename. A FUNCTION-qualified citation —
    `corpus.add_conversation:278-281` — matches nothing, so it is not an unpinned citation,
    it is an INVISIBLE one: `test_every_engine_citation_is_pinned` cannot complain about a
    citation it never parses, and every other leg here is downstream of that same parse.

    That is not hypothetical. Exactly one existed, in `claude_code.py`, and it was wrong in
    both available ways at once: the anchor pointed at `init_index` rather than
    `add_conversation`, and the CLAIM beside it — that a duplicate id is "treated as
    already-present" and "SILENTLY DROPS" — described the pre-reindex early-return. That
    dead premise is the same one that let `loaders._admit` destroy 47% of the real Codex
    store, living on in a second file where nothing could see it. It survived every pass
    this suite has made.

    So the fix is not to teach the scraper one more shape — it is to refuse shapes it cannot
    read, which converts an invisible citation into a loud one. A reader who wants to cite a
    FUNCTION can still name it in prose; what must be machine-checkable is the anchor.
    """
    offenders = []
    for name, path in list(PY_SOURCES.items()) + list(TS_FILES.items()):
        for lineno, line in enumerate(_read(path).split("\n"), 1):
            for m in _CITATION_SHAPED.finditer(line):
                dotted = m.group(1)
                if dotted.rsplit(".", 1)[-1].lower() in _READABLE_SUFFIXES:
                    continue                      # a real filename — the scraper reads it
                offenders.append(
                    "%s:%d cites %r, which `_PRIMARY` cannot parse — so nothing verifies "
                    "it. Re-spell it as <file>.py:<line> (name the function in prose if "
                    "that matters) and add a PINS row." % (name, lineno, m.group(0)))
    assert not offenders, "citation shapes the scraper cannot see:\n  " + "\n  ".join(offenders)


def test_the_unreadable_shape_detector_can_actually_fire():
    """Control for the test above — its whole value is a NEGATIVE result, and a negative
    result from a detector that cannot fire is worth nothing.

    The real corpus is (now) clean, so the passing case alone would be identical to a regex
    that matches nothing at all. This feeds it the exact string that was live in
    `claude_code.py`, and the two false-positive shapes that must NOT trip it: an IP:port
    and a dotted version, both of which appear in these files for real.
    """
    def unreadable(text):
        return [m.group(0) for m in _CITATION_SHAPED.finditer(text)
                if m.group(1).rsplit(".", 1)[-1].lower() not in _READABLE_SUFFIXES]

    assert unreadable("`corpus.add_conversation:278-281` treats a duplicate") == [
        "corpus.add_conversation:278"], "the shape that actually shipped must be caught"
    assert unreadable("outbound SynSent to 192.0.2.1:445 (SMB)") == [], "an IP:port is not a citation"
    assert unreadable("the agent-mail server on 127.0.0.1:8812") == []
    assert unreadable("see corpus.py:347 and mock.ts:1241") == [], "real filenames are readable"


def test_every_engine_citation_is_pinned():
    """No engine-to-engine citation may exist unverified.

    Also the fail-closed leg for the two citation SPELLINGS the original scraper could not
    see — a comma-list primary (`corpus.py:179,197`) and a bare comma secondary
    (`codex_rollout.py:343, :347`). Both are real in this tree and both were WRONG when
    found, so a scraper blind to them is worse than no scraper.

    The last two legs retire DEAD entries, in both directions.

    ORPHANED PIN ROWS. `unpinned` catches a citation with no row; nothing caught a row with
    no citation, and that asymmetry is not academic — five such rows accumulated here, and
    three of them are how a re-anchoring pass lost track of which side had moved. The pin
    said `sidecar.py:934`, the comment said `:928`, both looked deliberate, and the only
    thing that noticed was the token check firing on an unrelated line.

    DEAD EXEMPTIONS. PY_ORPHAN_SECONDARIES already fails closed when a spelling is wrong
    (the orphan is simply reported), but nothing noticed an entry that matches nothing at
    all — the same decoration hazard `test_the_engine_scrape_is_not_silently_empty` guards
    for PY_TARGETS. An exemption is a standing waiver, so it has to be spent or deleted.

    NOT DONE HERE: the IPC twin `test_every_citation_is_pinned` has the same one-directional
    hole, and PINS is not checked for orphaned rows. Left alone deliberately — mock.ts and
    types.ts were being rewritten while this was authored, and adding a gate whose first act
    is to red-build somebody else's in-flight edit is how a useful check gets disabled.
    """
    pinned = {(s, t, c) for s, t, c, _tok, _a in PY_PINS}
    unpinned, problems, exercised, cited = [], [], set(), set()
    for name, path in PY_SOURCES.items():
        rows, probs = _citations(_read(path), engines=PY_TARGETS)
        cited.update((name, t, a) for t, a in rows)
        for p in probs:
            spelling = p.split("anchor ")[1].split(" but")[0].strip("`") if "anchor " in p else ""
            key = (name, "`:" + spelling.lstrip("`:"))
            if key in PY_ORPHAN_SECONDARIES:
                # A waiver covers ONE site. This is what the line number used to do, without
                # the false alarms: dropping the line stopped the gate caring WHERE the
                # anchor sits, and this keeps it caring HOW MANY it excuses, so a second
                # occurrence of the same spelling cannot slip in under an existing waiver.
                if key in exercised:
                    problems.append(
                        "%s: a SECOND orphan anchor `%s` — one waiver excuses one site, so "
                        "this one is unwaived. Give it a primary on its own line, or add a "
                        "deliberate second entry." % (name, spelling))
                    continue
                exercised.add(key)
                continue
            problems.append("%s: %s" % (name, p))
        for target, anchor in sorted(set(rows)):
            if (name, target, anchor) not in pinned:
                unpinned.append(
                    "%s cites %s:%d but no PY_PINS row verifies it — add "
                    '("%s", "%s", %d, "<token on that line>", "<what the claim is about>")'
                    % (name, target, anchor, name, target, anchor)
                )
    assert not problems, "engine citation problems:\n  " + "\n  ".join(problems)
    assert not unpinned, "unverified engine citations:\n  " + "\n  ".join(unpinned)
    orphaned = sorted("%s -> %s:%d" % k for k in pinned - cited)
    assert not orphaned, (
        "PY_PINS rows verify citations that no source makes any more: %s. This is the "
        "mirror of the check above and it is not cosmetic — a row nobody cites is verified "
        "against nothing, yet it reads as coverage. FIVE such rows accumulated silently "
        "before this leg existed, and three of them were how a re-anchoring sweep lost "
        "track of which side had moved: the pin said one line, the comment said another, "
        "and only the pin was ever read. Re-point the row to the line the source now cites, "
        "or delete it with the citation." % ", ".join(orphaned)
    )
    spent = sorted("%s %s`" % k for k in PY_ORPHAN_SECONDARIES - exercised)
    assert not spent, (
        "PY_ORPHAN_SECONDARIES waives %s, which no longer matches any orphan anchor. Either "
        "the citation moved (re-point the entry to the spelling the scraper now reports) or "
        "it is gone (delete the entry) — a waiver nothing spends is decoration, and the next "
        "reader will trust it as evidence the anchor was checked." % ", ".join(spent)
    )


def test_the_engine_scrape_is_not_silently_empty():
    """Fail closed per SOURCE file, and retire a dead PY_TARGETS entry deliberately.

    Same reasoning as the IPC twin: a `> 0` floor rather than a pinned total, because an
    exact-count assertion reports a legitimate edit as a broken detector.
    """
    seen = set()
    for name, path in PY_SOURCES.items():
        rows, _probs = _citations(_read(path), engines=PY_TARGETS)
        assert rows, (
            "found ZERO engine citations in %s. Either it no longer cites other engine "
            "files (then delete its PY_SOURCES entry deliberately) or the scraper broke." % name
        )
        seen.update(t for t, _line in rows)
    unused = sorted(set(PY_TARGETS) - seen)
    assert not unused, (
        "PY_TARGETS lists %s, which no engine source cites any more. Remove the entry "
        "(and its PY_PINS rows) deliberately." % ", ".join(unused)
    )


def test_the_scraper_reads_both_secondary_spellings():
    """The both-states control for the two shapes added with the engine sweep.

    Each must FIRE on the real spelling and the comma-list must not swallow a following
    sentence. Without this, `_SECONDARY` could quietly regress to backtick-only again and
    every bare `, :347` would vanish from the pinned set with the suite still green.
    """
    # bare comma secondaries, as sidecar.py:548 writes them
    rows, problems = _citations("(`codex_rollout.py:343, :347, :435`) every producer",
                                engines=PY_TARGETS)
    assert problems == []
    assert sorted(rows) == [("codex_rollout.py", 343), ("codex_rollout.py", 347),
                            ("codex_rollout.py", 435)]
    # comma-list primaries, as discover.py writes them
    assert sorted(_citations("# corpus.py:179,197 — the index schema", engines=PY_TARGETS)[0]) == [
        ("corpus.py", 179), ("corpus.py", 197)]
    assert sorted(_citations("# adapters/gemini.py:1,8 — Takeout", engines=PY_TARGETS)[0]) == [
        ("gemini.py", 1), ("gemini.py", 8)]
    # a lone range still reports only its opening line, as before
    assert _citations("(`loaders.py:64-66`)", engines=PY_TARGETS)[0] == [("loaders.py", 64)]
    # and the backticked spelling still works, so the IPC rows are unaffected
    assert _citations("(`maintenance.py:645` ahead of `:669`)")[0] == [
        ("maintenance.py", 669), ("maintenance.py", 645)]


def test_the_recorded_false_premises_are_not_re_asserted():
    """The two FALSE claims this sweep fixed must not creep back verbatim.

    A rewritten sentence is only as durable as the next person's memory of why. These two
    were each contradicted by code that a LATER change introduced, which is the hardest
    class to notice by reading — so the exact dead phrasings are asserted absent.

    A LITERAL SUBSTRING BAN, AND IT CATCHES A QUOTATION TOO. This is deliberate, and it
    has already fired once for that reason: the corrected `cli.py` comment quoted its own
    dead phrasing back ('an earlier version said load_corpus "..."'), which is a MENTION,
    not a re-assertion — and the build went red anyway. The fix was to paraphrase the
    mention, not to teach this test the difference. A use-vs-mention-aware check would let
    a genuine re-assertion hide inside quote marks, and this gate's whole value is that it
    cannot be argued with. Paraphrasing costs the comment nothing: the `_build_index`
    comment in `cli.py` still says exactly what the old claim was and why it died. So if
    this goes red, fix the SOURCE — do not add a quotation carve-out here.

    (No line number on that pointer on purpose. Nothing pins a citation made from THIS
    file — adding it to PY_SOURCES would demand pinning the deliberately-broken control
    strings below too — so an unverifiable `cli.py:<line>` here would be the exact rot the
    module exists to stop. A construct name survives the edit; a line number does not.)
    """
    assert FALSE_PREMISES_FIXED, "the record of what was corrected may not be emptied"
    progress_is_forwarded = ("load_corpus takes `progress` (loaders.py:319) and forwards it "
                             "(loaders.py:428)")
    dead = [
        (REPO / "llm_anthology" / "cli.py",
         "exposes no progress hook", progress_is_forwarded),
        (REPO / "llm_anthology" / "sidecar.py",
         "loaders.load_corpus does not forward one", progress_is_forwarded),
        (REPO / "llm_anthology" / "discover.py",
         "# reads that shape yet, so this finding",
         "adapters/claude_code.py reads the Claude Code shape and sidecar.py:246 wires it "
         "into _REPARSERS"),
        # The former QUARANTINE row, resolved on BOTH sides. Pinned as dead phrasings so the
        # resolution cannot be reverted by a later sweep that only reads one of the two.
        # SCOPED TO corpus.build ON PURPOSE, and the scoping is the whole lesson. A bare
        # ban on "``codex_home`` is REQUIRED" went red against `_dedup_scan`'s docstring
        # (sidecar.py:1431) — where the claim is TRUE, because that method reads the param
        # with `_req_str` (sidecar.py:1436). Conflating the two methods is exactly how the
        # false premise was born: corpus.build's docstring copied dedup.scan's rule onto a
        # param it reads with `_opt_str`. So the dead phrasing carries the clause that
        # names dedup.scan, and dedup.scan's own correct sentence stays untouched.
        (REPO / "llm_anthology" / "sidecar.py",
         "is REQUIRED and never defaulted, exactly as ``dedup.scan``",
         "that is corpus.build's docstring, where codex_home is read with _opt_str "
         "(sidecar.py:876) and the only refusal is NEITHER root named (sidecar.py:858-861). "
         "It is OPTIONAL there — never DEFAULTED, which is the true half worth keeping. "
         "dedup.scan is the method that genuinely requires it (_req_str, sidecar.py:1436)"),
        (TS_FILES["types.ts"],
         "BOTH are required and neither is defaulted",
         "all three corpus.build params are read with _opt_str (sidecar.py:856-876)"),
    ]
    problems = []
    for path, phrase, why in dead:
        if phrase in _read(path):
            problems.append(
                "%s has re-acquired the refuted claim %r. It is FALSE: %s. See "
                "FALSE_PREMISES_FIXED." % (path.name, phrase, why)
            )
    assert not problems, "a corrected false premise came back:\n  " + "\n  ".join(problems)


def test_a_quarantined_claim_has_not_been_quietly_reworded():
    """A quarantined FALSE claim must survive VERBATIM until a human decides what it says.

    This is the one place the file asserts exact text rather than a token, and deliberately
    so: the value of the record is that the wrong sentence is still readable. Rewording it
    to something true would erase the evidence that it was ever wrong, which is exactly how
    a defect gets re-introduced later.

    The table is now EMPTY, because the only row in it was resolved — by a human, which is
    what it was waiting for. An empty table would make this function pass vacuously, so an
    empty table is legal only against a recorded resolution.
    """
    if not QUARANTINE:
        assert any("QUARANTINE row" in entry for entry in FALSE_PREMISES_FIXED), (
            "QUARANTINE is empty and nothing in FALSE_PREMISES_FIXED records a resolved "
            "quarantine, so this test now passes vacuously — the single outcome it exists "
            "to prevent. Either restore the row or record how it was resolved."
        )
    problems = []
    for name, engine, cited, sentence, why in QUARANTINE:
        assert why.strip(), "%s:%s quarantine row has no stated reason" % (name, cited)
        body = _read(TS_FILES[name])
        if sentence not in body:
            problems.append(
                "%s no longer contains the quarantined sentence\n"
                "      %r\n"
                "      If it was FIXED: delete this QUARANTINE row and add a normal PINS row\n"
                "      for the corrected anchor. If it was merely reworded, put it back —\n"
                "      the reason it is quarantined is:\n      %s" % (name, sentence, why)
            )
            continue
        lines = _engine_lines(engine)
        if cited <= len(lines) and lines[cited - 1].strip():
            problems.append(
                "%s:%d was quarantined as a BROKEN anchor under a false claim, but that "
                "line now holds code (%r). Someone re-pointed it without resolving the "
                "claim. The claim is the problem:\n      %s"
                % (engine, cited, lines[cited - 1].strip(), why)
            )
    assert not problems, "quarantine drift:\n  " + "\n  ".join(problems)
