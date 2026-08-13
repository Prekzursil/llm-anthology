"""archive.py — seekable-zstd re-encoding for rollout archives.

SYNTHETIC fixtures ONLY. The real corpus is PRIVATE medical/pharma data; every
byte here is a made-up rollout-shaped record (newline-delimited JSON-ish text),
never a real conversation. The adapter moves opaque bytes between compression
formats and reads only sizes/offsets — it never inspects content.

What is pinned, and WHY each test exists:

  * the ENCODING contract — every record becomes its OWN independently-compressed
    zstd frame, so one record decompresses without inflating the whole archive,
    and a trailing skippable frame carries the seek table (compressed +
    decompressed size per frame). Because each frame is a complete standard zstd
    frame and the seek table lives in a *skippable* frame, a stock zstd decoder
    reads the archive as an ordinary multi-frame stream and skips the table —
    the backward-compatibility guarantee tested below.

  * the SEEK-READ contract — the CORE property: a random-access read by byte
    offset equals the same slice of the fully-decompressed content, proven over
    every (offset, length) pair on a multi-frame archive. That is the whole point
    of the format: `reader.read(off, n) == full_decompress()[off:off+n]`.

  * the READER robustness contract — a too-small blob, a non-seekable blob, a
    seek table that overruns the archive, a corrupt skippable header, and a
    checksummed seek table produced by the reference tool (whose per-entry
    checksum this reader skips over) are each a real, exercised path, not a
    pragma.
"""
import io
import struct

import pytest
import zstandard

from llm_anthology import archive


# --------------------------------------------------------------------- helpers

def _mixed_records():
    """A multi-frame corpus with varied record lengths so byte ranges land both
    inside a single frame and straddling frame boundaries."""
    return [b"alpha\n", b"br\n", b"gammagamma\n", b"d\n", b"epsilon-tail"]


# ----------------------------------------------------- encode_records()

def test_each_frame_decompresses_to_its_record_independently():
    """The defining property: frame N is a complete standalone zstd frame, so it
    opens with a plain one-shot decompress and equals record N — no need to
    inflate the rest of the archive."""
    records = [b"aaa\n", b"bbbb\n", b"c\n"]
    reader = archive.SeekableReader(archive.encode_records(records))
    assert len(reader) == 3
    for i, record in enumerate(records):
        assert reader.frame(i) == record
    assert reader.frame(-1) == records[-1]          # negative index resolves


def test_encode_accepts_both_str_and_bytes_records():
    """str records are UTF-8 encoded; bytes records pass through unchanged."""
    reader = archive.SeekableReader(archive.encode_records(["ström-s\n", b"bytes-b\n"]))
    assert reader.frame(0) == "ström-s\n".encode("utf-8")   # isinstance(str) branch
    assert reader.frame(1) == b"bytes-b\n"                  # bytes branch


def test_a_stock_zstd_decoder_reads_the_whole_seekable_archive():
    """Backward compatibility: the archive is a valid multi-frame zstd stream and
    the seek table is a *skippable* frame, so a plain decoder yields exactly the
    concatenated records and silently skips the table."""
    records = _mixed_records()
    data = archive.encode_records(records)
    whole = zstandard.ZstdDecompressor().stream_reader(
        io.BytesIO(data), read_across_frames=True).read()
    assert whole == b"".join(records)


# ---------------------------------------------- seek-read core property

def test_read_equals_full_decompress_over_every_range():
    """THE contract: a seek-read by (offset, length) equals the same slice of the
    fully decompressed content, for every offset/length on a multi-frame archive —
    including empty ranges, ranges inside one frame, ranges straddling frames, and
    ranges that run past the end. Exercises the skip (`continue`), stop (`break`),
    and partial-chunk paths of read()."""
    records = _mixed_records()
    reader = archive.SeekableReader(archive.encode_records(records))
    full = b"".join(records)
    assert reader.decompressed_size == len(full)
    n = len(full)
    for offset in range(n + 2):
        for length in range(n + 2):
            assert reader.read(offset, length) == full[offset:offset + length]


