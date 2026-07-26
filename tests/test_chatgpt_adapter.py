"""ChatGPT native export (conversations.json) -> IR.

SYNTHETIC fixtures mirroring the documented export schema. NOTE: this adapter is
NOT yet validated against a real export — the user's Data Export never arrived.
Treat real-data validation as outstanding.

Schema: conversation{title, create_time, mapping{node_id:{id,message,parent,children}},
current_node}; message{author{role}, create_time, content{content_type, parts|text|
thoughts}, end_turn, metadata}. The rendered thread is current_node -> parent -> root,
reversed.
"""
from llm_anthology.adapters import chatgpt
from llm_anthology import ir


def _node(nid, parent, children, role=None, parts=None, ctype="text", **meta):
    msg = None
    if role:
        override = meta.pop("content_override", None)
        content = override or {"content_type": ctype, "parts": parts if parts is not None else []}
        msg = {"id": nid, "author": {"role": role}, "create_time": 1.0, "content": content,
               "metadata": meta.pop("metadata", {}), "end_turn": meta.pop("end_turn", True)}
        msg.update(meta)
    return {"id": nid, "message": msg, "parent": parent, "children": children}


def _conv(nodes, current, title="T", cid="c1"):
    return {"title": title, "id": cid, "create_time": 1.0, "update_time": 2.0,
            "mapping": {n["id"]: n for n in nodes}, "current_node": current}


def test_linearises_current_node_back_to_root():
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["a"]),
        _node("a", "root", ["b"], role="user", parts=["hello"]),
        _node("b", "a", [], role="assistant", parts=["hi there"]),
    ], current="b"))
    assert isinstance(c, ir.Conversation) and c.provider == "chatgpt"
    assert [t.role for t in c.turns] == ["human", "assistant"]
    assert c.turns[0].blocks[0].text == "hello"


def test_abandoned_branch_is_not_rendered():
    """current_node selects the live branch; the other child must not appear."""
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["a"]),
        _node("a", "root", ["b1", "b2"], role="user", parts=["q"]),
        _node("b1", "a", [], role="assistant", parts=["ABANDONED"]),
        _node("b2", "a", [], role="assistant", parts=["LIVE"]),
    ], current="b2"))
    texts = [b.text for t in c.turns for b in t.blocks]
    assert "LIVE" in texts and "ABANDONED" not in texts


def test_system_and_hidden_nodes_are_skipped():
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["s"]),
        _node("s", "root", ["a"], role="system", parts=["SYSTEMPROMPT"]),
        _node("a", "s", [], role="user", parts=["visible"],
              metadata={"is_visually_hidden_from_conversation": False}),
    ], current="a"))
    texts = [b.text for t in c.turns for b in t.blocks]
    assert "SYSTEMPROMPT" not in texts and "visible" in texts


def test_visually_hidden_message_is_skipped():
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["a"]),
        _node("a", "root", ["b"], role="user", parts=["HIDDENCTX"],
              metadata={"is_visually_hidden_from_conversation": True}),
        _node("b", "a", [], role="assistant", parts=["shown"]),
    ], current="b"))
    texts = [b.text for t in c.turns for b in t.blocks]
    assert "HIDDENCTX" not in texts and "shown" in texts


def test_code_content_type_becomes_code_block():
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["a"]),
        _node("a", "root", [], role="assistant", ctype="code", parts=None,
              content_override={"content_type": "code", "language": "python", "text": "x=1"}),
    ], current="a"))
    b = c.turns[0].blocks[0]
    assert b.type == "code" and "x=1" in b.text and b.data.get("language") == "python"


def test_thoughts_become_thinking_blocks():
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["a"]),
        _node("a", "root", [], role="assistant", ctype="thoughts", parts=None,
              content_override={"content_type": "thoughts",
                                "thoughts": [{"summary": "plan", "content": "REASONING"}]}),
    ], current="a"))
    b = c.turns[0].blocks[0]
    assert b.type == "thinking" and "REASONING" in b.text


def test_multimodal_image_asset_becomes_media_block():
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["a"]),
        _node("a", "root", [], role="user", ctype="multimodal_text",
              parts=["look", {"content_type": "image_asset_pointer",
                              "asset_pointer": "file-service://file-ABC"}]),
    ], current="a"))
    kinds = [b.type for t in c.turns for b in t.blocks]
    assert "media" in kinds and "text" in kinds


def _audio_conv(part):
    return _conv([
        _node("root", None, ["a"]),
        _node("a", "root", [], role="user", ctype="multimodal_text", parts=[part]),
    ], current="a")


def test_audio_transcription_becomes_readable_text():
    """Voice-mode conversations carry the SPOKEN WORDS in an audio_transcription
    part (694 in the real export). That is prose a reader must see — rendering it
    as a raw `unknown` payload loses real conversation content."""
    c = chatgpt.parse_conversation(_audio_conv(
        {"content_type": "audio_transcription", "text": "hello out loud",
         "direction": "in", "decoding_id": None}))
    blocks = [b for t in c.turns for b in t.blocks]
    assert [b.type for b in blocks] == ["text"]
    assert blocks[0].text == "hello out loud"


def test_empty_audio_transcription_is_skipped_not_emitted():
    c = chatgpt.parse_conversation(_audio_conv(
        {"content_type": "audio_transcription", "text": "   ", "direction": "in"}))
    assert [b.type for t in c.turns for b in t.blocks] == []


