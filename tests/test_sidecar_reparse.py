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


@pytest.mark.parametrize("leaf", ["", "gone.jsonl"])
def test_a_missing_path_is_unavailable_rather_than_an_error(tmp_path, leaf):
    """Two ways to have no rollout — an empty column, or a path to a file that is not there.
    Both are "unavailable", neither is an error.

    The missing path is built from `tmp_path` so it is DRIVE-absolute. It used to be the
    literal "/definitely/not/here.jsonl", which is absolute on POSIX but drive-RELATIVE on
    Windows since Python 3.13 changed `ntpath.isabs` (measured: 3.14.6/nt returns False for
    "/x", posixpath returns True). That was harmless while nothing checked absoluteness; now
    that `_reparse_rollout` reuses `_reject_nonlocal_path` it would report a DIFFERENT reason
    on each half of the windows+linux CI matrix. The fixture carried the platform assumption,
    not the code."""
    path = str(tmp_path / leaf) if leaf else ""
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


# ------------------------------------------------------- non-local rollout paths
#
# A SECOND defect in the same function. `rollout_path` arrives from the DATABASE, and every
# other path-bearing entry point in this engine — `index_path`, `codex_home`, `dest_path`,
# `destination_root`, `manifest_path` — is passed through `_reject_nonlocal_path` BEFORE any
# filesystem call. `_reparse_rollout` was the one that was not: it handed the stored string
# straight to `os.path.isfile` / `os.path.isdir`.
#
# On Windows that call is not a passive string test. Resolving `\\attacker\share\x` makes the
# OS open an SMB session to `attacker` and offer the logged-in user's NTLM credentials — the
# hash-leak / relay class. A hostile or corrupted `.sqlite` is enough to carry such a row, and
# `discover` OFFERS found indexes to `corpus.open`, so it need not be an index this machine
# wrote.
#
# These tests replace the provider's existence check with a SPY, for two reasons: it measures
# the property that actually matters — was the filesystem reached AT ALL, not merely what was
# returned — and it means the RED run of this file cannot emit a single SMB packet while the
# guard is still missing.


def _spy_exists(monkeypatch, provider="codex"):
    """Swap `provider`'s existence check for a recorder that always answers "no". Returns the
    list of paths it was asked about, so a test can assert the filesystem was never reached.

    Patching the TABLE rather than `os.path.isfile` keeps the blast radius to one provider and
    mirrors how the rest of this file patches adapter modules."""
    module, funcname, _real = sidecar._REPARSERS[provider]
    asked = []

    def spy(path):
        asked.append(path)
        return False

    monkeypatch.setitem(sidecar._REPARSERS, provider, (module, funcname, spy))
    return asked


def test_the_exists_spy_really_does_fire_for_an_ordinary_path(tmp_path, monkeypatch):
    """DETECTOR CONTROL for every `asked == []` assertion below.

    A spy that never fires would make those assertions vacuous — they would pass just as
    happily against a completely unguarded function. Prove it fires on a path the guard lets
    through before reading any `asked == []` as evidence of anything."""
    asked = _spy_exists(monkeypatch)
    local = str(tmp_path / "rollout.jsonl")
    srv, conn = _server(tmp_path, [("c1", "codex", local)])
    try:
        conv, reason = srv._reparse_rollout(local, "codex")
        assert asked == [local], "the spy never fired, so `asked == []` proves nothing"
        assert conv is None and reason == "rollout unavailable"    # the spy answered "no"
    finally:
        conn.close()


@pytest.mark.parametrize("hostile", [
    r"\\attacker\share\rollout.jsonl",          # the classic UNC / SMB-coercion target
    "//attacker/share/rollout.jsonl",           # same target, forward slashes
    r"\\attacker@SSL\DavWWWRoot\x.jsonl",       # the WebDAV spelling of the same coercion
    r"\\?\UNC\attacker\share\rollout.jsonl",    # a UNC wearing a device prefix
    r"\\?\C:\Windows\x.jsonl",                  # Win32 device namespace
    r"\\.\pipe\evil",                           # device namespace, named pipe
])
def test_a_nonlocal_rollout_path_never_reaches_the_filesystem(tmp_path, monkeypatch, hostile):
    """The security property stated as behaviour: for a UNC or device path the existence check
    is NEVER called. Asserting only on the returned reason would be strictly weaker — an
    implementation could stat the path first and still return a tidy string, having already
    leaked the credential."""
    asked = _spy_exists(monkeypatch)
    srv, conn = _server(tmp_path, [("c1", "codex", hostile)])
    try:
        conv, reason = srv._reparse_rollout(hostile, "codex")
        assert asked == [], (
            "%r reached the filesystem; on Windows that IS the SMB/NTLM coercion" % hostile)
        assert conv is None
        assert "local path" in reason, reason
    finally:
        conn.close()


