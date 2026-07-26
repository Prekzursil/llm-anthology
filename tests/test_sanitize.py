"""Contract for the hidden-unicode neutralizer + HTML escaping + link allowlist.

Security-critical: the corpus was flagged for hidden zero-width / bidi / PUA
prompt-injection. The renderer must PRESERVE evidence (visible inert badge) but
NEUTRALIZE it (never emit the raw invisible; never let it reach a copy surface),
while never breaking legitimate emoji (VS16 / in-sequence ZWJ).
"""
from llm_anthology import sanitize


# --- HTML escaping (raw HTML in message bodies must be inert, matching real UIs) ---

def test_plain_text_html_escaped():
    assert sanitize.neutralize_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_script_tag_is_escaped_not_live():
    out = sanitize.neutralize_html("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# --- invisible / dangerous codepoints become visible inert badges ---

def test_zero_width_space_badged():
    out = sanitize.neutralize_html("a​b")
    assert 'data-cp="U+200B"' in out
    assert "​" not in out            # raw invisible removed
    assert out.startswith("a") and out.endswith("b")


def test_bidi_override_badged():
    assert 'data-cp="U+202E"' in sanitize.neutralize_html("x‮y")


def test_tag_char_badged():
    assert 'data-cp="U+E0041"' in sanitize.neutralize_html("t\U000e0041g")


def test_private_use_badged():
    # U+E200 is a ChatGPT citation-marker PUA codepoint
    assert 'data-cp="U+E200"' in sanitize.neutralize_html("cd")


def test_newline_and_tab_not_flagged():
    out = sanitize.neutralize_html("a\tb\nc")
    assert "data-cp" not in out
    assert "\t" in out and "\n" in out


# --- legitimate emoji must survive ---

def test_vs16_preserved():
    out = sanitize.neutralize_html("❤️")      # red heart + VS16
    assert "️" in out
    assert "data-cp" not in out


def test_zwj_inside_emoji_sequence_preserved():
    out = sanitize.neutralize_html("\U0001f468‍\U0001f469")  # man ZWJ woman
    assert "‍" in out
    assert "data-cp" not in out


def test_bare_zwj_badged():
    assert 'data-cp="U+200D"' in sanitize.neutralize_html("a‍b")


# --- scan (for the forensic audit sidecar) ---

def test_scan_invisibles_reports_codepoints():
    hits = sanitize.scan_invisibles("a​b‮c")
    cps = {cp for _, cp in hits}
    assert "U+200B" in cps and "U+202E" in cps


def test_scan_clean_text_empty():
    assert sanitize.scan_invisibles("just normal text 123") == []


# --- copy/agent-feed surface must be stripped, not badged ---

def test_sanitize_for_copy_strips_invisibles():
    assert sanitize.sanitize_for_copy("a​‮b") == "ab"


def test_sanitize_for_copy_keeps_emoji():
    assert sanitize.sanitize_for_copy("❤️") == "❤️"


# --- link scheme allowlist ---

def test_is_safe_url_allows_http_https():
    assert sanitize.is_safe_url("https://example.com")
    assert sanitize.is_safe_url("http://x.y/z")


def test_is_safe_url_blocks_dangerous_schemes():
    assert not sanitize.is_safe_url("javascript:alert(1)")
    assert not sanitize.is_safe_url("data:text/html,<script>")
    assert not sanitize.is_safe_url("file:///etc/passwd")
    assert not sanitize.is_safe_url("  JavaScript:alert(1)")   # trimmed + cased
    assert not sanitize.is_safe_url("")


def test_is_local_media_path_allows_genuine_relative_paths():
    assert sanitize.is_local_media_path("pic.png")
    assert sanitize.is_local_media_path("media/pic.png")
    assert sanitize.is_local_media_path("a/b/c/pic.png")


def test_is_local_media_path_blocks_backslash_protocol_relative():
    """The bypass this function exists for.

    A browser normalises "\\" to "/" AFTER any naive check, so `/\\host/x.png`
    reached the DOM as a protocol-relative URL and fetched remotely. Measured 3/3
    bypasses against the previous inline guard. Normalise BEFORE deciding.
    """
    assert not sanitize.is_local_media_path("/\\evil.example.com/x.png")
    assert not sanitize.is_local_media_path("\\/evil.example.com/x.png")
    assert not sanitize.is_local_media_path("\\\\evil.example.com\\share\\x.png")


def test_is_local_media_path_blocks_traversal_and_absolute():
    assert not sanitize.is_local_media_path("../../../../etc/passwd")
    assert not sanitize.is_local_media_path("a/../../b.png")
    assert not sanitize.is_local_media_path("/etc/passwd")
    assert not sanitize.is_local_media_path("//evil.example.com/x.png")


def test_is_local_media_path_blocks_schemes_and_drives():
    assert not sanitize.is_local_media_path("https://evil.example.com/x.png")
    assert not sanitize.is_local_media_path("data:image/svg+xml,<svg onload=alert(1)>")
    assert not sanitize.is_local_media_path("C:/Windows/System32/x.png")
    assert not sanitize.is_local_media_path("C:\\Windows\\x.png")


def test_is_local_media_path_rejects_empty_and_non_str():
    assert not sanitize.is_local_media_path("")
    assert not sanitize.is_local_media_path(None)
    assert not sanitize.is_local_media_path(123)


def test_variation_selector_smuggling_is_neutralised():
    """VS1-16 (U+FE00-FE0F) + VS17-256 (U+E0100-E01EF) are category Mn, so NO
    category test catches them — a 256-value channel carrying arbitrary bytes.
    This is the primary modern invisible-text smuggling technique."""
    payload = "".join(chr(0xFE00 + b) if b < 16 else chr(0xE0100 + b - 16) for b in b"SECRET")
    text = "ordinary sentence" + payload
    assert sanitize.neutralize_html(text).count("cp-badge") >= 6
    assert sanitize.sanitize_for_copy(text) == "ordinary sentence"   # agent-feed copy is clean
    assert len(sanitize.scan_invisibles(text)) >= 6                  # audit sees it


def test_bare_vs16_flagged_but_emoji_vs16_preserved():
    assert 'data-cp="U+FE0F"' in sanitize.neutralize_html("a️b")   # bare VS16 = payload
    assert "data-cp" not in sanitize.neutralize_html("❤️")           # after a pictograph = legit


def test_invisible_hangul_filler_is_flagged():
    # category Lo — invisible, and no category test would catch it
    assert 'data-cp="U+3164"' in sanitize.neutralize_html("aㅤb")


def test_ncr_invisibles_is_inert_inside_markup():
    out = sanitize.ncr_invisibles('title="a​b"')
    assert "&#x200B;" in out and "<span" not in out


def test_lone_surrogate_is_badged_not_crashing():
    """A lone surrogate survives json.loads but breaks a UTF-8 write. It must be
    neutralised to a badge so a single poisoned message cannot truncate a build."""
    out = sanitize.neutralize_html("a\ud800b")
    assert 'data-cp="U+D800"' in out
    out.encode("utf-8")            # must not raise
    assert sanitize.sanitize_for_copy("a\ud800b") == "ab"


def test_badge_invisibles_leaves_html_intact():
    out = sanitize.badge_invisibles("<em>a​b</em>")
    assert out.startswith("<em>") and out.endswith("</em>")
    assert 'data-cp="U+200B"' in out
    assert "​" not in out
