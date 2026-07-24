"""Seekable-zstd re-encoding for aisr rollout archives.

A rollout `.zst` is a single zstd frame: opening one record means inflating the
whole file. This module re-encodes such an archive into the zstd **seekable
format** so a single record opens in isolation:

  * every record becomes its OWN complete, independently-compressed zstd frame,
    concatenated in order;
  * a trailing **skippable** frame carries the seek table — one
    (compressed_size, decompressed_size) entry per frame plus a footer with the
    frame count and the Seekable_Magic_Number.

Two properties fall out of that layout. Because each frame is a standard zstd
frame and the table lives in a skippable frame, a stock zstd decoder reads the
archive as an ordinary multi-frame stream and skips the table (backward
compatible). And because the table records each frame's compressed length, a
reader can slice out exactly one frame and decompress it by itself — random
access without touching the rest — which is the whole point.

`SeekableReader.read(offset, length)` then returns any byte range of the
decompressed content, decompressing only the frames that range overlaps, and is
byte-for-byte equal to slicing the fully-decompressed archive.

Format reference: the zstd seekable format (contrib/seekable_format).

PRIVACY: this module moves opaque bytes between compression formats and reads
only sizes/offsets — it never inspects or emits record content. Tests use
synthetic fixtures only.
"""
from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence, Tuple, Union

import zstandard

__all__ = [
    "SeekTableEntry",
    "split_records",
    "encode_records",
    "reencode",
    "SeekableReader",
]

# zstd skippable frames use magic 0x184D2A50..0x184D2A5F; the seekable spec uses
# the last of these plus an inner Seekable_Magic_Number in the table footer.
_SKIPPABLE_MAGIC = 0x184D2A5E
_SEEKABLE_MAGIC = 0x8F92EAB1
_CHECKSUM_FLAG = 0x80                      # seek-table descriptor bit 7

_SKIPPABLE_HEADER = struct.Struct("<II")   # skippable_magic, frame_content_size
_FOOTER = struct.Struct("<IBI")            # num_frames, descriptor, seekable_magic
_ENTRY = struct.Struct("<II")              # compressed_size, decompressed_size
_CHECKSUM = struct.Struct("<I")            # optional per-entry checksum

_DEFAULT_LEVEL = 3


@dataclass(frozen=True)
class SeekTableEntry:
    """One seek-table row: the on-disk byte length of a frame and the length of
    the record it decompresses to."""
    compressed_size: int
    decompressed_size: int


# ------------------------------------------------------------------ splitting

def split_records(raw: bytes, separator: bytes = b"\n") -> List[bytes]:
    """Split `raw` on `separator`, KEEPING the separator on each record, so that
    ``b"".join(split_records(raw)) == raw`` for any input — the property that lets
    re-encoding reconstruct the source byte-for-byte."""
    if not raw:
        return []
    records: List[bytes] = []
    start = 0
    step = len(separator)
    while True:
        index = raw.find(separator, start)
        if index < 0:                       # no further separator: final record
            records.append(raw[start:])
            break
        end = index + step
        records.append(raw[start:end])
        start = end
        if start >= len(raw):               # source ended exactly on a separator
            break
    return records


# ------------------------------------------------------------------ encoding

def _new_compressor(level: int) -> "zstandard.ZstdCompressor":
    """A one-shot compressor that embeds each frame's content size (so a reader
    can decompress by size) and a frame checksum (per-frame integrity)."""
    return zstandard.ZstdCompressor(level=level, write_checksum=True)


def _encode_seek_table(entries: Sequence[SeekTableEntry]) -> bytes:
    """The trailing skippable frame: the per-frame entries then the footer."""
    content = bytearray()
    for entry in entries:
        content += _ENTRY.pack(entry.compressed_size, entry.decompressed_size)
    content += _FOOTER.pack(len(entries), 0, _SEEKABLE_MAGIC)
    return _SKIPPABLE_HEADER.pack(_SKIPPABLE_MAGIC, len(content)) + bytes(content)


def encode_records(records: Iterable[Union[bytes, str]], *,
                   level: int = _DEFAULT_LEVEL) -> bytes:
    """Compress each record into its own frame and append the seek table.

    str records are UTF-8 encoded; bytes records pass through unchanged.
    """
    compressor = _new_compressor(level)
    body = bytearray()
    entries: List[SeekTableEntry] = []
    for record in records:
        raw = record.encode("utf-8") if isinstance(record, str) else bytes(record)
        frame = compressor.compress(raw)
        body += frame
        entries.append(SeekTableEntry(len(frame), len(raw)))
    body += _encode_seek_table(entries)
    return bytes(body)


def _decompress_all(blob: bytes) -> bytes:
    """Fully decompress a (possibly multi-frame) zstd blob, skipping any skippable
    frame. Used to read the plain source archive during re-encoding."""
    dctx = zstandard.ZstdDecompressor()
    return dctx.stream_reader(io.BytesIO(blob), read_across_frames=True).read()


