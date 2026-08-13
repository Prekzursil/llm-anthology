"""Pin the artifact-path defaults of the tracked cockpit PowerShell probes to the repo the
scripts actually live in, so the next checkout move cannot silently orphan them again.

THE DEFECT, AS MEASURED ON 2026-08-13 BEFORE THIS FILE EXISTED
--------------------------------------------------------------
Two tracked probes each defaulted their output artifact to an absolute path under a
checkout location this repository no longer occupies:

    cockpit/tools/drive_installed.ps1:16
      [string]$Shot   = "$env:USERPROFILE\\.local\\opt\\ai-sessions-render\\.audit\\installed.png"
    cockpit/tools/verify_installed.ps1:11
      [string]$OutPng = "$env:USERPROFILE\\.local\\opt\\ai-sessions-render\\.audit\\installed-app.png"

The repository moved to `Documents/GitHub/ai-sessions-render`; `~/.local/opt/ai-sessions-render`
survives only as an empty husk (a single childless `cockpit` directory, measured with
`Get-ChildItem -Recurse`) and holds no `.audit` at all. So a probe run today did not overwrite
a stale artifact -- it silently CREATED a fresh `.audit` inside a dead tree, and the screenshot
the operator went looking for in the repo was never there. Nothing else in the repo passes
`-Shot`/`-OutPng` explicitly (`git grep` for both names returns only a prose mention in
`cockpit/tools/probe_csp_installed.mjs:16`), so the default IS the behaviour in every real run.

WHY THE FIX IS `$PSScriptRoot`-ANCHORED AND NOT A DIFFERENT ABSOLUTE PATH
-------------------------------------------------------------------------
Swapping one hardcoded checkout path for another only reschedules the same failure for the
next move. `cockpit/scripts/stage-engine.ps1:36` already establishes the house idiom --
`$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path` -- and both probes sit at the
same depth (`cockpit/tools/`), so the same `..\\..` reaches the repo root. Measured directly by
running a `param()`-block probe from `cockpit/tools/`: `$PSScriptRoot` IS bound while a param
default expression is evaluated, and the default resolved to `<repo root>\\.audit\\installed.png`.
That measurement is what makes the fix legal in a `param()` block rather than only in the body.

The absolute form of that resolved path is deliberately NOT quoted above. `tests/test_redact.py:38`
classifies this machine's username as "a local path component = PII to keep out", and
`test_format_drift_canary.py:407-412` guards a sibling file on the stated grounds that "in a public
repo it is a disclosure" -- this repo is public. Quoting the literal here would also be
self-defeating: a hardcoded checkout path in the docstring of the unit whose entire thesis is
"derive the checkout, never name it" goes stale on exactly the move this file exists to survive.

`.audit/` is gitignored (`.gitignore:37`), so writing artifacts there stays out of the index.

WHAT IS DELIBERATELY *NOT* REWRITTEN
------------------------------------
`verify_installed.ps1`'s `$InstallDir` default (`$env:LOCALAPPDATA\\LLM Anthology`) is a
MACHINE location -- where the installer puts the app -- not a repo location. Rewriting it
repo-relative would break the script outright. `test_verify_installed_keeps_its_machine_path`
pins it so a later over-eager sweep of "hardcoded paths" cannot take it with them; the
distinction this file enforces is *repo* paths must be derived, *machine* paths may be named.

DETECTOR DISCIPLINE
-------------------
Most assertions here are ABSENCE claims, and an absence measured with a broken matcher is a
vacuous green. So `read_script` raises unless the file exists AND still carries a `param(`
block; `param_default` raises unless the named parameter is present AND its captured default
is structurally whole (balanced parentheses and quotes -- a multi-line default would otherwise
be silently truncated to a fragment that trivially satisfies every negative assertion); and
two tests re-feed the exact pre-fix literals back through both predicates to prove they still
fire on the known-BROKEN state (the both-states rule).

The repo-wide guard exempts COMMENT lines, and that exemption was forced by measurement, not
convenience: on its first green run it flagged the very comments the fix added to record which
path was abandoned and why. A guard that cannot tell USE from MENTION makes the durable
explanation the thing you must delete to stay green -- the same defect class as a command
matcher that reads a payload instead of a verb. Only a FULL-line comment is exempt; a trailing
`# ...` after code is still scanned whole, so the exemption over-flags rather than under-flags.
`test_comment_exemption_does_not_blind_the_guard_to_real_code` pins that it still fires on an
executable line sitting directly beneath an exempted comment carrying the same path.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Present in a git checkout, absent from an unpacked sdist -- pyproject.toml's sdist `include`
# allowlist (pyproject.toml:90-98) carries only /llm_anthology, /tests, README, LICENSE,
# SECURITY, ARCHITECTURE, so the whole `cockpit/` tree is legitimately missing there. The
# marker is deliberately NOT one of the scripts under test: using one of those would turn
# "somebody deleted the probe" into a skip instead of a failure.
SOURCE_TREE_MARKER = REPO / ".github" / "workflows" / "release.yml"

requires_source_tree = pytest.mark.skipif(
    not SOURCE_TREE_MARKER.is_file(),
    reason="not a source checkout ({} is absent, so this is an unpacked sdist and the "
           "cockpit PowerShell probes are legitimately missing)".format(SOURCE_TREE_MARKER),
)

TOOLS = REPO / "cockpit" / "tools"
DRIVE = TOOLS / "drive_installed.ps1"
VERIFY = TOOLS / "verify_installed.ps1"

# Every tracked PowerShell script under `cockpit/` is swept by the repo-wide guard. Measured
# membership on 2026-08-13 via `git ls-files "*.ps1"`: cockpit/scripts/stage-engine.ps1,
# cockpit/tools/{drive_installed,pick_file_uia,shoot_window,verify_installed}.ps1.
COCKPIT_PS1_GLOB = "cockpit/**/*.ps1"

# The three shapes an orphaned repo path takes here. `.local/opt` is the abandoned home; the
# other two name a checkout DIRECTORY literally, under the current name and under the remote's
# name (`git remote get-url origin` ends `llm-anthology.git`, so a future rename is the obvious
# next way to re-introduce this). Slashes are matched both ways because PowerShell accepts both.
# Note `LLM Anthology` (spaced) is NOT matched -- that is the install directory, not a checkout.
ORPHAN_MARKERS = (
    re.compile(r"\.local[\\/]+opt", re.IGNORECASE),
    re.compile(r"[\\/]ai-sessions-render[\\/]", re.IGNORECASE),
    re.compile(r"[\\/]llm-anthology[\\/]", re.IGNORECASE),
)

# The anchor that makes a default survive a checkout move: it must be derived from the
# script's own location rather than named.
SCRIPT_ROOT_ANCHOR = re.compile(r"\$PSScriptRoot")


def read_script(path):
    """Return a script's text, refusing to hand back anything unvetted.

    Raises rather than returning `""` when the file is gone or has been restructured past a
    `param()` block, because every assertion below is a substring test that an empty string
    would pass.
    """
    if not path.is_file():
        raise AssertionError("{} is missing -- the probe it pins no longer exists".format(path))
    text = path.read_text(encoding="utf-8")
    if "param(" not in text:
        raise AssertionError(
            "{} no longer has a param() block; every default pinned here has to be "
            "re-derived by hand".format(path)
        )
    return text


def param_default(text, name, path):
    """Return the default expression bound to `$name` in a `param()` block.

    Fail-closed twice over: an absent parameter raises (a rename must not silently pass), and
    a captured default whose parentheses or quotes do not balance raises too -- that is the
    signature of a default that was reflowed across lines, which this line-scoped reader would
    otherwise truncate into a harmless-looking fragment.
    """
    match = re.search(r"\$" + re.escape(name) + r"\s*=\s*(?P<default>.+)$", text, re.MULTILINE)
    if match is None:
        raise AssertionError(
            "{} has no ${} parameter -- it was renamed or removed, so this pin is "
            "measuring nothing".format(path, name)
        )
    default = match.group("default").strip().rstrip(",").strip()
    if default.count("(") != default.count(")") or default.count('"') % 2 or default.count("'") % 2:
        raise AssertionError(
            "${} in {} has a default this line-scoped reader cannot capture whole "
            "(unbalanced parens/quotes in {!r}) -- it was probably reflowed across "
            "lines".format(name, path, default)
        )
    return default


def orphan_hits(text):
    """Every orphaned-repo-path marker present in `text`, as the patterns that matched."""
    return [pattern.pattern for pattern in ORPHAN_MARKERS if pattern.search(text)]


def is_repo_relative(default):
    """True when a default derives the repo root from the script's own location."""
    return bool(SCRIPT_ROOT_ANCHOR.search(default))


