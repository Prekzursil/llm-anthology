"""The release version is declared in five places; this is the gate that keeps them equal.

A release ships THREE separate artifacts built from one commit — a PyPI wheel, an npm
tarball and a Windows installer — and each carries its own independently-editable
version string. Nothing in the build couples them, so before this test existed a bump
that touched `pyproject.toml` and forgot `js/package.json` produced a "0.1.1 release"
in which the npm package still claimed 0.1.0. No other gate catches it: every suite in
the repo stays green while the three artifacts disagree.

The five files (see RELEASING.md for the bump order):

  pyproject.toml                      -> the PyPI wheel / sdist
  llm_anthology/__init__.py           -> `__version__`, which SHIPS INSIDE that wheel and
                                         is what the engine reports to the app as
                                         `engine_version` over `health.ping`
                                         (llm_anthology/sidecar.py)
  js/package.json                     -> the npm package
  cockpit/src-tauri/tauri.conf.json   -> the NSIS installer, and the version Tauri
                                         reports for the app
  cockpit/src-tauri/Cargo.toml        -> compiled into the shipped binary as
                                         CARGO_PKG_VERSION

`__version__` is the one worth spelling out, because it is the only entry that is not a
build input: nothing reads it to *stamp* an artifact, so a bump that misses it still
produces a correctly-versioned wheel — carrying an engine that tells every caller the
OLD number. That is a wrong answer rather than a build failure, which is exactly the
class this gate exists for. It is read as text, deliberately not by importing the
package, so the check cannot be broken by an unrelated import-time error.

`cockpit/package.json` is deliberately NOT in the gate. It is a private dev-only
package (`"name": "cockpit"`, no `files` field, never published, not referenced by any
publish path), so its version is not a release-artifact version and forcing it to track
the product would be noise. `cockpit/src/ipc/mock.ts`'s `engine_version: "mock-0.1.0"`
is likewise excluded: it is a browser-mock fixture whose whole purpose is to be
visibly not a real engine.

GUARDING THE DETECTOR
---------------------
A version-comparison test has an obvious failure mode: if the extractors silently
return nothing, all four "agree" on nothing and the test passes while proving zero. So
every extractor here RAISES on a missing key instead of returning a default, the gate
asserts each value independently looks like a version before comparing, and the tests
at the bottom feed each extractor a deliberately broken input to prove it actually goes
red there — a detector that has never failed is indistinguishable from a no-op.

The TOML extractor is table-scoped for a measured reason, not for tidiness:
`cockpit/src-tauri/Cargo.toml` carries a second `version = "0.59"` under
`[target.'cfg(windows)'.dependencies.windows-sys]`. A first-match scan reads that
dependency pin as the application version.
"""
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Present in a git checkout, absent from the sdist — pyproject.toml's sdist `include`
# allowlist carries only llm_anthology/, tests/ and four docs. This tells "running from
# the source repo, so the js/ and cockpit/ trees MUST be here" apart from "running from
# an unpacked sdist, where they legitimately are not".
#
# The sdist DOES ship tests/, this file included (measured: `tar -tzf` on a built sdist
# lists tests/test_release_version_sync.py and no .github/, js/ or cockpit/). So every
# test here that opens a file outside llm_anthology/ or tests/ has to carry this guard,
# or `pytest` from an unpacked sdist goes red for a reason that is not a defect.
SOURCE_TREE_MARKER = REPO / ".github" / "workflows" / "release.yml"

requires_source_tree = pytest.mark.skipif(
    not SOURCE_TREE_MARKER.is_file(),
    reason="not a source checkout ({} is absent, so this is an unpacked sdist and the "
           "js/ and cockpit/ trees are legitimately missing)".format(SOURCE_TREE_MARKER),
)