def reencode(source, destination, *, level: int = _DEFAULT_LEVEL,
             separator: bytes = b"\n") -> int:
    """Re-encode the plain `.zst` at `source` into a seekable archive at
    `destination`, one frame per `separator`-delimited record. Returns the number
    of records written."""
    raw = _decompress_all(Path(source).read_bytes())
    records = split_records(raw, separator)
    Path(destination).write_bytes(encode_records(records, level=level))
    return len(records)


# ------------------------------------------------------------------ reading

def _parse_seek_table(data: bytes) -> Tuple[List[SeekTableEntry], int]:
    """Parse the trailing seek table. Returns (entries, skippable_frame_start).

    Raises ValueError if `data` is not a well-formed seekable archive. A
    checksummed table (descriptor bit 7) is parsed but its per-entry checksum is
    skipped — frame boundaries come from the compressed sizes, and integrity
    rides zstd's own per-frame checksum.
    """
    if len(data) < _SKIPPABLE_HEADER.size + _FOOTER.size:
        raise ValueError("archive too small to hold a seekable seek table")
    num_frames, descriptor, magic = _FOOTER.unpack_from(data, len(data) - _FOOTER.size)
    if magic != _SEEKABLE_MAGIC:
        raise ValueError("not a seekable-zstd archive: seekable magic missing")
    entry_size = _ENTRY.size + (_CHECKSUM.size if descriptor & _CHECKSUM_FLAG else 0)
    table_size = num_frames * entry_size + _FOOTER.size
    table_start = len(data) - _SKIPPABLE_HEADER.size - table_size
    if table_start < 0:
        raise ValueError("declared seek table is larger than the archive")
    skippable_magic, declared = _SKIPPABLE_HEADER.unpack_from(data, table_start)
    if skippable_magic != _SKIPPABLE_MAGIC or declared != table_size:
        raise ValueError("corrupt seekable seek-table header")
    entries: List[SeekTableEntry] = []
    pos = table_start + _SKIPPABLE_HEADER.size
    for _ in range(num_frames):
        compressed_size, decompressed_size = _ENTRY.unpack_from(data, pos)
        entries.append(SeekTableEntry(compressed_size, decompressed_size))
        pos += entry_size
    return entries, table_start


class SeekableReader:
    """Random-access reader over a seekable-zstd archive: open one frame, iterate
    every frame, or read an arbitrary decompressed byte range — each touching only
    the frames it needs."""

    def __init__(self, data: bytes):
        self._data = bytes(data)
        self._entries, _ = _parse_seek_table(self._data)
        self._dctx = zstandard.ZstdDecompressor()
        compressed = 0
        decompressed = 0
        self._frame_offsets: List[int] = []
        self._decompressed_offsets: List[int] = []
        for entry in self._entries:
            self._frame_offsets.append(compressed)
            self._decompressed_offsets.append(decompressed)
            compressed += entry.compressed_size
            decompressed += entry.decompressed_size
        self._decompressed_size = decompressed

    @classmethod
    def open(cls, path) -> "SeekableReader":
        """Read a seekable archive from `path`."""
        return cls(Path(path).read_bytes())

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def decompressed_size(self) -> int:
        """Total size of the fully-decompressed content."""
        return self._decompressed_size

    def frame(self, index: int) -> bytes:
        """Decompress frame `index` (record `index`) in isolation. Supports
        negative indexing; raises IndexError out of range."""
        count = len(self._entries)
        if index < 0:
            index += count
        if index < 0 or index >= count:
            raise IndexError("frame index out of range: %d" % index)
        entry = self._entries[index]
        start = self._frame_offsets[index]
        blob = self._data[start:start + entry.compressed_size]
        return self._dctx.decompress(blob, max_output_size=entry.decompressed_size)

    def __iter__(self) -> Iterator[bytes]:
        for index in range(len(self._entries)):
            yield self.frame(index)

    def read(self, offset: int, length: int) -> bytes:
        """Return `length` decompressed bytes starting at `offset`, decompressing
        only the frames the range overlaps. Equals the same slice of the
        fully-decompressed archive."""
        if offset < 0 or length < 0:
            raise ValueError("offset and length must be non-negative")
        end = offset + length
        out = bytearray()
        for index, entry in enumerate(self._entries):
            frame_start = self._decompressed_offsets[index]
            frame_end = frame_start + entry.decompressed_size
            if frame_end <= offset:            # frame ends before the range: skip
                continue
            if frame_start >= end:             # frame starts after the range: done
                break
            chunk = self.frame(index)
            lo = max(offset, frame_start) - frame_start
            hi = min(end, frame_end) - frame_start
            out += chunk[lo:hi]
        return bytes(out)