BLOCK_OPEN = "<#"
BLOCK_CLOSE = "#>"


def scan_for_orphan_paths(text):
    """Return `(lineno, hits, line)` for every EXECUTABLE line carrying an orphan marker.

    Comment lines are skipped -- see the module docstring for why that exemption exists and
    what pins it. A line counts as a comment only when its first non-whitespace character
    opens one, so `$x = $y  # see ~/.local/opt/...` is still scanned in full.
    """
    offenders = []
    in_block = False
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if in_block:
            in_block = BLOCK_CLOSE not in stripped
            continue
        if stripped.startswith(BLOCK_OPEN):
            in_block = BLOCK_CLOSE not in stripped[len(BLOCK_OPEN):]
            continue
        if stripped.startswith("#"):
            continue
        hits = orphan_hits(line)
        if hits:
            offenders.append((lineno, hits, stripped))
    return offenders


# --- the two defaults this unit repoints -------------------------------------------------

@requires_source_tree
@pytest.mark.parametrize(
    ("path", "param", "artifact"),
    [
        (DRIVE, "Shot", "installed.png"),
        (VERIFY, "OutPng", "installed-app.png"),
    ],
)
def test_artifact_default_is_derived_from_the_script_location(path, param, artifact):
    default = param_default(read_script(path), param, path)
    assert is_repo_relative(default), (
        "${} in {} still names a checkout instead of deriving it from $PSScriptRoot: "
        "{!r}".format(param, path, default)
    )
    assert orphan_hits(default) == [], (
        "${} in {} still carries an orphaned repo path ({}): {!r}".format(
            param, path, orphan_hits(default), default
        )
    )
    assert "..\\.." in default, (
        "${} in {} must walk cockpit/tools -> repo root, the same two levels "
        "cockpit/scripts/stage-engine.ps1:36 walks: {!r}".format(param, path, default)
    )
    assert ".audit" in default, (
        "${} in {} must land in the gitignored .audit/ artifact directory "
        "(.gitignore:37): {!r}".format(param, path, default)
    )
    assert artifact in default, (
        "${} in {} no longer writes {}; renaming the artifact silently breaks whoever "
        "goes looking for it: {!r}".format(param, path, artifact, default)
    )


