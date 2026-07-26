"""Generate js/test/fixtures/sanitize-parity.json — the cross-language gate.

The generated unicode table already guarantees the two rails agree on WHICH
codepoints are flagged. What it does NOT cover is everything built on top: the
position-aware rules (a ZWJ between pictographs is legitimate, one after a letter
is payload), the badge/NCR formatting, and HTML escaping. This fixture pins the
actual OUTPUT of the Python rail for an adversarial battery so the JS port has to
reproduce it byte for byte.

`ensure_ascii=True` matters: it encodes lone surrogates as \\udXXX escapes, which
JSON.parse turns back into the same lone surrogate, so the poisoned-surrogate case
survives the trip.

    python tools/gen-parity-fixture.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_anthology import sanitize                        # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "js", "test", "fixtures", "sanitize-parity.json")


def cases():
    yield ""
    yield "plain ascii text"
    yield "a < b & c > d"
    yield "<script>alert(1)</script>"
    yield "quotes \" and ' and & together"
    yield "a\tb\nc\rd"                                   # allowed control chars
    # one sample per flagged family
    yield "zwsp a​b"
    yield "bidi x‮y"
    yield "tag t\U000e0041g"
    yield "pua cd"
    yield "hangul aㅤb"
    yield "surrogate a\ud800b"
    yield "unassigned aࡰb"
    # emoji: legitimate sequences must survive
    yield "❤️"                                 # heart + VS16
    yield "\U0001f468‍\U0001f469"                   # man ZWJ woman
    yield "\U0001f468‍\U0001f469‍\U0001f466"   # family, two ZWJ
    yield "\U0001f1f7\U0001f1f4"                         # regional indicators
    yield "a‍b"                                     # BARE zwj = payload
    yield "a️b"                                     # BARE vs16 = payload
    yield "❤️‍\U0001f525"                 # heart-on-fire
    # boundaries
    yield "‍"                                       # zwj alone
    yield "️"                                       # vs16 alone
    yield "\U0001f600️"                             # vs16 after astral pictograph
    yield "‍\U0001f600"                             # zwj with nothing before
    yield "\U0001f600‍"                             # zwj with nothing after
    # the modern smuggling channel
    payload = "".join(chr(0xFE00 + b) if b < 16 else chr(0xE0100 + b - 16) for b in b"SECRET")
    yield "ordinary sentence" + payload
    yield "".join(chr(0xE0100 + i) for i in range(20))
    # markup contexts
    yield 'title="a​b"'
    yield "<em>a​b</em>"
    yield "https://example.com/?q=a​b"
    # density / mixture
    yield "".join("x​" for _ in range(50))
    yield "mixed ​ ‮  ㅤ ︁ \U000e0041 end"
    yield "astral \U0001d11e \U00020000 and é́ combining"


def main():
    data = []
    for s in cases():
        data.append({
            "input": s,
            "neutralize_html": sanitize.neutralize_html(s),
            "badge_invisibles": sanitize.badge_invisibles(s),
            "ncr_invisibles": sanitize.ncr_invisibles(s),
            "sanitize_for_copy": sanitize.sanitize_for_copy(s),
            "scan_invisibles": [[i, cp] for i, cp in sanitize.scan_invisibles(s)],
        })
    urls = ["https://example.com", "http://x.y/z", "javascript:alert(1)",
            "data:text/html,<script>", "file:///etc/passwd", "  JavaScript:alert(1)",
            "", "  https://ok.example  ", "HTTPS://SHOUTY.EXAMPLE", "//protocol-relative",
            "ftp://x", "mailto:a@b"]
    payload = {
        "unidata_version": __import__("unicodedata").unidata_version,
        "cases": data,
        "urls": [{"url": u, "safe": sanitize.is_safe_url(u)} for u in urls],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=True, indent=1)
    print("WROTE", OUT, os.path.getsize(OUT), "bytes")
    print("cases", len(data), "| urls", len(urls))


if __name__ == "__main__":
    main()
