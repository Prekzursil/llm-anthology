"""Guards the COUNTS that `CLAUDE.md` and `SECURITY.md` state about this repository.

Both documents assert numbers about things that change underneath them, and on 2026-08-13
every one of those numbers was measured wrong:

D-1 -- `CLAUDE.md`'s "What CI runs" bullet named ONE workflow file's jobs and called them
"FOUR pre-existing jobs ... plus the `cockpit` job". Measured by parsing
`.github/workflows/` directly: FOUR workflow files, SIXTEEN jobs. `codeql.yml`,
`format-drift-canary.yml` and `release.yml` were absent from the sentence entirely, and two
of the three run on pull requests, so a reader trying to answer "what will CI do to my PR"
got an answer that was missing a third of the surface.

D-2 -- `CLAUDE.md`'s TypeScript-coverage paragraph carried `75.85%` statements (1156/1524)
and enumerated SIX modules "imported by zero test files". Both were stale in BOTH directions
by 2026-08-13: `cockpit/vitest.config.ts` now pins a measured ratchet at 71.71/80.67/77.19/
70.95 over a denominator built from `src/**/*.ts`, `ipc/real.ts` and `ui/search.ts` acquired
tests, and `ui/maintenanceShell.ts` and `ui/workspace.ts` joined the dark set. A per-module
percentage cannot be recomputed cheaply and moves every time a DOM test lands, so the fix is
not a fresher snapshot -- it is to state the SSOT and stop restating derived numbers.
`test_claude_md_states_no_ts_percentage_absent_from_the_config` enforces that: any percentage
written into that section must be one of the floors in `cockpit/vitest.config.ts`.

D-3 -- `SECURITY.md:247` claimed "**1677 tests** under `tests/` as of 2026-08-10". Measured
2026-08-13: 1,665 test functions by AST across 61 files, 1,786 cases collected by
`python -m pytest --collect-only`. Neither matches, because the recorded literal is a
snapshot of a number that grows every working day. The replacement is a FLOOR plus a
staleness band, which is the only shape of that claim a test can keep honest.

D-4 -- `SECURITY.md:238-244` recorded the Claude-rail validation figure twice, "236 / gate
231 here and in `ARCHITECTURE.md`, against 212 / gate 207 in `README.md`", while also saying
the later pair supersedes the earlier one. A document that carries the superseded number as
its own headline figure has not resolved the contradiction, it has documented it. The exact
composition of the difference stays UNVERIFIED -- the corpus is private, and only the owner
re-running the build and reading `_fidelity-report.json` can settle it -- so this file does
NOT try to check 212 against a corpus. It checks the one thing that IS checkable: a figure
this document attributes to another document must still appear in that document.

WHAT IS PINNED, AND WHAT IS DELIBERATELY NOT
--------------------------------------------
PINNED: the set of workflow files; the set of job ids in each; the runner OS of each job;
whether each workflow runs on `pull_request`; the total job count; that every percentage in
the TS-coverage section matches `cockpit/vitest.config.ts`; that every `.ts` and
`tests/test_*.py` path either document names exists; a floor and a staleness band on the
suite size; and cross-document figure citations.

NOT PINNED: matrix cell counts, per-module coverage percentages, and the membership of the
dark set. Those move whenever a test lands -- several moved while this file was being
written -- so pinning them buys a red suite, not a true document. They are stated nowhere
and recomputed on demand instead.

DETECTOR DISCIPLINE
-------------------
Every reader here fails CLOSED: `workflow_jobs` raises rather than returning `{}` when it
cannot find the `jobs:` block, `vitest_thresholds` raises on an empty parse, and
`test_readers_are_not_vacuous` asserts plausible non-zero counts before any absence claim is
believed. Two tests run the parsers against the KNOWN-BROKEN prose they replaced and require
them to fire there, because a matcher that is silent in both states measures nothing.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Present in a git checkout, absent from an unpacked sdist (pyproject.toml's sdist include
# allowlist carries /llm_anthology, /tests, README, LICENSE, SECURITY, ARCHITECTURE -- so
# CLAUDE.md and .github/ are legitimately missing there). Deliberately NOT one of the files
# under test: using CLAUDE.md as the marker would turn "somebody deleted CLAUDE.md" into a
# skip instead of a failure.
SOURCE_TREE_MARKER = REPO / ".github" / "workflows" / "release.yml"

requires_source_tree = pytest.mark.skipif(
    not SOURCE_TREE_MARKER.is_file(),
    reason="not a source checkout ({} is absent, so this is an unpacked sdist and the "
           "workflow files are legitimately missing)".format(SOURCE_TREE_MARKER),
)

CLAUDE = REPO / "CLAUDE.md"
SECURITY = REPO / "SECURITY.md"
README = REPO / "README.md"
ARCHITECTURE = REPO / "ARCHITECTURE.md"
WORKFLOWS = REPO / ".github" / "workflows"
TESTS = REPO / "tests"
COCKPIT = REPO / "cockpit"
VITEST_CONFIG = COCKPIT / "vitest.config.ts"

OS_TOKEN = re.compile(r"(?:ubuntu|windows|macos)-latest")

# One row of the CI table in CLAUDE.md:
#   | `ci.yml` | `test` | ubuntu-latest ... | yes |
CI_ROW = re.compile(
    r"^\|\s*`(?P<workflow>[\w.-]+\.ya?ml)`\s*"
    r"\|\s*`(?P<job>[\w.-]+)`\s*"
    r"\|(?P<runners>[^|]*)"
    r"\|(?P<on_pr>[^|]*)\|\s*$"
)


def read_doc(path: pathlib.Path) -> str:
    """Read a governance document, raising rather than returning '' if it is missing."""
    if not path.is_file():
        raise AssertionError("{} is missing; every assertion below would be vacuous".format(path))
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise AssertionError("{} is empty".format(path))
    return text


def _top_level_block(text: str, key: str) -> list:
    """Lines of a column-0 YAML block (`jobs:` / `on:`), excluding the key line itself.

    Deliberately hand-rolled: PyYAML is not a declared dependency of this project and a
    `try: import yaml / except ImportError: skip` would turn a missing parser into a silent
    green. The workflow files are machine-generated-uniform (column-0 keys, two-space job
    ids, four-space job attributes), which is all this needs.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == key + ":":
            start = i + 1
            break
        if line.startswith(key + ":") and line.rstrip() != key + ":":
            raise AssertionError(
                "inline {!r} in a workflow is not supported by this parser; the file changed "
                "shape and the CI table in CLAUDE.md has to be re-derived by a human".format(key)
            )
    if start is None:
        raise AssertionError("no top-level {!r} block found".format(key))
    out = []
    for line in lines[start:]:
        if line.strip() and not line.startswith((" ", "\t")):
            break
        out.append(line)
    return out