@requires_source_tree
def test_verify_installed_keeps_its_machine_path():
    """`$InstallDir` names where the INSTALLER puts the app. It is not a repo path and must
    survive any sweep of hardcoded paths -- deriving it from $PSScriptRoot would point the
    script at the checkout and break it outright."""
    default = param_default(read_script(VERIFY), "InstallDir", VERIFY)
    assert "$env:LOCALAPPDATA" in default, (
        "$InstallDir must keep resolving the machine install location, not the "
        "checkout: {!r}".format(default)
    )
    assert not is_repo_relative(default), (
        "$InstallDir was rewritten repo-relative; the installed app does not live in the "
        "checkout: {!r}".format(default)
    )


# --- the guard that makes the next move safe ----------------------------------------------

@requires_source_tree
def test_no_cockpit_powershell_script_names_a_checkout_directory():
    """The repo-wide half of this unit: a NEW script, or a later edit to an existing one,
    must not reintroduce a hardcoded checkout path anywhere under `cockpit/`."""
    scripts = sorted(REPO.glob(COCKPIT_PS1_GLOB))
    assert scripts, (
        "found no PowerShell scripts under {} -- the glob {!r} is broken, so this guard "
        "is vacuous".format(TOOLS.parent, COCKPIT_PS1_GLOB)
    )
    offenders = []
    for script in scripts:
        for lineno, hits, line in scan_for_orphan_paths(script.read_text(encoding="utf-8")):
            offenders.append(
                "{}:{} {} -- {}".format(script.relative_to(REPO).as_posix(), lineno, hits, line)
            )
    assert offenders == [], (
        "these lines hardcode a checkout directory; derive it from $PSScriptRoot the way "
        "cockpit/scripts/stage-engine.ps1:36 does:\n  " + "\n  ".join(offenders)
    )


# --- both-states: prove the detectors fire on the known-BROKEN state ----------------------

# The exact pre-fix literals, quoted from drive_installed.ps1:16 and verify_installed.ps1:11
# as they stood on 2026-08-13. If a detector ever stops flagging these, it has been broken and
# every green above is meaningless.
PRE_FIX_DEFAULTS = (
    r'"$env:USERPROFILE\.local\opt\ai-sessions-render\.audit\installed.png"',
    r'"$env:USERPROFILE\.local\opt\ai-sessions-render\.audit\installed-app.png"',
)


