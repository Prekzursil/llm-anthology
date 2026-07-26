"""IR -> browser-faithful, self-contained static HTML (the 'view' copy).

- Text bodies -> HTML via markdown-it-py with html=False (raw HTML in a message is
  escaped, exactly as chatgpt.com / claude.ai / gemini render it).
- Links hardened: http/https only get a live href (+ rel=noopener); anything else
  is defanged to inert text.
- Hidden unicode -> visible inert badges (forensic), never the raw invisible.
- Zero remote fetch: theme CSS inlined, CSP default-src 'none', no scripts, and
  markdown images are defanged into labelled links rather than <img> loads.
  (Math via KaTeX and server-side code highlighting are documented follow-ups;
  math is currently preserved VERBATIM rather than rendered.)
"""
import html
import json
import os
import re

from markdown_it import MarkdownIt

from llm_anthology import sanitize

try:
    _MD = MarkdownIt("gfm-like", {"html": False, "linkify": False, "typographer": False})
except (KeyError, ValueError):        # pragma: no cover - version fallback: the pinned
    _MD = MarkdownIt("commonmark", {"html": False})   # markdown-it-py has the preset

_THEME_DIR = os.path.join(os.path.dirname(__file__), "themes")
_A_ANY = re.compile(r"<a\s+([^>]*)>", re.IGNORECASE)
_IMG = re.compile(r'<img\s+[^>]*src="([^"]*)"[^>]*>', re.IGNORECASE)
_TAG_SPLIT = re.compile(r"(<[^>]*>)")

_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self' data:; base-uri 'none'; form-action 'none'")

# $$display$$ or $inline$ on a single line; held out of markdown escaping entirely
_MATH = re.compile(r"\$\$[^\n]+?\$\$|\$[^\n$]+?\$")
_MATH_PH = "zMaThSpAnZ"          # plain-word sentinel: markdown leaves it untouched


