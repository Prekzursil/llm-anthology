"""`conversation.get` must read a conversation with ITS OWN provider's parser.

The defect: `_reparse_rollout` called `codex_rollout.parse_rollout_file` unconditionally,
ignoring the `provider` column sitting in the very row that supplied the path. Two distinct
consequences, and the quiet one is worse:

  * **Grok is unreadable.** Its `rollout_path` is a session DIRECTORY, so the
    `os.path.isfile` guard rejected it and every Grok conversation reported "rollout
    unavailable" — indistinguishable from a deleted file.
  * **Claude Code is read WRONG.** Its `rollout_path` is a `.jsonl`, so the guard passes and
    the Codex parser happily consumes it. Both are JSONL with a `type` field, so nothing
    raises; it just yields a different conversation from the one on disk. A reader that
    shows the wrong transcript is worse than one that shows none, because nothing signals it.

There is no `_reparse` for the EXPORT-based providers (chatgpt/claude/gemini): one export
file holds many conversations, so a path alone does not identify one. Those must say so
rather than be handed to whichever parser happens to be wired.
"""
import os

import pytest

from llm_anthology import corpus, index, ir, sidecar
from llm_anthology.adapters import claude_code, codex_rollout


# ------------------------------------------------------------------ fixtures on disk

def _codex_rollout_file(tmp_path):
    """A minimal but REAL Codex rollout, parsed by the real adapter in the assertions."""
    path = tmp_path / "rollout-2026-08-07T10-00-00-abc.jsonl"
    path.write_text(
        '{"timestamp":"2026-08-07T10:00:00.000Z","type":"session_meta",'
        '"payload":{"id":"sess-codex","cwd":"/w","originator":"codex"}}\n'
        '{"timestamp":"2026-08-07T10:00:01.000Z","type":"response_item",'
        '"payload":{"type":"message","role":"user",'
        '"content":[{"type":"input_text","text":"codex side of the story"}]}}\n',
        encoding="utf-8")
    return str(path)


def _claude_code_transcript(tmp_path):
    """A Claude Code transcript. Also `.jsonl` with a `type` field, which is exactly why
    the Codex parser accepts it without complaint."""
    projects = tmp_path / "projects" / "-w-proj"
    projects.mkdir(parents=True)
    path = projects / "11111111-2222-3333-4444-555555555555.jsonl"
    path.write_text(
        '{"type":"user","uuid":"u1","sessionId":"sess-cc","cwd":"/w/proj",'
        '"timestamp":"2026-08-07T10:00:00.000Z",'
        '"message":{"role":"user","content":"claude code side of the story"}}\n',
        encoding="utf-8")
    return str(path)


def _grok_session_dir(tmp_path):
    """A Grok session DIRECTORY — the shape the isfile guard silently rejected."""
    session = tmp_path / "grok" / "sessions" / "sess-grok"
    session.mkdir(parents=True)
    (session / "summary.json").write_text(
        '{"info":{"id":"sess-grok","title":"grok side of the story"}}', encoding="utf-8")
    (session / "updates.jsonl").write_text(
        '{"method":"session/update","timestamp":"2026-08-07T10:00:00.000Z",'
        '"params":{"sessionId":"sess-grok","_meta":{},"update":'
        '{"sessionUpdate":"user_message_chunk","_meta":{},'
        '"content":{"type":"text","text":"grok side of the story"}}}}\n',
        encoding="utf-8")
    return str(session)


def _server(tmp_path, rows):
    """A sidecar over an index holding one row per (id, provider, path)."""
    conn = corpus.open_index(":memory:")
    records = [ir.Conversation(id=cid, title=cid, provider=provider,
                               turns=[ir.Turn(role="human",
                                              blocks=[ir.Block(type="text", text=cid)])])
               for cid, provider, _ in rows]
    index.build_index(conn, [index.IndexSource(
        file="f.jsonl", content_hash=index.hash_content("v1"), records=records)])
    # `rollout_path` is set by the loader in production; set it directly here so the test
    # exercises the reader rather than re-testing ingest.
    for cid, _provider, path in rows:
        conn.execute("UPDATE conversations SET rollout_path=? WHERE conversation_id=?",
                     (path, cid))
    return sidecar.Sidecar(conn), conn


def _text_of(conv):
    return " ".join(b.text for t in conv.turns for b in t.blocks if b.type == "text")


# ------------------------------------------------------------------ per-provider reads

def test_a_codex_conversation_reads_through_the_codex_parser(tmp_path):
    path = _codex_rollout_file(tmp_path)
    srv, conn = _server(tmp_path, [("c1", "codex", path)])
    try:
        conv, errors = srv._reparse_rollout(path, "codex")
        assert conv is not None, errors
        assert "codex side of the story" in _text_of(conv)
    finally:
        conn.close()


def test_a_grok_conversation_reads_through_the_grok_parser(tmp_path):
    """Was ALWAYS a stub: rollout_path is a directory, and the guard was isfile."""
    path = _grok_session_dir(tmp_path)
    assert os.path.isdir(path) and not os.path.isfile(path)
    srv, conn = _server(tmp_path, [("c1", "grok", path)])
    try:
        conv, errors = srv._reparse_rollout(path, "grok")
        assert conv is not None, "a Grok session directory must be readable, got %r" % (errors,)
        assert "grok side of the story" in _text_of(conv)
    finally:
        conn.close()


def test_a_claude_code_conversation_reads_through_the_claude_code_parser(tmp_path):
    path = _claude_code_transcript(tmp_path)
    srv, conn = _server(tmp_path, [("c1", "claude-code", path)])
    try:
        conv, errors = srv._reparse_rollout(path, "claude-code")
        assert conv is not None, errors
        assert "claude code side of the story" in _text_of(conv)
    finally:
        conn.close()


