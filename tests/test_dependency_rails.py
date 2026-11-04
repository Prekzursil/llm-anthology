"""Gates for the dependency-update rails and the version bounds a bot bump must respect.

Three independent defects live here, each measured on 2026-08-10 before it was fixed.

R-3 -- A MANIFEST WITH NO RAIL IS A VULNERABILITY NOBODY CAN FIX.
`.github/dependabot.yml` declared three rails (`pip:/`, `npm:/js`, `github-actions:/`)
while the repo carries FOUR dependency manifests. `directory:` is an exact path, not a
prefix, so `npm:/js` does not reach `cockpit/package.json`, and nothing at all reached
`cockpit/src-tauri/Cargo.toml`. Measured via the alerts API: of three open Dependabot
alerts, TWO sat in that gap -- #7 (rust, glib, `cockpit/src-tauri/Cargo.lock`) and #6
(npm, postcss, `cockpit/package-lock.json`); only #8 (npm, postcss,
`js/package-lock.json`) had a rail that could propose a fix. Those are not idle dev
deps: `release.yml`'s `installer-build` job runs `npm ci` and `npx tauri build` from
`cockpit/`, so that tree builds the installer that ships to users.

R-4 -- AN ACTION WHOSE REFS RUN AHEAD OF ITS RELEASES.
`dtolnay/rust-toolchain` publishes a git BRANCH per Rust version, and it publishes them
before the toolchain exists. Measured: the action repo has branches `1.98`, `1.99`,
`1.100` (and `1.100.0`), while `https://static.rust-lang.org/dist/rust-<v>-x86_64-
unknown-linux-gnu.tar.gz.sha256` returns 200 for 1.97.0 and **404 for 1.98.0, 1.99.0 and
1.100.0**. So Dependabot's "highest version" resolves to a toolchain `rustup` cannot
download. PR #15 bumped 1.91.0 -> 1.100.0 and BOTH cockpit legs died on
`error: could not download nonexistent rust version 1.100.0-...: 404` (confirmed in the
run log for run 31318613838, on `x86_64-unknown-linux-gnu` and `x86_64-pc-windows-msvc`).
1.100.0 > 1.91.0 is CORRECT numeric ordering, so this is not a Dependabot sort bug that
a future release fixes -- it recurs every time a new branch appears.

R-5 -- AN UNBOUNDED RENDERER DEPENDENCY REWRITES THE PRODUCT SILENTLY.
`markdown-it-py` produces the HTML this product exists to show, and it was declared
`>=2.2` with no ceiling. No golden-HTML assertion exists to catch a markup change
(measured: `tests/test_render_html.py` contains zero occurrences of `<p>`, `<em>`,
`<code>` or `assert ... == "<`), and the Python rail has no lockfile, so a new major
would arrive on the next clean install and change user-facing output with the suite
still green.

WHY THESE ASSERTIONS ARE TEXT-BASED
-----------------------------------
No YAML parser is a declared dependency of this project, and `tomllib` does not exist on
the 3.9 CI leg (`requires-python = ">=3.9"`), so both readers here are hand-rolled --
the same reasoning, and the same detector-guarding discipline, as
`tests/test_release_version_sync.py`. Every reader RAISES instead of returning a default,
and each one has a test at the bottom feeding it a deliberately broken input, because a
reader that silently returns nothing turns this whole file into a vacuous green.
"""