# (path relative to the repo root, format, table) — table is None for JSON and python.
RELEASE_VERSION_FILES = (
    ("pyproject.toml", "toml", "project"),
    ("llm_anthology/__init__.py", "python", None),
    ("js/package.json", "json", None),
    ("cockpit/src-tauri/tauri.conf.json", "json", None),
    ("cockpit/src-tauri/Cargo.toml", "toml", "package"),
)

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.-]+)?$")
_TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_VERSION_LINE_RE = re.compile(r"""^\s*version\s*=\s*["']([^"']+)["']\s*$""")
# Anchored at column 0 on purpose: an indented `__version__ = ...` is an assignment
# inside some function, not the module-level constant the wheel exports.
_DUNDER_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']\s*$""")


def toml_version(text, table):
    """Return the `version` of exactly `[table]`, or raise.

    Deliberately not tomllib: `requires-python` is >=3.9 and CI runs a 3.9 leg, where
    tomllib does not exist. Only one scalar out of one top-level table is needed.
    """
    current = None
    for line in text.splitlines():
        header = _TABLE_RE.match(line)
        if header:
            current = header.group(1).strip()
            continue
        if current != table:
            continue
        found = _VERSION_LINE_RE.match(line)
        if found:
            return found.group(1)
    raise AssertionError("no `version` under [{}]".format(table))


def json_version(text):
    """Return the top-level `version` of a JSON document, or raise."""
    doc = json.loads(text)
    if not isinstance(doc, dict) or "version" not in doc:
        raise AssertionError("no top-level `version` key")
    return doc["version"]


def dunder_version(text):
    """Return the module-level `__version__` of a Python source file, or raise.

    By text, not by import: importing to read the constant would make an unrelated
    import-time failure look like a version desync, and would tie this gate to the
    package being importable in whatever environment pytest happens to run in.
    """
    for line in text.splitlines():
        found = _DUNDER_RE.match(line)
        if found:
            return found.group(1)
    raise AssertionError("no module-level `__version__`")


_EXTRACTORS = {"toml": toml_version, "json": json_version, "python": dunder_version}


def read_version(rel, kind, table):
    path = REPO / rel
    if not path.is_file():
        raise AssertionError("{} does not exist".format(rel))
    text = path.read_text(encoding="utf-8")
    # An unknown `kind` must not silently fall through to some default parser, which is
    # how a fifth entry added with a typo'd format would go green while reading nothing.
    if kind not in _EXTRACTORS:
        raise AssertionError("{}: unknown format {!r}".format(rel, kind))
    try:
        return toml_version(text, table) if kind == "toml" else _EXTRACTORS[kind](text)
    except AssertionError as exc:
        raise AssertionError("{}: {}".format(rel, exc))


def implausible(found):
    """Names of entries whose value does not look like a version at all."""
    return sorted(
        rel for rel, value in found.items()
        if not (isinstance(value, str) and _VERSION_RE.match(value))
    )


def disagree(found):
    """True when the mapping holds more than one distinct version."""
    return len(set(found.values())) != 1


# ------------------------------------------------------------------- the gate

@requires_source_tree
def test_every_release_artifact_declares_the_same_version():
    found = {rel: read_version(rel, kind, table)
             for rel, kind, table in RELEASE_VERSION_FILES}

    # Reject an implausible value BEFORE comparing, so four empty strings can never be
    # read as agreement.
    assert not implausible(found), "not version strings: {}".format(
        {rel: found[rel] for rel in implausible(found)}
    )
    assert not disagree(found), "release versions disagree: {}".format(
        json.dumps(found, indent=2, sort_keys=True)
    )


def test_the_two_versions_that_ship_inside_the_wheel_agree():
    """Unguarded on purpose — this is the part of the gate an sdist CAN still run.

    `pyproject.toml` and `llm_anthology/__init__.py` are both inside the sdist, so
    checking them needs no source tree. Without this, `pytest` from an unpacked sdist
    skips every version check there is and reports a green run that verified nothing
    about the artifact it is running from.
    """
    declared = read_version("pyproject.toml", "toml", "project")
    shipped = read_version("llm_anthology/__init__.py", "python", None)
    assert not implausible({"pyproject.toml": declared}), declared
    assert declared == shipped, (
        "the wheel is built as {} but the engine inside it reports {}".format(
            declared, shipped)
    )