def test_read_rejects_negative_offset_or_length():
    reader = archive.SeekableReader(archive.encode_records([b"x\n"]))
    with pytest.raises(ValueError):
        reader.read(-1, 1)          # offset < 0
    with pytest.raises(ValueError):
        reader.read(0, -1)          # length < 0


def test_frame_index_out_of_range_raises():
    reader = archive.SeekableReader(archive.encode_records([b"only\n"]))
    with pytest.raises(IndexError):
        reader.frame(1)             # index >= count
    with pytest.raises(IndexError):
        reader.frame(-2)            # index + count still < 0


# ------------------------------------------------------ empty archive

def test_empty_archive_round_trips_as_zero_frames():
    """Re-encoding an empty source yields a valid archive that is just the seek
    table (zero entries); the reader reports it as empty and every accessor copes."""
    reader = archive.SeekableReader(archive.encode_records([]))
    assert len(reader) == 0
    assert reader.decompressed_size == 0
    assert list(reader) == []
    assert reader.read(0, 5) == b""
    with pytest.raises(IndexError):
        reader.frame(0)


# ---------------------------------------------------- whole-archive round trip

def test_a_record_list_round_trips_through_an_archive_byte_for_byte(tmp_path):
    """End to end over the surface that actually ships: a JSONL-shaped record list is
    encoded, written to disk, read back and each record opens independently, with the
    concatenation equal to the original bytes.

    This REPLACES `test_reencode_a_plain_zst_into_a_seekable_archive`, which drove the same
    property through the deleted disk-to-disk converter. The property is kept, the dead
    entry point is not — the same trade the G-17 deletion made when it moved
    `index.posting_count`'s invariant into a local test helper."""
    records = [b'{"turn":0}\n', b'{"turn":1}\n', b'{"turn":2}\n']
    dst = tmp_path / "rollout.seekable.zst"
    dst.write_bytes(archive.encode_records(records))

    reader = archive.SeekableReader(dst.read_bytes())

    assert len(reader) == 3
    assert b"".join(reader) == b"".join(records)            # lossless round-trip
    assert reader.frame(1) == b'{"turn":1}\n'               # random single record


# ----------------------------------------------- reader robustness

def test_reader_rejects_data_too_small_for_a_seek_table():
    with pytest.raises(ValueError):
        archive.SeekableReader(b"tiny")


def test_reader_rejects_a_non_seekable_blob():
    with pytest.raises(ValueError):
        archive.SeekableReader(b"\x00" * 20)                # footer magic mismatch


def test_reader_rejects_a_seek_table_larger_than_the_archive():
    # a footer claiming ten million frames overruns any real archive
    bad = b"\x00" * 8 + struct.pack("<IBI", 10_000_000, 0, archive._SEEKABLE_MAGIC)
    with pytest.raises(ValueError):
        archive.SeekableReader(bad)


def test_reader_rejects_a_corrupt_skippable_header():
    data = bytearray(archive.encode_records([b"x\n", b"y\n"]))
    table_size = 2 * 8 + 9                                  # entries + footer
    skip_start = len(data) - 8 - table_size
    data[skip_start:skip_start + 4] = b"\x00\x00\x00\x00"   # break the skippable magic
    with pytest.raises(ValueError):
        archive.SeekableReader(bytes(data))


def test_reader_parses_a_checksummed_seek_table_and_skips_the_checksum():
    """The reference zstd seekable tool can write a per-entry checksum (descriptor
    bit 7). This reader does not verify it (per-frame integrity rides zstd's own
    frame checksum) but MUST parse past it to locate frame boundaries, so a
    checksummed archive still opens every record."""
    records = [b"aa\n", b"bbb\n"]
    cctx = zstandard.ZstdCompressor(level=3, write_checksum=True)
    frames = [cctx.compress(r) for r in records]
    content = bytearray()
    for frame, record in zip(frames, records):
        content += struct.pack("<III", len(frame), len(record), 0xDEADBEEF)  # +checksum
    content += struct.pack("<IBI", len(records), archive._CHECKSUM_FLAG,
                           archive._SEEKABLE_MAGIC)
    skippable = struct.pack("<II", archive._SKIPPABLE_MAGIC, len(content)) + bytes(content)
    reader = archive.SeekableReader(b"".join(frames) + skippable)
    assert list(reader) == records


