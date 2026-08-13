"""SSOT guards for the two governance docs, `AGENTS.md` and `CLAUDE.md`.

Measured on 2026-08-12, before this file existed, both halves of the defect were live.

G-1 -- THE TWO DOCS GAVE OPPOSITE ORDERS ABOUT PUSHING.
`AGENTS.md:98-115` carried a blockquote headed "ACTIVE EXCEPTION to the push rule above",
asserting "40 commits on `main` that are intentionally unpushed" and "Do not auto-push this
repo". `CLAUDE.md:332-345`, at the same moment, headed its own paragraph "The push exception
has EXPIRED -- the normal rule applies again" and said "push normally. No repo-specific
carve-out is in force." An agent that read AGENTS.md held work back; an agent that read
CLAUDE.md pushed it. The carve-out was written to expire on its own terms ("When the cockpit
CI job is merged and green, this exception expires -- delete it then"); that job exists at
`.github/workflows/ci.yml:171` and the held commits were pushed, so the AGENTS.md copy was
simply never deleted. `CLAUDE.md`'s retirement paragraph is KEPT on purpose -- a future
reader who finds the carve-out quoted in an old transcript needs to see that it was retired
deliberately rather than lost -- and `test_claude_md_keeps_the_retirement_paragraph` pins it
so a later cleanup cannot quietly remove the record.

G-2 -- THE BEADS ARCHITECTURE WAS STATED THREE TIMES.
The sentence beginning "Architecture in one line" appeared at `AGENTS.md:5` (hand-written),
`AGENTS.md:70` and `CLAUDE.md:304`, the last two inside a `bd`-generated block stamped
`<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->`. The `bd ready` quick
reference and the whole Session-Completion push checklist were duplicated along with it.

WHAT THIS FILE ENFORCES, AND WHY IN THIS DIRECTION
--------------------------------------------------
`bd` OWNS everything between its BEGIN/END BEADS INTEGRATION markers and rewrites it, so
nothing here may demand a hand-edit INSIDE those markers. Getting from three statements down
to one therefore means: delete the hand-written duplicate; keep exactly ONE whole generated
block, in `AGENTS.md` (the agent-instructions file `bd` addresses); and reduce the other
file's beads section to a one-line pointer by removing its block ENTIRELY -- markers
included -- rather than editing the text between them.

That last move makes this file a CANARY as well as a gate: if a future `bd` run re-stamps
`CLAUDE.md`, `test_claude_md_beads_section_is_a_one_line_pointer` goes red instead of the
duplication returning silently. A red there is a decision to take, not a bug to paper over.

DETECTOR DISCIPLINE
-------------------
Almost every assertion below is an ABSENCE claim, and an absence measured with a broken
matcher is a vacuous green. So: `read_governance_doc` raises unless the file exists AND still
carries its structural anchors; `beads_block_spans` raises on a torn marker pair instead of
returning `[]`; and two tests re-feed the deliberately-deleted text back through the
detectors to prove they still fire on the known-BROKEN state (the both-states rule).
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Present in a git checkout, absent from an unpacked sdist (pyproject.toml's sdist
# `include` allowlist carries only /llm_anthology, /tests, README, LICENSE, SECURITY,
# ARCHITECTURE -- so AGENTS.md and CLAUDE.md are legitimately missing there), and
# deliberately NOT one of the files under test: using AGENTS.md itself as the marker would
# turn "somebody deleted AGENTS.md" into a skip instead of a failure.
SOURCE_TREE_MARKER = REPO / ".github" / "workflows" / "release.yml"

requires_source_tree = pytest.mark.skipif(
    not SOURCE_TREE_MARKER.is_file(),
    reason="not a source checkout ({} is absent, so this is an unpacked sdist and the "
           "governance docs are legitimately missing)".format(SOURCE_TREE_MARKER),
)

AGENTS = REPO / "AGENTS.md"
CLAUDE = REPO / "CLAUDE.md"

BEGIN_BEADS = "<!-- BEGIN BEADS INTEGRATION"
END_BEADS = "<!-- END BEADS INTEGRATION -->"

# Structural anchors that must survive any legitimate edit. If one disappears, the document
# was restructured and every offset-based assertion below has to be re-derived by a human --
# so the reader RAISES rather than handing back text nobody has vetted.
ANCHORS = {
    "AGENTS.md": ("# Agent Instructions", "Non-Interactive Shell Commands"),
    "CLAUDE.md": ("# Project Instructions", "Testing"),
}

# Each of these belongs to the `bd`-generated block. Repo-wide, across the two governance
# docs, each must occur EXACTLY ONCE, and that occurrence must sit inside AGENTS.md's
# generated span. Measured counts before the fix: 3 / 3 / 2 respectively.
GENERATED_FINGERPRINTS = (
    "Architecture in one line",
    "bd ready",
    "## Session Completion",
)

# The retired carve-out, in four independent shapes: its heading, its two load-bearing
# sentences, and the blockquote-heading SHAPE it used. The shape pattern is what catches a
# reworded revival that keeps none of the original phrases.
EXCEPTION_PATTERNS = (
    re.compile(r"ACTIVE EXCEPTION", re.IGNORECASE),
    re.compile(r"do not auto-push", re.IGNORECASE),
    re.compile(r"intentionally unpushed", re.IGNORECASE),
    re.compile(r"(?m)^>\s*#{1,6}[^\n]*\bexception\b", re.IGNORECASE),
)

# Verbatim excerpt of AGENTS.md:98-110 as it stood on 2026-08-12. Used ONLY to prove the
# detectors above still fire on the known-BROKEN state; if this stops matching, the gate has
# gone blind and its silence means nothing.
RETIRED_EXCEPTION_BLOCK = "\n".join((
    "> ### ACTIVE EXCEPTION to the push rule above - read before pushing this repo",
    ">",
    "> The block above is a generic template appended by `bd init`. It currently contradicts a",
    "> deliberate decision about THIS repo, and the same exception is recorded in `CLAUDE.md`.",
    ">",
    "> As of 2026-08-07 there are **40 commits on `main` that are intentionally unpushed.** That is",
    "> not stranded work.",
    ">",
    "> **Precedence:** the owner's explicit decision outranks a template rule. Do not auto-push this",
    "> repo. When the cockpit CI job is merged and green, this exception expires - delete it then.",
))

# The record CLAUDE.md must keep, so the retirement stays legible to a future reader.
RETIREMENT_ANCHORS = (
    "The push exception has EXPIRED",
    "push normally",
)


# ------------------------------------------------------------------- readers

def read_governance_doc(path):
    """Return the doc's text, or raise if it is missing or has lost its anchors."""
    if not path.is_file():
        raise AssertionError("{} does not exist".format(path))
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise AssertionError("{} is empty".format(path))
    missing = [a for a in ANCHORS[path.name] if a not in text]
    if missing:
        raise AssertionError(
            "{} no longer contains its structural anchors {!r} -- the detectors in this "
            "file are keyed to that structure, so their silence would prove nothing until "
            "a human re-derives them".format(path.name, missing))
    return text


