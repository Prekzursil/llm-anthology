"""A synthetic conversation exercising every block type — no real content.

Used by `llm-anthology demo` to produce a sample page someone can look at before pointing the
tool at their own export. It deliberately includes the adversarial cases the renderer
has to survive: a javascript: link, a zero-width character, a branch marker, and a
tool call/result pair.
"""
from llm_anthology import ir


def demo_conversation():
    return ir.Conversation(
        id="demo", title="Demo — rendering fidelity", provider="claude",
        account="demo@local", created_at="2026-07-19",
        turns=[
            ir.Turn("human", [
                ir.Block("attachment", data={"file_name": "notes.pdf", "file_type": "pdf",
                                             "file_size": 12345}),
                ir.Block("text", text="Summarize **reductive amination** with a table + code. "
                                      "Link: https://example.org and a bad one "
                                      "[x](javascript:alert(1))."),
            ]),
            ir.Turn("assistant", [
                ir.Block("thinking", text="They want a concise summary, a table, and a code "
                                          "snippet. Keep it tight."),
                ir.Block("text",
                         text="## Reductive amination\n\nA **carbonyl** + amine form an imine, "
                              "then it is reduced to an amine.\n\n| step | reagent |\n|---|---|\n"
                              "| 1 | R-CHO + R'NH2 |\n| 2 | NaBH3CN |\n\n"
                              "```python\ndef yield_pct(a, b):\n    return round(100 * a / b, 1)\n"
                              "```\n\nNote a hidden char here: recovered​.org "
                              "(badged on the right).",
                         citations=[{"url": "https://en.wikipedia.org/wiki/Reductive_amination",
                                     "title": "Reductive amination — Wikipedia"}]),
            ]),
            ir.Turn("assistant", [
                ir.Block("tool_use", data={"name": "web_search",
                                           "input": {"query": "NaBH3CN selectivity pH"}}),
                ir.Block("tool_result", data={"name": "web_search", "is_error": False,
                                              "content": "Sodium cyanoborohydride is selective "
                                                         "for iminium ions at mildly acidic pH "
                                                         "(~6-7)."}),
                ir.Block("text", text="Regenerated answer (this is branch 2 of 2)."),
            ], branch={"index": 2, "total": 2}),
        ])
