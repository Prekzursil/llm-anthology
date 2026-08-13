"""Structural gate for the CI job surface (`.github/workflows/ci.yml`).

WHY THIS EXISTS.

`ci.yml` is the only thing standing between a green badge and a shipped defect, and its job
list is load-bearing knowledge that lives nowhere machine-checkable. Measured 2026-08-12 by
parsing the file (PyYAML, then a second independent text pass) rather than reading it:

    test        line  13   ubuntu 3.9 / ubuntu 3.13 / windows 3.13 / macos 3.13
    js          line  50   ubuntu, node 20 + 22, working-directory: js
    js-package  line 114   ubuntu
    package     line 140   ubuntu
    cockpit     line 171   ubuntu + windows, working-directory: cockpit

and -- with full-line comments stripped, so a prose mention does not count as a step -- the
first FOUR contain no `cockpit` or `src-tauri` token at all. Every gate that compiles, lints,
type-checks, vitests or cargo-tests the code the desktop release actually ships is therefore
carried by ONE job. If `cockpit` were dropped, renamed, or given `continue-on-error`, the
remaining four would still go green and nothing in the repository would notice.

That is the failure class this file pins: not "is the YAML valid" (Actions answers that
loudly) but "does the job surface still gate what we believe it gates".

WHAT THE PACKAGING ASSERTIONS ARE FOR.

The `test` job argues, in its own comment, that "a Windows-only green run can hide a Linux/CI
gap", and matrixes three OSes on that reasoning. The two jobs that install and drive the
artifacts actually published to the world -- the PyPI wheel and the npm tarball -- ran on
Linux only, so the same argument applied in reverse: a Windows-only packaging or path break
in either would have shipped green. The legs asserted below close that.

WHY THE ASSERTIONS ARE TEXT-BASED. No YAML parser is a declared dependency (`pyproject.toml`
dev extras are pytest + pytest-cov and nothing else), and the 3.9 CI leg has no `tomllib`
either -- the same reasoning as `tests/test_dependency_rails.py`,
`tests/test_release_version_sync.py` and `tests/test_format_drift_canary.py`. Every reader
below RAISES instead of returning a default, and each has a negative control at the bottom
feeding it a deliberately broken input, because a reader that silently returns nothing turns
this whole file into a vacuous green.

The job list is also read TWICE by mechanically different means -- the `  <name>:` keys under
`jobs:`, and the count of `runs-on:` lines -- and the two are asserted equal. A parser that
finds nothing is a broken detector, not a verdict.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Present in a git checkout, absent from the sdist (pyproject.toml's sdist allowlist carries
# only llm_anthology/, tests/ and four docs). Deliberately NOT the file under test: using
# ci.yml as its own marker would turn "somebody deleted CI" into a skip.
SOURCE_TREE_MARKER = REPO / ".github" / "workflows" / "release.yml"

requires_source_tree = pytest.mark.skipif(
    not SOURCE_TREE_MARKER.is_file(),
    reason="not a source checkout ({} is absent, so this is an unpacked sdist and "
           ".github/ is legitimately missing)".format(SOURCE_TREE_MARKER),
)

CI = REPO / ".github" / "workflows" / "ci.yml"

# The pinned surface. Changing this set is a DELIBERATE act: a job removed here silently is
# a gate removed silently.
PINNED_JOBS = ("cockpit", "js", "js-package", "package", "test")

# The one job that enters the desktop app's trees.
COCKPIT_JOB = "cockpit"
COCKPIT_TREES = ("cockpit", "src-tauri")

# The jobs that install a PUBLISHED artifact into a clean environment and drive it.
PACKAGING_JOBS = ("js-package", "package")

# Every packaging job must smoke-test on at least these. `test` additionally covers macos;
# the packaging legs deliberately do not, because macos publishes no artifact of its own and
# the wheel is `py3-none-any`.
PACKAGING_PLATFORMS = ("ubuntu-latest", "windows-latest")

# The postcondition each packaging leg must assert on what it produced. A smoke job that
# installs an artifact and never looks at its output proves only that nothing threw.
POSTCONDITION = "<!doctype html"


# ------------------------------------------------------------------ readers (they RAISE)

def ci_text():
    """`ci.yml` as text. RAISES if it is missing or empty."""
    assert CI.is_file(), "%s does not exist" % CI
    text = CI.read_text(encoding="utf-8")
    assert text.strip(), "%s is empty" % CI
    return text


def without_comments(text):
    """`text` with every FULL-LINE `#` comment dropped. RAISES if that removed everything.

    Load-bearing here, not hygiene: `ci.yml`'s longest comment is the cockpit job's own
    rationale, which spells out "Neither ever entered cockpit/ or cockpit/src-tauri/". A
    token scan that counted prose would read that sentence as four jobs entering the tree
    and invert the very fact this file pins. Only whole-line comments are removed -- an
    inline `#` after a YAML value can be part of a quoted string.
    """
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert any(line.strip() for line in kept), "stripping comments left nothing"
    return "\n".join(kept)


def job_ids(text):
    """Every `  <name>:` key under the top-level `jobs:`, in file order. RAISES if none.

    Scoped to the `jobs:` block on purpose: `on:` and `permissions:` also carry two-space
    keys (`push:`, `pull_request:`, `contents:`), and a reader that swept the whole file
    would report them as jobs.
    """
    found = []
    in_jobs = False
    for line in without_comments(text).splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if in_jobs and re.match(r"^\S", line):
            in_jobs = False
        match = re.match(r"^  ([A-Za-z_][A-Za-z0-9_-]*):\s*$", line)
        if in_jobs and match:
            found.append(match.group(1))
    assert found, "no jobs found under a top-level `jobs:` in %s" % CI
    return found


def runs_on_count(text):
    """How many `runs-on:` lines the file has. RAISES at zero.

    The SECOND, mechanically different detector for the job count. Agreement between the two
    is what licenses believing either -- a suspiciously plausible number from one parser is
    the shape a collection-level mistake takes.
    """
    found = re.findall(r"^\s+runs-on:", without_comments(text), re.MULTILINE)
    assert found, "no `runs-on:` in %s, so the job reader cannot be corroborated" % CI
    return len(found)


def job_block(text, job_id):
    """The body of one job: its declaration line through the line before the next job.

    RAISES if the job is absent, so an assertion built on this cannot pass vacuously against
    a workflow that no longer has the job at all.
    """
    stripped = without_comments(text)
    lines = stripped.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^  %s:\s*$" % re.escape(job_id), line):
            start = index
            break
    assert start is not None, "job %r is not in %s" % (job_id, CI)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^  [A-Za-z_][A-Za-z0-9_-]*:\s*$", lines[index]):
            end = index
            break
    body = "\n".join(lines[start:end])
    assert body.strip(), "job %r has an empty body" % job_id
    return body


def matrix_platforms(block):
    """Every OS this job's matrix names. RAISES if the job declares none.

    Handles both shapes `ci.yml` uses: a block list (`os: [ubuntu-latest, windows-latest]`)
    and the flow mappings inside `test`'s `include:` (`- { os: macos-latest, ... }`).
    """
    found = set()
    for value in re.findall(r"^\s*os:\s*(.+?)\s*$", block, re.MULTILINE):
        for item in value.strip("[]").split(","):
            if item.strip():
                found.add(item.strip().strip("\"'"))
    for value in re.findall(r"\{[^}]*\bos:\s*([A-Za-z0-9._-]+)", block):
        found.add(value)
    assert found, "no `os:` entry in this job, so its platform coverage is unknown"
    return found


def entered_trees(block):
    """The `cockpit` / `src-tauri` path tokens a job's STEPS reference (comments excluded)."""
    return {token for tree in COCKPIT_TREES
            for token in re.findall(r"%s[\w./-]*" % re.escape(tree), block)}


