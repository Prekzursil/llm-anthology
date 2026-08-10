"""Gate for the D-2 format-drift canary (`.github/workflows/format-drift-canary.yml`).

WHY THIS EXISTS.

This repo carries FOUR fixture generators that turn the Python rails' real output into the
committed inputs of the JS cross-language gates:

    js/test/fixtures/gen-adapter-parity-synthetic.py -> adapter-parity-synthetic.json
    tools/gen-render-parity.py                       -> render-parity.json
    tools/gen-parity-fixture.py                      -> sanitize-parity.json
    tools/gen-adapter-parity.py                      -> adapter-parity.json (LOCAL, gitignored)

Measured 2026-08-10 with `grep -rn` over `.github/`: only the FIRST of those is regenerated
by a workflow (`ci.yml`, the `js` job). The other three run nowhere. That matters in two
different ways, and both are drift the suite cannot see:

  * A committed fixture pins what Python produced AT GENERATION TIME, and the JS test
    compares the JS rail against the FIXTURE. So if a Python renderer or sanitizer changes
    and nobody reruns the generator, the JS test still matches the stale fixture and passes.
    The two rails separate silently -- exactly the hole `ci.yml`'s regenerate-and-diff step
    closes, but only for adapter parity.
  * `tools/gen-adapter-parity.py` is the owner's instrument for the one drift class no
    offline gate can reach -- PROVIDER format drift, which needs real exports. An adapter
    refactor can break that script and nothing notices until the owner reaches for it, which
    is precisely the moment they need it. Executing it weekly (it runs corpus-free, yielding
    only its synthetic cases) keeps the instrument from rotting.

WHAT THIS FILE DOES NOT CLAIM. Nothing here proves the canary catches provider-side drift;
it cannot, because a GitHub runner has no exports. It asserts the structure of a job whose
scope is OUR-side drift plus the liveness of the owner's provider-drift tool.

WHY THE ASSERTIONS ARE TEXT-BASED. No YAML parser is a declared dependency, and the 3.9 CI
leg has no `tomllib` either -- the same reasoning as `tests/test_dependency_rails.py` and
`tests/test_release_version_sync.py`. Every reader below RAISES instead of returning a
default, and each has a test at the bottom feeding it a deliberately broken input, because a
reader that silently returns nothing turns this whole file into a vacuous green.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Present in a git checkout, absent from the sdist (pyproject.toml's sdist allowlist carries
# only llm_anthology/, tests/ and four docs). Deliberately NOT the file under test: using the
# canary itself as the marker would turn "somebody deleted the canary" into a skip.
SOURCE_TREE_MARKER = REPO / ".github" / "workflows" / "release.yml"

requires_source_tree = pytest.mark.skipif(
    not SOURCE_TREE_MARKER.is_file(),
    reason="not a source checkout ({} is absent, so this is an unpacked sdist and "
           ".github/ is legitimately missing)".format(SOURCE_TREE_MARKER),
)

WORKFLOWS = REPO / ".github" / "workflows"
CANARY = WORKFLOWS / "format-drift-canary.yml"

# The three fixtures that are COMMITTED, so a regenerate-then-diff can see drift in them.
COMMITTED_FIXTURES = (
    "js/test/fixtures/adapter-parity-synthetic.json",
    "js/test/fixtures/render-parity.json",
    "js/test/fixtures/sanitize-parity.json",
)

# The generator for each of them.
GENERATORS = (
    "js/test/fixtures/gen-adapter-parity-synthetic.py",
    "tools/gen-render-parity.py",
    "tools/gen-parity-fixture.py",
)

# The LOCAL fixture: samples the owner's real private corpora, gitignored, never packaged.
LOCAL_FIXTURE = "js/test/fixtures/adapter-parity.json"
CORPUS_TOOL = "tools/gen-adapter-parity.py"


# ------------------------------------------------------------------ readers (they RAISE)

def canary_text():
    """The canary workflow's text. RAISES if it is missing or empty."""
    assert CANARY.is_file(), "%s does not exist" % CANARY
    text = CANARY.read_text(encoding="utf-8")
    assert text.strip(), "%s is empty" % CANARY
    return text


def index_of(text, needle):
    """Position of `needle` in `text`. RAISES if absent, so an ordering assertion built on
    this cannot pass vacuously against a workflow that never mentions the thing."""
    at = text.find(needle)
    assert at >= 0, "not found in the workflow: %r" % (needle,)
    return at


def cron_expressions(text):
    """Every `- cron: "..."` schedule in a workflow. RAISES if there is none."""
    found = re.findall(r"^\s*-\s*cron:\s*[\"']([^\"']+)[\"']\s*$", text, re.MULTILINE)
    assert found, "no `- cron:` schedule found"
    return found