def workflow_files() -> list:
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    if not files:
        raise AssertionError("no workflow files under {}".format(WORKFLOWS))
    return files


def workflow_jobs(path: pathlib.Path) -> dict:
    """{job_id: [lines of that job's block]} for one workflow file."""
    block = _top_level_block(path.read_text(encoding="utf-8"), "jobs")
    jobs = {}
    current = None
    for line in block:
        head = re.match(r"^  ([A-Za-z0-9_.-]+):\s*(#.*)?$", line)
        if head:
            current = head.group(1)
            jobs[current] = []
        elif current is not None:
            jobs[current].append(line)
    if not jobs:
        raise AssertionError("{}: parsed zero jobs".format(path.name))
    return jobs


def workflow_triggers(path: pathlib.Path) -> set:
    block = _top_level_block(path.read_text(encoding="utf-8"), "on")
    triggers = set()
    for line in block:
        head = re.match(r"^  ([A-Za-z0-9_]+):\s*(#.*)?$", line)
        if head:
            triggers.add(head.group(1))
    if not triggers:
        raise AssertionError("{}: parsed zero triggers".format(path.name))
    return triggers


def job_os_tokens(job_lines: list) -> set:
    """OS runners a job can land on: its `runs-on:` plus its `strategy:` block ONLY.

    Scoped rather than scanning the whole job because step-level guards such as
    `if: matrix.os == 'windows-latest'` also carry OS tokens, and counting those would
    report a Linux-only job as a Windows one.
    """
    scoped = []
    in_strategy = False
    for line in job_lines:
        if re.match(r"^    strategy:\s*(#.*)?$", line):
            in_strategy = True
            continue
        if in_strategy:
            if line.strip() and not line.startswith("     "):
                in_strategy = False
            else:
                scoped.append(line)
        if line.startswith("    runs-on:"):
            scoped.append(line)
    return set(OS_TOKEN.findall("\n".join(scoped)))


