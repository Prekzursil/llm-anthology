"""The per-group cap must not silently eat part of ONE export.

WHAT THIS PINS, AND WHY IT IS A SEPARATE FILE FROM `test_discover.py`.

A large ChatGPT history does not download as one file. It arrives as
`conversations-000.json` ... `conversations-016.json` in a single directory, and the
scanner reports each of those as its own `Finding` — deliberately, because a row per file
is what lets an operator see that an export contributed 16 of its 17 pieces rather than
having the shortfall rounded away into one opaque total.

The cap then undid exactly that. It is a cap on FINDINGS per `(provider, kind)`, and it
knew nothing about which findings belong to the same export, so it spent the budget
newest-first across the whole group and cut wherever the twenty-fifth row happened to
land — through the middle of an export set. MEASURED against the shipped scanner before
this file existed, over the real machine's shape (one export plus a duplicate copy, 17
shards each, the default cap of 25):

    dir ChatGPT       offered 12 of 17   missing 000 001 002 003 004
    dir ChatGPT copy  offered 13 of 17   missing 000 001 002 003
    stats.truncated_groups = ('chatgpt/export_file',)

Both halves of that are the defect. An operator who imports what is offered gets an
export missing its own oldest conversations, and the only signal that anything went
missing is a bare group NAME with no numbers on it — it cannot say 17 of 34, and it
cannot say which export came up short.

SCOPE OF THE CLAIM, MEASURED BOTH WAYS. A single 17-shard export on its own is NOT
affected: 17 is under the cap of 25, all seventeen are offered, and nothing is recorded
as truncated. `test_one_export_under_the_cap_is_untouched_and_unannotated` holds that
state, so the fix cannot be read as claiming a lone export was ever broken. The defect
needs the group to overflow, which needs more than one export set.

WHAT THE FIX OWES. Two things, and they are independent:
  * GROUPED — the cap packs whole export SETS (findings sharing a provider, a kind and a
    parent directory) so a set that fits is never cut in half.
  * LOUD — when the cap does bite, every surviving finding carries the arithmetic:
    `cap_group_found`/`cap_group_offered` for the whole group and
    `cap_set_found`/`cap_set_offered` for its own export. A set that genuinely cannot fit
    is still truncated, because the alternative is offering nothing, but then it SAYS so.
Those keys appear only on a group the cap actually touched, so their presence is itself
the signal and an ordinary scan's payload is unchanged.
"""
import os

import pytest

from llm_anthology import discover


# --------------------------------------------------------------------- helpers

@pytest.fixture
def downloads(tmp_path):
    """An empty user landing directory, and the only root any scan here is given."""
    d = str(tmp_path / "Downloads")
    os.makedirs(d, exist_ok=True)
    return d


def _export(base, name, shards, first_mtime, step=2):
    """One ChatGPT export directory: the `chat.html` sibling plus N shards.

    Shard 000 is the OLDEST, each `step` seconds newer than the last — the real export
    writes its pieces across a ~22-second window, so mtime order is write order.
    """
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "chat.html"), "wb") as fh:
        fh.write(b"<html></html>")
    for i in range(shards):
        p = os.path.join(d, "conversations-%03d.json" % i)
        with open(p, "wb") as fh:
            fh.write(b"[]")
        os.utime(p, (first_mtime + step * i, first_mtime + step * i))
    return d


def _scan(dl, **kw):
    return discover.discover(discover.Roots(user_dirs=(dl,)), **kw)


def _chatgpt(dl, **kw):
    result = _scan(dl, **kw)
    return result, [f for f in result.findings if f.provider == "chatgpt"]


def _per_dir(findings):
    """{directory basename: [shard basenames offered from it]}."""
    out = {}
    for f in findings:
        out.setdefault(os.path.basename(os.path.dirname(f.path)), []).append(
            os.path.basename(f.path))
    return {k: sorted(v) for k, v in out.items()}


def _shards(numbers):
    return ["conversations-%03d.json" % i for i in numbers]


# ------------------------------------------------------- GROUPED: never a partial set

def test_the_cap_never_cuts_through_an_export_that_could_have_fitted_whole(downloads):
    """The measured defect. Two 17-shard exports, cap 25: one must survive INTACT."""
    _export(downloads, "ChatGPT copy", 17, 1_700_000_000)        # older
    _export(downloads, "ChatGPT", 17, 1_700_001_000)             # newer
    _result, hits = _chatgpt(downloads)

    # Nothing partial: a directory that appears at all appears in full.
    assert _per_dir(hits) == {"ChatGPT": _shards(range(17))}
    assert len(hits) == 17


