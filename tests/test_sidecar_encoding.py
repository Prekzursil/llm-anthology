"""The engine must survive text that is not in the machine's ANSI code page.

THE DEFECT. `_dumps` uses `ensure_ascii=False` (sidecar.py), so a response carries real
non-ASCII characters, and `main` handed `serve()` a bare `sys.stdout`. On Windows,
`sys.stdout` defaults to the ANSI code page (cp1252 here) unless `PYTHONIOENCODING` says
otherwise, so the first `─`, `→`, `✓` or CJK character in ANY response killed the process
with `UnicodeEncodeError: 'charmap' codec can't encode character '\\u2500'` — exit 1, zero
bytes written, no JSON-RPC error, the UI just sees the pipe close.

WHY IT WAS INVISIBLE. Two independent masks, and BOTH had to be removed to see it:
  * this machine has `PYTHONIOENCODING=utf-8` persisted at User scope, so the developer's
    box is immune while a stock Windows box is not, and
  * every existing fixture in the suite is pure ASCII, so unsetting the variable alone still
    left all 1,225 tests green.
That is why these tests set BOTH knobs explicitly instead of trusting the ambient
environment: a test that inherits the developer's env measures the developer's env.

Curly quotes and em-dashes deliberately do NOT appear below — cp1252 encodes those, so they
cannot reproduce the failure. The characters used here are the ones agentic transcripts are
actually full of: box drawing, arrows, check marks, CJK, emoji.
"""
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

from llm_anthology import corpus, index, ir, sidecar

# Every one of these is OUTSIDE cp1252.
NON_ANSI = {
    "box-drawing": "─│┌",
    "arrows": "→⇒",
    "check-marks": "✓✗",
    "cjk": "日本語",
    "emoji": "\U0001f600",
    "cyrillic": "да",
}


def _repo_root():
    return pathlib.Path(sidecar.__file__).resolve().parents[1]


def _index_with(path, text):
    conn = corpus.open_index(str(path))
    index.build_index(conn, [index.IndexSource(
        file="f.jsonl", content_hash=index.hash_content("v1"),
        records=[ir.Conversation(
            id="c1", title=text, provider="codex",
            turns=[ir.Turn(role="human", blocks=[ir.Block(type="text", text=text)])])])])
    conn.commit()
    conn.close()


def _spawn(index_path, request, ansi_stdio):
    """Run the real sidecar over real pipes.

    `ansi_stdio=True` REMOVES PYTHONIOENCODING, reproducing a stock Windows box. The Tauri
    host passes a null lpEnvironment (`hardened_spawn.rs`), so the engine inherits whatever
    the user has — which for everyone but this developer is nothing.
    """
    env = dict(os.environ)
    if ansi_stdio:
        env.pop("PYTHONIOENCODING", None)
    else:
        env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "llm_anthology.sidecar", "--index", str(index_path)],
        input=request, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        cwd=str(_repo_root()), env=env, timeout=120)


@pytest.mark.parametrize("name,text", sorted(NON_ANSI.items()))
def test_the_engine_answers_non_ansi_text_on_a_stock_windows_box(tmp_path, name, text):
    """The regression itself. Every one of these killed the process before the fix."""
    path = tmp_path / ("%s.sqlite" % name)
    _index_with(path, "alpha %s bravo" % text)
    proc = _spawn(
        path,
        '{"jsonrpc":"2.0","id":1,"method":"search.query","params":{"q":"alpha"}}\n',
        ansi_stdio=True)
    assert proc.returncode == 0, (
        "engine died on %s with PYTHONIOENCODING unset; stderr tail: %s"
        % (name, (proc.stderr or "").strip().splitlines()[-1:]))
    assert proc.stdout.strip(), "engine wrote nothing"
    reply = json.loads(proc.stdout.strip().splitlines()[0])
    assert "error" not in reply, reply
    # The characters must SURVIVE, not be replaced with "?" — a mojibake transcript is a
    # different bug wearing the same green tick.
    assert text in json.dumps(reply, ensure_ascii=False), (
        "%s was mangled in transit rather than preserved" % name)


def test_the_control_passes_when_the_variable_IS_set(tmp_path):
    """BOTH-STATES CONTROL. If the with-variable run also failed, the test above would be
    measuring something other than the code page and its verdict would be void."""
    path = tmp_path / "control.sqlite"
    _index_with(path, "alpha ─ bravo")
    proc = _spawn(
        path,
        '{"jsonrpc":"2.0","id":1,"method":"search.query","params":{"q":"alpha"}}\n',
        ansi_stdio=False)
    assert proc.returncode == 0 and proc.stdout.strip()


class _Recording(io.StringIO):
    """A stream that records the reconfigure calls made against it."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reconfigured = []

    def reconfigure(self, **kw):
        self.reconfigured.append(kw)


def test_the_real_stdio_IS_forced_to_utf8(monkeypatch):
    """The positive case, driven through the path production uses: no stream arguments, so
    `main` falls back to `sys.stdin`/`sys.stdout`.

    The first version of this test passed the stream EXPLICITLY and asserted it was left
    alone — which is the branch that never reconfigures anything, so it passed before the fix
    existed and measured nothing at all."""
    monkeypatch.setattr(sidecar.sys, "argv", ["prog"])
    out, inp = _Recording(), _Recording("")
    monkeypatch.setattr(sidecar.sys, "stdout", out)
    monkeypatch.setattr(sidecar.sys, "stdin", inp)
    assert sidecar.main() == 0
    assert out.reconfigured == [{"encoding": "utf-8"}], out.reconfigured
    assert inp.reconfigured == [{"encoding": "utf-8"}], inp.reconfigured


def test_an_explicitly_passed_stream_is_left_alone(monkeypatch):
    """A caller that hands in its own stream owns its encoding; silently mutating it would
    be a surprise, and the in-process tests rely on it."""
    monkeypatch.setattr(sidecar.sys, "argv", ["prog"])
    out = _Recording()
    assert sidecar.main(stdin=io.StringIO(""), stdout=out) == 0
    assert out.reconfigured == [], "an explicitly-passed stream was reconfigured"


@pytest.mark.parametrize("attr", [None, "raises"])
def test_a_stream_that_cannot_be_reconfigured_does_not_become_a_startup_crash(
        monkeypatch, attr):
    """Hardening must not introduce a new failure mode. Both shapes are real: a stream with
    no usable `reconfigure` (a plain StringIO, a stdout another tool replaced), and one that
    has the method but rejects the call (already detached from its buffer)."""
    class Awkward(io.StringIO):
        pass

    if attr is None:
        Awkward.reconfigure = None            # present but not callable
    else:
        def _raise(self, **kw):
            raise ValueError("underlying buffer has been detached")
        Awkward.reconfigure = _raise

    monkeypatch.setattr(sidecar.sys, "argv", ["prog"])
    monkeypatch.setattr(sidecar.sys, "stdout", Awkward())
    monkeypatch.setattr(sidecar.sys, "stdin", Awkward(""))
    assert sidecar.main() == 0