def _load_theme(name):
    try:
        with open(os.path.join(_THEME_DIR, name + ".css"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _harden_links(frag):
    """Rewrite EVERY anchor, failing CLOSED.

    The previous regex only matched `<a href="…"` — an anchor with attributes in
    another order, single-quoted, or unquoted kept its raw href untouched. Nothing
    emits those today, but a markdown-it upgrade or an attrs plugin would silently
    disable the only href control. Match any anchor and drop what we can't verify.
    """
    def repl(m):
        attrs = m.group(1)
        href = re.search(r"""href\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""", attrs, re.IGNORECASE)
        if not href:
            return "<a>"
        url = href.group(2) or href.group(3) or href.group(4) or ""
        # preserve the link title (sanitised) — dropping it would lose real content
        tm = re.search(r"""title\s*=\s*("([^"]*)"|'([^']*)')""", attrs, re.IGNORECASE)
        title = (tm.group(2) or tm.group(3) or "") if tm else ""
        title_attr = (' title="%s"' % sanitize.ncr_invisibles(html.escape(title, quote=True))) if title else ""
        if sanitize.is_safe_url(url.replace("&amp;", "&")):
            return ('<a href="%s"%s rel="noopener noreferrer" target="_blank">'
                    % (sanitize.ncr_invisibles(html.escape(url, quote=True)), title_attr))
        return ('<a class="unsafe" data-unsafe-href="%s"%s>'
                % (html.escape(url, quote=True), title_attr))
    return _A_ANY.sub(repl, frag)


def _badge_text_nodes(frag):
    """Badge invisibles in TEXT nodes only.

    badge_invisibles over the whole fragment injected a <span> INSIDE tags when an
    invisible sat in an attribute (a link title, an image alt, a code-fence info
    string), corrupting the markup and destroying the evidence at exactly that spot.
    Inside a tag, emit an inert numeric character reference instead.
    """
    return "".join(
        sanitize.ncr_invisibles(part) if part.startswith("<") and part.endswith(">")
        else sanitize.badge_invisibles(part)
        for part in _TAG_SPLIT.split(frag))


def _defang_images(frag):
    """A markdown image would emit a REMOTE <img src>. CSP blocks the load, so it
    renders as a broken image and quietly contradicts the no-egress promise.
    Replace it with a chip naming the alt text plus an explicit link."""
    def repl(m):
        tag = m.group(0)
        src = m.group(1)
        alt_m = re.search(r'alt="([^"]*)"', tag, re.IGNORECASE)
        alt = html.escape(alt_m.group(1) if alt_m else "image", quote=False)
        if sanitize.is_safe_url(src.replace("&amp;", "&")):
            return ('<span class="chip">🖼 %s <a href="%s" rel="noopener noreferrer" '
                    'target="_blank">(remote image — not fetched)</a></span>'
                    % (alt, html.escape(src, quote=True)))
        return '<span class="chip">🖼 %s (image unavailable)</span>' % alt
    return _IMG.sub(repl, frag)


def _md_to_html(text):
    """Markdown -> HTML, with math spans held OUT of the markdown pass.

    CommonMark strips a backslash before ASCII punctuation, so `$\\{a\\,b\\}$`
    would silently become `${a,b}$` — a mutation of the TeX that is unrecoverable
    and that a token-based fidelity gate cannot detect. Stash math first, restore
    it verbatim (escaped) afterwards. Unrendered-but-intact beats mutated.
    """
    text = text or ""
    spans = []

    def _stash(m):
        spans.append(m.group(0))
        return "%s%d%s" % (_MATH_PH, len(spans) - 1, _MATH_PH)

    frag = _MD.render(_MATH.sub(_stash, text))
    for i, raw in enumerate(spans):                      # restore BEFORE badging
        frag = frag.replace("%s%d%s" % (_MATH_PH, i, _MATH_PH), html.escape(raw, quote=False))
    return _badge_text_nodes(_harden_links(_defang_images(frag)))


def _pre(text):
    return "<pre>%s</pre>" % sanitize.badge_invisibles(html.escape(text or "", quote=False))


def _citations_html(cites):
    pills = []
    for c in (cites or []):
        url = c.get("url") or ""
        label = sanitize.neutralize_html(c.get("title") or url)
        if sanitize.is_safe_url(url):
            # href is an ATTRIBUTE: neutralise invisibles as inert numeric refs,
            # never as a badge span (that would break out of the attribute)
            pills.append('<a href="%s" rel="noopener noreferrer" target="_blank">%s</a>'
                         % (sanitize.ncr_invisibles(html.escape(url, quote=True)), label))
        else:
            pills.append("<span>%s</span>" % label)
    return ('<div class="cites">%s</div>' % "".join(pills)) if pills else ""


def _block_html(b):
    if b.type == "text":
        return '<div class="md">%s</div>%s' % (_md_to_html(b.text), _citations_html(b.citations))
    if b.type == "thinking":
        # claude.ai may WITHHOLD this from the reader; surfacing it silently would
        # misrepresent what the conversation actually showed. Mark it.
        label = "Thought process (hidden in claude.ai)" if b.data.get("hidden") else "Thought process"
        return ('<details class="thinking"><summary>%s</summary>'
                '<div class="md">%s</div></details>' % (label, _md_to_html(b.text)))
    if b.type == "tool_use":
        name = sanitize.neutralize_html(b.data.get("name") or "tool")
        inp = b.data.get("input")
        body = json.dumps(inp, ensure_ascii=False, indent=2) if inp is not None else ""
        disp = ('<div class="tool-disp">%s</div>' % sanitize.neutralize_html(b.text)) if b.text else ""
        return '<div class="tool"><div class="tool-head">🔧 %s</div>%s%s</div>' % (name, disp, _pre(body))
    if b.type == "tool_result":
        name = sanitize.neutralize_html(b.data.get("name") or "tool")
        content = b.data.get("content")
        body = content if isinstance(content, str) else (
            json.dumps(content, ensure_ascii=False, indent=2) if content is not None else "")
        err = bool(b.data.get("is_error"))
        head = ("⚠️ %s" if err else "↩️ %s") % name
        disp = ('<div class="tool-disp">%s</div>' % sanitize.neutralize_html(b.text)) if b.text else ""
        return '<div class="tool%s"><div class="tool-head">%s</div>%s%s</div>' % (
            " err" if err else "", head, disp, _pre(body))
    if b.type == "attachment":
        chip = '<span class="chip">📎 %s</span>' % sanitize.neutralize_html(b.data.get("file_name") or "attachment")
        # the uploaded document's TEXT is real content — it must not be dropped
        # from the faithful copy just because the bytes aren't in the export
        extracted = b.data.get("extracted_content")
        if isinstance(extracted, str) and extracted.strip():
            chip += ('<details class="thinking"><summary>Attached document text</summary>'
                     '%s</details>' % _pre(extracted))
        return chip
    if b.type == "file":
        return ('<span class="chip">📎 %s <em>(no bytes in export)</em></span>'
                % sanitize.neutralize_html(b.data.get("file_name") or "file"))
    if b.type == "code":
        lang = b.data.get("language") or ""
        cls = ' class="language-%s"' % html.escape(lang, quote=True) if lang else ""
        return "<pre><code%s>%s</code></pre>" % (
            cls, sanitize.badge_invisibles(html.escape(b.text or "", quote=False)))
    if b.type == "event":
        # a Gemini feature event (Used / Canvas / feedback) — never a fabricated reply
        return ('<div class="tool"><div class="tool-head">✨ %s</div></div>'
                % sanitize.neutralize_html(b.text or b.data.get("name") or "event"))
    if b.type == "media":
        path = b.data.get("path") or ""
        # LOCAL relative media only; anything else is shown as a chip, never fetched.
        # The check lives in sanitize.is_local_media_path because the obvious inline
        # form ("://" not in path and not path.startswith("//")) is BYPASSABLE --
        # browsers normalise "\" to "/" after it runs, so /\host/x.png fetched remotely.
        if sanitize.is_local_media_path(path):
            rel = path if "/" in path else ("../media/" + path)
            return ('<figure class="media"><img src="%s" alt="%s" loading="lazy"></figure>'
                    % (html.escape(rel, quote=True), html.escape(path, quote=True)))
        return '<span class="chip">🖼 %s</span>' % sanitize.neutralize_html(path or "media")
    if b.type == "unknown":
        # show the payload, not just the type name — a future block type may carry
        # real user text; printing only the type name would silently hide it
        raw = b.data.get("x_raw")
        try:
            body = json.dumps(raw, ensure_ascii=False, indent=2) if raw is not None else ""
        except (TypeError, ValueError):
            body = str(raw)
        return ('<div class="tool"><div class="tool-head">unrendered %s block</div>%s</div>'
                % (sanitize.neutralize_html(str(b.data.get("orig_type"))), _pre(body)))
    return '<div class="md">%s</div>' % _md_to_html(b.text)


def _turn_html(turn):
    role = "human" if turn.role == "human" else "assistant"
    who = "You" if role == "human" else "Claude"
    av = "Y" if role == "human" else "C"
    branch = ('<span class="branch">%d/%d</span>' % (turn.branch["index"], turn.branch["total"])) if turn.branch else ""
    blocks = "".join(_block_html(b) for b in turn.blocks)
    return ('<div class="turn %s"><div class="role"><span class="avatar">%s</span>%s%s</div>'
            '<div class="bubble">%s</div></div>' % (role, av, who, branch, blocks))


def render_conversation_html(conv, theme="claude"):
    css = _load_theme(theme)
    title_plain = html.escape(sanitize.sanitize_for_copy(conv.title or "(untitled)"), quote=True)
    title_disp = sanitize.neutralize_html(conv.title or "(untitled)")
    meta = sanitize.neutralize_html(" · ".join(x for x in [conv.provider, conv.account, conv.created_at] if x))
    turns = "".join(_turn_html(t) for t in conv.turns)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="%s">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>%s</title><style>%s</style></head>"
        '<body><main class="wrap"><h1 class="conv-title">%s</h1>'
        '<div class="conv-meta">%s</div>%s</main></body></html>'
        % (_CSP, title_plain, css, title_disp, meta, turns)
    )
