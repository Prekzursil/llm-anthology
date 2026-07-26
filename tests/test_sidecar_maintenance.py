"""The maintenance RPC surface — the ONLY destructive surface in the app.

The safety model is the point of these tests, not the CRUD.

THE HANDLE DESIGN. `maintenance` validates paths against the roots carried INSIDE the
preview it is handed. That is sound while a preview can only be built in-process, and it is
precisely what breaks if an RPC layer rebuilds one from client JSON — a forged preview could
name its own store/checkpoint/destination root and the executor would honour them. So
`maintenance.plan` keeps the SERVER's preview under a single-use handle and `maintenance.execute`
runs that object. A client cannot express a forged preview at all, so the class is removed
structurally rather than defended against. The tests below pin the handle lifecycle:
unknown/replayed handles are refused, and a handle survives a WRONG confirmation (so a typo is
correctable without re-planning) but is consumed by an accepted one.

DRY-RUN IS THE DEFAULT on both execute and restore, so a destructive act is always an explicit
second step.

Every path is a tmp_path; no test touches a real session store, and no test emits real SMB.
"""
import os
import sqlite3

import pytest

from llm_anthology import corpus, maintenance, sidecar

_OPEN = []


def _track(conn):
    _OPEN.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _server():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    corpus.init_index(conn)
    _track(conn)
    return sidecar.Sidecar(conn)


def _store(tmp_path, *names):
    """A store root holding one file per name; returns (store_root, [paths])."""
    root = tmp_path / "store"
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        p = root / name
        p.write_text("session-body-%s" % name, encoding="utf-8")
        paths.append(str(p))
    return str(root), paths


def _plan(srv, tmp_path, action, paths, store_root, **kw):
    params = {
        "store_root": store_root,
        "checkpoint_root": str(tmp_path / "checkpoints"),
        "action": action,
        "targets": [{"session_id": "s%d" % i, "file_path": p}
                    for i, p in enumerate(paths)],
    }
    params.update(kw)
    return srv.dispatch("maintenance.plan", params)


# ------------------------------------------------------------------ param guarding

def test_plan_rejects_a_unc_or_relative_root(tmp_path):
    """A UNC root must never reach the engine: os.makedirs on \\\\host\\share initiates an
    outbound SMB/NTLM authentication, which this offline-only tool forbids."""
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    for key, bad in (("store_root", r"\\evil.example\share\store"),
                     ("checkpoint_root", r"\\evil.example\share\cp"),
                     ("destination_root", r"\\evil.example\share\dst"),
                     ("store_root", "relative/store")):
        params = {"store_root": store_root,
                  "checkpoint_root": str(tmp_path / "cp"),
                  "action": "delete",
                  "targets": [{"session_id": "s", "file_path": paths[0]}]}
        params[key] = bad
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("maintenance.plan", params)
        assert ei.value.code == -32602, (key, bad)


