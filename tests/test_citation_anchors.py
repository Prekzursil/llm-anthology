"""Pin the `maintenance.py:<line>` citations in the cockpit IPC layer to real code.

WHY THIS EXISTS. `cockpit/src/ipc/mock.ts` and `types.ts` document the destructive
`maintenance.*` surface by citing exact engine lines. Those anchors DRIFT: this session added
71 lines to `llm_anthology/maintenance.py`, and an audit then found 26 of 35 citations landing
on unrelated code — four of them on blank lines. The claims were all true; only the anchors had
rotted. That is not cosmetic. A reader who follows `maintenance.py:606` to check the
confirmation gate lands in `_manifest_doc` and concludes the comment is nonsense, so the next
person re-derives by hand what the citation was supposed to save them. This test makes that
drift a red build instead of a slow decay.

WHAT IT ASSERTS. For each pinned citation: the cited line must still CONTAIN a token belonging
to the construct the claim is about. Not an exact-text match — that would break on a reword —
and not a line-number equality against a pinned constant, which is self-falsifying (an
equality-to-a-pinned-value check reports a legitimate edit as a broken detector). A token is
the cheapest thing that survives formatting and dies on a real move.

WHY IN pytest AND NOT vitest. It reads Python source; the Python rail already runs on every CI
leg; and a vitest reaching into `llm_anthology/` needs a `?raw` import that only works because
vitest ignores the Vite `fs.allow` boundary — a coupling not worth a second instance of.

NOT COVERED: citations to `sidecar.py`, `dedup.py`, `discover.py`, `metadata.py` and
`test_sidecar_maintenance.py` in these same files — same defect class, same likely drift, never
audited; absence of a pin here is not evidence they are correct.

This file adds no `llm_anthology` code, so it does not move the 100% coverage gate.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "llm_anthology" / "maintenance.py"
TS_FILES = {
    "mock.ts": REPO / "cockpit" / "src" / "ipc" / "mock.ts",
    "types.ts": REPO / "cockpit" / "src" / "ipc" / "types.ts",
}

#: A citation to the ENGINE file, excluding `tests/test_sidecar_maintenance.py:<n>` — whose
#: name ENDS IN `maintenance.py`, so a naive pattern silently grades test-file citations
#: against the engine. That over-match produced eight false readings during the audit and is
#: the reason `test_the_scraper_tells_presence_from_absence` exists.
_CITE = re.compile(r"(?<!test_sidecar_)\bmaintenance\.py:(\d+)")

#: A SECONDARY anchor in the same sentence, e.g. "`:645-648` runs ahead of `:669`". Only
#: counted on a line that already cites the engine — otherwise it also captures
#: `metadata.py:243-269, :456-463` and `discover.py:106, :785`, other engine files entirely.
_SECONDARY = re.compile(r"`:(\d+)")

#: (ts file, cited engine line, token that line must contain, what the claim is about).
#:
#: The token is chosen from the FIRST line of each cited range. Where a claim cites a range,
#: only its opening line is pinned — enough to catch a move, cheap enough to stay honest about
#: what it proves.
PINS = [
    ("mock.ts", 295, "PROTECTED_PATH_MARKERS", "the protected-store markers, ported verbatim"),
    ("mock.ts", 384, "def _spells_protected", "the protected-path matcher this ports"),
    ("mock.ts", 452, "def _effective_destination_root", "the effective destination root"),
    ("mock.ts", 463, "def _confirmation_phrase", "the phrase the operator must type"),
    ("mock.ts", 345, "def _classify", "the lexical outside-store-root half"),
    ("mock.ts", 536, "Checked last", "duplicate checked last, so protected wins"),
    ("mock.ts", 549, "warnings.append", "the blocked-target DANGEROUS warning"),
    ("mock.ts", 529, "planned_sources", "duplicate-target detection"),
    ("mock.ts", 419, "def _measured_size", "size measured from disk, not the caller"),
    ("mock.ts", 558, "warnings.append", "a DANGEROUS warning per ALLOWED target"),
    ("mock.ts", 472, "def _unique_name", "the deterministic -N suffix"),
    ("mock.ts", 457, '"deleted"', "a delete quarantines under the checkpoint root"),
    ("mock.ts", 645, "requires_typed_confirmation or not confirmation", "the confirmation gate"),
    ("mock.ts", 669, "if not apply", "the apply branch the gate precedes"),
    ("mock.ts", 670, "moves=preview.plan", "a dry run returns the planned moves"),
    ("mock.ts", 750, 'status = doc.get("status"', "restore's first check"),
    ("mock.ts", 784, "os.path.lexists(relocated)", "the per-entry restore rule"),
    ("mock.ts", 776, "seen_originals", "the duplicate-entry restore guards"),
    ("types.ts", 141, "class MaintenanceAction", "the action enum"),
    ("types.ts", 158, "class SessionStoreKind", "the store-kind enum"),
    ("types.ts", 182, "class SessionCopy", "one session file inside a plan"),
    ("types.ts", 206, "last_write_ms", "epoch ms, or null"),
    ("types.ts", 561, "is_hot", "a hot target raises a REVIEW warning"),
    ("types.ts", 558, "warnings.append", "a DANGEROUS warning per ALLOWED target"),
    ("types.ts", 576, "warnings.append", "the closing INFO summary"),
    ("types.ts", 150, "class MaintenanceWarningSeverity", "the severity enum"),
    ("types.ts", 452, "def _effective_destination_root", "the EFFECTIVE destination"),
    ("types.ts", 587, "requires_checkpoint=True", "always true"),
    ("types.ts", 588, "requires_typed_confirmation=True", "always true"),
    ("types.ts", 463, "def _confirmation_phrase", "the exact phrase, from the ALLOWED count"),
    ("types.ts", 794, "skip_unaccounted", "without it, the whole batch is refused"),
    ("types.ts", 670, 'manifest_path=""', "an empty manifest path on a dry run"),
    ("types.ts", 285, "unaccounted", "recorded originals a restore could not account for"),
    ("types.ts", 849, "def record_run", "one row of the audit ledger"),
    ("types.ts", 823, "MAINTENANCE_SCHEMA", "the ledger schema"),
    ("types.ts", 870, "ORDER BY recorded_at_ms DESC, manifest_path", "newest first, path breaks ties"),
    ("types.ts", 715, '"pending"', "the pending status"),
    ("types.ts", 720, '"executed"', "the executed status"),
    ("types.ts", 808, '"restored"', "the restored status"),
    ("types.ts", 626, "def read_checkpoint", "opens the manifest directly"),
]


def _read(path):
    """Read a source file, FAILING LOUDLY if it has moved.

    Not `pytest.skip`: a skip on a missing input is the vacuous-pass shape this whole file
    exists to prevent. If a path moved, the pin must go red so someone re-points it.
    """
    if not path.exists():
        pytest.fail(
            "cannot find %s — this pin cannot verify anything until the path is re-pointed "
            "(a skip here would be a green build proving nothing)" % path
        )
    return path.read_text(encoding="utf-8")


def _engine_lines():
    return _read(ENGINE).split("\n")


def _cited_anchors(text):
    """Every engine line number cited by `text`, primary and secondary."""
    found = set(int(n) for n in _CITE.findall(text))
    for line in text.split("\n"):
        if _CITE.search(line):
            found |= set(int(n) for n in _SECONDARY.findall(line))
    return found


# ----------------------------------------------------------------- the detector's own control

def test_the_scraper_tells_presence_from_absence():
    """The both-states control: the pattern must FIRE on a real citation and stay SILENT on
    the test-file spelling. A detector that matches both measures nothing, and this exact
    over-match produced eight false readings before it was caught."""
    assert _CITE.findall("see (`llm_anthology/maintenance.py:645-648`) for the gate") == ["645"]
    assert _CITE.findall("bare (`maintenance.py:295`)") == ["295"]
    # The trap: this filename ENDS IN `maintenance.py` but is a different file.
    assert _CITE.findall("(`tests/test_sidecar_maintenance.py:194-206`)") == []
    # A secondary anchor only counts beside a primary one.
    assert _cited_anchors("(`metadata.py:243-269`, `:456-463`)") == set()
    assert _cited_anchors("(`maintenance.py:645` ahead of `:669`)") == {645, 669}


def test_the_scrape_finds_citations_at_all():
    """Fail closed on an empty scrape.

    Deliberately `> 0` per file rather than a pinned total: an exact-count assertion is
    self-falsifying — removing one legitimate citation would report "the scrape is broken",
    sending the next reader after a phantom parser bug instead of at their own edit.
    """
    for name, path in TS_FILES.items():
        found = _cited_anchors(_read(path))
        assert found, (
            "found ZERO maintenance.py citations in %s. Either the file no longer documents "
            "the engine (then delete this pin deliberately) or the scraper broke (then fix it) "
            "— but a green build here would prove nothing." % name
        )


# ------------------------------------------------------------------------------- the pin

def test_every_pinned_citation_lands_on_its_construct():
    """Each cited engine line must still contain a token from the construct it is cited for."""
    lines = _engine_lines()
    problems = []
    for name, cited, token, about in PINS:
        if cited > len(lines):
            problems.append(
                "%s cites maintenance.py:%d for %s, but that file now has only %d lines"
                % (name, cited, about, len(lines))
            )
            continue
        actual = lines[cited - 1]
        if token not in actual:
            problems.append(
                "%s cites maintenance.py:%d for %s\n"
                "      expected that line to contain: %r\n"
                "      the line actually contains:    %r\n"
                "      -> the code moved; find %s in maintenance.py and re-point the citation"
                % (name, cited, about, token, actual.strip(), token)
            )
    assert not problems, "citation anchors have drifted:\n  " + "\n  ".join(problems)


def test_every_citation_is_pinned():
    """A citation may not be added or re-anchored without also being pinned here.

    Without this, the pin protects only the anchors that happened to be wrong once, and the
    next drift lands in whatever was never listed. Two unpinned anchors were caught this way
    while the table was being written.
    """
    pinned = {(name, cited) for name, cited, _, _ in PINS}
    unpinned = []
    for name, path in TS_FILES.items():
        for anchor in sorted(_cited_anchors(_read(path))):
            if (name, anchor) not in pinned:
                unpinned.append(
                    "%s cites maintenance.py:%d but no PINS row verifies it — add "
                    '("%s", %d, "<token on that line>", "<what the claim is about>")'
                    % (name, anchor, name, anchor)
                )
    assert not unpinned, "unverified citations:\n  " + "\n  ".join(unpinned)
