"""The forensic audit must cover every surface, not just message text."""
from llm_anthology import audit, ir


def test_audit_scans_all_surfaces():
    conv = ir.Conversation(id="c", title="tit​le", provider="claude", turns=[
        ir.Turn("human", [
            ir.Block("attachment", data={"extracted_content": "doc​body", "file_name": "f.pdf"}),
            ir.Block("text", text="msg​text", citations=[{"title": "cit​e", "url": "https://x"}]),
        ]),
        ir.Turn("assistant", [
            ir.Block("tool_use", data={"name": "t", "input": {"q": "in​put"}}),
            ir.Block("tool_result", data={"name": "t", "content": "out​put"}),
        ]),
    ])
    # title + extracted_content + text + citation title + tool input + tool output
    assert len(audit.hidden_char_hits(conv)) >= 5


def test_audit_catches_poisoned_title_alone():
    """A payload hidden only in the TITLE previously reported clean."""
    conv = ir.Conversation(id="c", title="poi​soned", provider="claude",
                           turns=[ir.Turn("human", [ir.Block("text", text="clean")])])
    assert audit.hidden_char_hits(conv)


def test_audit_clean_conversation_reports_nothing():
    conv = ir.Conversation(id="c", title="clean", provider="claude",
                           turns=[ir.Turn("human", [ir.Block("text", text="all normal text")])])
    assert audit.hidden_char_hits(conv) == []