def test_plan_rejects_a_non_string_destination_root(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    with pytest.raises(sidecar.RpcError) as ei:
        _plan(srv, tmp_path, "archive", paths, store_root, destination_root=["C:/dst"])
    assert ei.value.code == -32602


def test_a_parent_traversal_root_is_caught_by_the_ENGINE_not_the_rpc_edge(tmp_path):
    """Defence in depth, demonstrated rather than asserted.

    The RPC guard rejects UNC and relative paths, but `C:/store/../evil` is BOTH absolute
    and non-UNC, so it passes the edge and is refused by the engine's own `_require_root`.
    That refusal is a MaintenancePathError — which subclasses both MaintenanceRefused and
    ValueError — and it must surface as a -32602 param error rather than as a generic
    refusal or an internal fault."""
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    traversal = os.path.join(str(tmp_path), "cp", "..", "escaped")
    # the edge lets it through: it is absolute and not UNC
    sidecar._reject_nonlocal_path(traversal, "checkpoint_root")
    with pytest.raises(sidecar.RpcError) as ei:
        _plan(srv, tmp_path, "delete", paths, store_root, checkpoint_root=traversal)
    assert ei.value.code == -32602
    assert ".." in str(ei.value) or "traversal" in str(ei.value).lower()


def test_plan_rejects_an_unknown_action(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    with pytest.raises(sidecar.RpcError) as ei:
        _plan(srv, tmp_path, "obliterate", paths, store_root)
    assert ei.value.code == -32602


def test_plan_rejects_malformed_targets(tmp_path):
    srv = _server()
    store_root, _ = _store(tmp_path, "a.jsonl")
    base = {"store_root": store_root, "checkpoint_root": str(tmp_path / "cp"),
            "action": "delete"}
    for targets in ([], "a.jsonl", [["a"]], [{"session_id": "s"}], [{"file_path": ""}]):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("maintenance.plan", dict(base, targets=targets))
        assert ei.value.code == -32602, targets


def test_execute_rejects_non_boolean_apply_and_bad_confirmation_type(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    preview = _plan(srv, tmp_path, "delete", paths, store_root)
    for bad in ({"plan_id": preview["plan_id"], "apply": "yes"},
                {"plan_id": preview["plan_id"], "confirmation": 5}):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("maintenance.execute", bad)
        assert ei.value.code == -32602


# --------------------------------------------------------------- plan is pure

def test_plan_is_pure_and_touches_nothing(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    before = {p: os.path.getsize(p) for p in paths}
    out = _plan(srv, tmp_path, "delete", paths, store_root)

    assert out["action"] == "delete"
    assert out["plan_id"] == "plan-1"
    assert len(out["allowed"]) == 2
    assert out["requires_typed_confirmation"] is True
    assert out["required_typed_confirmation"]
    # nothing created, nothing changed
    assert not (tmp_path / "checkpoints").exists()
    assert {p: os.path.getsize(p) for p in paths} == before


def test_plan_blocks_a_target_outside_the_store_root(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    outsider = tmp_path / "elsewhere.jsonl"
    outsider.write_text("not in the store", encoding="utf-8")
    out = _plan(srv, tmp_path, "delete", [paths[0], str(outsider)], store_root)
    assert [b["target"]["file_path"] for b in out["blocked"]] == [str(outsider)]
    assert out["blocked"][0]["reason"] == "outside-store-root"
    assert [t["file_path"] for t in out["allowed"]] == [paths[0]]


# --------------------------------------------------------- confirmation + dry run

def test_execute_refuses_without_the_typed_confirmation(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    preview = _plan(srv, tmp_path, "delete", paths, store_root)
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("maintenance.execute",
                     {"plan_id": preview["plan_id"], "confirmation": "wrong", "apply": True})
    assert ei.value.code == sidecar.MAINTENANCE_REFUSED
    assert os.path.isfile(paths[0])          # the file is still there


def test_a_refused_confirmation_leaves_the_handle_usable(tmp_path):
    """A typo must be correctable without forcing a re-plan; an ACCEPTED run consumes it."""
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    preview = _plan(srv, tmp_path, "delete", paths, store_root)
    with pytest.raises(sidecar.RpcError):
        srv.dispatch("maintenance.execute",
                     {"plan_id": preview["plan_id"], "confirmation": "nope", "apply": True})
    # same handle, right phrase -> accepted
    out = srv.dispatch("maintenance.execute", {
        "plan_id": preview["plan_id"],
        "confirmation": preview["required_typed_confirmation"], "apply": True})
    assert out["executed"] is True


def test_execute_defaults_to_a_dry_run_that_changes_nothing(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    preview = _plan(srv, tmp_path, "delete", paths, store_root)
    out = srv.dispatch("maintenance.execute", {
        "plan_id": preview["plan_id"],
        "confirmation": preview["required_typed_confirmation"]})
    assert out["executed"] is False
    assert out["manifest_path"] == ""
    assert os.path.isfile(paths[0])
    assert not (tmp_path / "checkpoints").exists()


# ------------------------------------------------------------- handle lifecycle

def test_an_unknown_or_replayed_handle_is_refused(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    preview = _plan(srv, tmp_path, "delete", paths, store_root)
    phrase = preview["required_typed_confirmation"]

    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("maintenance.execute", {"plan_id": "plan-999",
                                             "confirmation": phrase, "apply": True})
    assert ei.value.code == sidecar.MAINTENANCE_REFUSED

    srv.dispatch("maintenance.execute", {"plan_id": preview["plan_id"],
                                         "confirmation": phrase, "apply": True})
    # the same handle cannot be replayed
    with pytest.raises(sidecar.RpcError) as ei:
        srv.dispatch("maintenance.execute", {"plan_id": preview["plan_id"],
                                             "confirmation": phrase, "apply": True})
    assert ei.value.code == sidecar.MAINTENANCE_REFUSED


def test_plan_ids_are_distinct_per_plan(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    first = _plan(srv, tmp_path, "delete", paths, store_root)
    second = _plan(srv, tmp_path, "delete", paths, store_root)
    assert first["plan_id"] != second["plan_id"]


# ------------------------------------------------------- apply, restore, ledger

def test_apply_then_restore_round_trips_the_file(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    original = open(paths[0], encoding="utf-8").read()
    preview = _plan(srv, tmp_path, "delete", paths, store_root)

    done = srv.dispatch("maintenance.execute", {
        "plan_id": preview["plan_id"],
        "confirmation": preview["required_typed_confirmation"], "apply": True})
    assert done["executed"] is True
    assert done["manifest_path"]
    assert not os.path.isfile(paths[0])              # the delete really happened

    # a restore dry-run changes nothing ...
    dry = srv.dispatch("maintenance.restore", {"manifest_path": done["manifest_path"]})
    assert dry["executed"] is False
    assert not os.path.isfile(paths[0])
    # ... and an applied restore brings the file back byte-identical
    back = srv.dispatch("maintenance.restore",
                        {"manifest_path": done["manifest_path"], "apply": True})
    assert back["executed"] is True
    assert open(paths[0], encoding="utf-8").read() == original


def test_restore_rejects_a_unc_manifest_path_and_bad_flags(tmp_path):
    srv = _server()
    for bad in ({"manifest_path": r"\\evil.example\share\m.json"},
                {"manifest_path": "relative/m.json"},
                {"manifest_path": str(tmp_path / "m.json"), "apply": "yes"},
                {"manifest_path": str(tmp_path / "m.json"), "skip_unaccounted": 1}):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch("maintenance.restore", bad)
        assert ei.value.code == -32602, bad


def test_an_applied_run_lands_in_the_audit_ledger(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    assert srv.dispatch("maintenance.runs", {}) == []

    preview = _plan(srv, tmp_path, "delete", paths, store_root)
    done = srv.dispatch("maintenance.execute", {
        "plan_id": preview["plan_id"],
        "confirmation": preview["required_typed_confirmation"], "apply": True})

    runs = srv.dispatch("maintenance.runs", {})
    assert len(runs) == 1
    assert runs[0]["manifest_path"] == done["manifest_path"]
    assert runs[0]["action"] == "delete"


def test_a_dry_run_does_not_enter_the_ledger(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    preview = _plan(srv, tmp_path, "delete", paths, store_root)
    srv.dispatch("maintenance.execute", {
        "plan_id": preview["plan_id"],
        "confirmation": preview["required_typed_confirmation"]})
    assert srv.dispatch("maintenance.runs", {}) == []


def test_runs_respects_a_limit(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl", "b.jsonl")
    for path in paths:
        preview = _plan(srv, tmp_path, "delete", [path], store_root)
        srv.dispatch("maintenance.execute", {
            "plan_id": preview["plan_id"],
            "confirmation": preview["required_typed_confirmation"], "apply": True})
    assert len(srv.dispatch("maintenance.runs", {})) == 2
    assert len(srv.dispatch("maintenance.runs", {"limit": 1})) == 1


# --------------------------------------------------------------- archive / move

def test_archive_moves_into_the_destination_root(tmp_path):
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    dest = str(tmp_path / "archive")
    preview = _plan(srv, tmp_path, "archive", paths, store_root, destination_root=dest)
    assert len(preview["plan"]) == 1
    assert preview["plan"][0]["destination"].startswith(dest)

    out = srv.dispatch("maintenance.execute", {
        "plan_id": preview["plan_id"],
        "confirmation": preview["required_typed_confirmation"], "apply": True})
    assert out["executed"] is True
    assert not os.path.isfile(paths[0])
    assert os.path.isfile(out["moves"][0]["destination"])


# ------------------------------------------------------------------ no corpus

def test_maintenance_methods_require_a_corpus():
    srv = sidecar.Sidecar(None)
    for method, params in (
            ("maintenance.plan", {"store_root": "C:/s", "checkpoint_root": "C:/c",
                                  "action": "delete",
                                  "targets": [{"session_id": "s", "file_path": "C:/s/a"}]}),
            ("maintenance.execute", {"plan_id": "plan-1"}),
            ("maintenance.restore", {"manifest_path": "C:/c/m.json"}),
            ("maintenance.runs", {})):
        with pytest.raises(sidecar.RpcError) as ei:
            srv.dispatch(method, params)
        assert ei.value.code == sidecar.CORPUS_NOT_INDEXED, method


def test_maintenance_action_enum_values_are_all_plannable(tmp_path):
    """Every action the engine declares must be reachable through the RPC surface — a new
    enum member must not silently be unroutable."""
    srv = _server()
    store_root, paths = _store(tmp_path, "a.jsonl")
    dest = str(tmp_path / "dst")
    for action in maintenance.MaintenanceAction:
        out = _plan(srv, tmp_path, action.value, paths, store_root,
                    destination_root=dest)
        assert out["action"] == action.value
