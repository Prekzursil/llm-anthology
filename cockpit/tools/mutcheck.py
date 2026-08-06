"""Mutation check: revert each load-bearing decision in turn and require the suite to go RED.

A green suite is not evidence that a behaviour is protected -- it may simply never exercise
it. Each mutation below breaks ONE decision this work unit claims to have made; if the
suite still passes, that claim has no test behind it.

Run:  python tools/mutcheck.py
"""
import pathlib
import subprocess
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ui" / "discoveryPanel.ts"

# (label, find, replace) -- each must appear EXACTLY once, or the mutation is not aimed at
# what it thinks it is and the result would be meaningless.
MUTATIONS = [
    (
        "export_file is offered an import it cannot perform",
        'if (finding.kind === "export_file") {\n    return { kind: "none", label: "", enabled: false, reason: EXPORT_NO_IMPORT_REASON, build: null };',
        'if (finding.kind === "export_file") {\n    return { kind: "import", label: "Import", enabled: true, reason: EXPORT_NO_IMPORT_REASON, build: null };',
    ),
    (
        "a store with no derivable Codex home gets one guessed for it",
        "  if (samePath(finding.path, itemsRoot)) {",
        "  if (false && samePath(finding.path, itemsRoot)) {",
    ),
    (
        "the backend cap is dropped, leaving only the UI collapse",
        '      capNote: backendTruncated ? capNote(sorted.length) : "",',
        '      capNote: "",',
    ),
    (
        "an expanded group offers no way back",
        '  return expanded ? "Show fewer" : "";',
        '  return "";',
    ),
    (
        "the engine cap is phrased as something a click could reveal",
        "export function capNote(totalCount: number): string {\n  return `The scan listed only the newest",
        "export function capNote(totalCount: number): string {\n  return `more — the scan listed only the newest",
    ),
    (
        "mtime 0 is rendered as a real date instead of unknown",
        "  if (!Number.isFinite(seconds) || seconds <= 0) return null;",
        "  if (!Number.isFinite(seconds)) return null;",
    ),
    (
        "the poll never reaches a terminal state",
        'if (state !== "running") return "terminal";',
        'if (state === "\\u0000never") return "terminal";',
    ),
    (
        "findings are rendered in wire order instead of newest-first",
        "  if (a.newest_mtime !== b.newest_mtime) return b.newest_mtime - a.newest_mtime;",
        "  if (false) return b.newest_mtime - a.newest_mtime;",
    ),
]

original = SRC.read_text(encoding="utf-8")
survivors = []

for label, find, replace in MUTATIONS:
    hits = original.count(find)
    if hits != 1:
        print("SKIP  (anchor matched %d times, expected 1): %s" % (hits, label))
        survivors.append(label + "  [anchor missed]")
        continue
    SRC.write_text(original.replace(find, replace), encoding="utf-8")
    try:
        proc = subprocess.run(
            ["npx", "vitest", "run", "--reporter=dot", "--testTimeout=5000"],
            cwd=str(SRC.parent.parent.parent),
            capture_output=True,
            text=True,
            shell=True,
            timeout=300,
        )
    finally:
        SRC.write_text(original, encoding="utf-8")
    if proc.returncode == 0:
        print("SURVIVED (suite stayed green -> NOT protected): %s" % label)
        survivors.append(label)
    else:
        print("killed: %s" % label)

assert SRC.read_text(encoding="utf-8") == original, "FAILED: source not restored"
print("\nsource restored byte-identical")
if survivors:
    print("FAILED:mutcheck %d survivor(s)" % len(survivors))
    for s in survivors:
        print("  - " + s)
    sys.exit(1)
print("SUCCESS:mutcheck all %d mutations killed" % len(MUTATIONS))