def vitest_thresholds() -> set:
    """The coverage floors in cockpit/vitest.config.ts, as strings ('71.71')."""
    text = VITEST_CONFIG.read_text(encoding="utf-8")
    block = re.search(r"thresholds:\s*\{(.*?)\}", text, re.S)
    if not block:
        raise AssertionError("no coverage.thresholds block in {}".format(VITEST_CONFIG))
    values = set(re.findall(r"\w+:\s*([\d.]+)", block.group(1)))
    if not values:
        raise AssertionError("coverage.thresholds in {} parsed empty".format(VITEST_CONFIG))
    return values


def ci_table_rows(text: str) -> list:
    """Parsed CI-table rows from CLAUDE.md: (workflow, job, {os tokens}, on_pr: bool)."""
    rows = []
    for line in text.splitlines():
        match = CI_ROW.match(line)
        if not match:
            continue
        flag = match.group("on_pr").strip().lower()
        if flag not in ("yes", "no"):
            raise AssertionError(
                "CI table row for {}/{} has on-PR column {!r}; the column is a controlled "
                "vocabulary of exactly 'yes' or 'no' so the test can compare it".format(
                    match.group("workflow"), match.group("job"), match.group("on_pr").strip()
                )
            )
        rows.append(
            (
                match.group("workflow"),
                match.group("job"),
                set(OS_TOKEN.findall(match.group("runners"))),
                flag == "yes",
            )
        )
    return rows


def count_test_functions() -> tuple:
    """(functions, files) under tests/, by AST rather than by running pytest.

    An AST walk is deterministic, needs no subprocess, and cannot collide with the
    concurrently-running suites that share this working tree. It reports FEWER than
    `pytest --collect-only`, which expands each parametrised case.
    """
    files = sorted(TESTS.glob("test_*.py"))
    if not files:
        raise AssertionError("no test modules under {}".format(TESTS))
    total = 0
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                total += 1
    return total, len(files)


def cockpit_ts_paths() -> set:
    """Every TypeScript module path in cockpit/, relative to cockpit/, posix-style."""
    paths = set()
    for path in list(COCKPIT.glob("*.ts")) + list((COCKPIT / "src").rglob("*.ts")):
        paths.add(path.relative_to(COCKPIT).as_posix())
    if not paths:
        raise AssertionError("no .ts modules found under {}".format(COCKPIT))
    return paths


# --------------------------------------------------------------------------------------
# detector controls -- run before any absence claim below is believed
# --------------------------------------------------------------------------------------


@requires_source_tree
def test_readers_are_not_vacuous():
    """Each parser must find a plausible, non-zero surface on the known-good tree.

    The failure this guards against is a parser that returns an empty collection and makes
    every set-equality below trivially true. It also states the two hypotheses apart: a
    real comparison must see 16 jobs, not the 3 sub-keys of some wrong nesting level.
    """
    files = workflow_files()
    assert len(files) >= 4, "expected at least the four known workflow files, got {}".format(
        [f.name for f in files]
    )
    jobs = {f.name: workflow_jobs(f) for f in files}
    assert sum(len(v) for v in jobs.values()) >= 16, jobs
    assert "test" in jobs["ci.yml"] and "cockpit" in jobs["ci.yml"], sorted(jobs["ci.yml"])
    assert job_os_tokens(jobs["ci.yml"]["test"]) == {
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    }, "the three-OS pytest matrix is the control case for job_os_tokens"
    # The scoping control is SYNTHETIC on purpose. It used to assert that `package` was
    # Linux-only -- true when this file was written and false forty minutes later, because
    # another unit was adding Windows legs to the packaging jobs at the same time. A control
    # must not encode a fact somebody else owns; this one encodes only the parser's contract,
    # that a step-level `if: matrix.os ==` guard is NOT a runner.
    assert job_os_tokens(
        [
            "    runs-on: ubuntu-latest",
            "    steps:",
            "      - if: matrix.os == 'windows-latest'",
            "        run: echo macos-latest",
        ]
    ) == {"ubuntu-latest"}, "job_os_tokens is reading OS tokens from step bodies"
    spread = {len(job_os_tokens(block)) for block in jobs["ci.yml"].values()}
    assert spread & {1} and spread - {1}, (
        "ci.yml should hold both single-OS and multi-OS jobs; reading them all one way means "
        "the matrix scan or the runs-on scan is dead: {}".format(spread)
    )
    assert "pull_request" in workflow_triggers(WORKFLOWS / "ci.yml")
    assert "pull_request" not in workflow_triggers(WORKFLOWS / "release.yml")
    assert len(vitest_thresholds()) == 4, vitest_thresholds()
    functions, modules = count_test_functions()
    assert functions > 500 and modules > 20, (functions, modules)