def test_the_gate_covers_every_file_the_runbook_tells_you_to_bump():
    """RELEASING.md's bump list and this tuple must not drift apart."""
    assert {rel for rel, _, _ in RELEASE_VERSION_FILES} == {
        "pyproject.toml",
        "llm_anthology/__init__.py",
        "js/package.json",
        "cockpit/src-tauri/tauri.conf.json",
        "cockpit/src-tauri/Cargo.toml",
    }


# -------------------------------------------- the detector must be able to fail

@requires_source_tree
def test_toml_version_ignores_a_version_in_a_different_table():
    """The live trap: Cargo.toml pins windows-sys 0.59 after [package] version."""
    cargo = (REPO / "cockpit/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    assert 'version = "0.59"' in cargo, "the decoy this test guards against is gone"
    assert toml_version(cargo, "package") != "0.59"


def test_toml_version_raises_when_the_table_has_no_version():
    with pytest.raises(AssertionError, match=r"no `version` under \[project\]"):
        toml_version('[project]\nname = "x"\n[other]\nversion = "9.9.9"\n', "project")


def test_toml_version_raises_when_the_table_is_absent_entirely():
    with pytest.raises(AssertionError):
        toml_version('[tool.x]\nversion = "9.9.9"\n', "project")


def test_json_version_raises_when_the_key_is_missing():
    with pytest.raises(AssertionError, match="no top-level `version` key"):
        json_version('{"name": "x"}')


def test_json_version_raises_on_a_non_object_document():
    with pytest.raises(AssertionError):
        json_version("[]")


def test_dunder_version_raises_when_the_module_declares_none():
    with pytest.raises(AssertionError, match="no module-level `__version__`"):
        dunder_version('"""docstring."""\n\nNAME = "x"\n')


def test_dunder_version_ignores_an_indented_assignment():
    """A `__version__` inside a function is not the constant the wheel exports."""
    with pytest.raises(AssertionError):
        dunder_version('def f():\n    __version__ = "9.9.9"\n')


def test_dunder_version_reads_the_module_level_constant():
    assert dunder_version('"""d."""\n__version__ = "1.2.3"\nX = 1\n') == "1.2.3"


def test_read_version_raises_on_a_missing_file():
    with pytest.raises(AssertionError, match="does not exist"):
        read_version("no/such/file.json", "json", None)


@requires_source_tree
def test_read_version_rejects_an_unknown_format_instead_of_guessing():
    with pytest.raises(AssertionError, match="unknown format"):
        read_version("js/package.json", "yaml", None)


@requires_source_tree
def test_read_version_names_the_file_it_could_not_parse():
    # Guarded, and it has to be: without the marker this would still "pass" on the
    # `does not exist` message, which also matches — a vacuous green.
    with pytest.raises(AssertionError, match=r"js/package\.json: no `version`"):
        read_version("js/package.json", "toml", "project")   # wrong parser on purpose


# Both-states checks on the two predicates the gate is built out of: each must be
# False for the good state and True for the bad one, or the gate measures nothing.

def test_disagree_is_false_when_all_agree_and_true_when_one_differs():
    assert not disagree({"a": "0.1.0", "b": "0.1.0", "c": "0.1.0"})
    assert disagree({"a": "0.1.0", "b": "0.1.1", "c": "0.1.0"})


def test_implausible_catches_the_vacuous_agreement_a_broken_parser_would_produce():
    assert implausible({"a": "", "b": ""}) == ["a", "b"]
    assert implausible({"a": None}) == ["a"]
    assert implausible({"a": "0.1.0", "b": "1.2.3-rc.1"}) == []
