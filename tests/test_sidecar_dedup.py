"""The dedup RPC surface — Codex physical copies collapsed to logical sessions, over the wire.

Three properties here are safety, not convenience, and each is pinned:

  * ``codex_home`` is REQUIRED and never defaulted. ``loaders.load_corpus`` with no
    ``codex_home`` falls back to the LIVE Codex store, and an automated probe really did
    read the owner's real sessions that way — so a scan of private data must be something
    the caller named explicitly.
  * ``codex_home`` is refused if it is UNC or relative. A UNC root would make the scan emit
    outbound SMB/NTLM (the Windows hash-leak class) before touching anything.
  * ``has_larger_copy`` must reach the wire. The canonical rule prefers the LIVE store over
    a mirror — correct, since a stale mirror must never outrank the authoritative store —
    but that means a crash-truncated live rollout can win over a COMPLETE backup of the
    same session, and the view would then show the shorter conversation. Nothing is lost on
    disk, so the condition is reported rather than silently resolved.

Synthetic rollouts written into tmp_path only; no test reads a real Codex store.
"""
import json
import os
import sqlite3

import pytest

from llm_anthology import corpus, dedup, sidecar

_OPEN = []

UUID_A = "aaaaaaaa-1111-2222-3333-444444444444"
UUID_B = "bbbbbbbb-1111-2222-3333-444444444444"


def _track(conn):
    _OPEN.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _rollout_lines(session_id, user_text="hello", n_extra=0):
    """A minimal but realistic rollout: a session_meta header plus messages."""
    lines = [{"type": "session_meta",
              "timestamp": "2026-03-23T10:00:00Z",
              "payload": {"session_id": session_id, "cwd": "C:/work",
                          "model_provider": "openai", "git": {"branch": "main"}}},
             {"type": "response_item",
              "timestamp": "2026-03-23T10:00:01Z",
              "payload": {"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": user_text}]}}]
    for i in range(n_extra):
        lines.append({"type": "response_item",
                      "timestamp": "2026-03-23T10:00:02Z",
                      "payload": {"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text",
                                               "text": "reply %d padded out" % i}]}})
    return [json.dumps(line) for line in lines]


def _write_rollout(root, name, lines):
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def _server():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    corpus.init_index(conn)
    _track(conn)
    return sidecar.Sidecar(conn)


# -------------------------------------------------------------- codex_home guarding

def test_dedup_scan_requires_an_explicit_codex_home():
    """Never defaulted — a scan of private data must be asked for by name."""
    srv = _server()
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("dedup.scan", {})
    assert ei.value.code == -32602


def test_dedup_scan_refuses_a_unc_or_relative_codex_home():
    srv = _server()
    for bad in (r"\\evil.example\share\codex", "//evil.example/share/codex",
                "relative/codex", ""):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("dedup.scan", {"codex_home": bad})
        assert ei.value.code == -32602, bad


# ------------------------------------------------------------------------- scanning

def test_dedup_scan_of_a_missing_home_is_empty_not_an_error(tmp_path):
    """A missing store root is an empty result, not a failure."""
    srv = _server()
    out = srv.dispatch("dedup.scan", {"codex_home": str(tmp_path / "nope")})
    assert out["session_count"] == 0
    assert out["copy_count"] == 0
    assert out["errors"] == []


def test_dedup_scan_collapses_the_same_session_across_live_and_backup(tmp_path):
    home = tmp_path / "codex"
    live = home / "sessions"
    backup = home / "sessions_backup"
    _write_rollout(str(live), "rollout-a-%s.jsonl" % UUID_A, _rollout_lines(UUID_A))
    _write_rollout(str(backup), "rollout-a-%s.jsonl" % UUID_A, _rollout_lines(UUID_A))
    _write_rollout(str(live), "rollout-b-%s.jsonl" % UUID_B, _rollout_lines(UUID_B))

    srv = _server()
    out = srv.dispatch("dedup.scan", {"codex_home": str(home)})
    # three files on disk, two logical sessions, one duplicate collapsed
    assert out["copy_count"] == 3
    assert out["session_count"] == 2
    assert out["duplicate_count"] == 1
    assert out["errors"] == []

    rows = srv.dispatch("dedup.sessions", {})
    by_id = {r["session_id"]: r for r in rows}
    assert sorted(by_id) == sorted([UUID_A, UUID_B])
    assert by_id[UUID_A]["copy_count"] == 2
    assert len(by_id[UUID_A]["duplicate_paths"]) == 1
    # the LIVE store is authoritative, so it is the canonical copy
    assert by_id[UUID_A]["store_kind"] == dedup.STORE_LIVE
    assert by_id[UUID_B]["copy_count"] == 1
    assert by_id[UUID_B]["duplicate_paths"] == []


def test_dedup_scan_flags_a_truncated_live_copy_on_the_wire(tmp_path):
    """The crux: a crash-truncated LIVE copy legitimately outranks a COMPLETE backup, so the
    demotion must be REPORTED or the view silently shows the shorter conversation."""
    home = tmp_path / "codex"
    # live copy: minimal. backup copy: same session id, padded much larger.
    _write_rollout(str(home / "sessions"), "rollout-a-%s.jsonl" % UUID_A,
                   _rollout_lines(UUID_A))
    _write_rollout(str(home / "sessions_backup"), "rollout-a-%s.jsonl" % UUID_A,
                   _rollout_lines(UUID_A, n_extra=12))

    srv = _server()
    out = srv.dispatch("dedup.scan", {"codex_home": str(home)})
    assert out["session_count"] == 1
    assert out["flagged_truncated"] == 1

    row = srv.dispatch("dedup.sessions", {})[0]
    assert row["has_larger_copy"] is True
    assert row["store_kind"] == dedup.STORE_LIVE       # canonical rule NOT reversed
    assert len(row["duplicate_paths"]) == 1            # the fuller copy is still offered


def test_dedup_scan_is_idempotent_and_never_drops_a_known_copy(tmp_path):
    """A re-scan updates in place; it must not delete a copy it did not see this time."""
    home = tmp_path / "codex"
    _write_rollout(str(home / "sessions"), "rollout-a-%s.jsonl" % UUID_A,
                   _rollout_lines(UUID_A))
    srv = _server()
    first = srv.dispatch("dedup.scan", {"codex_home": str(home)})
    second = srv.dispatch("dedup.scan", {"codex_home": str(home)})
    assert first["copy_count"] == second["copy_count"] == 1
    assert len(srv.dispatch("dedup.sessions", {})) == 1


def test_dedup_sessions_is_empty_before_any_scan():
    assert _server().dispatch("dedup.sessions", {}) == []


# ------------------------------------------------------------------------ no corpus

def test_dedup_methods_require_a_corpus():
    srv = sidecar.Sidecar(None)
    for method, params in (("dedup.scan", {"codex_home": "C:/x"}),
                           ("dedup.sessions", {})):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch(method, params)
        assert ei.value.code == sidecar.CORPUS_NOT_INDEXED, method
