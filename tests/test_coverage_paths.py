"""Targeted tests for the error / edge branches that the feature tests do not reach.

These exist to keep the Lean 100% coverage gate honest WITHOUT function-level
pragmas that would hide real logic: every branch here is a genuine path (a malformed
file, an adapter that raises, a block type variant, a defanged unsafe URL), exercised
with a real input rather than excluded.
"""
import json

import pytest

from llm_anthology import audit, build, ir, loaders, render_html, render_md, verify
from llm_anthology.adapters import chatgpt, claude, gemini


# --------------------------------------------------------------------- loaders

def _write(p, obj):
    p.write_text(json.dumps(obj), encoding="utf-8")


def test_load_claude_reports_malformed_json(tmp_path):
    (tmp_path / "conversations.json").write_text("{not json", encoding="utf-8")
    convs, errors = loaders.load_claude(str(tmp_path), str(tmp_path / "out"))
    assert convs == [] and errors[0]["stage"] == "parse"


def test_load_claude_reports_adapter_failure(tmp_path, monkeypatch):
    _write(tmp_path / "conversations.json", [{"uuid": "c", "chat_messages": []}])
    monkeypatch.setattr(claude, "parse_export", lambda d: (_ for _ in ()).throw(ValueError("boom")))
    convs, errors = loaders.load_claude(str(tmp_path), str(tmp_path / "out"))
    assert convs == [] and errors[0]["stage"] == "adapt"


def test_load_chatgpt_reports_malformed_json_and_skips_junk(tmp_path):
    (tmp_path / "cg.json").write_text("{bad", encoding="utf-8")
    convs, errors, _ = loaders.load_chatgpt(str(tmp_path / "cg.json"))
    assert convs == [] and errors[0]["stage"] == "parse"


def test_load_chatgpt_skips_non_dict_and_idless_records(tmp_path):
    _write(tmp_path / "cg.json", ["a string", 42, {"no": "id"},
                                  {"conversation_id": "x", "mapping": {}, "current_node": None}])
    convs, errors, _ = loaders.load_chatgpt(str(tmp_path / "cg.json"))
    assert len(convs) == 1 and not errors


def test_load_chatgpt_reports_adapter_failure(tmp_path, monkeypatch):
    _write(tmp_path / "cg.json", [{"conversation_id": "x", "mapping": {}}])
    monkeypatch.setattr(chatgpt, "parse_conversation", lambda c: (_ for _ in ()).throw(ValueError("x")))
    convs, errors, _ = loaders.load_chatgpt(str(tmp_path / "cg.json"))
    assert convs == [] and errors[0]["stage"] == "adapt"


def test_load_chatgpt_reads_a_sharded_export_directory(tmp_path):
    """A real ChatGPT Data Export ships the corpus SHARDED across
    conversations-000.json ... conversations-NNN.json (17 shards / 1613 conversations
    in the observed export), so a DIRECTORY must load every shard, not one file."""
    _write(tmp_path / "conversations-000.json", [{"conversation_id": "a", "mapping": {}}])
    _write(tmp_path / "conversations-001.json", [{"conversation_id": "b", "mapping": {}}])
    _write(tmp_path / "not-a-conversation.json", [{"conversation_id": "zz", "mapping": {}}])
    convs, errors, _ = loaders.load_chatgpt(str(tmp_path))
    assert sorted(c.id for c in convs) == ["a", "b"] and not errors


def test_load_chatgpt_directory_also_takes_a_plain_conversations_json(tmp_path):
    """Older/renamed exports ship a single conversations.json; a shard glob alone
    would silently load NOTHING from such a directory."""
    _write(tmp_path / "conversations.json", [{"conversation_id": "solo", "mapping": {}}])
    convs, errors, _ = loaders.load_chatgpt(str(tmp_path))
    assert [c.id for c in convs] == ["solo"] and not errors


def test_load_chatgpt_dedupes_a_conversation_present_in_two_shards(tmp_path):
    _write(tmp_path / "conversations-000.json", [{"conversation_id": "dup", "mapping": {}}])
    _write(tmp_path / "conversations-001.json", [{"conversation_id": "dup", "mapping": {}}])
    convs, _, _ = loaders.load_chatgpt(str(tmp_path))
    assert [c.id for c in convs] == ["dup"]


def test_load_gemini_reports_malformed_transcript(tmp_path):
    (tmp_path / "t.json").write_text("{bad", encoding="utf-8")
    convs, errors, extra = loaders.load_gemini(str(tmp_path / "t.json"))
    assert convs == [] and errors[0]["stage"] == "parse" and extra == {}


