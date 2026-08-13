"""SSOT guard: `.coverage-thresholds.json` must state exactly what the stacks enforce.

`CLAUDE.md` ("Coverage Source of Truth") tells every agent and every metaswarm gate that
`.coverage-thresholds.json` is authoritative and that "no skill may skip it". Nothing
mechanically checked that the file's numbers matched the commands that actually gate the
build, and measured on 2026-08-13 they did not:

  * `stacks.typescript.thresholds` declared 100/100/100/100 with
    `status: "target-not-yet-enforceable"` and `blockPRCreation: false`, while
    `cockpit/vitest.config.ts` enforces a RATCHET of statements 71.71 / branches 80.67 /
    functions 77.19 / lines 70.95 through `cockpit/package.json`'s `test` script
    (`vitest run --coverage`), which the blocking `cockpit` CI job runs at
    `.github/workflows/ci.yml:273`. Four enforced numbers the SSOT did not state, and a
    status field asserting the opposite of the truth.
  * The `js/` rail was ABSENT from the SSOT entirely -- the file's own `$comment` said
    "This project has THREE stacks" -- while `js/vitest.config.ts` enforces lines 100 /
    functions 100 / statements 100 / branches 88 via `js/package.json`'s `test` script,
    run by the blocking `js` CI job at `.github/workflows/ci.yml:111`.

Python and Rust already agreed with reality and are guarded here so they cannot drift out
of it: `pyproject.toml:105` addopts carries `--cov-fail-under=100` with
`[tool.coverage.run] branch = true`, and no Rust coverage tool is wired anywhere.

WHAT THIS FILE ENFORCES, IN BOTH DIRECTIONS
-------------------------------------------
1. Every threshold the SSOT declares must be enforced, at that exact number, by the
   command it names in `enforcedBy`.
2. Every enforcement site in the tree must be declared by the SSOT. Coverage configs are
   DISCOVERED by walking the repo, not read from a list in this file, so a new rail cannot
   arrive with its own gate while the SSOT stays silent about it.

Direction 2 is the one that actually rotted, and a hardcoded list would not have caught it.

WHY THE NUMBERS LIVE IN ONE PLACE AND ARE NOT RESTATED
------------------------------------------------------
The drift this file exists to stop was caused by RESTATEMENT: the SSOT carried its own copy
of measured percentages and dark-module lists, which then aged badly in both directions
while the configs moved on. So the SSOT now states only the ENFORCED FLOORS -- the numbers
a command will fail on -- and points at `cockpit/vitest.config.ts:74-102` for the measured
breakdown rather than duplicating it. This file asserts the floors and deliberately asserts
NOTHING about the measured percentages, because a test that pins a measurement would rot
exactly as the prose did.

DETECTOR DISCIPLINE
-------------------
Nearly every assertion here is a parity or ABSENCE claim, and a parity claim computed with
a broken reader is the failure mode recorded in `rules/common/single-signal-verification.md`
(a control that "passes" while comparing the wrong collection, so both sides read equal).
So every reader FAILS CLOSED -- `read_ssot`, `vitest_thresholds`, `python_cov_fail_under`
and `discover_coverage_configs` all raise rather than return an empty dict, an empty list or
a default -- and `test_threshold_comparison_fires_on_a_changed_number` re-feeds a known-BAD
config through the same comparison to prove the parity tests can still go loud. A reader
that returned `{}` for every input would make every parity test below vacuously green.
"""
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

SSOT = REPO / ".coverage-thresholds.json"
PYPROJECT = REPO / "pyproject.toml"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CARGO_MANIFEST = REPO / "cockpit" / "src-tauri" / "Cargo.toml"

