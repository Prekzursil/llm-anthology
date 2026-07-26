/**
 * A synthetic conversation exercising every block type — no real content.
 * Mirrors llm_anthology/demo.py, including the adversarial cases the renderer must survive
 * (a javascript: link, a zero-width character, a branch marker, a tool call/result).
 */
import type { Conversation } from "./ir.js";
import { block, conversation, turn } from "./ir.js";

export function demoConversation(): Conversation {
  return conversation("demo", "Demo — rendering fidelity", "claude", {
    account: "demo@local",
    created_at: "2026-07-19",
    turns: [
      turn("human", [
        block("attachment", { data: { file_name: "notes.pdf", file_type: "pdf", file_size: 12345 } }),
        block("text", {
          text:
            "Summarize **reductive amination** with a table + code. " +
            "Link: https://example.org and a bad one [x](javascript:alert(1)).",
        }),
      ]),
      turn("assistant", [
        block("thinking", {
          text: "They want a concise summary, a table, and a code snippet. Keep it tight.",
        }),
        block("text", {
          text:
            "## Reductive amination\n\nA **carbonyl** + amine form an imine, then it is " +
            "reduced to an amine.\n\n| step | reagent |\n|---|---|\n| 1 | R-CHO + R'NH2 |\n" +
            "| 2 | NaBH3CN |\n\n```python\ndef yield_pct(a, b):\n    return round(100 * a / b, 1)\n" +
            "```\n\nNote a hidden char here: recovered​.org (badged on the right).",
          citations: [
            {
              url: "https://en.wikipedia.org/wiki/Reductive_amination",
              title: "Reductive amination — Wikipedia",
            },
          ],
        }),
      ]),
      turn(
        "assistant",
        [
          block("tool_use", { data: { name: "web_search", input: { query: "NaBH3CN selectivity pH" } } }),
          block("tool_result", {
            data: {
              name: "web_search",
              is_error: false,
              content:
                "Sodium cyanoborohydride is selective for iminium ions at mildly acidic pH (~6-7).",
            },
          }),
          block("text", { text: "Regenerated answer (this is branch 2 of 2)." }),
        ],
        { branch: { index: 2, total: 2 } },
      ),
    ],
  });
}