def without_comments(text):
    """`text` with every FULL-LINE `#` comment dropped. RAISES if that removed everything.

    Found by this file's own first red run: the ordering assertion below matched
    `git diff --exit-code` inside the PROSE explaining why the detector must precede it, so
    the check passed on a comment and would have passed on a workflow whose real diff ran
    FIRST. A commentary mention is not an executed command. Only whole-line comments are
    removed -- an inline `#` after a YAML value can be part of a quoted string.
    """
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert any(line.strip() for line in kept), "stripping comments left nothing"
    return "\n".join(kept)


# ---------------------------------------------------------------------------- the gate

@requires_source_tree
def test_the_canary_workflow_exists_and_is_not_empty():
    assert len(canary_text().splitlines()) > 20


@requires_source_tree
def test_it_is_scheduled_weekly_and_can_also_be_run_by_hand():
    text = canary_text()
    crons = cron_expressions(text)
    assert len(crons) == 1, "expected exactly one schedule, got %r" % (crons,)
    fields = crons[0].split()
    assert len(fields) == 5, "a cron expression has five fields, got %r" % (crons[0],)
    minute, hour, dom, month, dow = fields
    # WEEKLY, asserted on the shape rather than on a literal string so the day can move.
    # day-of-month and month must be wildcards and day-of-week must NOT be, or the cadence
    # is daily/monthly rather than weekly.
    assert dom == "*" and month == "*", "not a weekly cadence: %r" % (crons[0],)
    assert dow != "*", "day-of-week is a wildcard, so this runs DAILY: %r" % (crons[0],)
    assert minute != "0", (
        "minute 0 is the congested top of the hour, where GitHub delays or drops "
        "scheduled runs: %r" % (crons[0],))
    assert hour != "*", "hour is a wildcard, so this runs hourly: %r" % (crons[0],)
    # A canary nobody can trigger cannot be verified after an edit, and a scheduled-only
    # workflow is unreachable for a week.
    assert "workflow_dispatch:" in text


@requires_source_tree
def test_it_regenerates_every_committed_parity_fixture():
    """All THREE, not just the one `ci.yml` already covers -- see the module docstring."""
    text = canary_text()
    for generator in GENERATORS:
        assert generator in text, "the canary never runs %s" % generator


@requires_source_tree
def test_each_fixture_is_proven_tracked_before_its_diff_verdict_is_trusted():
    """`git diff --exit-code` on an UNTRACKED path reports no diff and exits 0.

    So on its own it degrades to a permanently green no-op the moment a fixture stops being
    committed -- which is how the gate `ci.yml` had to repair actually died (.gitignore
    already swallows the LOCAL fixture). `ls-files --error-unmatch` fails when a path is not
    tracked, so the detector is checked before its verdict is believed.
    """
    text = without_comments(canary_text())
    verdict = index_of(text, "git diff --exit-code")
    for fixture in COMMITTED_FIXTURES:
        detector = index_of(text, "git ls-files --error-unmatch " + fixture)
        assert detector < verdict, (
            "%s is diffed before it is proven tracked" % fixture)


@requires_source_tree
def test_it_runs_the_real_corpus_generator_that_no_other_workflow_runs():
    text = canary_text()
    assert CORPUS_TOOL in text
    others = [p.name for p in sorted(WORKFLOWS.glob("*.yml"))
              if p.name != CANARY.name and CORPUS_TOOL in p.read_text(encoding="utf-8")]
    assert others == [], (
        "%s is now also referenced by %r -- update this test deliberately rather than "
        "letting two workflows drift" % (CORPUS_TOOL, others))


@requires_source_tree
def test_the_provider_drift_step_refuses_to_run_where_a_real_corpus_exists():
    """MEASURED, not hypothetical: found by dry-running this job on the owner's machine.

    `tools/gen-adapter-parity.py` SAMPLES the corpus at its hardcoded `CORPUS` path when that
    directory exists. The dry-run wrote a 27,249,819-byte fixture holding 42 real Claude
    conversations, and the synthetic-only assertion below caught it -- but only AFTER the
    write. On a GitHub runner that path cannot exist; on a self-hosted runner, or a human
    running these steps by hand, it can.

    So the step must refuse BEFORE invoking the tool, reading the path out of the tool itself
    so the guard cannot drift from what is actually sampled, and must delete the output
    afterwards so no later step can upload it.
    """
    text = without_comments(canary_text())
    run = index_of(text, "python " + CORPUS_TOOL)
    guard = index_of(text, "no CORPUS constant to guard on")
    assert guard < run, "the corpus guard runs after the tool it is meant to gate"
    assert "os.path.isdir(" in text, "the guard never tests whether the corpus is present"
    cleanup = index_of(text, "rm -f " + LOCAL_FIXTURE)
    assert cleanup > run, "the step leaves its output on disk"


