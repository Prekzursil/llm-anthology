"""Gemini Takeout transcript -> IR. Fixtures are SYNTHETIC (mirror the real
transcript.json schema probed from disk; no real conversation content).

Real schema: each record is one EXCHANGE with keys idx, timestamp, timestamp_iso,
verb, gem, prompt, response_md, attachments, media, title, detail, zero_width.
Verbs on disk: Prompted 967, Used 60, Created Gemini Canvas 26, Gave feedback 6, Selected 1.
"""
from llm_anthology.adapters import gemini
from llm_anthology import ir


def _rec(idx, prompt="q", response="a", verb="Prompted", **extra):
    r = {"idx": idx, "timestamp": "Jan 1, 2025, 1:00:00 PM EEST",
         "timestamp_iso": "2025-01-01T13:00:00", "verb": verb, "gem": None,
         "prompt": prompt, "response_md": response, "attachments": [], "media": [],
         "title": "", "detail": "", "zero_width": []}
    r.update(extra)
    return r


def test_prompted_record_becomes_human_then_model_turn():
    c = gemini.parse_conversation([_rec(0, "hello", "hi there")], [0], title="T", conv_id="g1")
    assert isinstance(c, ir.Conversation) and c.provider == "gemini"
    assert c.id == "g1" and c.title == "T"
    assert [t.role for t in c.turns] == ["human", "assistant"]
    assert c.turns[0].blocks[0].text == "hello"
    assert c.turns[1].blocks[0].text == "hi there"


def test_multiple_records_are_ordered():
    recs = [_rec(0, "q1", "a1"), _rec(1, "q2", "a2")]
    c = gemini.parse_conversation(recs, [0, 1])
    assert [b.text for t in c.turns for b in t.blocks] == ["q1", "a1", "q2", "a2"]


def test_feature_event_verb_becomes_a_single_event_turn():
    """'Used'/'Created Gemini Canvas' are feature events, not prompt/response pairs."""
    c = gemini.parse_conversation([_rec(0, "", "", verb="Used", title="Used Deep Research")], [0])
    assert len(c.turns) == 1
    assert c.turns[0].blocks[0].type == "event"
    assert "Used" in c.turns[0].blocks[0].data.get("name", "")


def test_attachments_and_media_become_blocks():
    c = gemini.parse_conversation([_rec(
        0, "see this",
        attachments=[{"name": "doc.pdf", "on_disk": "media/doc.pdf", "resolved": True}],
        media=["shot.png"])], [0])
    kinds = [b.type for t in c.turns for b in t.blocks]
    assert "attachment" in kinds and "media" in kinds


def test_media_accepts_dict_or_string_entries():
    c = gemini.parse_conversation([_rec(0, "x", media=[{"on_disk": "a.png"}, "b.jpg"])], [0])
    names = [b.data.get("path") for t in c.turns for b in t.blocks if b.type == "media"]
    assert "a.png" in names and "b.jpg" in names


def test_gem_recorded_in_meta():
    c = gemini.parse_conversation([_rec(0, "x", gem="Coding Partner")], [0])
    assert c.meta.get("gems") == ["Coding Partner"]


def test_empty_response_still_yields_a_model_turn():
    c = gemini.parse_conversation([_rec(0, "q", "")], [0])
    assert [t.role for t in c.turns] == ["human", "assistant"]


def test_parse_all_without_grouping_returns_single_conversation():
    convs = gemini.parse_all([_rec(0, "a"), _rec(1, "b")])
    assert len(convs) == 1 and len(convs[0].turns) == 4


def test_parse_all_with_groups_splits():
    groups = [{"id": "c1", "title": "One", "turn_idxs": [0]},
              {"id": "c2", "title": "Two", "turn_idxs": [1]}]
    convs = gemini.parse_all([_rec(0, "a"), _rec(1, "b")], groups)
    assert [c.title for c in convs] == ["One", "Two"]
    assert all(len(c.turns) == 2 for c in convs)