def test_load_gemini_reports_malformed_harvest(tmp_path):
    _write(tmp_path / "t.json", [{"verb": "Prompted", "prompt": "q", "response_md": "a"}])
    (tmp_path / "h.json").write_text("{bad", encoding="utf-8")
    convs, errors, _ = loaders.load_gemini(str(tmp_path / "t.json"), str(tmp_path / "h.json"))
    assert convs == [] and errors[0]["stage"] == "parse"


def test_gemini_harvest_grouping_reports_unmatched_leftovers():
    records = [{"verb": "Prompted", "prompt": "matched"}, {"verb": "Prompted", "prompt": "orphan"}]
    harvest = [{"id": "g", "title": "T", "turns": [{"role": "user", "text": "matched"}]}]
    groups, matched = loaders.gemini_groups_from_harvest(records, harvest)
    assert matched == 1
    assert any(g["id"] == "unmatched" for g in groups)


def test_gemini_gap_heuristic_splits_on_gap_and_gem_change():
    records = [
        {"timestamp_iso": "2026-01-01T10:00:00", "gem": None, "verb": "Prompted", "prompt": "a"},
        {"timestamp_iso": "2026-01-01T10:05:00", "gem": None, "verb": "Prompted", "prompt": "b"},
        {"timestamp_iso": "2026-01-01T13:00:00", "gem": None, "verb": "Prompted", "prompt": "c"},
        {"timestamp_iso": "2026-01-01T13:01:00", "gem": "G", "verb": "Prompted", "prompt": "d"},
        {"timestamp_iso": "not-a-timestamp", "gem": "G", "verb": "Prompted", "prompt": "e"},
    ]
    groups = loaders.gemini_groups_from_gaps(records)     # bad ts -> _gemini_ts None branch
    assert len(groups) >= 3


def test_gemini_harvest_skips_non_user_turns():
    records = [{"verb": "Prompted", "prompt": "the question"}]
    harvest = [{"id": "g", "title": "T", "turns": [
        {"role": "assistant", "text": "the question"},        # non-user turn: skipped
        {"role": "user", "text": "the question"}]}]
    groups, matched = loaders.gemini_groups_from_harvest(records, harvest)
    assert matched == 1


# ------------------------------------------------------------------- render_html

def test_html_missing_theme_yields_empty_css():
    conv = ir.Conversation(id="c", title="t", provider="claude",
                           turns=[ir.Turn("human", [ir.Block("text", text="x")])])
    out = render_html.render_conversation_html(conv, theme="does-not-exist")
    assert "<style></style>" in out


def test_html_covers_every_block_variant():
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("code", text="x=1", data={"language": "python"}),
        ir.Block("event", text="Used Canvas", data={"name": "Used"}),
        ir.Block("file", text="f.png", data={"file_name": "f.png"}),
        ir.Block("media", data={"path": "local.png"}),
        ir.Block("media", data={"path": "https://remote.example/x.png"}),
        ir.Block("tool_result", data={"name": "t", "is_error": True, "content": {"k": "v"}}),
        ir.Block("attachment", text="d.pdf", data={"file_name": "d.pdf", "extracted_content": "doc text"}),
        ir.Block("unknown", data={"orig_type": "future", "x_raw": {"t": "P"}}),
        ir.Block("weird-future-type", text="fallback body"),
    ])])
    out = render_html.render_conversation_html(conv)
    assert 'class="language-python"' in out and "✨" in out and "no bytes in export" in out
    assert 'src="../media/local.png"' in out and "🖼" in out
    assert "err" in out and "Attached document text" in out
    assert "unrendered future block" in out and "fallback body" in out


def test_html_citation_with_unsafe_url_is_inert_span():
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("text", text="a", citations=[{"url": "javascript:alert(1)", "title": "bad"}])])])
    out = render_html.render_conversation_html(conv)
    assert "<span>bad</span>" in out and 'href="javascript:' not in out


def test_html_defangs_non_http_markdown_image():
    # markdown-it refuses javascript: (renders as text), but a relative/non-http src
    # IS emitted as <img> and must be defanged to a labelled chip, never fetched
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("text", text="![alt](assets/local.png)")])])
    out = render_html.render_conversation_html(conv)
    assert "image unavailable" in out and "<img" not in out