# --------------------------------------- U-CLEAN-DEADCODE: the re-encode closure is GONE
#
# WHY A TEST AND NOT JUST A DELETION — the reasoning `tests/test_g17_dead_code_removed.py`
# already established for `dedup.collapse_corpus` / `index.posting_count`: a deletion is not
# self-enforcing, and a function reachable only from its own test is exactly the shape that
# grows back. An absence assertion turns a re-introduction into a red build and records the
# reason, so the next author argues with the decision instead of guessing at it.
#
# WHAT WENT, AND WHY IT WAS A CLOSURE, NOT ONE FUNCTION. `reencode(source, destination)`
# read a plain `.zst` off disk, split it and wrote a seekable archive. Measured by an AST
# reference walk over every non-vendored `.py` in the repo (imports resolved absolute AND
# relative, module-attribute and bare-name loads counted, the defining `def` span excluded so
# self-delegation does not read as a use): zero production callers, one test caller. Its two
# callees then had no other caller at all — `split_records` was used ONLY at the old
# `archive.py:142` and `_decompress_all` ONLY at `:141` — and `SeekableReader.open` was
# reached ONLY from this file's `reencode` test. Deleting the head alone would have MADE
# three new orphans, so the whole dead subgraph went in one commit.
#
# WHAT SURVIVES AND WHY. `encode_records` and `SeekableReader` are the module's live
# production surface (`corpus.py:1200` stores a conversation body as a seekable archive;
# `:1222` and `:1250` read one back), and `SeekTableEntry` is used by three internal helpers
# outside the deleted span, so it is NOT an orphan — an earlier review that listed it
# alongside `split_records` was measuring text, not references.

def test_archive_no_longer_exposes_reencode():
    """The plain-`.zst` -> seekable re-encoder. Nothing in `corpus`, `loaders`, `sidecar`
    or `cli` ever re-encoded an archive; the corpus writes seekable frames directly through
    `encode_records`. Re-adding it means wiring a caller too."""
    assert not hasattr(archive, "reencode"), (
        "archive.reencode is back. It is only useful WITH a caller - if a production path "
        "now re-encodes a plain .zst rollout, say so and delete this assertion deliberately."
    )


def test_archive_no_longer_exposes_split_records():
    """The separator-preserving splitter, whose `b"".join(split_records(raw)) == raw`
    invariant existed to let `reencode` reconstruct its source byte-for-byte. With
    `reencode` gone the invariant has nothing to guarantee: every production caller already
    holds a record LIST and hands it straight to `encode_records`."""
    assert not hasattr(archive, "split_records"), (
        "archive.split_records is back. If a caller now needs to split a raw blob into "
        "records, wire it and delete this assertion; a test-only helper belongs in tests."
    )


def test_the_archive_module_surface_is_exactly_what_is_wired():
    """A named-absence pair only proves the two names. This asserts the WHOLE exported
    surface, so a third orphan cannot appear unremarked, and doubles as the detector control
    for the two assertions above: if the import were broken or the module empty, `hasattr`
    would be vacuously false while THIS assertion would fail loudly."""
    assert archive.__all__ == ["SeekTableEntry", "encode_records", "SeekableReader"]
    for name in archive.__all__:
        assert hasattr(archive, name), "__all__ names %s, which does not exist" % name


def test_the_archive_docstring_does_not_advertise_a_deleted_function():
    """A module docstring that still describes a removed callee is worse than the dead code
    was: the function a reader cannot find reads as a broken import. Same rule the G-17
    deletion applied to `dedup.py`."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "llm_anthology" / "archive.py").read_text(
        encoding="utf-8")
    for gone in ("reencode", "split_records", "_decompress_all"):
        assert gone not in source, (
            "archive.py still names %s. Every mention has to go with the function, or the "
            "file documents a callee that no longer exists." % gone
        )


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
