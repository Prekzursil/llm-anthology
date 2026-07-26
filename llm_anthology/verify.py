"""Text-exact fidelity gate.

The achievable "100% faithful" contract (per the design decisions): literal pixel
equality with a live page is impossible, but TEXT-exactness is a hard gate — every
prose word present in the source IR must survive into the rendered HTML. This
catches any rendering bug that silently drops or garbles content.

verify() returns {ok, missing_tokens, coverage}. Wire it into the build; a non-empty
missing_tokens on any conversation fails the fidelity gate.
"""
import html as _html
import re
from collections import Counter

_WORD = re.compile(r"\w+", re.UNICODE)
_BADGE_SPAN = re.compile(r'<span class="cp-badge".*?</span>', re.DOTALL | re.IGNORECASE)
_STYLE = re.compile(r"<style.*?</style>", re.DOTALL | re.IGNORECASE)
_HEAD = re.compile(r"<head.*?</head>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
# Source words can legitimately land in an ATTRIBUTE rather than visible text:
# a markdown link's URL -> href, an image -> src/alt, a code fence's language ->
# class="language-x". Harvest those so the gate does not false-positive on them.
# Harvest ONLY attributes that can legitimately hold source words. `class` is
# deliberately EXCLUDED: shell class names (avatar, bubble, turn, wrap, md, role)
# would mask a genuinely dropped body word that happens to match one.
_ATTR_VALS = re.compile(r'(?:href|src|alt|title)="([^"]*)"', re.IGNORECASE)
_LANG_CLASS = re.compile(r'class="[^"]*language-([\w+.-]+)', re.IGNORECASE)
# Ordered-list markers ("1." "2)") are STRUCTURAL: <ol> renders the number via a
# CSS counter, so it is visible to a reader but absent from the DOM text. Drop them
# from the source side or the gate reports every numbered list as missing content.
_OL_MARKER = re.compile(r"(?m)^[ \t]{0,3}\d+[.)][ \t]+")
# NOTE: do NOT strip markdown inline markers from the source. Tag-stripping below
# replaces every tag with a SPACE, so the rendered side SPLITS at emphasis
# boundaries (`<strong>bold</strong>text` -> "bold text"). Leaving the markers in
# the source makes \w+ split at exactly the same places — the two sides align.


def _tok(s):
    return _WORD.findall((s or "").lower())


def prose_tokens(conv):
    """Word tokens a reader must see: text + thinking bodies. The \\w+ tokenizer
    already treats invisible/format chars as boundaries — matching how the HTML
    renderer badges them — so a hidden char never joins or splits a real word here.
    Tool/attachment payloads are structural and excluded from this prose gate."""
    toks = []
    for turn in conv.turns:
        for b in turn.blocks:
            if b.type in ("text", "thinking"):
                toks += _tok(_OL_MARKER.sub(" ", b.text))
    return toks


def html_visible_tokens(h):
    h = _BADGE_SPAN.sub(" ", h or "")     # badges replace invisibles — not content words
    h = _STYLE.sub(" ", h)
    h = _HEAD.sub(" ", h)                 # drop CSS + <title> so they can't mask a real drop
    attrs = " ".join(_ATTR_VALS.findall(h) + _LANG_CLASS.findall(h))  # URLs, alt, code language
    body = _TAG.sub(" ", h)
    return _tok(_html.unescape(body + " " + attrs))


def verify(conv, rendered_html):
    want = Counter(prose_tokens(conv))
    got = Counter(html_visible_tokens(rendered_html))
    missing = want - got                  # multiset difference: prose words not in the HTML
    total = sum(want.values())
    covered = total - sum(missing.values())
    return {
        "ok": not missing,
        "missing_tokens": sorted(missing.elements()),
        "coverage": (covered / total) if total else 1.0,
    }