def beads_block_spans(text, where):
    """Return `[(start, end)]` per BEGIN..END BEADS block, or raise on a torn pair.

    Counting BEGIN markers alone reports a plausible number for a file whose END marker
    somebody deleted, and every "is this offset inside the generated block" question below
    would then be answered against a span running to EOF. So pair them, and refuse to guess.
    """
    begins = [m.start() for m in re.finditer(re.escape(BEGIN_BEADS), text)]
    ends = [m.end() for m in re.finditer(re.escape(END_BEADS), text)]
    if len(begins) != len(ends):
        raise AssertionError(
            "{}: {} BEGIN markers but {} END markers -- the generated block is torn, so no "
            "claim about its contents can be trusted".format(where, len(begins), len(ends)))
    spans = list(zip(begins, ends))
    if any(end <= start for start, end in spans):
        raise AssertionError("{}: an END BEADS marker precedes its BEGIN".format(where))
    return spans


def headed_sections(text):
    """Yield `(heading_line, [non-blank body lines])` for every ATX heading in `text`.

    A section ends at the next heading of the same level or higher, so a `###` subsection
    stays part of its `##` parent -- which is the level a "the body is one line" claim has
    to be measured at.
    """
    lines = text.splitlines()
    heads = [
        (i, len(m.group(1)))
        for i, line in enumerate(lines)
        for m in [re.match(r"(#{1,6})\s+\S", line)]
        if m
    ]
    for pos, (index, level) in enumerate(heads):
        end = len(lines)
        for later_index, later_level in heads[pos + 1:]:
            if later_level <= level:
                end = later_index
                break
        body = [line for line in lines[index + 1:end] if line.strip()]
        yield lines[index], body