def test_a_nonlocal_path_is_rejected_for_grok_too_not_just_codex(tmp_path, monkeypatch):
    """Grok's existence check is `os.path.isdir`, which coerces exactly the same SMB auth. The
    guard has to sit ahead of the dispatch, not inside one provider's branch."""
    asked = _spy_exists(monkeypatch, "grok")
    hostile = r"\\attacker\share\session"
    srv, conn = _server(tmp_path, [("c1", "grok", hostile)])
    try:
        conv, reason = srv._reparse_rollout(hostile, "grok")
        assert asked == [] and conv is None and "local path" in reason
    finally:
        conn.close()


def test_a_relative_rollout_path_is_rejected_before_it_resolves_against_the_cwd(
        tmp_path, monkeypatch):
    """`_reject_nonlocal_path` also requires absoluteness, and reusing it WHOLE means
    `_reparse_rollout` inherits that. Deliberate, not incidental: a relative string from the DB
    would otherwise resolve against whatever directory the sidecar process happens to sit in.

    It costs nothing in the sidecar's own world — `corpus.open` already forces `index_path`
    absolute (sidecar.py:760) and `codex_home` absolute (:863), so every path the sidecar
    indexes for itself is absolute. UNVERIFIED: an index built by `python -m llm_anthology`
    from a RELATIVE source root stores relative `rollout_path` values (cli.py absolutizes only
    `--out-index` / `--out-html`), and those conversations would now stub instead of opening.
    The experiment that settles it: build an index via the CLI from a relative root, open it,
    and check whether any conversation reports this rejection reason."""
    asked = _spy_exists(monkeypatch)
    srv, conn = _server(tmp_path, [("c1", "codex", "sessions/rollout.jsonl")])
    try:
        conv, reason = srv._reparse_rollout("sessions/rollout.jsonl", "codex")
        assert asked == [] and conv is None
        assert "absolute" in reason, reason
    finally:
        conn.close()


def test_an_ordinary_local_rollout_still_opens(tmp_path):
    """THE REGRESSION GUARD. A guard that blocks legitimate paths is not a fix. No spy here —
    this goes all the way to the real parser and the real bytes on disk."""
    path = _codex_rollout_file(tmp_path)
    srv, conn = _server(tmp_path, [("c1", "codex", path)])
    try:
        conv, errors = srv._reparse_rollout(path, "codex")
        assert conv is not None, errors
        assert "codex side of the story" in _text_of(conv)
    finally:
        conn.close()


@pytest.mark.skipif(os.name != "nt", reason="drive letters are a Windows path shape")
def test_a_mapped_drive_path_is_treated_as_local(tmp_path, monkeypatch):
    """DISCLOSED RESIDUAL, pinned so nobody mistakes it for coverage.

    `Z:\\share\\x` may be a mapped NETWORK drive, and this guard does NOT reject it. It cannot:
    a mapped drive is textually indistinguishable from a local one, so telling them apart needs
    a WinAPI call (`GetDriveType` == DRIVE_REMOTE) — a second, platform-specific, subtly
    different validator, which is precisely what reusing `_reject_nonlocal_path` exists to
    avoid. Blocking all drive letters instead would reject `D:`/`E:` and every legitimate path
    on this machine.

    The residual is also far smaller than the UNC one: a mapped drive resolves only because the
    user ALREADY authenticated to it, so there is no fresh credential coercion — the SMB
    session exists either way. What this pins is that such a path still reaches the existence
    check rather than being silently rejected."""
    asked = _spy_exists(monkeypatch)
    mapped = r"Z:\share\rollout.jsonl"
    srv, conn = _server(tmp_path, [("c1", "codex", mapped)])
    try:
        conv, reason = srv._reparse_rollout(mapped, "codex")
        assert asked == [mapped], "a drive-letter path must still be offered to the filesystem"
        assert conv is None and reason == "rollout unavailable"
    finally:
        conn.close()


