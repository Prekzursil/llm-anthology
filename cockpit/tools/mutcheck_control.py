"""Control for mutcheck.py: prove the detector can report a PASS.

Without this, six "killed" verdicts are worthless -- if the harness returned non-zero for
every run (a decode crash, a bad cwd, a wrong command), an unprotected mutation would be
reported as killed too. This runs the IDENTICAL subprocess call against the UNMUTATED
source and requires exit 0.
"""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent

proc = subprocess.run(
    ["npx", "vitest", "run", "--reporter=dot", "--testTimeout=5000"],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    shell=True,
    timeout=300,
)
tail = (proc.stdout or "").strip().splitlines()[-6:]
print("\n".join(tail))
print("returncode=%r" % proc.returncode)
print("SUCCESS:control-detector-can-pass" if proc.returncode == 0 else "FAILED:control-always-red")