import os
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Present in a git checkout, absent from the sdist (pyproject.toml's sdist `include`
# allowlist carries only llm_anthology/, tests/ and four docs), and deliberately NOT the
# file under test: using dependabot.yml itself as the marker would turn "somebody deleted
# dependabot.yml" into a skip instead of a failure.
SOURCE_TREE_MARKER = REPO / ".github" / "workflows" / "release.yml"

requires_source_tree = pytest.mark.skipif(
    not SOURCE_TREE_MARKER.is_file(),
    reason="not a source checkout ({} is absent, so this is an unpacked sdist and "
    ".github/, js/ and cockpit/ are legitimately missing)".format(SOURCE_TREE_MARKER),
)

DEPENDABOT = REPO / ".github" / "dependabot.yml"
WORKFLOWS = REPO / ".github" / "workflows"

# manifest on disk -> the (ecosystem, directory) rail that must watch it.
MANIFEST_RAILS = {
    "pyproject.toml": ("pip", "/"),
    "js/package.json": ("npm", "/js"),
    "cockpit/package.json": ("npm", "/cockpit"),
    "cockpit/src-tauri/Cargo.toml": ("cargo", "/cockpit/src-tauri"),
}

# Filenames Dependabot recognises as a dependency manifest for the ecosystems this repo
# uses. `Cargo.lock` and `package-lock.json` are covered by their sibling manifest's rail.
MANIFEST_NAMES = frozenset({"pyproject.toml", "package.json", "Cargo.toml"})

# Build outputs, caches and vendored trees. Everything here contains manifests that are
# NOT this repo's dependencies, so walking into them would demand rails for other
# people's packages.
PRUNED_DIRS = frozenset(
    {
        ".git",
        ".beads",
        ".claude",
        ".metaswarm",
        ".port",
        ".audit",
        ".scratch",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "__pycache__",
        "node_modules",
        "target",
        "dist",
        "build",
        "coverage",
        ".venv",
        "venv",
    }
)

# The one action whose "latest version" is not always installable (see R-4 above).
# FLEET RULE 2026-11-03: it is NO LONGER ignored in dependabot.yml -- the guard
# below now pins it to an exact X.Y.Z in every workflow instead, and a phantom
# Dependabot bump shows up as a red PR a human closes.
PHANTOM_VERSION_ACTIONS = ("dtolnay/rust-toolchain",)

# Runtime dependencies whose output IS the product, so a major bump must be a human
# decision rather than something a clean install picks up.
MUST_BE_CAPPED = ("markdown-it-py",)


# ------------------------------------------------------------------- readers

_COMMENT_RE = re.compile(r"#.*$")


def strip_comments(text):
    """Drop YAML comments, so config words in PROSE are not read as configuration.

    This file's comments name `dependency-name` and `package-ecosystem` while explaining
    them; without this, every one of those sentences would be parsed as a live entry.
    Naive on purpose -- it cuts at the first `#` on the line, which is correct only
    because no VALUE in dependabot.yml contains a `#`. If one ever does, this reader is
    what has to change.
    """
    return "\n".join(_COMMENT_RE.sub("", line) for line in text.splitlines())


def _scalar(block, key):
    found = re.search(r'{}:\s*"([^"]+)"'.format(re.escape(key)), block)
    if not found:
        raise AssertionError("an update entry has no `{}`".format(key))
    return found.group(1)


def update_entries(text):
    """Return one dict per item of the `updates:` list, or raise if there are none."""
    body = strip_comments(text)
    starts = [m.start() for m in re.finditer(r"(?m)^  - package-ecosystem:", body)]
    if not starts:
        raise AssertionError("no `- package-ecosystem:` entries found")
    bounds = starts[1:] + [len(body)]
    return [
        {
            "ecosystem": _scalar(body[start:end], "package-ecosystem"),
            "directory": _scalar(body[start:end], "directory"),
            "ignored": frozenset(re.findall(r'dependency-name:\s*"([^"]+)"', body[start:end])),
        }
        for start, end in zip(starts, bounds)
    ]


def rails(text):
    """The set of (ecosystem, directory) pairs dependabot.yml actually watches."""
    return {(e["ecosystem"], e["directory"]) for e in update_entries(text)}


def ignored_for(text, ecosystem, directory):
    """The dependency names ignored by exactly one rail, or raise if it is absent."""
    for entry in update_entries(text):
        if (entry["ecosystem"], entry["directory"]) == (ecosystem, directory):
            return entry["ignored"]
    raise AssertionError("no {} rail for {}".format(ecosystem, directory))


def discovered_manifests():
    """Every dependency manifest in the working tree, repo-relative, forward-slashed."""
    found = set()
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in PRUNED_DIRS]
        for name in files:
            if name in MANIFEST_NAMES:
                rel = pathlib.Path(root, name).relative_to(REPO)
                found.add(rel.as_posix())
    return found


_TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def project_dependencies(text):
    """Return the requirement strings of `[project] dependencies`, or raise.

    Deliberately not tomllib: CI runs a 3.9 leg where it does not exist. Table-scoped so
    `[project.optional-dependencies]`'s `dev = [...]` cannot be mistaken for the runtime
    list -- that table's header is `project.optional-dependencies`, not `project`.
    """
    lines = text.splitlines()
    table = None
    for index, line in enumerate(lines):
        header = _TABLE_RE.match(line)
        if header:
            table = header.group(1).strip()
            continue
        if table != "project" or not re.match(r"^\s*dependencies\s*=", line):
            continue
        chunk, cursor = line, index
        while "]" not in chunk and cursor + 1 < len(lines):
            cursor += 1
            chunk += lines[cursor]
        return re.findall(r'"([^"]+)"', chunk.split("=", 1)[1])
    raise AssertionError("no `dependencies` under [project]")


def specifiers(requirement):
    """Split `name>=1,<2` into ("name", [">=1", "<2"]), or raise."""
    name = re.match(r"[A-Za-z0-9._-]+", requirement)
    if not name:
        raise AssertionError("not a requirement: {!r}".format(requirement))
    rest = requirement[name.end() :]
    return name.group(0), [part.strip() for part in rest.split(",") if part.strip()]