def test_conversation_get_stubs_a_hostile_row_instead_of_raising(tmp_path, monkeypatch):
    """End to end through the RPC the reader pane calls. The rejection must arrive as a STUB,
    not an exception: `_reparse_rollout` is documented never to raise, and `conversation.get`
    is what a UI hits the moment discovery offers it a hostile index."""
    asked = _spy_exists(monkeypatch)
    srv, conn = _server(tmp_path, [("c1", "codex", r"\\attacker\share\x.jsonl")])
    try:
        out = srv.dispatch("conversation.get", {"id": "c1"})
        assert asked == []
        assert out["available"] is False and "local path" in out["reason"]
    finally:
        conn.close()


def test_one_hostile_row_does_not_abort_the_whole_local_research_tier(tmp_path, monkeypatch):
    """`_research_local` loops every row. Letting the guard's RpcError escape would turn ONE
    poisoned row into a total denial of the local synthesis — so the rejection degrades to a
    reason string and the loop simply skips that conversation.

    The spy here WRAPS the real `isfile` instead of replacing it, so the good row is genuinely
    parsed off disk while the hostile row is still proven never to be stat-ed."""
    seen = {}

    class Backend:
        def synthesize(self, prompt):
            seen["prompt"] = prompt
            return "ok"

    module, funcname, real_isfile = sidecar._REPARSERS["codex"]
    asked = []

    def wrapped(path):
        asked.append(path)
        return real_isfile(path)

    monkeypatch.setitem(sidecar._REPARSERS, "codex", (module, funcname, wrapped))
    good = _codex_rollout_file(tmp_path)
    srv, conn = _server(tmp_path, [
        ("c1", "codex", r"\\attacker\share\x.jsonl"),
        ("c2", "codex", good),
    ])
    srv.local_backend = Backend()
    try:
        out = srv.dispatch("research.synthesize", {"tier": "local"})
        assert out["conversation_count"] == 1, out
        assert asked == [good], "the hostile row was stat-ed: %r" % (asked,)
        assert "codex side of the story" in seen["prompt"]
    finally:
        conn.close()


def test_every_provider_the_engine_ingests_is_either_readable_or_explicitly_not(tmp_path):
    """No provider may fall through to a default parser. This is the invariant that stops
    the original defect from coming back the next time an adapter is added.

    Drive-absolute for the same reason as `test_a_missing_path_is_unavailable_rather_than_an
    _error`: "/nope" is drive-RELATIVE on Windows, so it would now be rejected by the path
    guard rather than reported missing, and this invariant would read differently on each half
    of the CI matrix."""
    from llm_anthology import discover

    missing = str(tmp_path / "nope.jsonl")
    srv, conn = _server(tmp_path, [("c1", "codex", "")])
    try:
        for provider in sorted({s.provider for s in discover.PROVIDERS}):
            conv, reason = srv._reparse_rollout(missing, provider)
            assert conv is None
            # Either "there is no file there" or "there is no reader for this" — never a
            # silent success from someone else's parser.
            assert reason in ("rollout unavailable",) or "reader" in reason, (
                "%s -> %r" % (provider, reason))
    finally:
        conn.close()


@pytest.mark.skipif(os.name != "nt", reason="drive-relative is a Windows-only path shape")
def test_a_drive_relative_path_is_rejected_on_windows(tmp_path, monkeypatch):
    """The shape that broke the two fixtures above, pinned as behaviour in its own right.

    "/x" on Windows is DRIVE-relative: it resolves against whatever drive the sidecar process
    is currently on, so the same stored string names a different file depending on the
    process's cwd. Python 3.13 stopped calling that absolute; the guard rejects it, which is
    the right answer for a path arriving from an untrusted index."""
    asked = _spy_exists(monkeypatch)
    srv, conn = _server(tmp_path, [("c1", "codex", "/definitely/not/here.jsonl")])
    try:
        conv, reason = srv._reparse_rollout("/definitely/not/here.jsonl", "codex")
        assert asked == [] and conv is None
        assert "absolute" in reason, reason
    finally:
        conn.close()
