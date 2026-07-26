"""Generate js/src/generated/unicode-data.ts from CPython's own unicodedata.

Why this exists: the JS rail needs `unicodedata.category` semantics for a SECURITY
predicate (which codepoints are invisible/dangerous). The options all leak:

  * native RegExp `\\p{Cf}` etc. works, but V8's Unicode version is not pinnable and
    shifts under every Node upgrade — measured 4,803 disagreements with CPython's
    UCD 16.0.0, every one a codepoint Python calls Cn that a newer UCD has assigned;
  * third-party category tables are a second, independently-generated source of truth
    (one popular package returns Cc for assigned letters).

So instead of reimplementing the predicate in JS, we EXPORT IT. This script imports
the real `llm_anthology.sanitize._is_flagged` and encodes the whole predicate as ranges, which
means sanitize.ts contains ZERO category logic: `isFlagged(cp)` is a binary search
over a table generated from the same Python that the Python rail runs. The two rails
cannot drift by construction.

Re-run whenever the Python rail moves to a CPython with a newer UCD; the version
stamp changes and the parity tests re-lock both rails.

    python tools/gen-unicode-data.py
"""
import hashlib
import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_anthology.sanitize import _is_flagged            # noqa: E402  the REAL predicate

MAX_CP = 0x110000
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "js", "src", "generated", "unicode-data.ts")


def ranges_for(predicate):
    """[(start, length), ...] for every maximal run where predicate(cp) is True."""
    out, start = [], None
    for cp in range(MAX_CP):
        if predicate(cp):
            if start is None:
                start = cp
        elif start is not None:
            out.append((start, cp - start))
            start = None
    if start is not None:
        out.append((start, MAX_CP - start))
    return out


def _is_word(cp):
    """Python re's \\w for str patterns: alphanumeric (str.isalnum()) or underscore."""
    return cp == 0x5F or chr(cp).isalnum()


def _is_digit(cp):
    """Python re's \\d for str patterns: category Nd."""
    return unicodedata.category(chr(cp)) == "Nd"


def fmt_ranges(name, ranges, comment):
    body = ",".join("[%d,%d]" % (s, n) for s, n in ranges)
    return ("/** %s (%d ranges, generated) */\nexport const %s: ReadonlyArray<readonly "
            "[number, number]> = [%s];\n" % (comment, len(ranges), name, body))


def main():
    flagged = ranges_for(_is_flagged)
    word = ranges_for(_is_word)
    digit = ranges_for(_is_digit)

    names = {}
    for start, length in flagged:
        for cp in range(start, start + length):
            try:
                names[cp] = unicodedata.name(chr(cp))
            except ValueError:
                pass                              # Python's own fallback is "unnamed"

    canon = ";".join("%d:%d" % (s, n) for s, n in flagged)
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    total = sum(n for _, n in flagged)

    names_body = ",".join('%d:"%s"' % (cp, nm) for cp, nm in sorted(names.items()))
    src = [
        "// GENERATED FILE - do not edit by hand.\n",
        "// Produced by tools/gen-unicode-data.py from CPython's unicodedata, so the JS\n",
        "// rail's flag decisions are identical to the Python rail's by construction.\n",
        "// Re-run that script (not this file) when the Python rail's UCD changes.\n\n",
        'export const UNIDATA_VERSION = "%s";\n' % unicodedata.unidata_version,
        'export const GENERATED_BY_PYTHON = "%s";\n' % sys.version.split()[0],
        "export const FLAGGED_CODEPOINT_COUNT = %d;\n" % total,
        '/** sha256 over the canonical range list; the table self-tests against this. */\n',
        'export const BITMAP_SHA256 = "%s";\n\n' % digest,
        fmt_ranges("FLAGGED_RANGES", flagged,
                   "Every codepoint llm_anthology.sanitize._is_flagged() calls invisible/dangerous"),
        "\n",
        fmt_ranges("WORD_RANGES", word,
                   "Python re \\\\w for str patterns: str.isalnum() or underscore"),
        "\n",
        fmt_ranges("ND_RANGES", digit, "Python re \\\\d for str patterns: category Nd"),
        "\n",
        "/** unicodedata.name() for flagged codepoints that have one; miss => \"unnamed\". */\n",
        "export const FLAGGED_NAMES: Readonly<Record<number, string>> = {%s};\n" % names_body,
    ]

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(src))

    print("UNIDATA_VERSION", unicodedata.unidata_version)
    print("FLAGGED_RANGES", len(flagged), "ranges covering", total, "codepoints")
    print("FLAGGED_NAMES", len(names))
    print("WORD_RANGES", len(word), "| ND_RANGES", len(digit))
    print("BITMAP_SHA256", digest)
    print("WROTE", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
