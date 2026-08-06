"""``sources.discover`` — the first-run entry point that finds session data on the machine.

The behaviour that matters is that it works with NO corpus attached: it exists precisely for
the moment the app has nothing, so requiring one would make it unreachable exactly when it is
needed. The rest pins the RPC boundary — a JSON-serialisable payload, sanitized strings, and
a translated error — because the scanning itself is already covered by ``test_discover.py``
and re-testing it here would duplicate that suite rather than test the wiring.

No real session data is read. `discover.discover` is substituted where a fixed payload is what
is under test, and the one live call asserts only structure.
"""
import json

import pytest

from llm_anthology import discover, sidecar


def _fake_result(findings=()):
    """A ScanResult built from real dataclasses, so as_dict() is the production one."""
    return discover.ScanResult(
        findings=tuple(findings),
        stats=discover.ScanStats(
            elapsed_seconds=0.5, roots_scanned=3, dirs_visited=10,
            files_examined=99, budget_exhausted=False,
            truncated_groups=(), errors=(),
        ),
    )


def _finding(path, provider="codex", kind=None, detail=None):
    return discover.Finding(
        provider=provider,
        kind=kind or discover.KIND_SESSION_STORE,
        path=path,
        count=7,
        newest_mtime=1.0,
        confidence=discover.CONF_HIGH,
        detail=detail or {},
    )


def test_discover_works_with_NO_corpus_attached(monkeypatch):
    """The load-bearing case. A first-run user has no index, so this must not require one.

    If this ever starts raising -32000 (no corpus attached), auto-detection is unreachable
    for the only user who needs it and the app is a dead end again.
    """
    monkeypatch.setattr(
        discover, "discover",
        lambda: _fake_result([_finding("C:/synthetic/.codex")]))

    out = sidecar.Sidecar(None).dispatch("sources.discover", {})

    assert [f["path"] for f in out["findings"]] == ["C:/synthetic/.codex"]
    assert out["stats"]["roots_scanned"] == 3


def test_discover_payload_survives_the_rpc_boundary(monkeypatch):
    """Whatever it returns has to serialise: the transport is line-delimited JSON."""
    monkeypatch.setattr(
        discover, "discover",
        lambda: _fake_result([
            _finding("C:/synthetic/.codex", detail={"rollouts_zst": 2024, "ingestable": 0}),
            _finding("C:/synthetic/idx.sqlite", provider="corpus",
                     kind=discover.KIND_BUILT_INDEX),
        ]))

    out = sidecar.Sidecar(None).dispatch("sources.discover", {})
    payload = json.dumps(out)          # must not raise

    assert "rollouts_zst" in payload
    assert {f["kind"] for f in out["findings"]} == {
        discover.KIND_SESSION_STORE, discover.KIND_BUILT_INDEX}
    # The per-finding contract the UI codes against.
    assert set(out["findings"][0]) == {"provider", "kind", "path", "count",
                                       "newest_mtime", "confidence", "detail"}


def test_discover_sanitizes_strings_bound_for_the_ui(monkeypatch):
    """A FILENAME is attacker-influenceable text heading for a UI.

    Anyone can create a file whose name carries a bidi override or a zero-width joiner; a
    discovery panel then renders it. Sanitizing at the wire means the UI cannot be made to
    display a spoofed path, and it is why this goes through _sanitize_tree rather than
    returning as_dict() directly.
    """
    nasty = "C:/synthetic/\u202egnp.exe"          # U+202E RIGHT-TO-LEFT OVERRIDE
    monkeypatch.setattr(discover, "discover", lambda: _fake_result([_finding(nasty)]))

    out = sidecar.Sidecar(None).dispatch("sources.discover", {})

    assert "\u202e" not in out["findings"][0]["path"], \
        "a bidi override must not reach the UI"


def test_discover_translates_a_rejected_root_into_an_invalid_params_error(monkeypatch):
    """`discover` raises ValueError for a UNC/relative root; the RPC layer owes -32602."""
    def boom():
        raise ValueError("refusing a UNC root: //host/share")
    monkeypatch.setattr(discover, "discover", boom)

    with pytest.raises(sidecar.RpcError) as excinfo:
        sidecar.Sidecar(None).dispatch("sources.discover", {})
    assert excinfo.value.code == -32602
    assert "UNC" in str(excinfo.value.message)


def test_discover_ignores_caller_supplied_roots(monkeypatch):
    """There is no `roots` parameter, and passing one must not become a scan of it.

    Honouring caller roots would turn this into a directory-enumeration primitive against
    any path the engine can read — a capability autodetection does not need. This pins the
    absence, because adding the parameter later would be a silent expansion of what an
    untrusted caller can ask the engine to walk.
    """
    seen = []

    def only_defaults(*args, **kwargs):
        seen.append((args, kwargs))
        return _fake_result()

    monkeypatch.setattr(discover, "discover", only_defaults)

    sidecar.Sidecar(None).dispatch(
        "sources.discover", {"roots": ["C:/somewhere/else", "//host/share"]})

    assert seen == [((), {})], f"discover must be called with no arguments, got {seen}"


def test_discover_runs_for_real_and_reports_structure(monkeypatch):
    """One unmocked call, asserting SHAPE only.

    Grounded in whatever this machine happens to hold, so it cannot assert counts or paths
    without becoming irreproducible. What it does prove is that the real call path — through
    dispatch, through discover, through as_dict and _sanitize_tree — produces something a UI
    can consume, which the mocked tests above cannot establish.
    """
    out = sidecar.Sidecar(None).dispatch("sources.discover", {})

    assert set(out) >= {"findings", "stats"}
    assert isinstance(out["findings"], list)
    assert out["stats"]["roots_scanned"] >= 1
    assert out["stats"]["elapsed_seconds"] >= 0
    json.dumps(out)
    for f in out["findings"]:
        assert f["kind"] in {discover.KIND_BUILT_INDEX, discover.KIND_SESSION_STORE,
                             discover.KIND_EXPORT_FILE}
        # A count and a path are the contract; conversation text never is.
        assert isinstance(f["count"], int)