def test_html_unknown_block_with_unserialisable_payload():
    class Bad:
        pass
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("unknown", data={"orig_type": "x", "x_raw": {"o": Bad()}})])])
    out = render_html.render_conversation_html(conv)          # must not raise
    assert "unrendered x block" in out


def test_harden_links_drops_hrefless_anchor():
    # markdown-it never emits one, but the fail-closed guard must survive a fragment
    assert render_html._harden_links('<a name="x">t</a>') == "<a>t</a>"


# --------------------------------------------------------------------- render_md

def test_md_covers_every_block_variant():
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("tool_use", text="disp", data={"name": "s", "input": {"q": 1}}),
        ir.Block("tool_result", text="disp", data={"name": "t", "is_error": True, "content": {"k": 1}}),
        ir.Block("code", text="x=1", data={"language": "py"}),
        ir.Block("event", text="Used", data={"name": "Used"}),
        ir.Block("media", data={"path": "https://remote.example/x.png"}),
        ir.Block("weird-future-type", text="fallback md"),
    ])])
    md = render_md.render_conversation_md(conv)
    assert "Tool call" in md and "error" in md and "```py" in md
    assert "✨" in md and "remote media" in md and "fallback md" in md


def test_md_unknown_block_with_unserialisable_payload():
    class Bad:
        pass
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("unknown", data={"orig_type": "x", "x_raw": {"o": Bad()}})])])
    md = render_md.render_conversation_md(conv)          # must not raise; body = str(raw)
    assert "unrendered x block" in md


def test_md_skips_non_dict_citation():
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("text", text="a", citations=["not a dict", {"url": "https://ok.example", "title": "T"}])])])
    md = render_md.render_conversation_md(conv)
    assert "https://ok.example" in md


# ------------------------------------------------------------------------ audit

def test_audit_handles_unserialisable_blob_payload():
    class Bad:
        pass
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("tool_use", data={"name": "n", "input": {"o": Bad()}})])])
    assert audit.hidden_char_hits(conv) == []                 # unserialisable -> skipped, no crash


# --------------------------------------------------------------------- adapters

def test_claude_account_as_plain_string():
    c = claude.parse_conversation({"uuid": "c", "name": "n", "account": "just-a-string",
                                   "chat_messages": []})
    assert c.account == "just-a-string"


def test_claude_design_chat_flat_string_and_non_text_block():
    c = claude.parse_design_chat({"uuid": "d", "title": "T", "messages": [
        {"uuid": "m1", "role": "user", "content": "flat string body"},
        {"uuid": "m2", "role": "assistant",
         "content": {"contentBlocks": ["not a dict", {"type": "img", "id": "a"}]}},  # non-dict block skipped
        {"uuid": "m3", "role": "assistant", "content": 12345},          # non str/dict -> no blocks
        "a bare string, not a message dict",                            # non-dict message skipped
    ]})
    kinds = [b.type for t in c.turns for b in t.blocks]
    assert "text" in kinds and "unknown" in kinds


def test_chatgpt_unknown_content_type_becomes_unknown_block():
    c = chatgpt.parse_conversation({"title": "t", "id": "x", "current_node": "a", "mapping": {
        "a": {"id": "a", "parent": None, "children": [],
              "message": {"id": "a", "author": {"role": "assistant"}, "create_time": 1.0,
                          "content": {"content_type": "tether_quote", "url": "u"}, "metadata": {}}}}})
    assert c.turns[0].blocks[0].type == "unknown"


def test_chatgpt_out_of_range_timestamp_degrades_to_empty():
    c = chatgpt.parse_conversation({"title": "t", "id": "x", "current_node": "a", "create_time": 1e20,
        "mapping": {"a": {"id": "a", "parent": None, "children": [],
                          "message": {"id": "a", "author": {"role": "user"}, "create_time": 1e20,
                                      "content": {"content_type": "text", "parts": ["hi"]}, "metadata": {}}}}})
    assert c.created_at == "" and c.turns[0].timestamp == ""


def test_chatgpt_reasoning_recap_content_string_becomes_thinking():
    c = chatgpt.parse_conversation({"title": "t", "id": "x", "current_node": "a", "mapping": {
        "a": {"id": "a", "parent": None, "children": [],
              "message": {"id": "a", "author": {"role": "assistant"}, "create_time": 1.0,
                          "content": {"content_type": "reasoning_recap", "content": "recap body"},
                          "metadata": {}}}}})
    assert c.turns[0].blocks[0].type == "thinking" and "recap body" in c.turns[0].blocks[0].text