# Present in a git checkout, absent from an unpacked sdist -- pyproject.toml's sdist
# `include` allowlist carries only /llm_anthology, /tests, README, LICENSE, SECURITY and
# ARCHITECTURE, so `.coverage-thresholds.json`, `cockpit/`, `js/` and `.github/` are all
# legitimately missing there. Deliberately NOT one of the files under test: using the SSOT
# itself as the marker would turn "somebody deleted the SSOT" into a skip, not a failure.
SOURCE_TREE_MARKER = REPO / ".github" / "workflows" / "release.yml"

requires_source_tree = pytest.mark.skipif(
    not SOURCE_TREE_MARKER.is_file(),
    reason="not a source checkout ({} is absent, so this is an unpacked sdist and the "
           "coverage SSOT plus the stacks it describes are legitimately missing)".format(
               SOURCE_TREE_MARKER),
)

# Filename of a per-rail coverage gate. Discovery keys on this rather than on a list of
# known rails, so `js/` cannot be forgotten a second time.
COVERAGE_CONFIG_NAME = "vitest.config.ts"

# Vendored, generated and scratch trees. A `vitest.config.ts` inside `node_modules` belongs
# to a dependency and is not one of this repo's gates.
UNWALKED_DIRS = frozenset((
    "node_modules", "target", "dist", "coverage", "build", "venv", ".venv", "__pycache__",
))

# Rust coverage instrumentation, none of which is wired today. If any of these appears the
# SSOT's `rust.thresholds: null` becomes a lie in the other direction.
RUST_COVERAGE_TOOLS = ("tarpaulin", "llvm-cov", "grcov")


# ------------------------------------------------------------------------ readers

def read_ssot():
    """Return the parsed SSOT, or raise if it is missing, unparseable or shapeless."""
    if not SSOT.is_file():
        raise AssertionError("{} does not exist".format(SSOT))
    text = SSOT.read_text(encoding="utf-8")
    if not text.strip():
        raise AssertionError("{} is empty".format(SSOT))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AssertionError("{} is not valid JSON: {}".format(SSOT, exc))
    stacks = data.get("stacks")
    if not isinstance(stacks, dict) or not stacks:
        raise AssertionError(
            "{} carries no `stacks` map, so every per-stack assertion below would compare "
            "an empty collection against reality and report parity -- refusing to "
            "guess".format(SSOT))
    return data


def parse_vitest_thresholds(text, where):
    """Return `{metric: float}` from a vitest config's `coverage.thresholds` block.

    Raises rather than returning `{}`: an empty dict compares equal to an empty declared
    dict, which is precisely the shape of a control that proves only that the file was
    readable.
    """
    match = re.search(r"thresholds\s*:\s*\{", text)
    if match is None:
        raise AssertionError(
            "{}: no `thresholds: {{` block found. Either the gate was deleted -- which is "
            "the finding, not a reason to pass -- or this reader has gone blind".format(
                where))
    open_brace = match.end() - 1
    depth = 0
    close_brace = None
    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                close_brace = index
                break
    if close_brace is None:
        raise AssertionError("{}: unbalanced braces after `thresholds:`".format(where))
    body = re.sub(r"(?m)^\s*//.*$", "", text[open_brace + 1:close_brace])
    pairs = dict((key, float(value)) for key, value
                 in re.findall(r"(\w+)\s*:\s*([0-9]+(?:\.[0-9]+)?)", body))
    if not pairs:
        raise AssertionError(
            "{}: the `thresholds` block parsed to ZERO metrics. A gate that names no metric "
            "enforces nothing, and comparing it would report parity".format(where))
    return pairs


def vitest_thresholds(path):
    if not path.is_file():
        raise AssertionError("{} does not exist".format(path))
    return parse_vitest_thresholds(path.read_text(encoding="utf-8"), path.name)