@requires_source_tree
def test_ci_row_parser_fires_on_the_prose_it_replaced():
    """Both-states control for the CI table.

    The retired sentence is what a regression looks like. Fed to the row parser it must
    yield ZERO rows, so `test_claude_md_ci_table_lists_every_job` goes red on a revert
    instead of passing over prose it cannot see.
    """
    retired = (
        "- What CI runs: FOUR pre-existing jobs -- `test` (pytest, on a python x OS matrix), "
        "`js`, `js-package` and `package` -- none of which enters `cockpit/`; plus the "
        "`cockpit` job.\n"
    )
    assert ci_table_rows(retired) == []
    good = "| `ci.yml` | `test` | ubuntu-latest, windows-latest | yes |\n"
    assert ci_table_rows(good) == [
        ("ci.yml", "test", {"ubuntu-latest", "windows-latest"}, True)
    ], "the parser must actually parse a well-formed row, or its silence means nothing"


@requires_source_tree
def test_percentage_scanner_fires_on_the_retired_snapshot():
    """Both-states control for the TS-coverage assertion.

    `75.85`/`74.80` are the retired literals. They must be seen by the scanner AND rejected
    by the config comparison; if either half is silent the guard is decorative.
    """
    retired = "**75.85% statements** (1156/1524), 74.80% lines"
    seen = set(re.findall(r"(\d+\.\d+)\s*%", retired))
    assert seen == {"75.85", "74.80"}, seen
    assert not (seen & vitest_thresholds()), (
        "the retired snapshot must not coincide with a live floor, or this test cannot tell "
        "a stale number from a current one"
    )


# --------------------------------------------------------------------------------------
# D-1: the CI surface CLAUDE.md describes
# --------------------------------------------------------------------------------------


@requires_source_tree
def test_claude_md_ci_table_lists_every_workflow_file():
    rows = ci_table_rows(read_doc(CLAUDE))
    assert rows, "CLAUDE.md carries no parseable CI table"
    documented = {row[0] for row in rows}
    actual = {path.name for path in workflow_files()}
    assert documented == actual, (
        "CLAUDE.md's CI table and .github/workflows/ disagree.\n"
        "  in the tree but not the table: {}\n"
        "  in the table but not the tree: {}\n"
        "Fix the table in CLAUDE.md (section 'What CI runs').".format(
            sorted(actual - documented), sorted(documented - actual)
        )
    )


@requires_source_tree
def test_claude_md_ci_table_lists_every_job():
    rows = ci_table_rows(read_doc(CLAUDE))
    documented = {(row[0], row[1]) for row in rows}
    actual = set()
    for path in workflow_files():
        for job in workflow_jobs(path):
            actual.add((path.name, job))
    assert documented == actual, (
        "CLAUDE.md's CI table and the workflow job ids disagree.\n"
        "  jobs the tree has and the table lacks: {}\n"
        "  jobs the table has and the tree lacks: {}\n"
        "Fix the table in CLAUDE.md (section 'What CI runs').".format(
            sorted(actual - documented), sorted(documented - actual)
        )
    )


@requires_source_tree
def test_claude_md_ci_table_runner_column_matches_the_workflows():
    rows = ci_table_rows(read_doc(CLAUDE))
    assert rows, "CLAUDE.md carries no parseable CI table, so this comparison is vacuous"
    mismatches = []
    for workflow, job, documented_os, _ in rows:
        path = WORKFLOWS / workflow
        if not path.is_file():
            continue  # reported by test_claude_md_ci_table_lists_every_workflow_file
        jobs = workflow_jobs(path)
        if job not in jobs:
            continue  # reported by test_claude_md_ci_table_lists_every_job
        actual_os = job_os_tokens(jobs[job])
        if actual_os != documented_os:
            mismatches.append((workflow, job, sorted(documented_os), sorted(actual_os)))
    assert not mismatches, (
        "CLAUDE.md's runner column is stale (workflow, job, documented, actual): {}\n"
        "A packaging job that quietly becomes single-OS -- or stops being one -- is exactly "
        "the drift this column exists to show.".format(mismatches)
    )


