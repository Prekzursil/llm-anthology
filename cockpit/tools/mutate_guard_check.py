"""Mutation check for the open-vs-create guard in cockpit/src-tauri/src/lib.rs.

A green test is not evidence that a test PROTECTS a behaviour. This neuters
`validate_index_path` so it always accepts, runs the test, and requires it to go RED. If the
test still passes against the neutered guard, the test is decoration and says so loudly.

Restores from an in-memory BYTE snapshot and verifies byte-identity afterwards. It does not
use `git checkout --`: doing that during an earlier mutation run destroyed an uncommitted fix
and silently contaminated the result of the very experiment it was part of.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src-tauri"
TARGET = ROOT / "src" / "lib.rs"
TEST = "validate_index_path"

ORIGINAL = TARGET.read_bytes()

# The real body, and a mutant that accepts everything.
NEEDLE = b"""    let path = std::path::Path::new(index_path);
    if path.is_file() {
        return Ok(());
    }"""
MUTANT = b"""    let path = std::path::Path::new(index_path);
    if true {
        return Ok(());
    }"""


def run_test():
    p = subprocess.run(
        ["cargo", "test", "--lib", TEST],
        cwd=ROOT, capture_output=True, text=True,
    )
    return p.returncode, (p.stdout + p.stderr)


def main():
    if NEEDLE not in ORIGINAL:
        print("FAILED:mutate-guard could not locate the guard body to mutate")
        print("       (the anchor text changed — fix this script, do not skip the check)")
        return 2

    # 1. Baseline: the test must PASS unmutated. Without this the red result below could
    #    just mean the suite is broken for an unrelated reason.
    code, out = run_test()
    if code != 0:
        print("FAILED:mutate-guard baseline is already red — cannot attribute a mutation kill")
        print(out[-1500:])
        return 2
    print("baseline: PASS (unmutated)")

    # 2. Mutate and require RED.
    try:
        TARGET.write_bytes(ORIGINAL.replace(NEEDLE, MUTANT))
        code, out = run_test()
    finally:
        TARGET.write_bytes(ORIGINAL)

    restored = TARGET.read_bytes()
    if restored != ORIGINAL:
        print("FAILED:mutate-guard RESTORE MISMATCH — the file on disk is not the original")
        return 2
    print("restored: byte-identical to the original")

    if code == 0:
        print("FAILED:mutate-guard the mutant SURVIVED — the test does not protect this guard")
        return 1

    killed = [ln for ln in out.splitlines() if "panicked" in ln or "must be rejected" in ln]
    print("mutant KILLED (test went red as required)")
    for ln in killed[:4]:
        print(f"  {ln.strip()}")
    print("SUCCESS:mutate-guard the test genuinely protects the open-vs-create guard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
