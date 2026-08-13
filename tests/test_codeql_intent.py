"""Gate for the declared gating intent of `.github/workflows/codeql.yml`.

WHY THIS EXISTS.

Every other gate in this repo argues its blocking posture out loud -- `ci.yml:187-190` spends
four lines on why the `cockpit` job is "Deliberately BLOCKING on both legs". `codeql.yml` said
nothing at all, so a reader could not tell whether a CodeQL finding was a merge gate or a note,
and neither reading was written down anywhere.

MEASURED 2026-08-13 against the live repo, read-only, four mechanically different probes:

    gh api repos/Prekzursil/llm-anthology/branches/main/protection  -> 404 "Branch not protected"
    gh api repos/Prekzursil/llm-anthology/branches/main --jq .protected            -> false
    gh api repos/Prekzursil/llm-anthology/rulesets                                 -> []
    gh api repos/Prekzursil/llm-anthology/rules/branches/main                      -> []

Detector control for the 404: `gh api repos/Prekzursil/llm-anthology --jq .permissions.admin`
-> `true`, so the token CAN read protection and the 404 means "no protection", not "no access".
A 404 from a non-admin token would have been an auth failure reported as a verdict.

Empirical confirmation that nothing gates on the alerts, from a fifth, independent direction:
34 code-scanning alerts were OPEN (7 error / 22 warning / 5 note by rule severity; 6 of them
`high` security-severity) while the three most recent `codeql.yml` runs all concluded
`success`. Alerts present AND the check green is the definition of advisory.

So the settled answer is ADVISORY, and this file exists to stop the declaration and the
machinery from drifting apart. It asserts a two-way invariant: the workflow declares exactly
one intent, and that declaration MATCHES what the workflow actually does. Declare ADVISORY
while carrying a `fail-on:` threshold and this goes red; declare BLOCKING without one and it
goes red too. `test_the_consistency_check_fires_in_the_broken_state` proves both directions on
mutated text, because a checker that has never been seen red is indistinguishable from a no-op.

WHY THE ASSERTIONS ARE TEXT-BASED. No YAML parser is a declared dependency, and the 3.9 CI leg
has no `tomllib` either -- the same reasoning as `tests/test_format_drift_canary.py` and
`tests/test_dependency_rails.py`. Every reader below RAISES instead of returning a default, and
each has a negative test feeding it deliberately broken input, because a reader that silently
returns nothing turns this whole file into a vacuous green.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Present in a git checkout, absent from the sdist (pyproject.toml's sdist allowlist carries
# only llm_anthology/, tests/ and four docs). Deliberately NOT the file under test: using
# codeql.yml itself as the marker would turn "somebody deleted the workflow" into a skip.
SOURCE_TREE_MARKER = REPO / ".github" / "workflows" / "release.yml"

requires_source_tree = pytest.mark.skipif(
    not SOURCE_TREE_MARKER.is_file(),
    reason="not a source checkout ({} is absent, so this is an unpacked sdist and "
           ".github/ is legitimately missing)".format(SOURCE_TREE_MARKER),
)

WORKFLOWS = REPO / ".github" / "workflows"
CODEQL = WORKFLOWS / "codeql.yml"

RECOGNISED_INTENTS = ("ADVISORY", "BLOCKING")


# ------------------------------------------------------------------ readers (they RAISE)

def codeql_text():
    """The CodeQL workflow's text. RAISES if it is missing or empty."""
    assert CODEQL.is_file(), "%s does not exist" % CODEQL
    text = CODEQL.read_text(encoding="utf-8")
    assert text.strip(), "%s is empty" % CODEQL
    return text


def without_comments(text):
    """`text` with every FULL-LINE `#` comment dropped. RAISES if that removed everything.

    The intent block below is a long comment that necessarily quotes the very tokens the
    executable assertions look for (`fail-on`, `continue-on-error`). Matching prose instead of
    machinery is the failure mode `test_format_drift_canary.py:103-114` was written to close;
    the same guard applies here. Only whole-line comments are removed -- an inline `#` after a
    YAML value can be part of a quoted string.
    """
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert any(line.strip() for line in kept), "stripping comments left nothing"
    return "\n".join(kept)