def discover_coverage_configs(root):
    """Every non-vendored `vitest.config.ts` under `root`, as repo-relative POSIX paths.

    Discovery, not a hardcoded list. The defect this file guards is a rail that enforces a
    gate the SSOT has never heard of, and a list written here would have been written from
    the same stale premise as the SSOT.
    """
    found = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:                                    # pragma: no cover - unreadable dir
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in UNWALKED_DIRS and not entry.name.startswith("."):
                    pending.append(entry)
            elif entry.name == COVERAGE_CONFIG_NAME:
                found.append(entry.relative_to(root).as_posix())
    if not found:
        raise AssertionError(
            "found ZERO {} under {} -- fail closed. Either every coverage gate was deleted "
            "or this walk is broken; both make the parity tests below "
            "meaningless".format(COVERAGE_CONFIG_NAME, root))
    return sorted(found)


def parse_cov_fail_under(text, where):
    """Return pytest's `--cov-fail-under` value from a pyproject's addopts line."""
    addopts = re.search(r'(?m)^\s*addopts\s*=\s*"([^"]*)"', text)
    if addopts is None:
        raise AssertionError(
            "{}: no `[tool.pytest.ini_options] addopts` line found, so the Python gate "
            "cannot be read and its declared thresholds cannot be checked".format(where))
    gate = re.search(r"--cov-fail-under=([0-9]+(?:\.[0-9]+)?)", addopts.group(1))
    if gate is None:
        raise AssertionError(
            "{}: addopts is {!r} and carries NO --cov-fail-under, so the Python coverage "
            "numbers in the SSOT are enforced by nothing".format(where, addopts.group(1)))
    return float(gate.group(1))


def python_cov_fail_under():
    if not PYPROJECT.is_file():
        raise AssertionError("{} does not exist".format(PYPROJECT))
    return parse_cov_fail_under(PYPROJECT.read_text(encoding="utf-8"), PYPROJECT.name)


def python_branch_mode_enabled():
    """True when `[tool.coverage.run] branch = true`, which is what makes `branches` real."""
    text = PYPROJECT.read_text(encoding="utf-8")
    section = re.search(r"(?ms)^\[tool\.coverage\.run\]\s*$(.*?)(?=^\[|\Z)", text)
    if section is None:
        raise AssertionError(
            "{}: no [tool.coverage.run] section, so whether branch coverage is measured at "
            "all is unknown".format(PYPROJECT.name))
    return re.search(r"(?m)^\s*branch\s*=\s*true\s*$", section.group(1)) is not None


def rust_coverage_tooling():
    """Names of any Rust coverage instrumentation wired into CI or the crate manifest.

    The SSOT is deliberately NOT searched: it mentions `cargo-tarpaulin` in prose to say
    the tool is absent, and matching that would make this detector agree with the document
    it is supposed to audit.
    """
    hits = []
    for path in (CI_WORKFLOW, CARGO_MANIFEST):
        if not path.is_file():
            raise AssertionError(
                "{} does not exist, so 'no Rust coverage gate is wired' cannot be "
                "measured".format(path))
        text = path.read_text(encoding="utf-8")
        for tool in RUST_COVERAGE_TOOLS:
            if tool in text:
                hits.append("{}: {}".format(path.name, tool))
    return hits


def declared_stacks():
    return sorted(read_ssot()["stacks"].items())


# ------------------------------------- direction 1: nothing declared that nothing enforces

@requires_source_tree
def test_every_declared_threshold_names_the_site_that_enforces_it():
    problems = []
    for name, stack in declared_stacks():
        thresholds = stack.get("thresholds")
        if not thresholds:
            continue
        if not stack.get("enforcedBy"):
            problems.append(
                "stacks.{}: declares thresholds {} but names no `enforcedBy`, so no command "
                "is known to enforce them".format(name, sorted(thresholds)))
    assert not problems, (
        "\n  " + "\n  ".join(problems) + "\n\nAdd `enforcedBy` naming the repo-relative "
        "file whose gate produces those numbers.")