# ------------------------------------------------------------------- the pinned surface

@requires_source_tree
def test_the_workflow_exists_and_is_not_a_stub():
    assert len(ci_text().splitlines()) > 100


@requires_source_tree
def test_the_two_independent_job_detectors_agree():
    """Detector control, run BEFORE any verdict below is believed.

    If the key reader and the `runs-on:` counter disagree, one of them is parsing the wrong
    collection and every count in this file is meaningless.
    """
    text = ci_text()
    ids = job_ids(text)
    assert len(ids) == runs_on_count(text), (
        "the `  <name>:` reader found %d jobs %r but there are %d `runs-on:` lines -- one of "
        "the two parsers is wrong, so no assertion in this file can be trusted"
        % (len(ids), ids, runs_on_count(text)))


@requires_source_tree
def test_the_job_surface_is_exactly_the_pinned_set():
    """A job cannot be dropped, renamed or added without this test saying so out loud."""
    found = job_ids(ci_text())
    assert sorted(found) == sorted(PINNED_JOBS), (
        "ci.yml now defines %r, pinned is %r. If this change is intended, update PINNED_JOBS "
        "deliberately -- a gate that disappears quietly is the failure this test exists for."
        % (sorted(found), sorted(PINNED_JOBS)))


@requires_source_tree
def test_no_job_and_no_step_is_allowed_to_be_non_blocking():
    """`continue-on-error` produces the red-X-on-a-green-run state that trains reviewers to
    discount the entire check surface -- the cockpit job's own comment says so. A gate
    permitted to fail is not a gate."""
    text = without_comments(ci_text())
    assert "continue-on-error" not in text, (
        "some job or step in ci.yml is now non-blocking; `fail-fast: false` is the setting "
        "that keeps one red leg from cancelling its siblings, and it is already in use")