def has_upper_bound(specs):
    """True when at least one specifier puts a ceiling on the version."""
    return any(spec.startswith(("<", "==", "~=")) for spec in specs)


def pinned_action_versions(name):
    """Every `uses: <name>@<ref>` ref across .github/workflows, with the file it is in."""
    pattern = re.compile(r"uses:\s*{}@(\S+)".format(re.escape(name)))
    return {
        "{}:{}".format(path.name, index): match
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for index, match in enumerate(pattern.findall(path.read_text(encoding="utf-8")))
    }


# ------------------------------------------------------------------- the gates


@requires_source_tree
def test_every_dependency_manifest_in_the_tree_has_an_update_rail():
    """R-3. A manifest with no rail cannot receive a security fix, ever."""
    declared = rails(DEPENDABOT.read_text(encoding="utf-8"))
    missing = {manifest: rail for manifest, rail in MANIFEST_RAILS.items() if rail not in declared}
    assert not missing, (
        "these manifests have no dependabot rail, so Dependabot can raise alerts on "
        "them and never propose a fix: {}".format(missing)
    )


@requires_source_tree
def test_the_manifest_table_matches_what_is_actually_on_disk():
    """Guards the gate above: a NEW manifest must fail, not quietly go unwatched."""
    on_disk = discovered_manifests()
    assert on_disk, "the manifest walk found nothing, so the gate above proves nothing"
    assert on_disk == set(MANIFEST_RAILS), (
        "MANIFEST_RAILS and the working tree disagree -- unwatched: {}; listed but "
        "absent: {}".format(
            sorted(on_disk - set(MANIFEST_RAILS)),
            sorted(set(MANIFEST_RAILS) - on_disk),
        )
    )


@requires_source_tree
def test_every_listed_manifest_really_exists():
    for manifest in MANIFEST_RAILS:
        assert (REPO / manifest).is_file(), "{} is listed but absent".format(manifest)


@requires_source_tree
def test_no_dependency_is_ignored_anywhere_in_dependabot_yml():
    """FLEET RULE (2026-11-03). Zero tolerated suppression across every rail.

    Superscedes the old R-4 guard, which required `dtolnay/rust-toolchain` to be
    IGNORED because its refs run ahead of the release train (PR #15 proposed a
    phantom 1.100.0 and both cockpit legs 404'd, run 31318613838). The fleet rule
    now takes that cost in the open instead of suppressing updates: a phantom bump
    arrives as a RED Dependabot PR a human closes, and the exact-version pin guard
    below keeps main itself from ever floating. Nothing may be muted, ever -- an
    ignore block here would silently hide the availability of every future update
    of the ignored dependency, security releases included.
    """
    entries = update_entries(DEPENDABOT.read_text(encoding="utf-8"))
    offenders = {
        "{}:{}".format(entry["ecosystem"], entry["directory"]): sorted(entry["ignored"])
        for entry in entries
        if entry["ignored"]
    }
    assert not offenders, (
        "the fleet rule tolerates ZERO ignored dependencies; these rails still "
        "ignore some (every ignore hides security updates too): {}".format(offenders)
    )


@requires_source_tree
def test_the_phantom_ref_actions_are_pinned_to_an_exact_installable_version():
    """R-4, re-guarded. The pin -- not an ignore -- is the phantom-ref protection.

    `dtolnay/rust-toolchain` publishes a ref per Rust version ahead of the release
    train, so a floating ref ("stable", "master", a minor-glob) would let a hand
    edit or a template copy drift onto a toolchain rustup 404s on. An EXACT X.Y.Z
    pin can only change when a human types it, and the synced-twins test below
    keeps every copy of it in agreement. Dependabot may still OPEN a phantom bump
    PR (nothing ignores it any more) -- but it arrives red, in the open.
    """
    found = pinned_action_versions("dtolnay/rust-toolchain")
    assert found, "the toolchain pin disappeared from every workflow"
    exact = re.compile(r"^\d+\.\d+\.\d+$")
    floating = {where: ref for where, ref in found.items() if not exact.match(ref)}
    assert not floating, (
        "the rust toolchain must be pinned at an exact X.Y.Z so a phantom ref "
        "cannot ride in: {}".format(floating)
    )