def test_the_codex_parser_really_does_misread_a_claude_code_transcript(tmp_path):
    """BOTH-STATES CONTROL for the quiet half of the defect.

    If the Codex parser happened to produce the same conversation, the dispatch above would
    be a refactor rather than a fix and this suite would be measuring nothing. It does not
    raise — that is the whole problem — it silently returns different content."""
    path = _claude_code_transcript(tmp_path)
    wrong, _ = codex_rollout.parse_rollout_file(path)
    right, _ = claude_code.parse_transcript_file(path)
    assert "claude code side of the story" in _text_of(right.conversation)
    assert _text_of(wrong.conversation) != _text_of(right.conversation), (
        "the Codex parser reproduced the Claude Code transcript exactly, so this test "
        "cannot detect the misread it exists to prove")


# ------------------------------------------------------------------ honest failures

def test_an_export_only_provider_says_it_has_no_reader(tmp_path):
    """chatgpt/claude/gemini arrive as ONE export holding many conversations, so a path
    does not identify one. Saying so beats feeding the file to the Codex parser."""
    path = _codex_rollout_file(tmp_path)
    srv, conn = _server(tmp_path, [("c1", "chatgpt", path)])
    try:
        conv, reason = srv._reparse_rollout(path, "chatgpt")
        assert conv is None
        assert "chatgpt" in reason and "reader" in reason, reason
    finally:
        conn.close()


def test_an_unknown_provider_is_a_reason_not_a_crash(tmp_path):
    srv, conn = _server(tmp_path, [("c1", "codex", "")])
    try:
        conv, reason = srv._reparse_rollout("/nope", "martian-llm")
        assert conv is None and "martian-llm" in reason
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["", "/definitely/not/here.jsonl"])
def test_a_missing_path_is_unavailable_rather_than_an_error(tmp_path, path):
    srv, conn = _server(tmp_path, [("c1", "codex", path)])
    try:
        conv, reason = srv._reparse_rollout(path, "codex")
        assert conv is None and reason == "rollout unavailable"
    finally:
        conn.close()


def test_a_grok_path_that_is_a_FILE_is_unavailable_not_a_crash(tmp_path):
    """Each provider's existence check matches its own on-disk shape: a file where Grok
    expects a directory is 'unavailable', not an OSError escaping the RPC."""
    lone = tmp_path / "not-a-session.jsonl"
    lone.write_text("{}", encoding="utf-8")
    srv, conn = _server(tmp_path, [("c1", "grok", str(lone))])
    try:
        conv, reason = srv._reparse_rollout(str(lone), "grok")
        assert conv is None and reason == "rollout unavailable"
    finally:
        conn.close()


def test_an_unreadable_rollout_degrades_to_a_reason(tmp_path, monkeypatch):
    """A mid-read OSError (the disk goes away, a permission changes) must become a reason
    string, not an exception escaping the RPC.

    Patching the adapter MODULE works because the dispatch table holds the parser by name
    and resolves it with getattr at call time. Had it captured the function object, this
    patch would be ignored and the test would pass while running the real parser."""
    path = _codex_rollout_file(tmp_path)
    srv, conn = _server(tmp_path, [("c1", "codex", path)])

    def boom(_path):
        raise OSError("disk went away")

    monkeypatch.setattr(codex_rollout, "parse_rollout_file", boom)
    try:
        conv, reason = srv._reparse_rollout(path, "codex")
        assert conv is None and "disk went away" in reason
    finally:
        conn.close()


# ------------------------------------------------------------------ through the RPCs

def test_conversation_get_returns_the_grok_transcript_not_a_stub(tmp_path):
    """End to end through the RPC the reader pane actually calls."""
    path = _grok_session_dir(tmp_path)
    srv, conn = _server(tmp_path, [("c1", "grok", path)])
    try:
        out = srv.dispatch("conversation.get", {"id": "c1"})
        blob = repr(out)
        assert "grok side of the story" in blob, blob[:400]
    finally:
        conn.close()


def test_the_local_research_tier_reads_every_provider_not_just_codex(tmp_path):
    """`_research_local` shares this reader. While it hard-coded Codex, a Grok
    conversation contributed nothing to the local synthesis and the count silently
    under-reported."""
    seen = {}

    class Backend:
        def synthesize(self, prompt):
            seen["prompt"] = prompt
            return "ok"

    srv, conn = _server(tmp_path, [
        ("c1", "codex", _codex_rollout_file(tmp_path)),
        ("c2", "grok", _grok_session_dir(tmp_path)),
    ])
    srv.local_backend = Backend()
    try:
        out = srv.dispatch("research.synthesize", {"tier": "local"})
        assert out["conversation_count"] == 2, out
        assert "grok side of the story" in seen["prompt"]
        assert "codex side of the story" in seen["prompt"]
    finally:
        conn.close()


def test_every_provider_the_engine_ingests_is_either_readable_or_explicitly_not(tmp_path):
    """No provider may fall through to a default parser. This is the invariant that stops
    the original defect from coming back the next time an adapter is added."""
    from llm_anthology import discover

    srv, conn = _server(tmp_path, [("c1", "codex", "")])
    try:
        for provider in sorted({s.provider for s in discover.PROVIDERS}):
            conv, reason = srv._reparse_rollout("/nope", provider)
            assert conv is None
            # Either "there is no file there" or "there is no reader for this" — never a
            # silent success from someone else's parser.
            assert reason in ("rollout unavailable",) or "reader" in reason, (
                "%s -> %r" % (provider, reason))
    finally:
        conn.close()
