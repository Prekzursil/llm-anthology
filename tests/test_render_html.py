"""HTML renderer (visual fidelity). Structure + security contract; exact CSS is
validated separately by the screenshot-diff gate, not here."""
import re

from llm_anthology import render_html, ir


def _conv(turns, title="t"):
    return ir.Conversation(id="c1", title=title, provider="claude", account="a", turns=turns)


def test_html_is_full_doc_with_csp():
    h = render_html.render_conversation_html(_conv([ir.Turn("human", [ir.Block("text", text="hi")])]))
    assert "<!doctype html>" in h.lower()
    assert "content-security-policy" in h.lower() and "default-src 'none'" in h
    assert "hi" in h


def test_html_escapes_script_in_text():
    h = render_html.render_conversation_html(_conv([ir.Turn("human", [ir.Block("text", text="<script>alert(1)</script>")])]))
    assert "<script>alert(1)</script>" not in h
    assert "alert(1)" in h


def test_html_badges_hidden_unicode():
    h = render_html.render_conversation_html(_conv([ir.Turn("human", [ir.Block("text", text="a​b")])]))
    assert 'data-cp="U+200B"' in h
    assert "​" not in h


def test_html_role_classes():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("human", [ir.Block("text", text="q")]),
        ir.Turn("assistant", [ir.Block("text", text="a")]),
    ]))
    assert "turn human" in h and "turn assistant" in h


def test_html_thinking_is_collapsible_details():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("thinking", text="reason"), ir.Block("text", text="ans")]),
    ]))
    assert "<details" in h and "reason" in h and "ans" in h


def test_html_tool_use_card():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("tool_use", data={"name": "web_search", "input": {"q": "x"}})]),
    ]))
    assert "web_search" in h and 'class="tool"' in h


def test_html_citation_link_hardened():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("text", text="see", citations=[{"url": "https://ex.com", "title": "Ex"}])]),
    ]))
    assert "https://ex.com" in h and "noopener" in h


def test_html_dangerous_link_defanged():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("text", text="[x](javascript:alert(1))")]),
    ]))
    assert 'href="javascript:' not in h


def test_html_markdown_rendered():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("text", text="# Heading\n\n- a\n- b")]),
    ]))
    assert "<h1" in h and "<li>" in h


def test_html_branch_indicator():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("text", text="v2")], branch={"index": 2, "total": 2}),
    ]))
    assert "2/2" in h


def test_html_attachment_shows_extracted_document_text():
    """The uploaded document's TEXT is real content; a filename chip alone drops it."""
    h = render_html.render_conversation_html(_conv([
        ir.Turn("human", [ir.Block("attachment", text="d.pdf",
                                   data={"file_name": "d.pdf", "extracted_content": "EXTRACTEDBODY"})]),
    ]))
    assert "EXTRACTEDBODY" in h


def test_html_unknown_block_shows_payload():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("unknown", data={"orig_type": "future", "x_raw": {"t": "PAYLOADTEXT"}})]),
    ]))
    assert "PAYLOADTEXT" in h


def test_html_tool_display_content_shown():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("tool_use", text="DISPLAYTEXT", data={"name": "t", "input": {}})]),
    ]))
    assert "DISPLAYTEXT" in h


def test_html_latex_backslashes_not_eaten():
    """CommonMark strips `\\` before ASCII punctuation, silently mutating TeX.
    Math must survive verbatim (unrendered is fine; corrupted is not)."""
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("text", text=r"Given $\{a\,b\}$ and $$\int_0^1 x^2\,dx$$ done.")]),
    ]))
    assert r"\{a\,b\}" in h
    assert r"x^2\,dx" in h


def test_html_hidden_thinking_is_marked():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("thinking", text="withheld", data={"hidden": True})]),
    ]))
    assert "hidden in claude.ai" in h


def test_html_remote_image_is_not_an_img_tag():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("text", text="![diagram](https://evil.example/track.png)")]),
    ]))
    assert "<img" not in h                 # no remote load attempt
    assert "diagram" in h                  # alt text preserved


