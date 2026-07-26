"""IR -> clean, portable Markdown (the 'keep' copy).

Content-faithful: Claude/ChatGPT/Gemini bodies are already Markdown, so text
passes through untouched. Hidden unicode is STRIPPED (this file is a portable
copy that may be re-fed to a model — no invisible injection payload survives).
The HTML renderer instead BADGES the same chars for forensic viewing.
"""
import json
import re

from llm_anthology import sanitize

_NEWLINES = re.compile(r"\s*[\r\n]+\s*")
# Characters that let a value break OUT of markdown link syntax
_URL_UNSAFE = {"(": "%28", ")": "%29", " ": "%20", "<": "%3C", ">": "%3E",
               '"': "%22", "[": "%5B", "]": "%5D", "\t": "%09", "\n": "", "\r": ""}


def _md_line(s):
    """A single-line field. Newlines are stripped so a value can never forge a
    turn header (`## Human`) inside the file we may re-feed to a model."""
    return _NEWLINES.sub(" ", _clean(s)).strip()


def _md_inline(s):
    """Inline text that sits inside markdown structure (e.g. link text)."""
    s = _md_line(s)
    return s.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _md_code_span(s):
    """Content of a `...` span: no backticks, no newlines."""
    return _md_line(s).replace("`", "'")


def _md_url(u):
    """Link destination: percent-encode what would terminate the destination."""
    return "".join(_URL_UNSAFE.get(ch, ch) for ch in _clean(u))


def render_conversation_md(conv):
    out = ["# %s" % _md_line(conv.title), ""]
    meta = " · ".join(x for x in [
        conv.provider,
        ("account " + conv.account) if conv.account else "",
        ("created " + conv.created_at) if conv.created_at else "",
    ] if x)
    if meta:
        out += ["> " + meta, ""]
    for turn in conv.turns:
        who = "Human" if turn.role == "human" else "Assistant"
        hdr = "## %s" % who
        if turn.branch:
            hdr += "  _(branch %d/%d)_" % (turn.branch["index"], turn.branch["total"])
        out += [hdr, ""]
        for b in turn.blocks:
            out += [_block_md(b), ""]
        out += ["---", ""]
    return "\n".join(out).rstrip() + "\n"


def _clean(s):
    return sanitize.sanitize_for_copy(s or "")


def _block_md(b):
    if b.type == "text":
        body = _clean(b.text)
        parts = []
        for c in (b.citations or []):
            if not isinstance(c, dict):
                continue
            url = c.get("url") or ""
            title = _md_inline(c.get("title") or url or "source")
            # is_safe_url gates the SCHEME; the value must still be ENCODED, or a
            # ')' in the url (or a ']' in the title) forges a second, live link
            parts.append("[%s](%s)" % (title, _md_url(url)) if sanitize.is_safe_url(url)
                         else "%s (%s)" % (title, _md_inline(url)))
        if parts:
            body += "\n\nSources: " + " · ".join(parts)
        return body
    if b.type == "thinking":
        body = _clean(b.text)
        quoted = "\n".join("> " + ln for ln in body.splitlines()) if body else "> "
        return "> 🧠 **Thinking**\n>\n%s" % quoted
    if b.type == "tool_use":
        name = _md_code_span(b.data.get("name") or "tool")
        inp = b.data.get("input")
        inp_s = json.dumps(inp, ensure_ascii=False, indent=2) if inp is not None else ""
        disp = ("\n\n" + _clean(b.text)) if b.text else ""      # display_content
        return "**🔧 Tool call: `%s`**%s\n\n```json\n%s\n```" % (name, disp, _clean(inp_s))
    if b.type == "tool_result":
        name = _md_code_span(b.data.get("name") or "tool")
        content = b.data.get("content")
        cs = content if isinstance(content, str) else (
            json.dumps(content, ensure_ascii=False, indent=2) if content is not None else "")
        tag = "⚠️ error" if b.data.get("is_error") else "result"
        disp = ("\n\n" + _clean(b.text)) if b.text else ""      # display_content
        return "**↩️ Tool %s (`%s`)**%s\n\n```\n%s\n```" % (tag, name, disp, _clean(cs))
    if b.type == "attachment":
        d = b.data
        line = "📎 **%s** (%s, %s bytes)" % (
            _md_line(d.get("file_name") or "attachment"),
            _md_line(d.get("file_type") or "?"), d.get("file_size") or "?")
        ex = d.get("extracted_content")
        if ex:
            line += ("\n\n<details><summary>extracted content</summary>\n\n%s\n\n</details>" % _clean(ex))
        return line
    if b.type == "code":
        return "```%s\n%s\n```" % (_md_line(b.data.get("language") or ""), _clean(b.text))
    if b.type == "event":
        return "> ✨ **%s**" % _md_line(b.text or b.data.get("name") or "event")
    if b.type == "media":
        path = b.data.get("path") or ""
        # a REMOTE media url must not become a fetchable image in the portable copy
        # (it would beacon when this .md is rendered elsewhere) — mirror the HTML defang
        if "://" in path or path.startswith("//"):
            return "🖼 %s _(remote media — not embedded)_" % _md_inline(path)
        rel = path if "/" in path else ("../media/" + path)
        return "![%s](%s)" % (_md_inline(path or "media"), _md_url(rel))
    if b.type == "file":
        return "📎 %s _(no content in export)_" % _md_line(b.data.get("file_name") or "file")
    if b.type == "unknown":
        out = "_[unrendered %s block]_" % _md_line(str(b.data.get("orig_type") or "unknown"))
        raw = b.data.get("x_raw")
        try:
            body = json.dumps(raw, ensure_ascii=False, indent=2) if raw is not None else ""
        except (TypeError, ValueError):
            body = str(raw)
        if body:
            out += "\n\n```json\n%s\n```" % _clean(body)   # never hide a payload
        return out
    return _clean(b.text)