@requires_source_tree
def test_declared_thresholds_equal_what_each_coverage_config_enforces():
    problems = []
    for name, stack in declared_stacks():
        enforced_by = stack.get("enforcedBy") or ""
        if not enforced_by.endswith(COVERAGE_CONFIG_NAME):
            continue
        enforced = vitest_thresholds(REPO / enforced_by)
        declared = stack.get("thresholds") or {}
        for metric in sorted(set(enforced) | set(declared)):
            if declared.get(metric) != enforced.get(metric):
                problems.append(
                    "stacks.{}.thresholds.{}: SSOT says {!r}, {} enforces {!r}".format(
                        name, metric, declared.get(metric), enforced_by,
                        enforced.get(metric)))
    assert not problems, (
        "\n  " + "\n  ".join(problems) + "\n\nThe config is the gate that fails the build, "
        "so it wins: restate its numbers in .coverage-thresholds.json. A ratchet may only "
        "be RAISED, and raising it means updating both places in the same commit.")


@requires_source_tree
def test_python_stack_states_the_gate_pyproject_actually_enforces():
    stack = read_ssot()["stacks"]["python"]
    declared = stack.get("thresholds") or {}
    gate = python_cov_fail_under()
    assert declared, "stacks.python declares no thresholds at all"
    for metric in ("lines", "statements", "branches"):
        assert declared.get(metric) == gate, (
            "stacks.python.thresholds.{} is {!r} but pyproject.toml addopts enforces "
            "--cov-fail-under={!r}".format(metric, declared.get(metric), gate))
    assert python_branch_mode_enabled(), (
        "stacks.python declares a `branches` threshold, but pyproject.toml does not set "
        "[tool.coverage.run] branch = true, so branch coverage is not measured and that "
        "number is enforced by nothing")


@requires_source_tree
def test_python_functions_threshold_is_only_honest_because_100_entails_it():
    """coverage.py reports no per-function metric, so `functions` is not directly gated.

    At `--cov-fail-under=100` with branch mode on, every statement is executed, which
    entails that every function carrying an executable body ran -- so stating 100 is
    honest. At any lower gate the entailment breaks and the number would be a claim
    nothing checks.
    """
    stack = read_ssot()["stacks"]["python"]
    declared = (stack.get("thresholds") or {}).get("functions")
    if declared is None:
        return
    gate = python_cov_fail_under()
    assert declared == 100 and gate == 100, (
        "stacks.python.thresholds.functions is {!r} at a --cov-fail-under of {!r}. "
        "coverage.py measures no functions metric; the only value entailed by another gate "
        "is 100, and only when the statement gate is also 100".format(declared, gate))


@requires_source_tree
def test_rust_stack_and_the_tree_agree_on_having_no_coverage_gate():
    stack = read_ssot()["stacks"]["rust"]
    wired = rust_coverage_tooling()
    if stack.get("thresholds") is None:
        assert not wired, (
            "stacks.rust.thresholds is null (no coverage gate) but Rust coverage "
            "instrumentation IS wired: {}. State the thresholds it enforces.".format(wired))
    else:
        assert wired, (
            "stacks.rust declares thresholds {} but no Rust coverage instrumentation is "
            "wired anywhere in {} or {} -- nothing enforces them".format(
                sorted(stack["thresholds"]), CI_WORKFLOW.name, CARGO_MANIFEST.name))


# ------------------------------------- direction 2: nothing enforced that nothing declares

@requires_source_tree
def test_every_coverage_config_in_the_tree_is_declared_by_the_ssot():
    """The direction that actually rotted: `js/` enforced a gate the SSOT never mentioned."""
    found = discover_coverage_configs(REPO)
    declared = {}
    for name, stack in declared_stacks():
        enforced_by = stack.get("enforcedBy")
        if enforced_by:
            declared.setdefault(enforced_by, []).append(name)

    undeclared = [path for path in found if path not in declared]
    assert not undeclared, (
        "these coverage gates run in the tree but no stack in .coverage-thresholds.json "
        "names them, so the declared SSOT does not describe what the build "
        "enforces: {}".format(undeclared))

    phantom = sorted(path for path in declared
                     if path.endswith(COVERAGE_CONFIG_NAME) and path not in found)
    assert not phantom, (
        "the SSOT names these coverage configs, which do not exist: {}. Found: {}".format(
            phantom, found))

    doubled = sorted((path, names) for path, names in declared.items() if len(names) > 1)
    assert not doubled, (
        "one config is claimed by several stacks, so its numbers would be asserted twice "
        "and could be stated inconsistently: {}".format(doubled))


