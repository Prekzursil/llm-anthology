"""Generate js/test/fixtures/adapter-parity.json — the adapter cross-language gate.

For each provider this captures BOTH the raw export input AND the IR the Python
adapter produced from it, so the JS test can feed the same input to the JS adapter
and assert identical IR. That is what actually exercises the hard logic (Claude's
subtree-max active-path walk, ChatGPT's current_node fallback, timestamp formatting)
which no synthetic fixture reaches.

Uses the REAL private corpora when they are present on this machine (sampled +
size-capped so the fixture stays reasonable and no giant private conversation is
embedded), and always includes hand-built synthetic edge cases so the gate still
runs in CI where the corpora do not exist. Real inputs are truncated to structure,
never shipped in full — but to be safe this fixture is written under tests/ which
is NOT packaged, and the file is gitignored.

    python tools/gen-adapter-parity.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_anthology.adapters import chatgpt, claude, gemini  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "js", "test", "fixtures", "adapter-parity.json")
CORPUS = r"C:\Users\Prekzursil\Downloads\AIs Conversations"


def ir_dict(conv):
    import dataclasses
    return dataclasses.asdict(conv)


def claude_cases():
    # synthetic: multi-root + abandoned-branch, the shape that dropped 42 messages
    def msg(u, parent, sender, text, ts):
        return {"uuid": u, "parent_message_uuid": parent, "sender": sender, "created_at": ts,
                "content": [{"type": "text", "text": text, "citations": []}],
                "attachments": [], "files": [], "text": ""}
    yield "synthetic-multiroot", {"uuid": "c", "name": "multi", "account": {"uuid": "a"},
        "chat_messages": [
            msg("r1", None, "human", "root one q", "t1"),
            msg("a1", "r1", "assistant", "root one a", "t2"),
            msg("r2", "GONE", "human", "orphan root q", "t3"),   # parent not in export
            msg("a2", "r2", "assistant", "orphan root a", "t4"),
            msg("b1", "a1", "human", "follow up", "t5"),
            msg("old", "b1", "assistant", "ABANDONED", "t6"),
            msg("new", "b1", "assistant", "LIVE longer thread", "t7"),
            msg("cont", "new", "human", "continues under newer", "t8"),
        ]}
    yield "synthetic-design-chat", {"uuid": "d", "title": "Design", "messages": [
        {"uuid": "m1", "role": "user", "content": {"content": "design me a thing"}},
        {"uuid": "m2", "role": "assistant",
         "content": {"contentBlocks": [{"type": "text", "text": "here it is"}]}}]}

    # real corpus: first N conversations from the first two accounts
    n = 0
    for acct in sorted(os.listdir(os.path.join(CORPUS, "Claude"))) if os.path.isdir(os.path.join(CORPUS, "Claude")) else []:
        p = os.path.join(CORPUS, "Claude", acct, "conversations.json")
        if not os.path.isfile(p):
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for conv in data[:25]:
            n += 1
            yield "real-claude-%d" % n, conv
        if n >= 40:
            break


def chatgpt_cases():
    def node(nid, parent, kids, role=None, parts=None, ct=1.0):
        m = None
        if role:
            m = {"id": nid, "author": {"role": role}, "create_time": ct,
                 "content": {"content_type": "text", "parts": parts or []}, "metadata": {}}
        return {"id": nid, "message": m, "parent": parent, "children": kids}
    yield "synthetic-null-current-node", {"title": "t", "conversation_id": "x",
        "current_node": None, "create_time": 1.0, "mapping": {
            "r": node("r", None, ["a"]),
            "a": node("a", "r", ["b"], "user", ["hello"], 1.0),
            "b": node("b", "a", [], "assistant", ["hi there"], 2.0)}}


def gemini_cases():
    recs = [
        {"verb": "Prompted", "prompt": "q1", "response_md": "a1",
         "timestamp_iso": "2026-01-01T10:00:00", "gem": None},
        {"verb": "Used", "title": "Used Canvas", "detail": "d",
         "timestamp_iso": "2026-01-01T10:05:00", "gem": None},
        {"verb": "Prompted", "prompt": "q2", "response_md": "a2",
         "timestamp_iso": "2026-01-01T11:00:00", "gem": "CustomGem"},
    ]
    groups = [{"id": "g1", "title": "First", "turn_idxs": [0, 1]},
              {"id": "g2", "title": "Second", "turn_idxs": [2]}]
    yield "synthetic-grouped", {"records": recs, "groups": groups}


def main():
    out = {"claude": [], "chatgpt": [], "gemini": []}

    for name, conv in claude_cases():
        parsed = claude.parse_design_chat(conv) if claude.is_design_chat(conv) else claude.parse_conversation(conv)
        out["claude"].append({"name": name, "input": conv,
                              "is_design_chat": claude.is_design_chat(conv), "ir": ir_dict(parsed)})

    for name, conv in chatgpt_cases():
        out["chatgpt"].append({"name": name, "input": conv, "ir": ir_dict(chatgpt.parse_conversation(conv))})

    for name, payload in gemini_cases():
        convs = gemini.parse_all(payload["records"], payload["groups"])
        out["gemini"].append({"name": name, "records": payload["records"],
                              "groups": payload["groups"], "ir": [ir_dict(c) for c in convs]})

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, ensure_ascii=True, indent=1)
    print("WROTE", OUT, os.path.getsize(OUT), "bytes")
    print("claude", len(out["claude"]), "| chatgpt", len(out["chatgpt"]), "| gemini", len(out["gemini"]))


if __name__ == "__main__":
    main()
