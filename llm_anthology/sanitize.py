"""Hidden-unicode neutralizer + HTML escaping + link-scheme allowlist.

The corpus was flagged for hidden zero-width / bidi / private-use / TAG-block
prompt-injection. Policy (from the design decisions, 4-lens convergence):

- PRESERVE the evidence but NEUTRALIZE it: every flagged codepoint becomes a
  VISIBLE, inert badge (`<span class="cp-badge" data-cp="U+XXXX">`), never the
  raw invisible char, so a reader (and a diff) can see exactly what was there.
- Escape raw HTML in bodies (matches what chatgpt.com / claude.ai / gemini all
  do — bodies are text, not live markup).
- Do NOT break legitimate emoji: VS16 (U+FE0F) and ZWJ (U+200D) *inside* an
  emoji sequence are preserved; a BARE zero-width joiner is badged.
- A separate copy/agent-feed surface (`sanitize_for_copy`) STRIPS the flagged
  chars so re-pasting an archived message into the next model can't re-inject.

IMPORTANT: callers must pass DECODED strings. Claude exports store invisibles as
`\\uXXXX` JSON escapes, so scanning the raw file bytes reports a false "clean";
json.load decodes them first (verified twice this session).
"""
import html
import unicodedata

_WS_CC_OK = frozenset({0x09, 0x0A, 0x0D})   # tab, LF, CR are legitimate
_ZWJ = 0x200D
_VS16 = 0xFE0F
# Invisible Hangul fillers — render as nothing but are category Lo, so no
# category test catches them.
_INVISIBLE_LETTERS = frozenset({0x115F, 0x1160, 0x3164, 0xFFA0})


def _is_pictographic(cp):
    """Approximate Extended_Pictographic — enough to keep in-sequence ZWJ intact."""
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or 0x1F1E6 <= cp <= 0x1F1FF        # regional indicators
        or cp in (0x2764, 0x2665, 0x203C, 0x2049)
    )


def _is_flagged(cp):
    """True for invisible/dangerous codepoints, IGNORING position.

    Position-sensitive cases (ZWJ inside an emoji sequence, VS16 straight after a
    pictograph) are decided by _flagged_at(), which is what callers must use.
    """
    if 0xE0000 <= cp <= 0xE007F:            # TAG block (emoji-tag / deprecated)
        return True
    # Variation selectors are category Mn, so NO category test catches them. This
    # is the 256-value channel behind modern invisible-text smuggling — arbitrary
    # bytes encoded as VS1-16 + VS17-256, invisible in every renderer.
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return True
    if cp in _INVISIBLE_LETTERS:
        return True
    cat = unicodedata.category(chr(cp))
    if cat == "Cc":
        return cp not in _WS_CC_OK
    # Cs = lone surrogate: survives json.loads but raises UnicodeEncodeError on a
    # UTF-8 write, which previously aborted the whole build mid-corpus.
    return cat in ("Cf", "Co", "Cn", "Cs")


def _flagged_at(s, i):
    """Position-aware flag decision — the ONLY predicate callers should use.

    Keeps legitimate emoji intact (a ZWJ between two pictographs, a VS16 right
    after a pictograph) while flagging the same codepoints anywhere else, where
    they carry payload rather than presentation.
    """
    cp = ord(s[i])
    if cp == _ZWJ:
        return not _zwj_in_emoji(s, i)
    if cp == _VS16:
        return not (i > 0 and _is_pictographic(ord(s[i - 1])))
    return _is_flagged(cp)


def _badge(cp):
    code = "U+%04X" % cp
    try:
        name = unicodedata.name(chr(cp))
    except ValueError:
        name = "unnamed"
    return ('<span class="cp-badge" data-cp="%s" title="%s (hidden)">⚑</span>'
            % (code, html.escape(name, quote=True)))


def _zwj_in_emoji(s, i):
    prev = s[i - 1] if i > 0 else ""
    nxt = s[i + 1] if i + 1 < len(s) else ""
    return bool(prev) and bool(nxt) and _is_pictographic(ord(prev)) and _is_pictographic(ord(nxt))


def neutralize_html(s):
    """DECODED text -> HTML-safe string: raw HTML escaped, flagged invisibles
    replaced with visible inert badges, legitimate emoji preserved."""
    out = []
    s = s if isinstance(s, str) else ""
    for i, ch in enumerate(s):
        out.append(_badge(ord(ch)) if _flagged_at(s, i) else html.escape(ch, quote=False))
    return "".join(out)


def badge_invisibles(s):
    """Replace flagged invisible codepoints with visible badges WITHOUT escaping
    other characters — for post-processing an already-rendered HTML fragment
    (the markdown library already escaped the raw HTML)."""
    out = []
    s = s if isinstance(s, str) else ""
    for i, ch in enumerate(s):
        out.append(_badge(ord(ch)) if _flagged_at(s, i) else ch)
    return "".join(out)


def ncr_invisibles(s):
    """Replace flagged invisibles with INERT numeric character references.

    For use inside a tag/attribute, where injecting a badge <span> would break
    out of the markup. Preserves the forensic evidence without emitting markup.
    """
    out = []
    s = s if isinstance(s, str) else ""
    for i, ch in enumerate(s):
        out.append("&#x%04X;" % ord(ch) if _flagged_at(s, i) else ch)
    return "".join(out)


def scan_invisibles(s):
    """Return [(index, 'U+XXXX'), ...] for each flagged codepoint (audit sidecar)."""
    hits = []
    s = s if isinstance(s, str) else ""
    for i, ch in enumerate(s):
        if _flagged_at(s, i):
            hits.append((i, "U+%04X" % ord(ch)))
    return hits


def sanitize_for_copy(s):
    """Strip flagged invisibles entirely (clipboard / agent-feed surface)."""
    keep = []
    s = s if isinstance(s, str) else ""
    for i, ch in enumerate(s):
        if not _flagged_at(s, i):
            keep.append(ch)
    return "".join(keep)


def is_safe_url(u):
    """Allowlist http/https only (kills javascript:, data:, file:, etc.)."""
    u = (u if isinstance(u, str) else "").strip().lower()
    return u.startswith("http://") or u.startswith("https://")


def is_local_media_path(p):
    """True only for a genuinely LOCAL, relative media path.

    Guarding with `"://" not in p and not p.startswith("//")` was bypassable: a
    browser normalises backslashes to forward slashes AFTER any such check, so
    `/\\host/x.png` and `\\/host/x.png` reach the DOM as protocol-relative URLs and
    fetch remotely. `../../../etc/passwd` also passed straight into src=. Measured
    3/3 bypasses before this function existed.

    Allowlist, not blocklist: normalise separators first, then require a relative
    path with no scheme, no root anchor, no drive letter and no parent traversal.
    """
    p = p if isinstance(p, str) else ""
    if not p:
        return False
    # normalise FIRST -- the browser will, so the check must too
    norm = p.replace("\\", "/")
    if "://" in norm or norm.startswith("/"):      # scheme, root-relative, protocol-relative
        return False
    if norm.startswith("data:") or ":" in norm.split("/")[0]:   # data:, C:, any drive/scheme
        return False
    return not any(seg == ".." for seg in norm.split("/"))      # no parent traversal