@requires_source_tree
def test_the_ssot_states_how_many_stacks_it_carries_and_the_count_is_right():
    """The `$comment` said "THREE stacks" while four rails ran their own test command."""
    data = read_ssot()
    comment = data.get("$comment", "")
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
    stated = [count for word, count in words.items()
              if re.search(r"\b{}\s+stacks\b".format(word), comment, re.IGNORECASE)]
    assert len(stated) == 1, (
        "the SSOT `$comment` must state its stack count exactly once, in words, so the "
        "count cannot silently disagree with the `stacks` map; found {}".format(stated))
    assert stated[0] == len(data["stacks"]), (
        "the `$comment` says {} stacks but the `stacks` map carries {}: {}".format(
            stated[0], len(data["stacks"]), sorted(data["stacks"])))


@requires_source_tree
def test_a_stack_that_blocks_a_pr_is_a_stack_that_is_actually_enforced():
    """`blockPRCreation` is what an agent reads to decide whether it may ship."""
    problems = []
    for name, stack in declared_stacks():
        blocks = bool(stack.get("blockPRCreation"))
        enforced = bool(stack.get("thresholds")) and bool(stack.get("enforcedBy"))
        if blocks and not enforced:
            problems.append(
                "stacks.{}: blockPRCreation is true but it declares no enforced "
                "thresholds".format(name))
        if enforced and not blocks:
            problems.append(
                "stacks.{}: {} fails the build on these thresholds, so blockPRCreation "
                "must be true".format(name, stack.get("enforcedBy")))
    assert not problems, "\n  " + "\n  ".join(problems)


# --------------------------------------------------------------- detector controls

def test_threshold_comparison_fires_on_a_changed_number():
    """Both-states control for `test_declared_thresholds_equal_...`.

    Parity above is only evidence if the reader can SEE a difference. Take the real cockpit
    config, move one number, and require the parse to disagree with the SSOT.
    """
    real = (REPO / "cockpit" / COVERAGE_CONFIG_NAME)
    if not real.is_file():
        pytest.skip("{} is absent (sdist)".format(real))
    text = real.read_text(encoding="utf-8")
    honest = parse_vitest_thresholds(text, "real")
    tampered = parse_vitest_thresholds(
        text.replace("statements: {}".format(honest["statements"]), "statements: 3.5"),
        "tampered")
    assert tampered["statements"] == 3.5, (
        "the reader did not pick up a changed number, so every parity assertion in this "
        "file is measuring something other than the enforced value")
    assert tampered != honest


def test_threshold_reader_refuses_a_config_with_no_thresholds_block():
    with pytest.raises(AssertionError) as caught:
        parse_vitest_thresholds("export default { test: { coverage: {} } }", "synthetic")
    assert "no `thresholds:" in str(caught.value)


def test_threshold_reader_refuses_an_empty_thresholds_block():
    with pytest.raises(AssertionError) as caught:
        parse_vitest_thresholds("thresholds: {\n  // all removed\n},", "synthetic")
    assert "ZERO metrics" in str(caught.value)


def test_threshold_reader_refuses_an_unterminated_block():
    with pytest.raises(AssertionError) as caught:
        parse_vitest_thresholds("thresholds: { lines: 100,", "synthetic")
    assert "unbalanced" in str(caught.value)