# ------------------------------------------------------------------- G-1: one push order

@requires_source_tree
def test_agents_md_has_no_active_exception_blockquote():
    text = read_governance_doc(AGENTS)
    hits = []
    for pattern in EXCEPTION_PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            hits.append("{}:{} matched {!r}".format(
                AGENTS.name, line, pattern.pattern))
    assert not hits, (
        "AGENTS.md still carries the retired push carve-out, which contradicts CLAUDE.md's "
        "retirement paragraph:\n  " + "\n  ".join(hits))


@requires_source_tree
def test_claude_md_keeps_the_retirement_paragraph():
    text = read_governance_doc(CLAUDE)
    missing = [a for a in RETIREMENT_ANCHORS if a not in text]
    assert not missing, (
        "CLAUDE.md must keep the record that the push exception was retired deliberately; "
        "missing {!r}".format(missing))


def test_exception_detector_fires_on_the_retired_block():
    """Both-states control: silence above is only evidence if these can go loud."""
    blind = [p.pattern for p in EXCEPTION_PATTERNS
             if not p.search(RETIRED_EXCEPTION_BLOCK)]
    assert not blind, (
        "these patterns no longer match the block they were written to catch, so "
        "test_agents_md_has_no_active_exception_blockquote measures nothing: {!r}".format(
            blind))


# ------------------------------------------------------------------- G-2: one beads block

@requires_source_tree
def test_agents_md_carries_exactly_one_generated_beads_block():
    text = read_governance_doc(AGENTS)
    spans = beads_block_spans(text, AGENTS.name)
    assert len(spans) == 1, (
        "AGENTS.md must carry exactly one `bd`-generated block; found {}".format(len(spans)))


@requires_source_tree
def test_beads_body_is_stated_once_and_only_inside_the_generated_block():
    agents = read_governance_doc(AGENTS)
    claude = read_governance_doc(CLAUDE)
    (span_start, span_end), = beads_block_spans(agents, AGENTS.name)
    problems = []
    for needle in GENERATED_FINGERPRINTS:
        in_claude = claude.count(needle)
        if in_claude:
            problems.append("{!r} appears {}x in CLAUDE.md (must be 0 -- `bd` owns this "
                            "body and it is stated in AGENTS.md)".format(needle, in_claude))
        offsets = [m.start() for m in re.finditer(re.escape(needle), agents)]
        if len(offsets) != 1:
            problems.append("{!r} appears {}x in AGENTS.md (must be exactly 1)".format(
                needle, len(offsets)))
        outside = [o for o in offsets if not span_start <= o < span_end]
        if outside:
            problems.append(
                "{!r} appears at AGENTS.md offset(s) {} OUTSIDE the generated block -- a "
                "hand-written duplicate of `bd`-owned text".format(needle, outside))
    assert not problems, "\n  ".join([""] + problems)