@requires_source_tree
def test_claude_md_ci_table_on_pr_column_matches_the_triggers():
    rows = ci_table_rows(read_doc(CLAUDE))
    assert rows, "CLAUDE.md carries no parseable CI table, so this comparison is vacuous"
    mismatches = []
    for workflow, job, _, documented_on_pr in rows:
        path = WORKFLOWS / workflow
        if not path.is_file():
            continue
        actual = "pull_request" in workflow_triggers(path)
        if actual != documented_on_pr:
            mismatches.append((workflow, job, documented_on_pr, actual))
    assert not mismatches, (
        "CLAUDE.md's on-PR column disagrees with the workflow `on:` triggers "
        "(workflow, job, documented, actual): {}".format(mismatches)
    )


@requires_source_tree
def test_claude_md_states_the_job_count_it_can_be_held_to():
    text = read_doc(CLAUDE)
    stated = re.search(r"\*\*[^*]*?(\w+) workflow files? .{0,8} (\w+) jobs[^*]*\*\*", text)
    assert stated, (
        "CLAUDE.md must state the CI surface in bold as '<N> workflow files - <M> jobs' so "
        "the headline number is checkable and cannot drift back into unpinned prose"
    )
    words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10,
    }

    def value(token):
        return words.get(token.lower(), None) if not token.isdigit() else int(token)

    files = workflow_files()
    jobs = sum(len(workflow_jobs(path)) for path in files)
    assert value(stated.group(1)) == len(files), (
        "CLAUDE.md says {} workflow files; the tree has {}".format(stated.group(1), len(files))
    )
    assert value(stated.group(2)) == jobs, (
        "CLAUDE.md says {} jobs; the workflows define {}".format(stated.group(2), jobs)
    )


# --------------------------------------------------------------------------------------
# D-2: the TypeScript coverage figures
# --------------------------------------------------------------------------------------


@requires_source_tree
def test_claude_md_states_no_ts_percentage_absent_from_the_config():
    """Any coverage percentage in CLAUDE.md must be one of the live floors.

    This is the anti-snapshot rule. It does not ask the document to hold a particular
    number; it forbids it from holding a number the machine-readable SSOT does not.
    """
    text = read_doc(CLAUDE)
    section = text.split("## Coverage")[0]
    floors = vitest_thresholds()
    allowed = floors | {"100", "100.0"}
    offenders = [
        pct
        for pct in re.findall(r"(\d+\.\d+)\s*%", section)
        if pct not in allowed
    ]
    assert not offenders, (
        "CLAUDE.md states coverage percentage(s) {} that are not floors in {} ({}). Either "
        "the number is a stale snapshot -- the case on 2026-08-13, when 75.85% had been "
        "superseded by a measured 71.71% ratchet -- or the config moved and the document "
        "did not.".format(offenders, VITEST_CONFIG.name, sorted(floors))
    )


@requires_source_tree
def test_claude_md_points_at_the_ts_coverage_ssot():
    text = read_doc(CLAUDE)
    assert "cockpit/vitest.config.ts" in text, (
        "CLAUDE.md must name cockpit/vitest.config.ts as the source of truth for the cockpit "
        "coverage floors; without the pointer a reader has only this document's snapshot"
    )
    assert "coverageGate.test.ts" in text, (
        "CLAUDE.md must name the test that asserts those floors, so a reader knows the "
        "ratchet is enforced rather than aspirational"
    )


@requires_source_tree
def test_docs_name_only_typescript_modules_that_exist():
    """A module path in a document is a claim about the tree; check it."""
    known = cockpit_ts_paths()
    named = set()
    for path in (CLAUDE, SECURITY):
        named |= set(re.findall(r"`([\w./-]+\.ts)`", read_doc(path)))
    assert named, "no TypeScript module is named in either document; detector is not wired"

    def resolves(token):
        # Documents write these three ways: bare (`app.ts`), cockpit-relative
        # (`ui/workspace.ts`) and repo-relative (`cockpit/vitest.config.ts`).
        candidates = {token}
        if token.startswith("cockpit/"):
            candidates.add(token[len("cockpit/"):])
        return any(
            known_path == candidate or known_path.endswith("/" + candidate)
            for candidate in candidates
            for known_path in known
        )

    missing = sorted(token for token in named if not resolves(token))
    assert not missing, (
        "document(s) name TypeScript module(s) that do not exist: {}\n"
        "known modules: {}".format(missing, sorted(known))
    )
    assert resolves("ipc/mock.ts") and not resolves("ui/nonexistentModule.ts"), (
        "both-states control: the resolver must find a module that exists and reject one "
        "that does not, or its silence about `missing` means nothing"
    )


