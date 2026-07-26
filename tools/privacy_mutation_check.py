"""Mutation-pin the Phase-4 privacy fix at BOTH defense layers.

The fix is defense-in-depth: redact.py drops free text from the crossing surface, and
research.py refuses to render it. A test suite that stays GREEN when either layer is
removed is coverage theater (exactly the failure mode that let the original leak ship
under a 100%-covered, "airtight CONFIRMED" suite).

Two independent mutations, applied and reverted ONE AT A TIME so each is measured in
isolation:
  M1  research.py  -> re-emit `title` in the cloud prompt (the pre-fix renderer).
  M2  redact.py    -> re-add `title` to MetadataView and populate it from the
                      conversation / row (the pre-fix projection).

Each must turn the privacy suite RED. Exit 0 only if BOTH mutations are caught.

RESTORE DISCIPLINE (learned the hard way): restore from an IN-MEMORY SNAPSHOT of the
exact bytes on disk at start — never `git checkout --`, which silently reverts to HEAD
and destroys an UNCOMMITTED fix. A snapshot restore is correct whether or not the work
has been committed yet.
"""
import pathlib
import subprocess
import sys

REPO = pathlib.Path(r"C:\Users\Prekzursil\.local\opt\ai-sessions-render")
TESTS = ["tests/test_redact.py", "tests/test_research.py",
         "tests/test_redaction_e2e.py", "tests/test_sidecar.py"]

TARGETS = {
    "research": REPO / "aisr" / "research.py",
    "redact": REPO / "aisr" / "redact.py",
}
# the exact BYTES at start — the ONLY source of truth for restoration.
# read_bytes/write_bytes, NOT read_text/write_text: on Windows the text round-trip
# rewrites every LF as CRLF, so a "restored" file is semantically identical but
# byte-different, and git reports the whole file as modified. (Measured: it did.)
SNAPSHOT = {k: p.read_bytes() for k, p in TARGETS.items()}


def _text(key):
    return SNAPSHOT[key].decode("utf-8")


def _write(path, text):
    """Write with explicit LF endings so a mutation never rewrites line endings."""
    path.write_bytes(text.encode("utf-8"))


def _run_suite():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "-o", "addopts="],
        cwd=REPO, capture_output=True, text=True,
    )
    tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()][-1:]
    return r.returncode, (tail[0] if tail else "(no output)")


def _restore_all():
    """Put every target back to its start-of-run bytes."""
    for key, path in TARGETS.items():
        path.write_bytes(SNAPSHOT[key])


def mutate_research():
    """M1: put `title` back into the cloud prompt renderer."""
    p = TARGETS["research"]
    src = _text("research")
    out = src.replace(
        '_ID_FIELDS = ("conversation_id", "provider", "account", "thread_id",\n'
        '              "created_at", "updated_at")',
        '_ID_FIELDS = ("conversation_id", "provider", "account", "thread_id",\n'
        '              "created_at", "updated_at", "title")',
    ).replace(
        '        "  char_count: %d" % p["char_count"],',
        '        "  char_count: %d" % p["char_count"],\n'
        '        "  title: %s" % p["title"],',
    )
    assert out != src, "M1 anchors not found — update the mutation script"
    _write(p, out)


def mutate_redact():
    """M2: put the `title` field back on the crossing surface."""
    p = TARGETS["redact"]
    src = _text("redact")
    # NOTE: `title: str` with NO default. An earlier version used `title: str = ""`,
    # which put a defaulted field before non-defaulted ones -> the dataclass raised
    # TypeError at import and every test ERRORED. That is a BOGUS mutation: the suite
    # went red because Python rejected the class, not because the leak detector fired.
    # A mutation must produce a WORKING, LEAKY build, or it proves nothing.
    out = src.replace(
        "    conversation_id: str\n    provider: str\n    account: str\n",
        "    conversation_id: str\n    provider: str\n    account: str\n"
        "    title: str\n",
    ).replace(
        "        conversation_id=conv.id,\n        provider=conv.provider,\n"
        "        account=conv.account,\n",
        "        conversation_id=conv.id,\n        provider=conv.provider,\n"
        "        account=conv.account,\n        title=conv.title,\n",
    ).replace(
        '        conversation_id=d.get("conversation_id", ""),\n'
        '        provider=d.get("provider", ""),\n'
        '        account=d.get("account", ""),\n',
        '        conversation_id=d.get("conversation_id", ""),\n'
        '        provider=d.get("provider", ""),\n'
        '        account=d.get("account", ""),\n'
        '        title=d.get("title", ""),\n',
    )
    assert out != src, "M2 anchors not found — update the mutation script"
    _write(p, out)


def main():
    base_rc, base_line = _run_suite()
    print(f"BASELINE (unmutated): exit={base_rc} :: {base_line}")
    if base_rc != 0:
        print("*** baseline is not green — fix that before mutation-testing ***")
        return 1

    results = {}
    for name, mutate in (
        ("M1 research.py re-emits title", mutate_research),
        ("M2 redact.py re-adds title field", mutate_redact),
    ):
        try:
            _restore_all()          # isolate: only THIS mutation is active
            mutate()
            rc, line = _run_suite()
        finally:
            _restore_all()
        caught = rc != 0
        results[name] = caught
        print(f"{name}: exit={rc} caught={caught} :: {line}")

    # prove the restore really put the fixed source back (not merely "matches HEAD")
    restored_ok = all(TARGETS[k].read_bytes() == SNAPSHOT[k] for k in TARGETS)
    print("source restored byte-identical to start:", restored_ok)

    post_rc, post_line = _run_suite()
    print(f"POST-RUN re-verify: exit={post_rc} :: {post_line}")

    ok = all(results.values()) and restored_ok and post_rc == 0
    print("VERDICT:", "BOTH LAYERS MUTATION-PINNED (real detector)"
          if ok else "*** COVERAGE THEATER — a mutation SURVIVED (or restore failed) ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