def marker_values(text, marker):
    """Every `# <marker>: value` line's value, in file order. RAISES if there is none."""
    found = re.findall(
        r"^\s*#\s*%s:\s*(\S.*?)\s*$" % re.escape(marker), text, re.MULTILINE
    )
    assert found, "no `# %s:` line in the workflow" % marker
    return found


def gating_intent(text):
    """The single declared gating intent. RAISES unless exactly one recognised one is declared."""
    found = marker_values(text, "GATING-INTENT")
    assert len(found) == 1, (
        "expected exactly one `# GATING-INTENT:` declaration, got %r -- two declarations "
        "means the file states two intents and a reader cannot tell which holds" % (found,)
    )
    intent = found[0]
    assert intent in RECOGNISED_INTENTS, (
        "unrecognised gating intent %r; expected one of %r" % (intent, list(RECOGNISED_INTENTS))
    )
    return intent


def analyze_step_block(text):
    """The executable lines of the `codeql-action/analyze` step. RAISES if the step is absent.

    Comment lines are stripped FIRST, so the prose explaining `fail-on:` cannot be mistaken for
    a configured `fail-on:`.
    """
    lines = without_comments(text).splitlines()
    at = None
    for i, line in enumerate(lines):
        if "github/codeql-action/analyze@" in line:
            at = i
            break
    assert at is not None, "no `github/codeql-action/analyze@` step in the workflow"
    indent = len(lines[at]) - len(lines[at].lstrip())
    block = [lines[at]]
    for line in lines[at + 1:]:
        if not line.strip():
            block.append(line)
            continue
        here = len(line) - len(line.lstrip())
        if here < indent:
            break
        if here == indent and line.lstrip().startswith("- "):
            break
        block.append(line)
    return "\n".join(block)


def fail_on_value(text):
    """The `analyze` step's `fail-on:` threshold, or None when it declares no threshold."""
    found = re.search(r"^\s*fail-on:\s*(\S+)\s*$", analyze_step_block(text), re.MULTILINE)
    return found.group(1) if found else None


def matrix_languages(text):
    """The CodeQL language matrix. RAISES if there is no `language: [...]` list."""
    found = re.search(r"^\s*language:\s*\[(.+?)\]\s*$", text, re.MULTILINE)
    assert found, "no `language: [...]` matrix in the workflow"
    langs = [item.strip().strip("\"'") for item in found.group(1).split(",")]
    langs = [lang for lang in langs if lang]
    assert langs, "the `language:` matrix is empty"
    return langs


def job_name_template(text):
    """The job `name:` that expands over the language matrix. RAISES unless exactly one exists."""
    found = re.findall(
        r"^\s*name:\s*(\S.*\$\{\{\s*matrix\.language\s*\}\}.*?)\s*$", text, re.MULTILINE
    )
    assert len(found) == 1, (
        "expected exactly one `name:` referencing matrix.language, got %r -- the check-run "
        "context names cannot be derived unambiguously otherwise" % (found,)
    )
    return found[0].strip("\"'")


def derived_check_contexts(text):
    """The check-run context names this workflow actually produces, derived from the matrix."""
    template = job_name_template(text)
    return [
        re.sub(r"\$\{\{\s*matrix\.language\s*\}\}", lang, template)
        for lang in matrix_languages(text)
    ]


def intent_violations(text):
    """Every way the DECLARED intent contradicts the machinery. Empty list == consistent."""
    intent = gating_intent(text)
    threshold = fail_on_value(text)
    violations = []
    if intent == "ADVISORY" and threshold is not None:
        violations.append(
            "declared ADVISORY but the analyze step carries `fail-on: %s`, which fails the "
            "run on a finding -- that is a BLOCKING workflow" % threshold
        )
    if intent == "BLOCKING" and threshold is None:
        violations.append(
            "declared BLOCKING but the analyze step declares no `fail-on:` threshold, so a "
            "new finding leaves the check green"
        )
    return violations