@requires_source_tree
def test_docs_name_only_test_modules_that_exist():
    named = set()
    for path in (CLAUDE, SECURITY):
        named |= set(re.findall(r"`?(tests/test_\w+\.py)`?", read_doc(path)))
    assert named, "no tests/ module is named in either document; detector is not wired"
    missing = sorted(token for token in named if not (REPO / token).is_file())
    assert not missing, "document(s) name test module(s) that do not exist: {}".format(missing)


# --------------------------------------------------------------------------------------
# D-3 / D-4: the counts in SECURITY.md
# --------------------------------------------------------------------------------------

# How far above the documented floor the live suite may drift before the floor is stale
# enough to be misleading. A floor is used rather than an exact literal because an exact
# literal is wrong the next working day -- 1677 recorded on 2026-08-10 was already 1,786
# collected cases three days later -- and because several agents add tests to this tree
# concurrently. 25% is wide enough to survive a release's worth of new tests and narrow
# enough to catch the failure that actually happened here: a literal left behind by an
# order of magnitude (the "101 tests" this section previously recorded).
SUITE_FLOOR_STALENESS = 1.25


@requires_source_tree
def test_security_md_suite_floor_is_a_live_floor():
    text = read_doc(SECURITY)
    stated = re.search(r"over \*\*([\d,]+) test functions\*\*", text)
    assert stated, (
        "SECURITY.md must state the suite size as 'over **<N> test functions**' -- a floor "
        "this test can hold it to, not a snapshot literal that is stale on write"
    )
    floor = int(stated.group(1).replace(",", ""))
    functions, modules = count_test_functions()
    assert functions >= floor, (
        "SECURITY.md claims over {} test functions; an AST walk of tests/ finds {} in {} "
        "modules. Either tests were deleted or the claim was never true.".format(
            floor, functions, modules
        )
    )
    assert functions <= floor * SUITE_FLOOR_STALENESS, (
        "SECURITY.md's floor of {} is stale: the suite now has {} test functions, more than "
        "{:.0%} above it. Raise the floor in SECURITY.md section 9.".format(
            floor, functions, SUITE_FLOOR_STALENESS - 1
        )
    )


@requires_source_tree
def test_security_md_does_not_carry_a_snapshot_test_count():
    text = read_doc(SECURITY)
    assert "1677 tests" not in text and "1,677 tests" not in text, (
        "the retired 2026-08-10 snapshot is back in SECURITY.md; it was already wrong by "
        "1,786 collected cases on 2026-08-13"
    )


@requires_source_tree
def test_security_md_cites_figures_the_cited_documents_still_carry():
    """A number this document attributes to another document must be findable there.

    The Claude-rail validation figure itself is NOT checkable from this repository -- the
    corpus is private -- so the composition of the 236-vs-212 difference stays UNVERIFIED
    and is settled only by the owner re-running the build and reading `_fidelity-report.json`.
    What is checkable, and what rotted, is the citation.
    """
    text = read_doc(SECURITY)
    citations = [
        (README, ("212", "207"), "the superseding Claude-rail pair"),
        (ARCHITECTURE, ("236", "231"), "the superseded Claude-rail pair"),
    ]
    for path, numbers, label in citations:
        assert path.name in text, (
            "SECURITY.md no longer cites {}; this guard was written for {}".format(path.name, label)
        )
        for number in numbers:
            assert number in text, (
                "SECURITY.md must still name {} when it describes {}".format(number, label)
            )
        lines = [
            line
            for line in read_doc(path).splitlines()
            if all(number in line for number in numbers)
        ]
        assert lines, (
            "SECURITY.md attributes {} to {}, but no single line of that file carries both "
            "numbers any more. One of the two documents was renumbered without the "
            "other.".format(" / ".join(numbers), path.name)
        )