def test_threshold_reader_reads_both_config_spellings():
    """One rail writes the block multi-line, the other inline; both must parse."""
    multi = parse_vitest_thresholds(
        "coverage: {\n  thresholds: {\n    statements: 71.71,\n    lines: 70.95,\n  },\n}",
        "multi")
    inline = parse_vitest_thresholds(
        "thresholds: { lines: 100, functions: 100, branches: 88 },", "inline")
    assert multi == {"statements": 71.71, "lines": 70.95}
    assert inline == {"lines": 100.0, "functions": 100.0, "branches": 88.0}


def test_config_discovery_refuses_a_tree_with_no_gates(tmp_path):
    with pytest.raises(AssertionError) as caught:
        discover_coverage_configs(tmp_path)
    assert "ZERO" in str(caught.value)


def test_config_discovery_finds_a_nested_gate_and_skips_vendored_ones(tmp_path):
    """Control: the walk must be able to SEE a new rail, or direction 2 is vacuous."""
    (tmp_path / "newrail").mkdir()
    (tmp_path / "newrail" / COVERAGE_CONFIG_NAME).write_text("thresholds: { lines: 1 },")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / COVERAGE_CONFIG_NAME).write_text("x")
    (tmp_path / ".scratch").mkdir()
    (tmp_path / ".scratch" / COVERAGE_CONFIG_NAME).write_text("x")
    assert discover_coverage_configs(tmp_path) == ["newrail/" + COVERAGE_CONFIG_NAME]


@requires_source_tree
def test_config_discovery_finds_both_real_rails():
    """Control against the wrong-collection failure: name the rails, do not just count.

    `rules/common/single-signal-verification.md` records the shape this guards -- a control
    that iterates one level above the collection under test, reports a plausible equal
    number on both sides, and proves only that the file was readable.
    """
    found = discover_coverage_configs(REPO)
    assert "cockpit/" + COVERAGE_CONFIG_NAME in found
    assert "js/" + COVERAGE_CONFIG_NAME in found


def test_addopts_reader_refuses_a_gateless_addopts():
    with pytest.raises(AssertionError) as caught:
        parse_cov_fail_under('addopts = "-q --cov=llm_anthology"', "synthetic")
    assert "NO --cov-fail-under" in str(caught.value)


def test_addopts_reader_refuses_a_pyproject_without_addopts():
    with pytest.raises(AssertionError) as caught:
        parse_cov_fail_under("[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n", "syn")
    assert "no `[tool.pytest.ini_options] addopts` line" in str(caught.value)


def test_addopts_reader_reads_the_real_gate():
    """Both-states control: the reader returns a real number, not a default."""
    assert parse_cov_fail_under('addopts = "-q --cov-fail-under=87"', "syn") == 87.0


def test_ssot_reader_refuses_a_stackless_file(monkeypatch):
    monkeypatch.setattr(pathlib.Path, "read_text",
                        lambda self, **kw: "{\"thresholds\": {}}")
    with pytest.raises(AssertionError) as caught:
        read_ssot()
    assert "no `stacks` map" in str(caught.value)


def test_ssot_reader_refuses_unparseable_json(monkeypatch):
    monkeypatch.setattr(pathlib.Path, "read_text", lambda self, **kw: "{not json")
    with pytest.raises(AssertionError) as caught:
        read_ssot()
    assert "not valid JSON" in str(caught.value)


def test_ssot_reader_refuses_an_empty_file(monkeypatch):
    monkeypatch.setattr(pathlib.Path, "read_text", lambda self, **kw: "   \n")
    with pytest.raises(AssertionError) as caught:
        read_ssot()
    assert "is empty" in str(caught.value)


def test_ssot_reader_refuses_a_missing_file(monkeypatch):
    monkeypatch.setattr(pathlib.Path, "is_file", lambda self: False)
    with pytest.raises(AssertionError) as caught:
        read_ssot()
    assert "does not exist" in str(caught.value)


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