# ------------------------------------------------------- the cockpit tree has ONE gate

@requires_source_tree
def test_exactly_one_job_enters_the_cockpit_and_src_tauri_trees():
    """Measured, and pinned because it is counter-intuitive: neither `test` (pytest at the
    repo root) nor `js` (the SEPARATE TypeScript port, working-directory: js) nor either
    packaging job touches the desktop app. Anyone reading "CI is green" as "the cockpit was
    checked" is relying entirely on the one job named below."""
    text = ci_text()
    enterers = sorted(job for job in job_ids(text) if entered_trees(job_block(text, job)))
    assert enterers == [COCKPIT_JOB], (
        "jobs entering %r are %r, expected exactly [%r]" % (
            list(COCKPIT_TREES), enterers, COCKPIT_JOB))


@requires_source_tree
def test_the_cockpit_job_still_reaches_both_of_its_trees():
    """The converse of the test above, which would also pass if the cockpit job kept its name
    and stopped doing anything."""
    block = job_block(ci_text(), COCKPIT_JOB)
    for tree in COCKPIT_TREES:
        assert any(token.startswith(tree) for token in entered_trees(block)), (
            "the %s job no longer references %s" % (COCKPIT_JOB, tree))


@requires_source_tree
def test_the_cockpit_job_runs_on_both_platforms():
    """A Windows-only green run is the exact shape that hides a Linux gap: `sidecar.rs`
    carries cfg(windows)/cfg(not(windows)) arms that only the Linux leg ever compiles."""
    found = matrix_platforms(job_block(ci_text(), COCKPIT_JOB))
    for platform in ("ubuntu-latest", "windows-latest"):
        assert platform in found, (
            "the %s job no longer runs on %s (matrix: %r)" % (COCKPIT_JOB, platform, found))


# ------------------------------------------- the published artifacts are driven on both

