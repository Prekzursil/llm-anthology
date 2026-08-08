"""Shared build layer: IR conversations -> html/ + md/ + index.html + reports.

Extracted from the three build_*.py scripts so the installed package ships ONE
tested implementation behind the `llm-anthology` console entry point. Providers differ only
in how they LOAD conversations and what their index meta column shows; everything
downstream of the IR is identical.

Two invariants are load-bearing:
  * a single malformed conversation is isolated and reported, never fatal — one bad
    record must not truncate the corpus;
  * the index is built from attacker-influenced titles, so it escapes/neutralizes
    rather than interpolating raw.

Nothing here touches the network.
"""
import json
import os
import re

from llm_anthology import audit, render_html, render_md, sanitize, verify

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")

THEMES = {
    "claude": {"title": "Claude sessions", "bg": "#1f1e1b", "fg": "#f4f3ee",
               "link": "#d97757", "muted": "#b4b0a4"},
    "chatgpt": {"title": "ChatGPT sessions", "bg": "#212121", "fg": "#ececec",
                "link": "#7ab7ff", "muted": "#a0a0a0"},
    "gemini": {"title": "Gemini sessions", "bg": "#1e1f20", "fg": "#e3e3e3",
               "link": "#8ab4f8", "muted": "#9aa0a6"},
    "codex": {"title": "Codex sessions", "bg": "#0d1117", "fg": "#e6edf3",
              "link": "#58a6ff", "muted": "#8b949e"},
}
_FALLBACK_THEME = {"title": "Sessions", "bg": "#1b1b1b", "fg": "#ededed",
                   "link": "#7ab7ff", "muted": "#999999"}


def safe_name(title, idx):
    """A filesystem-safe, index-prefixed name. Hidden unicode is stripped first so
    a zero-width char can never smuggle itself into a filename."""
    base = _ILLEGAL.sub(" ", sanitize.sanitize_for_copy(title or "untitled"))
    base = _WS.sub(" ", base).strip()[:60].rstrip(". ") or "untitled"
    return "%03d-%s" % (idx, base)


def write_text(path, text):
    """UTF-8 write that cannot die on an unpaired surrogate that slipped through."""
    with open(path, "w", encoding="utf-8", newline="\n", errors="replace") as fh:
        fh.write(text)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n", errors="replace") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def render_corpus(convs, out_dir, provider="claude", meta_of=None, load_errors=None, extra=None):
    """Render every conversation to <out_dir>/html and <out_dir>/md, then write
    index.html, _hidden-char-audit.json and _fidelity-report.json.

    convs        iterable of ir.Conversation
    meta_of      callable(conv) -> str for the index's per-provider meta column
    load_errors  errors already collected while loading (parse/adapt stages)
    extra        provider-specific report fields (e.g. Gemini's grouping_mode label,
                 which must reach the report so a heuristic is never mistaken for
                 ground truth)
    Returns the report dict that is also written to _fidelity-report.json.
    """
    theme = THEMES.get(provider, _FALLBACK_THEME)
    if meta_of is None:
        def meta_of(conv):
            return getattr(conv, "account", "") or ""

    html_dir = os.path.join(out_dir, "html")
    md_dir = os.path.join(out_dir, "md")
    os.makedirs(html_dir, exist_ok=True)
    os.makedirs(md_dir, exist_ok=True)

    index, audit_rows, fidelity_fail = [], [], []
    errors = list(load_errors or [])
    n = 0
    for conv in convs:
        n += 1
        name = safe_name(getattr(conv, "title", None), n)
        try:
            hits = audit.hidden_char_hits(conv)
            if hits:
                audit_rows.append({"file": name + ".html", "title": conv.title,
                                   "hidden_char_count": len(hits),
                                   "codepoints": sorted(set(hits))})
            html_out = render_html.render_conversation_html(conv)
            v = verify.verify(conv, html_out)          # text-exact fidelity gate
            if not v["ok"]:
                fidelity_fail.append({"file": name + ".html", "title": conv.title,
                                      "coverage": round(v["coverage"], 4),
                                      "missing_sample": v["missing_tokens"][:20]})
            write_text(os.path.join(html_dir, name + ".html"), html_out)
            write_text(os.path.join(md_dir, name + ".md"),
                       render_md.render_conversation_md(conv))
            index.append((name, conv.title, len(conv.turns), meta_of(conv)))
        except Exception as e:                          # isolate: one bad record only
            errors.append({"file": name, "stage": "render", "error": repr(e)})

    write_index(out_dir, index, theme)
    write_json(os.path.join(out_dir, "_hidden-char-audit.json"), audit_rows)
    # A conversation that parses cleanly but carries NO turns is a silent
    # false-success: feeding a Codex-shaped export to the ChatGPT loader yields
    # 1 conversation / 0 turns / 0 errors, so real content vanishes while the
    # caller sees a clean load. Count turns so the caller can tell "rendered"
    # from "rendered something". Measured: 1 conv, 0 turns, 0 errors pre-fix.
    turns = sum(row[2] for row in index)
    empty = [row[0] for row in index if row[2] == 0]
    report = {
        "conversations": n,
        "rendered": len(index),
        "turns": turns,
        "empty_conversations": len(empty),
        "fidelity_passed": len(index) - len(fidelity_fail),
        "failed": fidelity_fail,
        "errors": errors,
        "hidden_char_conversations": len(audit_rows),
        "out_dir": out_dir,
    }
    report.update(extra or {})
    write_json(os.path.join(out_dir, "_fidelity-report.json"), report)
    return report