def test_a_smaller_older_export_still_fits_beside_the_newest_one(downloads):
    """Packing continues past a set that does not fit — 10 + (20 skipped) + 5 = 15."""
    _export(downloads, "oldest-5", 5, 1_700_000_000)
    _export(downloads, "middle-20", 20, 1_700_001_000)
    _export(downloads, "newest-10", 10, 1_700_002_000)
    _result, hits = _chatgpt(downloads)

    per_dir = _per_dir(hits)
    assert sorted(per_dir) == ["newest-10", "oldest-5"]
    assert per_dir["newest-10"] == _shards(range(10))
    assert per_dir["oldest-5"] == _shards(range(5))
    assert len(hits) == 15


def test_a_whole_export_that_cannot_fit_is_dropped_whole_not_shaved(downloads):
    """20 + 20 against a cap of 25: the newest survives, the other leaves nothing behind."""
    _export(downloads, "older-20", 20, 1_700_000_000)
    _export(downloads, "newer-20", 20, 1_700_001_000)
    _result, hits = _chatgpt(downloads)

    assert _per_dir(hits) == {"newer-20": _shards(range(20))}


def test_one_export_bigger_than_the_cap_is_still_offered_rather_than_hidden(downloads):
    """A 30-shard export against a cap of 25 IS cut — offering nothing would be worse —
    and the newest 25 are the ones kept."""
    _export(downloads, "huge", 30, 1_700_000_000)
    result, hits = _chatgpt(downloads)

    assert _per_dir(hits) == {"huge": _shards(range(5, 30))}
    assert result.stats.truncated_groups == ("chatgpt/export_file",)


def test_findings_that_are_one_per_directory_are_capped_exactly_as_before(downloads):
    """Every singleton is its own set, so the newest-first cap is unchanged for them."""
    for i in range(8):
        d = os.path.join(downloads, "d%d" % i)
        os.makedirs(d)
        p = os.path.join(d, "codex.json")
        with open(p, "wb") as fh:
            fh.write(b"[]")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
    result = _scan(downloads, max_per_group=3)
    hits = [f for f in result.findings if f.provider == "codex"]

    assert sorted(_per_dir(hits)) == ["d5", "d6", "d7"]
    assert result.stats.truncated_groups == ("codex/export_file",)


# ------------------------------------------------------------ LOUD: say the numbers

def test_a_capped_group_carries_the_arithmetic_of_what_it_dropped(downloads):
    """17 of 34 offered, and each surviving row knows its own export is complete."""
    _export(downloads, "ChatGPT copy", 17, 1_700_000_000)
    _export(downloads, "ChatGPT", 17, 1_700_001_000)
    _result, hits = _chatgpt(downloads)

    assert {f.detail["cap_group_found"] for f in hits} == {34}
    assert {f.detail["cap_group_offered"] for f in hits} == {17}
    assert {f.detail["cap_set_found"] for f in hits} == {17}
    assert {f.detail["cap_set_offered"] for f in hits} == {17}


def test_an_export_that_contributed_less_than_all_of_itself_says_which_fraction(downloads):
    """The per-export promise made machine-readable: 25 of this export's 30 are offered."""
    _export(downloads, "huge", 30, 1_700_000_000)
    _result, hits = _chatgpt(downloads)

    assert {f.detail["cap_set_found"] for f in hits} == {30}
    assert {f.detail["cap_set_offered"] for f in hits} == {25}
    assert {f.detail["cap_group_found"] for f in hits} == {30}
    assert {f.detail["cap_group_offered"] for f in hits} == {25}


def test_the_bookkeeping_survives_the_json_projection(downloads):
    """`as_dict` is what crosses the RPC wire, so the numbers must be there too."""
    _export(downloads, "a", 20, 1_700_000_000)
    _export(downloads, "b", 20, 1_700_001_000)
    result, _hits = _chatgpt(downloads)

    rows = [f for f in result.as_dict()["findings"] if f["provider"] == "chatgpt"]
    assert rows and all(r["detail"]["cap_group_found"] == 40 for r in rows)
    assert all(r["detail"]["cap_group_offered"] == 20 for r in rows)


def test_one_export_under_the_cap_is_untouched_and_unannotated(downloads):
    """SCOPE CONTROL. A lone 17-shard export was never affected and must stay that way:
    all 17 offered, nothing recorded as truncated, and no cap bookkeeping invented."""
    _export(downloads, "ChatGPT", 17, 1_700_000_000)
    result, hits = _chatgpt(downloads)

    assert _per_dir(hits) == {"ChatGPT": _shards(range(17))}
    assert result.stats.truncated_groups == ()
    assert all(not [k for k in f.detail if k.startswith("cap_")] for f in hits)