@requires_source_tree
@pytest.mark.parametrize("job", PACKAGING_JOBS)
def test_every_published_artifact_is_installed_and_driven_on_every_platform(job):
    """`package` publishes the PyPI wheel and `js-package` the npm tarball. Both used to
    install and drive on ubuntu only, while the source suite matrixed three OSes on the
    stated grounds that one platform's green hides another's gap -- so a Windows packaging
    or path break in either artifact would ship green."""
    found = matrix_platforms(job_block(ci_text(), job))
    for platform in PACKAGING_PLATFORMS:
        assert platform in found, (
            "the %s job does not smoke-test on %s (matrix: %r)" % (job, platform, found))


@requires_source_tree
@pytest.mark.parametrize("job", PACKAGING_JOBS)
def test_every_packaging_leg_asserts_a_postcondition_on_what_it_produced(job):
    """One postcondition per OS leg. A smoke job that installs an artifact, runs it, and
    never inspects the output proves only that nothing threw -- which a CLI that writes a
    zero-byte file also satisfies."""
    block = job_block(ci_text(), job)
    legs = len(matrix_platforms(block))
    checks = len(re.findall(re.escape(POSTCONDITION), block, re.IGNORECASE))
    assert checks >= legs, (
        "the %s job runs %d OS legs but asserts %r only %d time(s); each leg needs its own "
        "postcondition, and the POSIX and Windows spellings cannot be shared"
        % (job, legs, POSTCONDITION, checks))


# ---------------------------------------------------------- negative controls (READERS)
#
# Each reader is fed a deliberately broken input and must RAISE. Without these, a reader
# that quietly returns an empty result would make every assertion above pass vacuously --
# the both-states rule: a probe that is silent in the broken state measures nothing.

def test_without_comments_raises_when_nothing_survives():
    with pytest.raises(AssertionError):
        without_comments("# every\n  # line\n# is a comment\n")


def test_job_ids_raises_on_a_workflow_with_no_jobs_block():
    with pytest.raises(AssertionError):
        job_ids("name: CI\non:\n  push:\n    branches: [main]\n")


def test_job_ids_raises_on_an_empty_jobs_block():
    with pytest.raises(AssertionError):
        job_ids("jobs:\n")


def test_job_ids_does_not_mistake_trigger_keys_for_jobs():
    """The control that proves the reader is scoped: `push:` and `contents:` are two-space
    keys too, and a whole-file sweep would report them as gates."""
    assert job_ids("on:\n  push:\n  pull_request:\n"
                   "permissions:\n  contents: read\n"
                   "jobs:\n  only-one:\n    runs-on: x\n") == ["only-one"]


def test_runs_on_count_raises_when_there_are_none():
    with pytest.raises(AssertionError):
        runs_on_count("jobs:\n  a:\n    steps: []\n")


def test_job_block_raises_for_a_job_that_is_not_there():
    with pytest.raises(AssertionError):
        job_block("jobs:\n  a:\n    runs-on: x\n", "b")


def test_job_block_stops_at_the_next_job():
    """A block reader that ran to EOF would attribute the cockpit job's steps to whichever
    job precedes it, and `test_exactly_one_job_enters_the_cockpit_and_src_tauri_trees` would
    then report two enterers -- or, read the other way round, could hide a real one."""
    text = "jobs:\n  a:\n    runs-on: x\n    steps: [one]\n  b:\n    runs-on: y\n"
    assert "one" in job_block(text, "a")
    assert "runs-on: y" not in job_block(text, "a")


def test_matrix_platforms_raises_when_the_job_names_no_os():
    with pytest.raises(AssertionError):
        matrix_platforms("  a:\n    runs-on: ubuntu-latest\n")


def test_matrix_platforms_reads_both_the_list_and_the_include_mapping_shapes():
    assert matrix_platforms("        os: [ubuntu-latest, windows-latest]\n") == {
        "ubuntu-latest", "windows-latest"}
    assert matrix_platforms('          - { os: macos-latest, python: "3.13" }\n') == {
        "macos-latest"}