def test_chatgpt_multimodal_unrecognised_part_becomes_unknown():
    """A part type the adapter has never seen must be preserved as `unknown` with its
    raw payload — not dropped. (audio_* and image_asset_pointer are handled types now,
    so this fixture uses a genuinely unrecognised one.)"""
    c = chatgpt.parse_conversation({"title": "t", "id": "x", "current_node": "a", "mapping": {
        "a": {"id": "a", "parent": None, "children": [],
              "message": {"id": "a", "author": {"role": "user"}, "create_time": 1.0,
                          "content": {"content_type": "multimodal_text",
                                      "parts": ["look", {"content_type": "some_future_part", "x": 1}]},
                          "metadata": {}}}}})
    kinds = [b.type for t in c.turns for b in t.blocks]
    assert "text" in kinds and "unknown" in kinds


def test_chatgpt_execution_output_and_empty_are_handled():
    c = chatgpt.parse_conversation({"title": "t", "id": "x", "current_node": "b", "mapping": {
        "a": {"id": "a", "parent": None, "children": ["b"],
              "message": {"id": "a", "author": {"role": "tool"}, "create_time": 1.0,
                          "content": {"content_type": "execution_output", "text": "out"}, "metadata": {}}},
        "b": {"id": "b", "parent": "a", "children": [],
              "message": {"id": "b", "author": {"role": "assistant"}, "create_time": 2.0,
                          "content": {"content_type": "execution_output", "text": ""}, "metadata": {}}}}})
    kinds = [b.type for t in c.turns for b in t.blocks]
    assert "tool_result" in kinds


def test_gemini_out_of_range_index_is_skipped():
    c = gemini.parse_conversation([{"verb": "Prompted", "prompt": "q", "response_md": "a"}],
                                  [0, 5, -1], title="T", conv_id="g")
    assert len([b for t in c.turns for b in t.blocks]) > 0


def test_gemini_string_attachment_and_media():
    c = gemini.parse_conversation([{"verb": "Prompted", "prompt": "q", "response_md": "a",
                                    "attachments": ["file.txt"], "media": ["pic.png"]}],
                                  [0], title="T", conv_id="g")
    kinds = [b.type for t in c.turns for b in t.blocks]
    assert "attachment" in kinds and "media" in kinds


# ------------------------------------------------------------------------ build

def test_build_meta_of_default_uses_account(tmp_path):
    conv = ir.Conversation(id="c", title="t", provider="claude", account="me@x",
                           turns=[ir.Turn("human", [ir.Block("text", text="hi")])])
    build.render_corpus([conv], str(tmp_path), provider="claude")
    assert "me@x" in (tmp_path / "index.html").read_text(encoding="utf-8")


def test_build_records_a_fidelity_failure(tmp_path, monkeypatch):
    conv = ir.Conversation(id="c", title="t", provider="claude",
                           turns=[ir.Turn("human", [ir.Block("text", text="hi")])])
    monkeypatch.setattr(verify, "verify",
                        lambda c, h: {"ok": False, "coverage": 0.5, "missing_tokens": ["gone"]})
    rep = build.render_corpus([conv], str(tmp_path), provider="claude")
    assert rep["fidelity_passed"] == 0 and rep["failed"][0]["coverage"] == 0.5


def test_verify_flags_a_missing_word():
    conv = ir.Conversation(id="c", title="t", provider="claude",
                           turns=[ir.Turn("human", [ir.Block("text", text="uniqueword")])])
    v = verify.verify(conv, "<html><body>nothing here</body></html>")
    assert not v["ok"] and "uniqueword" in v["missing_tokens"] and v["coverage"] < 1


# ------------------------------------- branch coverage: empty / falsy collections

def _empty():
    return ir.Conversation(id="c", title="t", provider="claude", turns=[])


def test_empty_conversation_every_consumer():
    conv = _empty()
    assert "conv-title" in render_html.render_conversation_html(conv)   # turns loop: 0 iters
    assert render_md.render_conversation_md(conv).startswith("# t")     # turns loop: 0 iters
    assert verify.verify(conv, "<html></html>")["ok"]                   # prose loop: 0 iters
    assert audit.hidden_char_hits(conv) == []                           # audit loop: 0 iters


def test_turn_with_no_blocks_renders():
    conv = ir.Conversation(id="c", title="t", provider="claude",
                           turns=[ir.Turn("human", [])])               # blocks loop: 0 iters
    assert "human" in render_html.render_conversation_html(conv)
    assert "Human" in render_md.render_conversation_md(conv)


