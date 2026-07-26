"""Text-exact fidelity gate: every prose word in the source IR must appear in the
rendered HTML (multiset containment). Proves rendering drops no content."""
from llm_anthology import verify, render_html, ir


def _c(text):
    return ir.Conversation(id="c", title="t", provider="claude",
                           turns=[ir.Turn("assistant", [ir.Block("text", text=text)])])


def test_verify_passes_when_all_prose_present():
    conv = _c("the quick brown fox jumps over")
    r = verify.verify(conv, render_html.render_conversation_html(conv))
    assert r["ok"] and not r["missing_tokens"] and r["coverage"] == 1.0


def test_verify_detects_a_dropped_word():
    conv = _c("alpha beta gamma delta epsilon")
    broken = render_html.render_conversation_html(conv).replace("gamma", "")   # simulate a drop
    r = verify.verify(conv, broken)
    assert not r["ok"] and "gamma" in r["missing_tokens"]


def test_verify_ignores_markdown_and_chrome():
    conv = _c("# Heading\n\nsome **bold** and `code` text")
    r = verify.verify(conv, render_html.render_conversation_html(conv))
    assert r["ok"]        # markdown syntax + role/label chrome never cause a false miss


def test_verify_counts_thinking_prose():
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[
        ir.Turn("assistant", [ir.Block("thinking", text="hidden reasoning tokens"), ir.Block("text", text="answer")]),
    ])
    r = verify.verify(conv, render_html.render_conversation_html(conv))
    assert r["ok"]


def test_verify_counts_link_urls_and_code_language():
    """Source words that land in ATTRIBUTES (a link href, a code-fence language)
    are still present in the output — the gate must not report them missing."""
    conv = _c("see [docs](https://example.org/guide)\n\n```python\nx = 1\n```")
    r = verify.verify(conv, render_html.render_conversation_html(conv))
    assert r["ok"], r["missing_tokens"]


def test_verify_markdown_emphasis_joining_not_flagged():
    """Rendering removes inline markers and JOINS the surrounding text; the gate
    must not read that as dropped content."""
    conv = _c("intra*word*emphasis plus **bold**text and `code`span")
    r = verify.verify(conv, render_html.render_conversation_html(conv))
    assert r["ok"], r["missing_tokens"]


def test_verify_ordered_list_markers_not_flagged():
    """`1.` `2.` are structural: <ol> draws them with a CSS counter, so they are
    visible to a reader but absent from the DOM text. Not a content drop."""
    conv = _c("steps:\n\n1. first\n2. second\n3. third")
    r = verify.verify(conv, render_html.render_conversation_html(conv))
    assert r["ok"], r["missing_tokens"]


def test_verify_does_not_mask_words_matching_css_class_names():
    """Harvesting class="..." let shell class names (avatar/bubble/turn) count as
    'present', hiding a genuine drop of those very words."""
    conv = _c("avatar bubble turn wrap")
    h = render_html.render_conversation_html(conv)
    r = verify.verify(conv, h.replace("avatar bubble turn wrap", ""))
    assert not r["ok"] and "bubble" in r["missing_tokens"]


def test_verify_invisible_chars_do_not_break_it():
    conv = _c("before​after")     # zero-width space between words
    r = verify.verify(conv, render_html.render_conversation_html(conv))
    assert r["ok"]
