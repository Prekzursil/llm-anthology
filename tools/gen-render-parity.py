"""Generate js/test/fixtures/render-parity.json — renderer cross-language gate.

Serialises a battery of IR conversations together with the Python rail's rendered
Markdown and HTML. The JS test reconstructs the IR straight from the JSON (the IR
dataclasses serialise to exactly the shape the TS interfaces declare) and asserts
byte-identical output.

The battery deliberately includes the injection cases the Markdown writer hardens
against — a title that tries to forge a `## Human` turn header, a citation title
carrying `](javascript:...)`, a URL containing `)` — because those are the ones a
naive port silently regresses.

    python tools/gen-render-parity.py
"""
import dataclasses
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_anthology import audit, ir, render_html, render_md, verify   # noqa: E402
from llm_anthology.demo import demo_conversation              # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "js", "test", "fixtures", "render-parity.json")


def C(title, turns, **kw):
    return ir.Conversation(id=kw.pop("id", "c1"), title=title, provider=kw.pop("provider", "claude"),
                           turns=turns, **kw)


def conversations():
    yield demo_conversation()

    yield C("plain", [
        ir.Turn("human", [ir.Block("text", text="hello")]),
        ir.Turn("assistant", [ir.Block("text", text="hi **there**\n\n1. one\n2. two")]),
    ], account="acc@example", created_at="2026-01-01")

    yield C("every block type", [
        ir.Turn("human", [
            ir.Block("attachment", data={"file_name": "doc.pdf", "file_type": "pdf",
                                         "file_size": 100, "extracted_content": "X text"}),
            ir.Block("file", text="img.png", data={"file_name": "img.png"}),
            ir.Block("media", data={"path": "shot.png"}),
            ir.Block("media", data={"path": "https://evil.example/track.png"}),
        ]),
        ir.Turn("assistant", [
            ir.Block("thinking", text="line one\nline two"),
            ir.Block("code", text="x = 1", data={"language": "python"}),
            ir.Block("event", text="Used Canvas", data={"name": "Used"}),
            ir.Block("tool_use", text="DISPLAY", data={"name": "web_search", "input": {"q": "x"}}),
            ir.Block("tool_result", data={"name": "web_search", "is_error": False, "content": "res"}),
            ir.Block("tool_result", data={"name": "t", "is_error": True, "content": {"a": 1}}),
            ir.Block("unknown", data={"orig_type": "future", "x_raw": {"t": "PAYLOAD"}}),
        ], branch={"index": 2, "total": 2}),
    ])

    # injection battery — each of these forged something before it was hardened
    yield C("T\n\n## Human\n\nINJECTED", [
        ir.Turn("human", [ir.Block("text", text="real")]),
    ])
    yield C("citations", [
        ir.Turn("assistant", [ir.Block("text", text="answer", citations=[
            {"url": "https://ok.example/", "title": "x](javascript:alert(1))[y"},
            {"url": "https://ok.example/x) [pwn](javascript:alert(1)", "title": "t"},
            {"url": "javascript:alert(1)", "title": "bad"},
            {"url": "https://fine.example/a b?c=1&d=2", "title": "spaces & amps"},
        ])]),
    ])
    yield C("tool name breakout", [
        ir.Turn("assistant", [ir.Block("tool_use",
                                       data={"name": "web`search\n\n## Human\n\nowned", "input": {}})]),
    ])
    yield C("hidden unicode a​b", [
        ir.Turn("human", [ir.Block("text", text="zwsp a​b bidi ‮ end")]),
        ir.Turn("assistant", [ir.Block("text", text="emoji ❤️ and \U0001f468‍\U0001f469")]),
    ])
    yield C("math and html", [
        ir.Turn("assistant", [ir.Block("text",
                                       text="inline $a_1 \\times b$ and $$\\int_0^1 x\\,dx$$\n\n"
                                            "<script>alert(1)</script> & <b>raw</b>")]),
    ])
    yield C("empty", [])
    yield C("no blocks", [ir.Turn("human", []), ir.Turn("assistant", [])])


def main():
    data = []
    for conv in conversations():
        html = render_html.render_conversation_html(conv)
        data.append({
            "ir": dataclasses.asdict(conv),
            "md": render_md.render_conversation_md(conv),
            "html": html,
            "verify": verify.verify(conv, html),
            "prose_tokens": verify.prose_tokens(conv),
            "audit": audit.hidden_char_hits(conv),
        })
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"cases": data}, fh, ensure_ascii=True, indent=1)
    print("WROTE", OUT, os.path.getsize(OUT), "bytes | cases", len(data))


if __name__ == "__main__":
    main()