@requires_source_tree
def test_the_local_real_corpus_fixture_must_stay_untracked():
    """The privacy invariant, mechanised.

    `tools/gen-adapter-parity.py` samples REAL conversation content when the corpus is
    present. Its output is gitignored, but a `git add -f` would defeat that silently, so the
    canary asserts the file is NOT tracked and turns red if it ever becomes so.
    """
    text = canary_text()
    assert "git ls-files --error-unmatch " + LOCAL_FIXTURE in text
    # Negated: the assertion must be that the path is ABSENT from the index.
    assert re.search(
        r"if\s+git ls-files --error-unmatch " + re.escape(LOCAL_FIXTURE), text), (
        "the untracked-ness of %s is not asserted as a failure condition" % LOCAL_FIXTURE)


@requires_source_tree
def test_it_carries_a_negative_control_that_proves_the_gate_can_fail():
    """A scheduled job that can only ever be green is a no-op.

    The control mutates one IR value in the committed fixture and requires BOTH detectors --
    the staleness diff AND the parity suite that consumes the fixture -- to report it, then
    restores the file. Asserted here so the control cannot be quietly deleted, leaving a
    canary whose fail-capability is once again only a claim.
    """
    text = without_comments(canary_text())
    control = index_of(text, "negative control")
    assert "MUTANT-" in text, "nothing in the canary mutates the fixture"
    assert "FAILED:negative-control" in text, (
        "the control does not fail loudly when a detector stays silent")
    assert "git diff --quiet" in text, "the control never checks the staleness detector"
    assert index_of(text, "npx vitest run test/adapter-parity.test.ts") > control, (
        "the control never checks that the CONSUMING suite goes red")
    assert index_of(text, "git checkout -- " + COMMITTED_FIXTURES[0]) > control, (
        "the control mutates the fixture and never restores it")


@requires_source_tree
def test_no_step_in_the_canary_is_allowed_to_fail_silently():
    """`continue-on-error` produces the red-X-on-a-green-run state `ci.yml`'s cockpit job
    comment already rejects, and on a canary it is worse: nobody is watching a weekly run,
    so a non-blocking failure is a failure nobody ever sees."""
    text = canary_text()
    assert "continue-on-error" not in text


@requires_source_tree
def test_the_canary_never_names_the_owners_private_corpus():
    """`tools/gen-adapter-parity.py` hardcodes a local corpus path. The workflow must not
    reproduce it: on a runner it cannot exist, and in a public repo it is a disclosure."""
    text = canary_text()
    for leak in ("AIs Conversations", "Prekzursil", "C:\\Users", "/home/"):
        assert leak not in text, "the canary names a local path: %r" % (leak,)


@requires_source_tree
def test_the_canary_asks_for_no_more_permission_than_it_needs():
    text = canary_text()
    at = index_of(text, "permissions:")
    assert "contents: read" in text[at:at + 120]
    for write in ("contents: write", "pull-requests: write", "id-token: write"):
        assert write not in text, "the canary requests %r" % (write,)


@requires_source_tree
def test_the_canary_floats_the_interpreter_the_per_push_job_pins():
    """This is what makes the canary more than a duplicate of `ci.yml`'s `js` job.

    That job pins `python-version: "3.13"`, so a serialisation change arriving with a NEW
    interpreter cannot turn it red. The canary regenerates on the floating latest, which is
    a drift signal no per-push run produces -- and it produces it weekly, ahead of a release
    rather than during one.
    """
    text = canary_text()
    assert re.search(r'python-version:\s*"3\.x"', text), (
        "the canary pins its interpreter, so it cannot see toolchain-side drift")


# -------------------------------------------------------- the readers' own failure modes

def test_index_of_raises_when_the_needle_is_absent():
    with pytest.raises(AssertionError, match="not found in the workflow"):
        index_of("name: CI\n", "git diff --exit-code")


def test_index_of_finds_a_needle_at_the_start():
    assert index_of("abc", "a") == 0


def test_cron_expressions_raises_when_there_is_no_schedule():
    with pytest.raises(AssertionError, match="no `- cron:` schedule found"):
        cron_expressions("on:\n  workflow_dispatch:\n")


def test_cron_expressions_reads_every_schedule_and_both_quote_styles():
    doc = "on:\n  schedule:\n    - cron: \"17 6 * * 1\"\n    - cron: '0 0 1 * *'\n"
    assert cron_expressions(doc) == ["17 6 * * 1", "0 0 1 * *"]


def test_without_comments_drops_a_whole_line_comment_and_keeps_an_inline_hash():
    doc = "  # git diff --exit-code (prose)\n  run: echo 'a # b'\n"
    assert without_comments(doc) == "  run: echo 'a # b'"


def test_without_comments_raises_when_everything_was_a_comment():
    with pytest.raises(AssertionError, match="stripping comments left nothing"):
        without_comments("# all\n  # of it\n")


def test_cron_expressions_ignores_a_commented_out_schedule():
    """A `#`-prefixed cron is documentation, not a trigger; counting it would let a
    disabled schedule satisfy the weekly assertion above."""
    assert cron_expressions(
        "  schedule:\n    # - cron: \"0 0 * * *\"\n    - cron: \"17 6 * * 1\"\n"
    ) == ["17 6 * * 1"]
