"""Claude native-export -> IR adapter. Fixtures are SYNTHETIC (mirror the real
schema probed from disk; no real conversation content)."""
from llm_anthology.adapters import claude
from llm_anthology import ir


def _msg(uuid, parent, sender, content, ts, **extra):
    m = {"uuid": uuid, "parent_message_uuid": parent, "sender": sender,
         "created_at": ts, "content": content, "attachments": [], "files": [], "text": ""}
    m.update(extra)
    return m


def _conv(messages, name="Test", uuid="c1"):
    return {"uuid": uuid, "name": name, "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-02T00:00:00Z", "summary": "sum",
            "account": {"uuid": "acc1"}, "chat_messages": messages}


def _design_chat(messages, title="Design", uuid="dc1"):
    """Claude 'design chat' export — a SEPARATE shape from conversations.json."""
    return {"uuid": uuid, "title": title, "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z", "messages": messages}


def test_is_design_chat_distinguishes_the_two_export_shapes():
    assert claude.is_design_chat(_design_chat([]))
    assert not claude.is_design_chat(_conv([]))


def test_design_chat_is_parsed_into_turns():
    """design_chats/*.json use messages[] with `role` + a content DICT, not
    chat_messages[] with `sender` + a content LIST. Real design chats carrying 9
    messages rendered as EMPTY conversations until this was handled."""
    c = claude.parse_design_chat(_design_chat([
        {"uuid": "m1", "role": "user", "created_at": "t1",
         "content": {"content": "hello there"}},
        {"uuid": "m2", "role": "assistant", "created_at": "t2",
         "content": {"contentBlocks": [{"type": "text", "text": "hi back"}]}},
    ]))
    assert isinstance(c, ir.Conversation) and c.provider == "claude" and c.title == "Design"
    assert [t.role for t in c.turns] == ["human", "assistant"]
    assert [b.text for t in c.turns for b in t.blocks] == ["hello there", "hi back"]


def test_design_chat_prefers_content_blocks_over_the_flat_string():
    """contentBlocks is the structured truth; the flat string duplicates it."""
    c = claude.parse_design_chat(_design_chat([
        {"uuid": "m", "role": "assistant",
         "content": {"content": "FLAT", "contentBlocks": [{"type": "text", "text": "BLOCKS"}]}},
    ]))
    texts = [b.text for t in c.turns for b in t.blocks]
    assert "BLOCKS" in texts and "FLAT" not in texts


def test_design_chat_non_text_block_is_not_silently_dropped():
    c = claude.parse_design_chat(_design_chat([
        {"uuid": "m", "role": "assistant",
         "content": {"contentBlocks": [{"type": "artifact", "id": "a1"}]}},
    ]))
    assert "unknown" in [b.type for t in c.turns for b in t.blocks]


def test_design_chat_tool_call_renders_as_tool_use_and_result():
    """Real design chats carry `tool_call` contentBlocks shaped
    {toolCall:{id,name,input,output}} (140 in the live corpus). They must render as
    tool_use (+tool_result) blocks like the regular chat_messages path — NOT fall
    through to a raw `unknown` payload."""
    c = claude.parse_design_chat(_design_chat([
        {"uuid": "m", "role": "assistant", "content": {"contentBlocks": [
            {"type": "tool_call", "toolCall": {
                "id": "tc1", "type": "function", "name": "web_search",
                "input": {"q": "x"}, "output": "3 results"}},
        ]}},
    ]))
    blocks = [b for t in c.turns for b in t.blocks]
    assert [b.type for b in blocks] == ["tool_use", "tool_result"]
    assert blocks[0].data["name"] == "web_search" and blocks[0].data["input"] == {"q": "x"}
    assert blocks[0].data["id"] == "tc1"
    assert blocks[1].data["content"] == "3 results" and blocks[1].data["tool_use_id"] == "tc1"


def test_design_chat_tool_call_without_output_has_no_result_block():
    """1 of the 140 real tool_calls carries no `output` — emit tool_use only, never a
    fabricated empty result."""
    c = claude.parse_design_chat(_design_chat([
        {"uuid": "m", "role": "assistant", "content": {"contentBlocks": [
            {"type": "tool_call", "toolCall": {"id": "t", "name": "run", "input": {}}},
        ]}},
    ]))
    assert [b.type for t in c.turns for b in t.blocks] == ["tool_use"]