@requires_source_tree
def test_claude_md_beads_section_is_a_one_line_pointer():
    text = read_governance_doc(CLAUDE)
    spans = beads_block_spans(text, CLAUDE.name)
    assert not spans, (
        "CLAUDE.md carries a `bd`-generated block again ({} of them). The beads body is "
        "stated once in AGENTS.md; if `bd` re-stamped this file, decide deliberately rather "
        "than letting the duplication return".format(len(spans)))
    beads_sections = [(head, body) for head, body in headed_sections(text)
                      if "beads" in head.lower()]
    assert len(beads_sections) == 1, (
        "CLAUDE.md must carry exactly one beads heading, whose body is a one-line pointer; "
        "found {}".format([h for h, _ in beads_sections]))
    head, body = beads_sections[0]
    assert len(body) == 1, (
        "CLAUDE.md's {!r} section must be a ONE-line pointer, not a second copy of the "
        "body; found {} lines".format(head, len(body)))
    assert "AGENTS.md" in body[0], (
        "CLAUDE.md's beads pointer must name the file that holds the real block; got "
        "{!r}".format(body[0])
    )


@requires_source_tree
def test_agents_md_points_at_claude_md_as_the_governance_ssot():
    text = read_governance_doc(AGENTS)
    pointers = [line for line in text.splitlines()
                if "CLAUDE.md" in line and re.search(r"governance|SSOT", line)]
    assert pointers, (
        "AGENTS.md must name CLAUDE.md as the governance SSOT, so the two docs cannot drift "
        "into giving different orders again")


def test_marker_reader_refuses_a_torn_block():
    """Detector control: a missing END must raise, never report a plausible span."""
    torn = "intro\n{} v:1 -->\nbody\n".format(BEGIN_BEADS)
    with pytest.raises(AssertionError) as caught:
        beads_block_spans(torn, "synthetic")
    assert "torn" in str(caught.value)


def test_marker_reader_counts_two_blocks_as_two():
    """Detector control: the reader must be able to SEE duplication, or parity is vacuous.

    A reader that answers "1 block" for every input would have passed
    `test_agents_md_carries_exactly_one_generated_beads_block` in the broken state too.
    """
    one = "{} v:1 -->\nbody\n{}\n".format(BEGIN_BEADS, END_BEADS)
    assert len(beads_block_spans(one + "prose\n" + one, "synthetic")) == 2


def test_anchor_reader_refuses_an_unanchored_doc(tmp_path):
    """Detector control: a doc that lost its anchors must raise, not return text."""
    fake = tmp_path / "AGENTS.md"
    fake.write_text("nothing recognisable here\n", encoding="utf-8")
    with pytest.raises(AssertionError) as caught:
        read_governance_doc(fake)
    assert "structural anchors" in str(caught.value)


def test_anchor_reader_refuses_a_missing_or_empty_doc(tmp_path):
    with pytest.raises(AssertionError) as caught:
        read_governance_doc(tmp_path / "CLAUDE.md")
    assert "does not exist" in str(caught.value)
    blank = tmp_path / "CLAUDE.md"
    blank.write_text("   \n", encoding="utf-8")
    with pytest.raises(AssertionError) as caught:
        read_governance_doc(blank)
    assert "is empty" in str(caught.value)


def test_section_reader_scopes_a_body_to_its_own_heading():
    """Detector control for `headed_sections`: subsections belong to their parent.

    If this returned only the lines up to the next heading of ANY level, a two-copy beads
    section split by a `###` would read as a one-line body and pass.
    """
    doc = "\n".join((
        "# Title", "", "## Beads", "pointer line", "", "### Sub", "extra line", "",
        "## After", "unrelated",
    ))
    sections = dict((head, body) for head, body in headed_sections(doc))
    assert sections["## Beads"] == ["pointer line", "### Sub", "extra line"]
    assert sections["## After"] == ["unrelated"]
