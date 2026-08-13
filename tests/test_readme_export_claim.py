"""Doc guards for the ONE capability claim that a green gate could not see.

WHAT WAS BROKEN, MEASURED ON THE TREE AT `624c9da`
--------------------------------------------------
`624c9da` propagated the six downloaded-export fields across the TypeScript wire
contract (`cockpit/src/ipc/types.ts:617,627,629,631` declare `chatgpt_export`,
`claude_export`, `codex_export`, `gemini_export`), so the cockpit can name a
downloaded export in `corpus.build`. Two README sentences went on denying it:

* `README.md:50` -- "the cockpit's own import (`corpus.build`) currently accepts only
  the three session ROOTS ... That is a gap in the app, not in the engine."
* `README.md:125` -- "The cockpit's own `corpus.build` is the one narrower door: it
  accepts the three session ROOTS, so exports are ingested with the CLI."

and `CHANGELOG.md`'s `[Unreleased]` section still read "Nothing yet" after 2,182
insertions across 20 files had landed.

WHY NO GATE CAUGHT IT
---------------------
Nothing in `tests/` or `cockpit/` referenced either sentence -- a repo-wide search for
"gap in the app" returned exactly one hit, the README line itself. `test_citation_anchors`
scans `llm_anthology/**/*.py` and its `TS_FILES` map plus `llm_anthology`/`cockpit/src`
for `lib.rs:` citations; it reads neither `README.md` nor `CHANGELOG.md`. So the prose
could contradict the shipped contract indefinitely while every suite stayed green. That
is the hole this file closes: the claim now has a test, so the next stale string fails
CI instead of riding a green gate.

DETECTOR DISCIPLINE
-------------------
Most assertions here are ABSENCE claims, and an absence measured with a broken matcher
is a vacuous green. Two defences, both mandatory:

1. `read_doc` RAISES when the file is missing or has lost a structural anchor, so a
   restructured or truncated document fails loudly instead of trivially satisfying
   every "X is not present" assertion.
2. The both-states rule: every detector is re-fed the deliberately-deleted text and
   must FIRE on it. A probe that is silent in the known-BROKEN state measures nothing,
   so `test_*_detector_fires_on_the_known_broken_state` are not redundant with the
   forward assertions -- they are what makes the forward assertions mean anything.

`CHANGELOG.md` is absent from an unpacked sdist (`pyproject.toml:90-98` allows only
`/llm_anthology`, `/tests`, `/README.md`, `/LICENSE`, `/SECURITY.md`, `/ARCHITECTURE.md`),
so its tests carry a source-tree guard. `README.md` IS shipped (`pyproject.toml:94`) and
its tests therefore run everywhere, unguarded, deliberately.
"""
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

README = REPO / "README.md"
CHANGELOG = REPO / "CHANGELOG.md"

# Present in a git checkout, absent from an unpacked sdist. Deliberately NOT one of the
# files under test: using CHANGELOG.md itself as the marker would turn "somebody deleted
# CHANGELOG.md" into a skip instead of a failure.
SOURCE_TREE_MARKER = REPO / ".github" / "workflows" / "release.yml"

requires_source_tree = pytest.mark.skipif(
    not SOURCE_TREE_MARKER.is_file(),
    reason="not a source checkout ({} is absent, so this is an unpacked sdist and "
           "CHANGELOG.md is legitimately missing)".format(SOURCE_TREE_MARKER),
)

# Structural anchors that must survive any legitimate edit. If one disappears the
# document was restructured and every absence assertion below has to be re-derived by a
# human -- so the reader RAISES rather than handing back text nobody has vetted.
ANCHORS = {
    "README.md": ("## What it does", "### Ingest", "## Architecture"),
    "CHANGELOG.md": ("# Changelog", "## [Unreleased]", "## [0.1.1]"),
}