@requires_source_tree
def test_the_pinned_rust_toolchain_agrees_across_every_workflow_that_declares_it():
    """The pin is bumped by hand now, and it lives in TWO files that must move together.

    ci.yml's cockpit job and release.yml's installer-build job both pin
    dtolnay/rust-toolchain. Bumping one leaves the release compiling against a different
    rustc than the gate that cleared it -- and with the bot no longer proposing the bump
    (R-4), nothing else would notice.
    """
    found = pinned_action_versions("dtolnay/rust-toolchain")
    # A zero-match regex would make the equality below trivially true.
    assert len(found) >= 2, (
        "expected the toolchain pin in at least ci.yml and release.yml, found {}".format(found)
    )
    assert len(set(found.values())) == 1, "the rust toolchain pins disagree: {}".format(found)


def test_the_renderer_dependency_that_decides_user_facing_html_is_capped():
    """R-5. Unguarded by the source-tree marker: pyproject.toml IS in the sdist."""
    declared = project_dependencies((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert declared, "no runtime dependencies parsed, so this gate proves nothing"
    specs = dict(specifiers(req) for req in declared)
    for name in MUST_BE_CAPPED:
        assert name in specs, "{} is not a declared dependency: {}".format(name, sorted(specs))
        assert has_upper_bound(specs[name]), (
            "{} has no upper bound ({!r}), so the next major arrives on the next clean "
            "install and rewrites user-facing HTML with nothing going red".format(name, specs[name])
        )


# ------------------------------------------ the readers must be able to fail


def test_strip_comments_removes_a_dependency_name_that_is_only_prose():
    """Both states: the same line counts when live and does not when commented."""
    live = (
        '  - package-ecosystem: "npm"\n    directory: "/x"\n    ignore:\n'
        '      - dependency-name: "left-pad"\n'
    )
    assert update_entries(live)[0]["ignored"] == frozenset({"left-pad"})
    prose = (
        '  - package-ecosystem: "npm"\n    directory: "/x"\n'
        '    # e.g. - dependency-name: "left-pad"\n'
    )
    assert update_entries(prose)[0]["ignored"] == frozenset()


def test_update_entries_raises_when_there_are_no_entries():
    with pytest.raises(AssertionError, match="no `- package-ecosystem:` entries"):
        update_entries("version: 2\nupdates: []\n")


def test_update_entries_raises_when_an_entry_has_no_directory():
    with pytest.raises(AssertionError, match="no `directory`"):
        update_entries('  - package-ecosystem: "pip"\n    schedule:\n      x: "y"\n')


def test_update_entries_does_not_leak_a_later_entrys_ignore_list_into_an_earlier_one():
    doc = (
        '  - package-ecosystem: "pip"\n    directory: "/"\n'
        '  - package-ecosystem: "github-actions"\n    directory: "/"\n'
        '    ignore:\n      - dependency-name: "some/action"\n'
    )
    first, second = update_entries(doc)
    assert first["ignored"] == frozenset()
    assert second["ignored"] == frozenset({"some/action"})


def test_ignored_for_raises_when_the_rail_does_not_exist():
    doc = '  - package-ecosystem: "pip"\n    directory: "/"\n'
    with pytest.raises(AssertionError, match="no cargo rail"):
        ignored_for(doc, "cargo", "/nope")


def test_project_dependencies_raises_when_the_table_has_none():
    with pytest.raises(AssertionError, match=r"no `dependencies` under \[project\]"):
        project_dependencies('[project]\nname = "x"\n')


def test_project_dependencies_ignores_the_optional_dependencies_table():
    """The live trap: `dev = [...]` lives under [project.optional-dependencies]."""
    doc = '[project]\nname = "x"\n[project.optional-dependencies]\ndev = ["pytest>=7"]\n'
    with pytest.raises(AssertionError):
        project_dependencies(doc)


def test_project_dependencies_reads_a_multi_line_array():
    doc = '[project]\ndependencies = [\n  "a>=1",\n  "b<2",\n]\nother = 1\n'
    assert project_dependencies(doc) == ["a>=1", "b<2"]


def test_specifiers_splits_a_two_sided_requirement():
    assert specifiers("markdown-it-py>=2.2,<5") == ("markdown-it-py", [">=2.2", "<5"])
    assert specifiers("zstandard") == ("zstandard", [])


def test_specifiers_raises_on_something_that_is_not_a_requirement():
    with pytest.raises(AssertionError, match="not a requirement"):
        specifiers(">=1.0")


def test_has_upper_bound_is_false_for_a_floor_only_pin_and_true_for_a_ceiling():
    assert not has_upper_bound([">=2.2"])
    assert not has_upper_bound([">=2.2", "!=3.0.1"])
    assert has_upper_bound([">=2.2", "<5"])
    assert has_upper_bound(["<=4.9"])
    assert has_upper_bound(["==4.2.0"])
    assert has_upper_bound(["~=4.2"])


@requires_source_tree
def test_pinned_action_versions_finds_nothing_for_an_action_this_repo_never_uses():
    assert pinned_action_versions("nobody/not-an-action") == {}