# ---------------------------------------------------------------------------- the gate

@requires_source_tree
def test_the_codeql_workflow_exists_and_is_not_empty():
    assert len(codeql_text().splitlines()) > 20


@requires_source_tree
def test_it_declares_exactly_one_machine_readable_gating_intent():
    assert gating_intent(codeql_text()) in RECOGNISED_INTENTS


@requires_source_tree
def test_the_declared_intent_matches_the_machinery():
    violations = intent_violations(codeql_text())
    assert violations == [], "codeql.yml states one thing and does another: %s" % violations


@requires_source_tree
def test_no_step_is_permitted_to_fail_silently():
    # ci.yml:188-190: "continue-on-error produces the red-X-on-a-green-run state that trains
    # reviewers to discount the whole check surface". On an ADVISORY workflow it is worse than
    # useless -- there is no run to rescue, only a masked step.
    body = without_comments(codeql_text())
    assert "continue-on-error" not in body, (
        "a `continue-on-error` step would mask a CodeQL upload failure as a green run"
    )


@requires_source_tree
def test_the_documented_check_contexts_are_the_ones_the_matrix_produces():
    # THE RENAME TRAP, pinned. A required check is matched by its context STRING; a matrix
    # change or a job rename silently changes that string, and a required context that no job
    # ever produces sits pending forever. Deriving the list from the matrix means the
    # documentation cannot rot past this test.
    text = codeql_text()
    documented = sorted(marker_values(text, "CHECK-CONTEXT"))
    derived = sorted(derived_check_contexts(text))
    assert documented == derived, (
        "the documented check contexts %r are not the ones this workflow produces %r"
        % (documented, derived)
    )


@requires_source_tree
def test_it_warns_that_the_bare_codeql_context_is_not_one_of_them():
    # Measured on the live repo: the check runs on the tip of main are named `analyze (python)`
    # and `analyze (javascript-typescript)`. There is NO check named `CodeQL` and none named
    # `codeql / CodeQL`, so requiring either string would block every PR forever on a context
    # that never arrives. The trap has to be written next to the fix.
    trap = " ".join(marker_values(codeql_text(), "CONTEXT-TRAP"))
    assert "CodeQL" in trap, "the CONTEXT-TRAP note must name the bare `CodeQL` string it warns about"


@requires_source_tree
def test_it_names_the_default_setup_conflict():
    # Advanced setup (this file) and code-scanning default setup cannot both own a language;
    # enabling default setup disables the advanced workflow's uploads for it. Measured
    # 2026-08-13: `gh api .../code-scanning/default-setup` -> state "not-configured", so the
    # conflict is latent here, not active -- which is exactly when it gets tripped by accident.
    note = " ".join(marker_values(codeql_text(), "SETUP-CONFLICT"))
    assert "default setup" in note.lower()


@requires_source_tree
def test_it_names_the_probes_that_re_settle_the_intent():
    # A settled question needs its settling experiment recorded, or the next reader infers
    # instead of measuring -- which is how this finding was filed as UNVERIFIED in the first
    # place. Naming the endpoints makes re-measurement cheaper than guessing.
    probes = " ".join(marker_values(codeql_text(), "SETTLING-PROBE"))
    for endpoint in ("branches/main/protection", "rulesets", "code-scanning/default-setup"):
        assert endpoint in probes, "the settling probes must name the %r endpoint" % endpoint


@requires_source_tree
def test_the_settlement_is_dated():
    # Not a freshness assertion -- that would rot by design. Only that a reader can tell HOW
    # OLD the measurement behind the declared intent is.
    stamps = marker_values(codeql_text(), "MEASURED-ON")
    assert len(stamps) == 1, "expected exactly one `# MEASURED-ON:` stamp, got %r" % (stamps,)
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", stamps[0]), (
        "`# MEASURED-ON:` must be an ISO date, got %r" % (stamps[0],)
    )