def test_entered_trees_ignores_a_bare_mention_that_is_not_a_path():
    """`entered_trees` is a token scan, so it is only meaningful once comments are gone --
    which `job_block` guarantees. This pins the tokenisation itself."""
    assert entered_trees("      - run: npx tsc\n") == set()
    assert entered_trees("    working-directory: cockpit\n") == {"cockpit"}


# ------------------------------------------------- negative controls (THE PIN ITSELF)
#
# The readers above are proven fail-capable by broken INPUTS. That is not the same as
# proving the three assertions that pass on today's file -- the job set, the non-blocking
# scan and the single-enterer verdict -- would notice the regressions they exist to catch.
# Each was green before this file was written, so each is exactly the shape that can be a
# no-op forever without anyone finding out. Below, the REAL workflow text is mutated into
# the regression and the verdict is required to flip. All READ-ONLY: `ci.yml` is never
# written, both arms are compared in memory.

def _drop_job(text, job_id):
    """`text` with one job's block removed. RAISES if the mutation changed nothing."""
    stripped = without_comments(text)
    mutated = stripped.replace(job_block(text, job_id), "", 1)
    assert len(mutated) < len(stripped), "dropping %r removed nothing" % job_id
    return mutated


def _insert_into_job(text, job_id, line):
    """`text` with `line` spliced into the top of a job's body. RAISES if it did not apply."""
    stripped = without_comments(text)
    anchor = "  %s:\n" % job_id
    assert stripped.count(anchor) == 1, (
        "%r is not a unique job anchor, so this mutation is not aimed at what it claims"
        % anchor)
    return stripped.replace(anchor, anchor + line, 1)


@requires_source_tree
def test_the_surface_pin_would_notice_a_job_that_quietly_disappeared():
    """The regression this file was written for. Without this control, the pin could be
    comparing two constants and nobody would know."""
    mutated = _drop_job(ci_text(), COCKPIT_JOB)
    assert COCKPIT_JOB not in job_ids(mutated)
    assert sorted(job_ids(mutated)) != sorted(PINNED_JOBS)


@requires_source_tree
def test_the_single_enterer_verdict_would_notice_a_second_job_reaching_the_tree():
    """The verdict must be discriminating in BOTH directions: it already reports one
    enterer, and here a second job is given a cockpit path and must be seen."""
    mutated = _insert_into_job(ci_text(), "js", "    working-directory: cockpit/src-tauri\n")
    enterers = sorted(job for job in job_ids(mutated)
                      if entered_trees(job_block(mutated, job)))
    assert enterers == [COCKPIT_JOB, "js"], (
        "a job given a cockpit path went unnoticed; enterers read %r" % (enterers,))


@requires_source_tree
def test_the_non_blocking_scan_is_not_fooled_by_the_prose_that_argues_against_it():
    """The sharpest of the three, and not a tautology: `ci.yml` genuinely CONTAINS the
    string `continue-on-error` today -- inside the cockpit job's comment explaining why a
    gate must never use it. A scan over the raw file would therefore fail permanently on
    the repository's own correct reasoning, and the obvious "fix" for that (dropping the
    assertion, or matching a narrower pattern) is how this check would quietly stop
    protecting anything. Both arms are required: present in the raw text, absent once
    comments are gone."""
    raw = ci_text()
    assert "continue-on-error" in raw, (
        "the comment this control is calibrated against is gone; re-derive whether the "
        "comment-stripping in test_no_job_and_no_step_is_allowed_to_be_non_blocking is "
        "still load-bearing before trusting that test")
    assert "continue-on-error" not in without_comments(raw)
    mutated = _insert_into_job(raw, COCKPIT_JOB, "    continue-on-error: true\n")
    assert "continue-on-error" in without_comments(mutated), (
        "a real non-blocking key survived comment-stripping undetected")