def test_text_block_without_citations_has_no_sources_line():
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("text", text="answer", citations=[])])])              # citation loop: 0 iters
    assert "Sources:" not in render_md.render_conversation_md(conv)


def test_chatgpt_thinking_with_multiple_thoughts_and_a_body():
    c = chatgpt.parse_conversation({"title": "t", "id": "x", "current_node": "a", "mapping": {
        "a": {"id": "a", "parent": None, "children": [],
              "message": {"id": "a", "author": {"role": "assistant"}, "create_time": 1.0,
                          "content": {"content_type": "thoughts",
                                      "thoughts": [{"summary": "s1", "content": "c1"},
                                                   "not-a-dict",
                                                   {"summary": "s2", "content": "c2"}],
                                      "content_field_ignored": ""},
                          "metadata": {}}}}})
    bodies = [b.text for t in c.turns for b in t.blocks]
    assert "c1" in bodies and "c2" in bodies


def test_chatgpt_text_part_that_is_only_whitespace_is_skipped():
    c = chatgpt.parse_conversation({"title": "t", "id": "x", "current_node": "a", "mapping": {
        "a": {"id": "a", "parent": None, "children": [],
              "message": {"id": "a", "author": {"role": "user"}, "create_time": 1.0,
                          "content": {"content_type": "text", "parts": ["   ", "real"]},
                          "metadata": {}}}}})
    texts = [b.text for t in c.turns for b in t.blocks]
    assert texts == ["real"]


def test_gemini_prompt_event_record_without_media_or_attachments():
    c = gemini.parse_conversation([{"verb": "Used", "title": "Used X", "detail": "d"}],
                                  [0], title="T", conv_id="g")          # attachment/media loops: 0
    assert c.turns[0].blocks[0].type == "event"


# --------------------------------- branch coverage: the remaining falsy-path edges

def test_render_with_empty_provider_meta_line_omitted():
    # meta = provider · account · created; with all empty the `if meta` false path runs
    conv = ir.Conversation(id="c", title="t", provider="", turns=[
        ir.Turn("assistant", [ir.Block("text", text="x")])])
    assert "conv-meta\"></div>" in render_html.render_conversation_html(conv)
    md = render_md.render_conversation_md(conv)
    assert md.splitlines()[1] == ""            # no "> provider · ..." meta line


def test_verify_ignores_non_prose_blocks():
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("tool_use", data={"name": "n", "input": {}}),     # not text/thinking -> skipped
        ir.Block("text", text="realword")])])
    assert verify.prose_tokens(conv) == ["realword"]


def test_audit_skips_non_dict_citation_and_non_string_title():
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("text", text="a", citations=[
            "not a dict",                                     # isinstance(c, dict) false
            {"title": 123, "url": "https://ok.example"}])])])  # c.get("title") not a str
    assert audit.hidden_char_hits(conv) == []                 # no crash, nothing flagged


def test_chatgpt_empty_body_thought_is_skipped():
    c = chatgpt.parse_conversation({"title": "t", "id": "x", "current_node": "a", "mapping": {
        "a": {"id": "a", "parent": None, "children": [],
              "message": {"id": "a", "author": {"role": "assistant"}, "create_time": 1.0,
                          "content": {"content_type": "thoughts",
                                      "thoughts": [{"summary": "", "content": ""},   # body empty -> skip
                                                   {"summary": "s", "content": "kept"}]},
                          "metadata": {}}}}})
    assert [b.text for t in c.turns for b in t.blocks] == ["kept"]


def test_chatgpt_multimodal_part_that_is_neither_str_nor_dict():
    c = chatgpt.parse_conversation({"title": "t", "id": "x", "current_node": "a", "mapping": {
        "a": {"id": "a", "parent": None, "children": [],
              "message": {"id": "a", "author": {"role": "user"}, "create_time": 1.0,
                          "content": {"content_type": "multimodal_text", "parts": [12345, "real"]},
                          "metadata": {}}}}})              # int part: neither str nor dict -> skipped
    assert [b.text for t in c.turns for b in t.blocks] == ["real"]