def write_index(out_dir, index, theme):
    import html as _h
    rows = "".join(
        '<li><a href="html/%s.html">%s</a> <span class="muted">· %d turns%s</span></li>'
        % (_h.escape(name, quote=True), sanitize.neutralize_html(title), turns,
           (" · " + _h.escape(str(meta), quote=True)) if meta else "")
        for name, title, turns, meta in index)
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        "<title>%s</title><style>"
        "body{background:%s;color:%s;font-family:-apple-system,Segoe UI,sans-serif;"
        "max-width:820px;margin:0 auto;padding:32px 20px}"
        "a{color:%s;text-decoration:none}li{margin:6px 0;line-height:1.5}"
        ".muted{color:%s;font-size:.85em}"
        "</style></head><body><h1>%s (%d)</h1><ul>%s</ul></body></html>"
        % (_h.escape(theme["title"]), theme["bg"], theme["fg"], theme["link"],
           theme["muted"], _h.escape(theme["title"]), len(index), rows))
    write_text(os.path.join(out_dir, "index.html"), doc)


def print_report(report):
    print("CONVERSATIONS_RENDERED", report["rendered"], "of", report["conversations"])
    print("TURNS_RENDERED", report["turns"])
    if report["empty_conversations"]:
        # loud, because the usual cause is a wrong-provider or drifted export --
        # a case that otherwise renders blank pages and reports success
        print("EMPTY_CONVERSATIONS", report["empty_conversations"],
              "(no turns parsed - wrong provider, or the export format changed)")
    if report.get("grouping_mode"):
        # Gemini only, and printed on EVERY gemini run rather than only on the downgrade.
        # A field that appears only when something went wrong is a field nobody learns to
        # read, and its absence would be ambiguous between "grouping was fine" and "this
        # build predates the line". Conversation boundaries decide what a "conversation"
        # even IS for this provider, so provisional-vs-true is not a detail: it used to
        # live only inside `_fidelity-report.json`, which nobody opens after a run that
        # looked like it worked.
        print("GROUPING_MODE", report["grouping_mode"])
    print("FIDELITY_GATE_PASSED", report["fidelity_passed"], "of", report["rendered"])
    print("HIDDEN_CHAR_CONVERSATIONS", report["hidden_char_conversations"])
    print("ERRORS", len(report["errors"]))
    print("OUT_DIR", report["out_dir"])
