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


# --------------------------------------------------------- split_records()

def test_split_records_is_a_lossless_inverse_of_join():
    """`b"".join(split_records(raw)) == raw` for every shape a rollout file takes:
    empty, single line, trailing newline, no trailing newline, a blank line, and
    a body with no separator at all. This is what lets re-encode reconstruct the
    source byte-for-byte."""
    for raw in (b"", b"a\n", b"a\nb\n", b"a\nb", b"\n", b"a\n\nb\n", b"no-sep"):
        assert b"".join(archive.split_records(raw)) == raw
    assert archive.split_records(b"") == []                       # `not raw` true
    assert archive.split_records(b"a\nb\n") == [b"a\n", b"b\n"]   # trailing-sep end
    assert archive.split_records(b"a\nb") == [b"a\n", b"b"]       # no-trailing-sep end


def test_split_records_honours_a_custom_separator():
    assert archive.split_records(b"a|b|", b"|") == [b"a|", b"b|"]


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


# ---------------------------------------------------- reencode() flow

def test_reencode_a_plain_zst_into_a_seekable_archive(tmp_path):
    """End to end: a plain single-frame .zst rollout is re-encoded so each JSONL
    record becomes an independently openable frame, the source is reconstructed
    byte-for-byte, and a single record opens without inflating the whole file."""
    raw = b'{"turn":0}\n{"turn":1}\n{"turn":2}\n'          # synthetic JSONL rollout
    src = tmp_path / "rollout.jsonl.zst"
    src.write_bytes(zstandard.ZstdCompressor(level=5).compress(raw))
    dst = tmp_path / "rollout.seekable.zst"

    count = archive.reencode(str(src), str(dst))

    assert count == 3
    reader = archive.SeekableReader.open(str(dst))
    assert b"".join(reader) == raw                          # lossless round-trip
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


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