def read_doc(path):
    """Return `path`'s text, or RAISE if it is missing or lost a structural anchor."""
    if not path.is_file():
        raise AssertionError("%s does not exist, so nothing below measured anything"
                             % path)
    text = path.read_text(encoding="utf-8")
    missing = [a for a in ANCHORS[path.name] if a not in text]
    if missing:
        raise AssertionError(
            "%s lost structural anchor(s) %r -- it was restructured, so every absence "
            "assertion in this file would pass vacuously. Re-derive them by hand."
            % (path.name, missing))
    return text


# --------------------------------------------------------------------------- README

#: The exact wording `624c9da` made false. Each is a SUBSTRING of one shipped sentence,
#: so a reworded-but-still-wrong sentence is caught by the positive assertions below
#: rather than by these.
STALE_README_FRAGMENTS = (
    "gap in the app",
    "currently accepts only the three session ROOTS",
    "it accepts the three session ROOTS",
)

#: The four PRIMARY export parameters the cockpit can now name. Underscored wire keys,
#: not the `--chatgpt-export` CLI spellings the README already carried at line 46 --
#: naming the wire key is the whole point, because the wire is where the gap was.
PRIMARY_EXPORT_KEYS = (
    "chatgpt_export",
    "claude_export",
    "codex_export",
    "gemini_export",
)

#: The two companion paths v1 deliberately does NOT send, and the error code that is the
#: reason. Documenting the omission is what stops a later reader "fixing" it back.
OMITTED_COMPANIONS = ("chatgpt_projects", "gemini_harvest")
REFUSAL_CODE = "-32602"


def stale_fragments_in(text):
    """-> the stale README fragments present in `text`. Empty tuple means clean."""
    return tuple(f for f in STALE_README_FRAGMENTS if f in text)


def missing_export_keys_in(text):
    """-> the primary export wire keys ABSENT from `text`. Empty tuple means all named."""
    return tuple(k for k in PRIMARY_EXPORT_KEYS if k not in text)


def test_readme_no_longer_denies_the_export_import():
    """The two sentences that contradicted the shipped wire contract are gone."""
    found = stale_fragments_in(read_doc(README))
    assert found == (), (
        "README.md still carries %r. `cockpit/src/ipc/types.ts` declares "
        "chatgpt_export / claude_export / codex_export / gemini_export on BuildParams, "
        "so the cockpit CAN name a downloaded export and the README says it cannot."
        % (found,))


def test_readme_names_the_four_export_parameters_the_app_accepts():
    """Deleting the denial is not enough -- the README must say what is true instead."""
    missing = missing_export_keys_in(read_doc(README))
    assert missing == (), (
        "README.md does not name %r. Removing a false sentence without stating the "
        "capability leaves the reader exactly as misinformed." % (missing,))


def test_readme_records_why_the_two_companion_paths_are_omitted():
    """`chatgpt_projects` / `gemini_harvest` are omitted ON PURPOSE, with a measured reason.

    Each alone is refused -32602 because `specs` is keyed on the primary
    `<provider>_export` (`llm_anthology/sidecar.py:996-998`), so a caller sending only a
    companion has named no source at all. Unrecorded, that reads as an oversight and the
    next contributor "completes" the set and ships a refusal the user cannot act on.
    """
    text = read_doc(README)
    missing = [t for t in OMITTED_COMPANIONS + (REFUSAL_CODE,) if t not in text]
    assert missing == [], (
        "README.md does not record %r -- the deliberate v1 omission reads as an "
        "oversight without it." % (missing,))


def test_readme_export_claim_is_stated_once_per_section():
    """Both stale copies were fixed, not just the one the finding happened to name.

    The denial shipped TWICE -- `README.md:50` in Ingest and `README.md:125` in
    Architecture. Fixing only the cited line would have left the other contradicting it,
    which is the same SSOT split in miniature.
    """
    text = read_doc(README)
    ingest = text.index("### Ingest")
    architecture = text.index("## Architecture")
    for name, body in (("Ingest", text[ingest:architecture]),
                       ("Architecture", text[architecture:])):
        assert stale_fragments_in(body) == (), (
            "the %s section still denies the export import" % name)
        assert "corpus.build" in body, (
            "the %s section no longer discusses `corpus.build` at all -- this test's "
            "section split is stale and is measuring the wrong text" % name)