def test_chatgpt_fallback_tip_keeps_the_first_best_over_a_worse_later_node():
    # current_node missing; the NEWER leaf appears first in iteration order, so a later
    # older node must NOT replace it (the `cand > best` false branch)
    c = chatgpt.parse_conversation({"title": "t", "id": "x", "current_node": None, "mapping": {
        "root": {"id": "root", "parent": None, "children": ["new", "old"], "message": None},
        "new": {"id": "new", "parent": "root", "children": [],
                "message": {"id": "new", "author": {"role": "assistant"}, "create_time": 9.0,
                            "content": {"content_type": "text", "parts": ["NEWER"]}, "metadata": {}}},
        "old": {"id": "old", "parent": "root", "children": [],
                "message": {"id": "old", "author": {"role": "assistant"}, "create_time": 2.0,
                            "content": {"content_type": "text", "parts": ["OLDER"]}, "metadata": {}}}}})
    texts = [b.text for t in c.turns for b in t.blocks]
    assert "NEWER" in texts and "OLDER" not in texts


def test_claude_subtree_max_revisits_a_memoised_node():
    # a branch whose sibling subtrees share a descendant id forces the memo-hit path
    def m(u, parent, ts):
        return {"uuid": u, "parent_message_uuid": parent, "sender": "human", "created_at": ts,
                "content": [{"type": "text", "text": u, "citations": []}], "attachments": [], "files": []}
    c = claude.parse_conversation({"uuid": "c", "name": "n", "account": {"uuid": "a"},
        "chat_messages": [m("r", None, "t1"), m("a", "r", "t2"), m("b", "r", "t3"),
                          m("shared", "a", "t4"), m("shared", "b", "t5")]})  # dup uuid -> memo hit
    assert len(c.turns) >= 3


def test_load_claude_single_file_path():
    import os as _os
    import tempfile
    d = tempfile.mkdtemp()
    p = _os.path.join(d, "export.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump([{"uuid": "c", "name": "n", "chat_messages": []}], fh)
    convs, errors = loaders.load_claude(p, _os.path.join(d, "out"))   # isfile true branch
    assert len(convs) == 1 and not errors


def test_gemini_harvest_prompt_with_no_matching_record():
    records = [{"verb": "Prompted", "prompt": "known"}]
    harvest = [{"id": "g", "title": "T", "turns": [{"role": "user", "text": "unknown prompt"}]}]
    groups, matched = loaders.gemini_groups_from_harvest(records, harvest)  # no match -> empty group
    assert matched == 0 and any(g["id"] == "unmatched" for g in groups)


# --------------------------------- branch coverage: the final falsy-path edges

def test_load_claude_without_out_dir_skips_self_output_filter(tmp_path):
    (tmp_path / "conversations.json").write_text(
        json.dumps([{"uuid": "c", "name": "n", "chat_messages": []}]), encoding="utf-8")
    convs, errors = loaders.load_claude(str(tmp_path))          # out_dir None -> `if out_dir` false
    assert len(convs) == 1 and not errors


def test_gemini_harvest_ignores_records_with_no_prompt():
    records = [{"verb": "Prompted", "prompt": ""}, {"verb": "Prompted", "prompt": "real"}]
    harvest = [{"id": "g", "title": "T", "turns": [{"role": "user", "text": "real"}]}]
    groups, matched = loaders.gemini_groups_from_harvest(records, harvest)   # empty prompt skipped
    assert matched == 1


def test_gemini_harvest_second_turn_finds_record_already_claimed():
    records = [{"verb": "Prompted", "prompt": "dup"}]
    harvest = [{"id": "g", "title": "T", "turns": [
        {"role": "user", "text": "dup"}, {"role": "user", "text": "dup"}]}]  # 2nd: already claimed
    groups, matched = loaders.gemini_groups_from_harvest(records, harvest)
    assert matched == 1


def test_gemini_gap_heuristic_on_empty_records():
    assert loaders.gemini_groups_from_gaps([]) == []           # `if cur` false path


def test_md_attachment_without_extracted_content():
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("human", [
        ir.Block("attachment", data={"file_name": "a.bin", "file_type": "bin", "file_size": 9})])])
    md = render_md.render_conversation_md(conv)                 # `if ex` false path
    assert "a.bin" in md and "extracted content" not in md


def test_md_unknown_block_with_no_payload():
    conv = ir.Conversation(id="c", title="t", provider="claude", turns=[ir.Turn("assistant", [
        ir.Block("unknown", data={"orig_type": "empty"})])])   # x_raw None -> body "" -> `if body` false
    md = render_md.render_conversation_md(conv)
    assert "unrendered empty block" in md and "```json" not in md


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