def test_design_chat_malformed_tool_call_falls_back_to_unknown():
    """A tool_call block with no toolCall dict must not crash — preserve it as unknown."""
    c = claude.parse_design_chat(_design_chat([
        {"uuid": "m", "role": "assistant",
         "content": {"contentBlocks": [{"type": "tool_call", "toolCall": "oops"}]}},
    ]))
    assert [b.type for t in c.turns for b in t.blocks] == ["unknown"]


def test_design_chat_with_no_messages_is_empty_not_a_crash():
    assert claude.parse_design_chat(_design_chat([])).turns == []


def test_basic_linear_conversation():
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "human", [{"type": "text", "text": "hello", "citations": []}], "2025-01-01T00:00:01Z"),
        _msg("m2", "m1", "assistant", [{"type": "text", "text": "hi", "citations": []}], "2025-01-01T00:00:02Z"),
    ]))
    assert isinstance(c, ir.Conversation)
    assert c.provider == "claude" and c.id == "c1" and c.title == "Test" and c.account == "acc1"
    assert [t.role for t in c.turns] == ["human", "assistant"]
    assert c.turns[0].blocks[0].type == "text" and c.turns[0].blocks[0].text == "hello"


def test_thinking_and_text_blocks():
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "human", [{"type": "text", "text": "q", "citations": []}], "t1"),
        _msg("m2", "m1", "assistant", [
            {"type": "thinking", "thinking": "let me think", "summaries": []},
            {"type": "text", "text": "answer", "citations": []},
        ], "t2"),
    ]))
    blocks = c.turns[1].blocks
    assert [b.type for b in blocks] == ["thinking", "text"]
    assert blocks[0].text == "let me think" and blocks[1].text == "answer"


def test_tool_use_and_result():
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "human", [{"type": "text", "text": "search", "citations": []}], "t1"),
        _msg("m2", "m1", "assistant", [
            {"type": "tool_use", "name": "web_search", "input": {"query": "x"}, "id": "tu1"},
            {"type": "tool_result", "name": "web_search", "content": [{"type": "text", "text": "res"}],
             "tool_use_id": "tu1", "is_error": False},
        ], "t2"),
    ]))
    blocks = c.turns[1].blocks
    assert [b.type for b in blocks] == ["tool_use", "tool_result"]
    assert blocks[0].data["name"] == "web_search" and blocks[0].data["input"] == {"query": "x"}
    assert blocks[1].data["is_error"] is False


def test_token_budget_skipped():
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "human", [{"type": "text", "text": "q", "citations": []}], "t1"),
        _msg("m2", "m1", "assistant", [
            {"type": "token_budget", "remaining": 1000},
            {"type": "text", "text": "a", "citations": []},
        ], "t2"),
    ]))
    assert [b.type for b in c.turns[1].blocks] == ["text"]


def test_unknown_type_passthrough():
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "assistant", [{"type": "future_widget", "payload": 1}], "t1"),
    ]))
    b = c.turns[0].blocks[0]
    assert b.type == "unknown" and b.data["orig_type"] == "future_widget"


def test_branch_picks_latest_and_annotates():
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "human", [{"type": "text", "text": "q", "citations": []}], "2025-01-01T00:00:01Z"),
        _msg("m2a", "m1", "assistant", [{"type": "text", "text": "old", "citations": []}], "2025-01-01T00:00:02Z"),
        _msg("m2b", "m1", "assistant", [{"type": "text", "text": "new", "citations": []}], "2025-01-01T00:00:03Z"),
    ]))
    assert [t.role for t in c.turns] == ["human", "assistant"]
    assert c.turns[1].blocks[0].text == "new"           # latest branch is the active path
    assert c.turns[1].branch == {"index": 2, "total": 2}


def test_multi_root_conversation_loses_nothing():
    """A message whose parent_message_uuid points outside the set starts a SECOND
    root. Walking only one root silently dropped whole subtrees (42 real messages
    across 18 conversations on disk). Every root must be walked."""
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "human", [{"type": "text", "text": "a", "citations": []}], "2025-01-01T00:00:01Z"),
        _msg("m2", "m1", "assistant", [{"type": "text", "text": "b", "citations": []}], "2025-01-01T00:00:02Z"),
        _msg("m3", "GONE", "human", [{"type": "text", "text": "c", "citations": []}], "2025-01-01T00:00:03Z"),
        _msg("m4", "m3", "assistant", [{"type": "text", "text": "d", "citations": []}], "2025-01-01T00:00:04Z"),
    ]))
    assert [b.text for t in c.turns for b in t.blocks] == ["a", "b", "c", "d"]