@requires_source_tree
def test_it_still_runs_on_pull_requests_and_on_a_schedule():
    # Advisory is not the same as absent. The value of an advisory scan is that it keeps
    # reporting; a scan reduced to push-only would quietly stop covering PRs.
    body = without_comments(codeql_text())
    assert "pull_request:" in body
    assert "- cron:" in body


# ------------------------------------------------- detector controls (the readers must RAISE)

BROKEN = "name: CodeQL\njobs:\n  analyze:\n    steps: []\n"


def test_without_comments_raises_when_everything_was_a_comment():
    with pytest.raises(AssertionError):
        without_comments("# only\n# comments\n")


def test_marker_values_raises_when_the_marker_is_absent():
    with pytest.raises(AssertionError):
        marker_values(BROKEN, "GATING-INTENT")


def test_gating_intent_raises_on_two_declarations():
    with pytest.raises(AssertionError):
        gating_intent("# GATING-INTENT: ADVISORY\n# GATING-INTENT: BLOCKING\n")


def test_gating_intent_raises_on_an_unrecognised_word():
    with pytest.raises(AssertionError):
        gating_intent("# GATING-INTENT: MAYBE\n")


def test_analyze_step_block_raises_when_the_step_is_absent():
    with pytest.raises(AssertionError):
        analyze_step_block(BROKEN)


def test_matrix_languages_raises_when_there_is_no_matrix():
    with pytest.raises(AssertionError):
        matrix_languages(BROKEN)


def test_job_name_template_raises_when_no_name_expands_the_matrix():
    with pytest.raises(AssertionError):
        job_name_template(BROKEN)


def test_job_name_template_raises_when_two_names_expand_the_matrix():
    # A step whose `name:` is not the list-item key -- `- uses:` first, `name:` second -- is
    # indistinguishable from a job name to a line-anchored reader, so two matches must RAISE
    # rather than silently pick the first. Found by this file's own first red run: the earlier
    # fixture used `- name:`, which the anchor cannot match at all, so it proved nothing.
    with pytest.raises(AssertionError):
        job_name_template(
            "    name: analyze (${{ matrix.language }})\n"
            "      - uses: some/action@v1\n"
            "        name: step ${{ matrix.language }}\n"
        )


# ------------------------------------------------- both-states control on the consistency check

ADVISORY_HEAD = (
    "# GATING-INTENT: ADVISORY\n"
    "jobs:\n"
    "  analyze:\n"
    "    steps:\n"
    "      - uses: github/codeql-action/analyze@v4\n"
    "        with:\n"
    "          category: \"/language:python\"\n"
)

BLOCKING_HEAD = ADVISORY_HEAD.replace("ADVISORY", "BLOCKING")
WITH_THRESHOLD = "          fail-on: error\n"


def test_the_consistency_check_fires_in_the_broken_state():
    # Direction 1: says advisory, behaves blocking.
    said_advisory_but_blocks = intent_violations(ADVISORY_HEAD + WITH_THRESHOLD)
    assert len(said_advisory_but_blocks) == 1
    assert "fail-on: error" in said_advisory_but_blocks[0]

    # Direction 2: says blocking, behaves advisory.
    said_blocking_but_advises = intent_violations(BLOCKING_HEAD)
    assert len(said_blocking_but_advises) == 1
    assert "no `fail-on:` threshold" in said_blocking_but_advises[0]


def test_the_consistency_check_is_silent_in_both_coherent_states():
    assert intent_violations(ADVISORY_HEAD) == []
    assert intent_violations(BLOCKING_HEAD + WITH_THRESHOLD) == []


def test_fail_on_is_read_from_the_analyze_step_and_not_from_a_neighbour():
    # A `fail-on:` belonging to a LATER step must not be attributed to the analyze step, or
    # the consistency check reports a threshold the analyze action never receives.
    neighbour = ADVISORY_HEAD + (
        "      - uses: some/other-action@v1\n"
        "        with:\n"
        "          fail-on: error\n"
    )
    assert fail_on_value(neighbour) is None
    assert intent_violations(neighbour) == []