@pytest.mark.parametrize("broken", PRE_FIX_DEFAULTS)
def test_orphan_detector_still_fires_on_the_pre_fix_default(broken):
    hits = orphan_hits(broken)
    assert r"\.local[\\/]+opt" in hits, (
        "the .local/opt matcher no longer fires on the literal that motivated this "
        "file: {!r}".format(broken)
    )
    assert r"[\\/]ai-sessions-render[\\/]" in hits, (
        "the checkout-name matcher no longer fires on the literal that motivated this "
        "file: {!r}".format(broken)
    )


def test_comment_exemption_does_not_blind_the_guard_to_real_code():
    """The sharpest both-states case for the exemption added above: the SAME orphan path on
    two adjacent lines, once as a line comment and once as an executable default. Exactly the
    executable one must be reported -- a guard that returns 0 here has been silenced, and one
    that returns 2 is back to forcing the explanation out of the file."""
    script = "\n".join([
        "param(",
        r"  # the old default was $env:USERPROFILE\.local\opt\ai-sessions-render\.audit\x.png",
        r'  [string]$Shot = "$env:USERPROFILE\.local\opt\ai-sessions-render\.audit\x.png"',
        ")",
    ])
    offenders = scan_for_orphan_paths(script)
    assert [lineno for lineno, _hits, _line in offenders] == [3], (
        "expected only the executable line 3 to be flagged, got {!r}".format(offenders)
    )


def test_block_comments_are_exempt_but_the_code_after_them_is_not():
    """`<# ... #>` spans are exempt on the same use-vs-mention grounds, including the
    single-line form. Code following the span is still scanned."""
    spanning = "\n".join([
        BLOCK_OPEN,
        "  the old default lived under ~/.local/opt/ai-sessions-render",
        BLOCK_CLOSE,
        r"$p = 'C:/ai-sessions-render/.audit'",
    ])
    assert [lineno for lineno, _h, _l in scan_for_orphan_paths(spanning)] == [4], (
        "a multi-line block comment must be skipped and the code under it must not be"
    )
    one_liner = "{} old: ~/.local/opt/ai-sessions-render {}".format(BLOCK_OPEN, BLOCK_CLOSE)
    assert scan_for_orphan_paths(one_liner) == [], (
        "a single-line <# ... #> comment must be skipped without opening a span that "
        "swallows the rest of the file"
    )


def test_a_trailing_comment_after_code_is_still_scanned():
    """The exemption is deliberately narrow: only a FULL-line comment is skipped, so an
    orphan path smuggled onto a code line behind a `#` is still caught."""
    assert [lineno for lineno, _h, _l in scan_for_orphan_paths(
        r"$p = $default  # fallback: $env:USERPROFILE\.local\opt\ai-sessions-render"
    )] == [1]


@pytest.mark.parametrize("broken", PRE_FIX_DEFAULTS)
def test_repo_relative_predicate_rejects_the_pre_fix_default(broken):
    assert not is_repo_relative(broken), (
        "is_repo_relative() accepts the hardcoded pre-fix default, so it cannot "
        "distinguish the fix from the bug: {!r}".format(broken)
    )


def test_param_default_raises_on_a_missing_parameter():
    """A renamed parameter must fail loudly, never skip silently."""
    with pytest.raises(AssertionError, match=r"has no \$Nope parameter"):
        param_default("param(\n  [string]$Shot = 'x'\n)", "Nope", DRIVE)


def test_param_default_raises_on_a_reflowed_default():
    """A default reflowed across lines must fail loudly rather than be truncated into a
    fragment that trivially satisfies every negative assertion above."""
    with pytest.raises(AssertionError, match="cannot capture whole"):
        param_default("param(\n  [string]$Shot = (Join-Path $PSScriptRoot\n  '..')\n)", "Shot", DRIVE)


def test_read_script_raises_when_the_param_block_is_gone(tmp_path):
    stub = tmp_path / "no_params.ps1"
    stub.write_text("Write-Output 'hi'\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="no longer has a param"):
        read_script(stub)


def test_read_script_raises_when_the_script_is_missing(tmp_path):
    with pytest.raises(AssertionError, match="is missing"):
        read_script(tmp_path / "absent.ps1")