def test_active_path_follows_live_thread_not_newest_immediate_reply():
    """Greedy 'newest child' picks an abandoned regeneration and drops the whole
    live thread. The active path must descend toward the subtree containing the
    globally newest message."""
    c = claude.parse_conversation(_conv([
        _msg("q", None, "human", [{"type": "text", "text": "q", "citations": []}], "2025-01-01T00:00:01Z"),
        _msg("a1", "q", "assistant", [{"type": "text", "text": "a1", "citations": []}], "2025-01-01T00:00:02Z"),
        _msg("f1", "a1", "human", [{"type": "text", "text": "f1", "citations": []}], "2025-01-01T00:00:10Z"),
        _msg("f2", "f1", "assistant", [{"type": "text", "text": "f2", "citations": []}], "2025-01-01T00:00:20Z"),
        _msg("a2", "q", "assistant", [{"type": "text", "text": "a2", "citations": []}], "2025-01-01T00:00:03Z"),
    ]))
    assert [b.text for t in c.turns for b in t.blocks] == ["q", "a1", "f1", "f2"]


def test_unreachable_cycle_nodes_are_not_dropped():
    """x<->y form a cycle reachable from NO root, so no walk visits them. Their
    text is still real content and must be swept in, not silently lost."""
    c = claude.parse_conversation(_conv([
        _msg("r1", None, "human", [{"type": "text", "text": "root", "citations": []}], "2025-01-01T00:00:01Z"),
        _msg("x", "y", "human", [{"type": "text", "text": "xxcontent", "citations": []}], "2025-01-01T00:00:02Z"),
        _msg("y", "x", "assistant", [{"type": "text", "text": "yycontent", "citations": []}], "2025-01-01T00:00:03Z"),
    ]))
    texts = [b.text for t in c.turns for b in t.blocks]
    assert "xxcontent" in texts and "yycontent" in texts


def test_cycle_does_not_hang():
    c = claude.parse_conversation(_conv([
        _msg("x", "y", "human", [{"type": "text", "text": "1", "citations": []}], "t1"),
        _msg("y", "x", "assistant", [{"type": "text", "text": "2", "citations": []}], "t2"),
    ]))
    assert len(c.turns) <= 2      # terminates


def test_attachments_and_files():
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "human", [{"type": "text", "text": "see file", "citations": []}], "t1",
             attachments=[{"file_name": "doc.pdf", "file_type": "pdf", "file_size": 10,
                           "extracted_content": "body text"}],
             files=[{"file_name": "img.png", "file_uuid": "f1"}]),
    ]))
    types = [b.type for b in c.turns[0].blocks]
    assert "attachment" in types and "file" in types
    att = next(b for b in c.turns[0].blocks if b.type == "attachment")
    assert att.data["file_name"] == "doc.pdf" and att.data["extracted_content"] == "body text"


def test_citations_attached_to_text():
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "human", [{"type": "text", "text": "q", "citations": []}], "t1"),
        _msg("m2", "m1", "assistant",
             [{"type": "text", "text": "a", "citations": [{"url": "https://ex.com", "title": "Ex"}]}], "t2"),
    ]))
    tb = c.turns[1].blocks[0]
    assert tb.citations and tb.citations[0]["url"] == "https://ex.com"


def test_tool_display_content_as_list_does_not_crash():
    # real exports carry structured (list) display_content; Block.text must stay a str
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "assistant", [
            {"type": "tool_use", "name": "t", "input": {}, "display_content": [{"type": "text", "text": "x"}]},
            {"type": "tool_result", "name": "t", "content": "ok", "display_content": [{"a": 1}]},
        ], "t1"),
    ]))
    for b in c.turns[0].blocks:
        assert isinstance(b.text, str)


def test_non_string_text_fields_coerced():
    c = claude.parse_conversation(_conv([
        _msg("m1", None, "human", [{"type": "text", "text": ["oops list"], "citations": []}], "t1"),
    ]))
    assert c.turns[0].blocks[0].text == ""


def test_parse_export_returns_all_conversations():
    export = [
        _conv([_msg("m1", None, "human", [{"type": "text", "text": "a", "citations": []}], "t1")], name="A", uuid="a"),
        _conv([_msg("n1", None, "human", [{"type": "text", "text": "b", "citations": []}], "t1")], name="B", uuid="b"),
    ]
    assert [c.title for c in claude.parse_export(export)] == ["A", "B"]