def test_html_invisible_in_attribute_does_not_inject_markup():
    """An invisible inside a link title / image alt / fence info sits in an
    ATTRIBUTE. Badging it there injected a <span> into the tag, corrupting the
    markup and destroying the evidence. It must become an inert numeric ref."""
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("text", text='[x](https://ok.example/ "ti​tle")')]),
    ]))
    assert 'cp-badge"' not in h        # no badge span spliced into a tag
    assert "&#x200B;" in h             # evidence kept, inertly


def test_html_unknown_orig_type_invisible_is_neutralised():
    h = render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("unknown", data={"orig_type": "a‮b", "x_raw": {}})]),
    ]))
    assert "‮" not in h


def test_harden_links_fails_closed_on_unusual_anchor_forms():
    """Nothing emits these today, but a markdown-it upgrade or attrs plugin would
    silently bypass an href-first-double-quoted-only regex."""
    for frag in ('<a title="t" href="javascript:alert(1)">x</a>',
                 "<a href='javascript:alert(1)'>x</a>",
                 '<a href=javascript:alert(1)>x</a>'):
        out = render_html._harden_links(frag)
        # the real property: a dangerous scheme may only survive DEFANGED, never live
        assert "javascript:" not in out or "data-unsafe-href" in out
        assert not re.search(r'<a\s+href\s*=\s*["\']?javascript:', out, re.IGNORECASE)


def test_html_self_contained_no_remote_refs():
    h = render_html.render_conversation_html(_conv([ir.Turn("human", [ir.Block("text", text="x")])]))
    assert "http://" not in h and "https://" not in h        # no remote deps in the shell/theme


def _media(path):
    return render_html.render_conversation_html(_conv([
        ir.Turn("assistant", [ir.Block("media", text="", data={"path": path})]),
    ]))


def test_media_local_relative_still_renders():
    """Control: if this stops rendering, the block below proves nothing."""
    h = _media("pic.png")
    assert '<img src="../media/pic.png"' in h


def test_media_sink_cannot_be_tricked_into_a_remote_fetch():
    """The rendered page claims zero remote fetches; the media sink broke that.

    `/\\host/x.png` passed the old guard (it starts with "/\\", not "//") and the
    browser then normalised the backslash, producing a live protocol-relative
    fetch -- a tracking pixel in a page opened from a stranger's export. Measured
    3/3 bypasses before the fix.
    """
    for hostile in ("/\\evil.example.com/x.png",
                    "\\/evil.example.com/x.png",
                    "//evil.example.com/x.png",
                    "https://evil.example.com/x.png",
                    "../../../../../../etc/passwd"):
        h = _media(hostile)
        assert "<img" not in h, f"{hostile!r} reached a raw <img>"
        assert "evil.example.com" not in h or "chip" in h


def test_pre_escapes_markup_and_badges_invisibles():
    """_pre is a raw sink: tool bodies reach it verbatim from an untrusted export.

    100% line+branch coverage did NOT protect it — deleting the escape from _pre left
    the whole suite green (mutation check, 2026-07-21). These assertions kill that mutant.
    """
    payload = '</pre><script>SENTINEL</script>​'
    h = render_html.render_conversation_html(_conv([ir.Turn("assistant", [
        ir.Block("tool_result", text="", data={"name": "t", "content": payload}),
    ])]))
    assert "<script>" not in h                       # tag never survives as markup
    assert "&lt;script&gt;SENTINEL&lt;/script&gt;" in h
    assert "</pre><script>" not in h                 # the <pre> container is not escapable
    assert "​" not in h                         # invisible codepoint is badged, not passed through


def test_pre_escapes_tool_use_input_too():
    """Same sink, the other call site: tool_use serialises input through _pre."""
    h = render_html.render_conversation_html(_conv([ir.Turn("assistant", [
        ir.Block("tool_use", text="", data={"name": "t", "input": {"q": "<img src=x onerror=BOOM>"}}),
    ])]))
    assert "<img" not in h
    assert "&lt;img src=x onerror=BOOM&gt;" in h