def test_audio_asset_pointer_becomes_media_block():
    c = chatgpt.parse_conversation(_audio_conv(
        {"content_type": "audio_asset_pointer", "asset_pointer": "file-service://file-AUD",
         "format": "wav", "size_bytes": 123, "metadata": {}}))
    blocks = [b for t in c.turns for b in t.blocks]
    assert [b.type for b in blocks] == ["media"]
    assert blocks[0].data["pointer"] == "file-service://file-AUD"
    assert blocks[0].data["path"] == "file-AUD"


def test_realtime_audio_video_pointer_uses_its_nested_audio_asset():
    """The real-time voice part wraps the usable pointer in a NESTED
    audio_asset_pointer dict (347 in the real export)."""
    c = chatgpt.parse_conversation(_audio_conv(
        {"content_type": "real_time_user_audio_video_asset_pointer",
         "audio_asset_pointer": {"content_type": "audio_asset_pointer",
                                 "asset_pointer": "file-service://file-RT"},
         "audio_start_timestamp": 1.0, "frames_asset_pointers": [],
         "video_container_asset_pointer": None}))
    blocks = [b for t in c.turns for b in t.blocks]
    assert [b.type for b in blocks] == ["media"]
    assert blocks[0].data["pointer"] == "file-service://file-RT"


def test_realtime_pointer_without_nested_asset_is_preserved_as_unknown():
    """No usable pointer -> preserve the raw payload rather than drop it."""
    c = chatgpt.parse_conversation(_audio_conv(
        {"content_type": "real_time_user_audio_video_asset_pointer",
         "audio_asset_pointer": None}))
    assert [b.type for t in c.turns for b in t.blocks] == ["unknown"]


def test_consecutive_assistant_nodes_coalesce_into_one_turn():
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["a"]),
        _node("a", "root", ["b"], role="user", parts=["q"]),
        _node("b", "a", ["c"], role="assistant", parts=["part one"], end_turn=False),
        _node("c", "b", [], role="assistant", parts=["part two"], end_turn=True),
    ], current="c"))
    assert [t.role for t in c.turns] == ["human", "assistant"]
    assert [b.text for b in c.turns[1].blocks] == ["part one", "part two"]


def test_cycle_in_parent_chain_terminates():
    c = chatgpt.parse_conversation(_conv([
        _node("x", "y", [], role="user", parts=["1"]),
        _node("y", "x", [], role="assistant", parts=["2"]),
    ], current="x"))
    assert len(c.turns) <= 2


def test_parse_export_handles_multiple_conversations():
    export = [_conv([_node("root", None, ["a"]), _node("a", "root", [], role="user", parts=["p"])],
                    current="a", title="A", cid="a"),
              _conv([_node("root", None, ["b"]), _node("b", "root", [], role="user", parts=["q"])],
                    current="b", title="B", cid="b")]
    assert [c.title for c in chatgpt.parse_export(export)] == ["A", "B"]


def test_missing_current_node_falls_back_to_newest_leaf():
    """Real exports sometimes have current_node null/absent; the conversation must
    still render (walk up from the newest leaf) instead of coming out blank."""
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["a"]),
        _node("a", "root", ["b"], role="user", parts=["hello"]),
        _node("b", "a", [], role="assistant", parts=["reply"]),
    ], current=None))
    assert [t.role for t in c.turns] == ["human", "assistant"]
    assert [b.text for t in c.turns for b in t.blocks] == ["hello", "reply"]


def test_stale_current_node_not_in_mapping_falls_back():
    """current_node pointing at a since-deleted node must not blank the conversation."""
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["a"]),
        _node("a", "root", ["b"], role="user", parts=["hi"]),
        _node("b", "a", [], role="assistant", parts=["yo"]),
    ], current="GHOST-NODE-ID"))
    assert [b.text for t in c.turns for b in t.blocks] == ["hi", "yo"]


def test_newest_leaf_wins_across_branches_when_current_node_missing():
    """With current_node gone and two candidate leaves, the fallback takes the
    NEWER one (by create_time) — the most likely active tip."""
    nodes = [
        _node("root", None, ["a"]),
        _node("a", "root", ["b1", "b2"], role="user", parts=["q"]),
        {"id": "b1", "parent": "a", "children": [],
         "message": {"id": "b1", "author": {"role": "assistant"}, "create_time": 5.0,
                     "content": {"content_type": "text", "parts": ["OLDER"]}, "metadata": {}}},
        {"id": "b2", "parent": "a", "children": [],
         "message": {"id": "b2", "author": {"role": "assistant"}, "create_time": 9.0,
                     "content": {"content_type": "text", "parts": ["NEWER"]}, "metadata": {}}},
    ]
    c = chatgpt.parse_conversation(_conv(nodes, current=None))
    texts = [b.text for t in c.turns for b in t.blocks]
    assert "NEWER" in texts and "OLDER" not in texts


def test_empty_mapping_yields_no_turns_not_a_crash():
    """A conversation with no message nodes at all must degrade to zero turns."""
    c = chatgpt.parse_conversation({"title": "empty", "id": "e", "mapping": {}, "current_node": None})
    assert c.turns == [] and c.title == "empty"


def test_project_tagged_conversation_still_parses():
    """The harvester tags project conversations with __project_id; that extra key
    must not disturb parsing."""
    c = chatgpt.parse_conversation(_conv([
        _node("root", None, ["a"]),
        _node("a", "root", [], role="user", parts=["in a project"]),
    ], current="a") | {"__project_id": "g-p-XYZ"})
    assert [b.text for t in c.turns for b in t.blocks] == ["in a project"]