def test_readme_stale_detector_fires_on_the_known_broken_state():
    """BOTH-STATES: the detector must FIRE on the text that was actually shipped.

    Without this, `stale_fragments_in` returning `()` proves only that the function ran.
    """
    shipped_before = (
        "**One asymmetry to know about:** the cockpit's own import (`corpus.build`) "
        "currently accepts only the three session ROOTS. To pull a downloaded export "
        "into the corpus, build the index with the CLI and point the app at it. That is "
        "a gap in the app, not in the engine."
    )
    assert stale_fragments_in(shipped_before) != (), (
        "the stale-fragment detector is silent on the exact sentence it exists to "
        "catch, so its silence on the live README means nothing")
    assert missing_export_keys_in(shipped_before) == PRIMARY_EXPORT_KEYS, (
        "the export-key detector claims the pre-fix sentence already named the wire "
        "keys, so it cannot tell the fixed state from the broken one")


def test_readme_anchor_guard_raises_on_a_restructured_document(tmp_path):
    """BOTH-STATES for `read_doc`: a gutted README must RAISE, not read as clean."""
    gutted = tmp_path / "README.md"
    gutted.write_text("# LLM Anthology\n\nnothing else\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="lost structural anchor"):
        read_doc(gutted)

    absent = tmp_path / "nope" / "README.md"
    with pytest.raises(AssertionError, match="does not exist"):
        read_doc(absent)


# ------------------------------------------------------------------------ CHANGELOG

#: The six batch-1 work units `624c9da` landed. Named in the entry so the changelog is
#: traceable to `.scratch/perfect/GAP-INVENTORY.md` rather than being prose.
LANDED_UNITS = ("U-CA1", "U-CB1", "U-SEC1", "U-SEC2", "U-GATE1", "U-SSOT1")

EMPTY_UNRELEASED = "Nothing yet."


def unreleased_section(text):
    """-> the body of `## [Unreleased]`, exclusive of the next `## ` heading."""
    start = text.index("## [Unreleased]") + len("## [Unreleased]")
    rest = text[start:]
    end = rest.index("\n## ")
    return rest[:end].strip()


@requires_source_tree
def test_changelog_unreleased_is_no_longer_empty():
    """2,182 insertions across 20 files landed while this said "Nothing yet."."""
    body = unreleased_section(read_doc(CHANGELOG))
    assert EMPTY_UNRELEASED not in body, (
        "CHANGELOG.md [Unreleased] still says %r while 624c9da is on the branch"
        % EMPTY_UNRELEASED)
    assert len(body) > 200, (
        "CHANGELOG.md [Unreleased] is %d chars -- too short to describe six work units"
        % len(body))


@requires_source_tree
def test_changelog_unreleased_names_every_landed_unit():
    """Each unit is traceable to its finding, so a reader can audit the claim."""
    body = unreleased_section(read_doc(CHANGELOG))
    missing = [u for u in LANDED_UNITS if u not in body]
    assert missing == [], (
        "CHANGELOG.md [Unreleased] does not name %r -- the entry is not traceable to "
        "the units it describes." % (missing,))


@requires_source_tree
def test_changelog_unreleased_detector_fires_on_the_known_broken_state():
    """BOTH-STATES: the section reader must return the EMPTY body from the old file."""
    shipped_before = (
        "# Changelog\n\n## [Unreleased]\n\nNothing yet.\n\n## [0.1.1] - 2026-08-12\n\nx\n"
    )
    body = unreleased_section(shipped_before)
    assert body == EMPTY_UNRELEASED, (
        "the [Unreleased] reader returned %r for the exact pre-fix file, so it is not "
        "reading the section it claims to" % body)
    assert [u for u in LANDED_UNITS if u not in body] == list(LANDED_UNITS), (
        "the unit detector found a unit id in the empty pre-fix section, so it cannot "
        "distinguish the fixed state from the broken one")
