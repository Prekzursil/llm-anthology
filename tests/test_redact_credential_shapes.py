"""redact.py — the credential-SHAPE scanner, the ~-relativizer, and the shareable
thread projection (DECISIONS G-5 and G-6).

SYNTHETIC PROBES ONLY. Every "key" below is a hand-typed nonsense string in the SHAPE of
a real credential; none of them authenticates anything, and no real conversation, path or
token appears anywhere in this file.

WHAT IS PINNED HERE, and why each one is load-bearing:

  * the scanner detects a pinned set of credential SHAPES (>= the 6 probe classes that
    were measured surviving a shareable render verbatim) and reports each hit WITH its
    location — shape name + character offset + a MASKED preview, never the value;
  * THE MOST IMPORTANT TEST IN THIS FILE is
    `test_a_medical_and_personal_probe_is_invisible_and_the_stated_limit_says_so`:
    a medical/personal probe produces ZERO findings, and the coverage-limit sentence the
    warning must carry says exactly that in words. A scanner that reported "clean" on a
    medical corpus WITHOUT that sentence would be factually correct and dangerously
    misleading, because it lowers the reader's guard on the risk that actually applies to
    them;
  * the scan NEVER mutates: `scrub_credential_shapes` is a SEPARATE function the caller
    must ask for, and the both-states test proves the scanner FIRES before the scrub and
    is silent after it;
  * `relativize_home` removes the OS username from an absolute path without inventing one;
  * `shareable_thread` is an EXPLICIT allowlist projection (the same construction
    discipline as `MetadataView`): `preview` is dropped entirely, cwd/rollout_path are
    relativized, and structure + title + repo/branch are kept — the owner's accepted
    residual, asserted here so nobody reads "shareable" as "safe".
"""
import os
from dataclasses import fields

import pytest

from llm_anthology import corpus, redact

# --- synthetic credential-shaped probes (nonsense; none of these authenticate) -------
OPENAI = "sk-" + "S" * 40
ANTHROPIC = "sk-ant-api03-" + "A" * 32
AWS = "AKIA" + "SYNTHETIC0000000"      # EXAMPLE placeholder — authenticates nothing
GITHUB = "ghp_" + "G" * 36
GITHUB_FINE = "github_pat_" + "P" * 30
GOOGLE = "AIza" + "Z" * 35
SLACK = "xoxb-" + "1234567890-SYNTHETICSLACK"   # EXAMPLE placeholder, not a real token
JWT = "eyJhbGciOi.eyJzdWIiOi.SflKxwRJSMsynth"
PEM = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"   # EXAMPLE header only, no key material
BEARER = "Authorization: Bearer SYNTHETICBEARER00000"

ALL_PROBES = (OPENAI, ANTHROPIC, AWS, GITHUB, GITHUB_FINE, GOOGLE, SLACK, JWT, PEM,
              BEARER)

# a personal / medical probe — the risk class this scanner is BLIND to, by design
MEDICAL = ("Patient Jane Q. Doe, DOB 1970-01-01, asked about tapering sertraline 50mg "
           "after the pharmacy at 14 Elm Street refused the refill.")


# ------------------------------------------------------------------ shape detection

@pytest.mark.parametrize("probe", ALL_PROBES)
def test_every_pinned_credential_shape_is_detected(probe):
    text = "prose before " + probe + " prose after"
    hits = redact.scan_credential_shapes(text)
    assert len(hits) == 1, hits
    assert hits[0]["offset"] == text.index(probe.split(": ")[-1] if probe is BEARER
                                          else probe)
    assert hits[0]["shape"] in redact.CREDENTIAL_SHAPE_NAMES


def test_the_pinned_shape_set_covers_at_least_the_six_measured_probe_classes():
    """The review measured 6 of 6 credential-shaped probes surviving a shareable render
    verbatim. The pinned set must be at least that wide, and every name must be a stable,
    reportable label (no duplicates)."""
    names = redact.CREDENTIAL_SHAPE_NAMES
    assert len(names) >= 6
    assert len(set(names)) == len(names)
    detected = {redact.scan_credential_shapes(p)[0]["shape"] for p in ALL_PROBES}
    assert detected <= set(names)
    assert len(detected) >= 6


def test_a_finding_reports_a_masked_preview_and_never_the_value():
    hits = redact.scan_credential_shapes("here: " + OPENAI)
    assert len(hits) == 1
    preview = hits[0]["preview"]
    assert OPENAI not in preview                       # the value itself never travels
    assert preview.startswith(OPENAI[:4])              # enough to locate it by eye
    assert str(len(OPENAI)) in preview                 # ... and to know how long it was


def test_findings_are_sorted_by_offset_and_deduped_across_overlapping_shapes():
    """Two shapes matching the SAME region report ONCE (the widest match wins), so a
    bearer header carrying a JWT is one finding, not two — and multiple distinct hits
    come back in document order."""
    text = OPENAI + " ... Authorization: Bearer " + JWT
    hits = redact.scan_credential_shapes(text)
    assert [h["offset"] for h in hits] == sorted(h["offset"] for h in hits)
    assert len(hits) == 2                              # openai key + bearer (JWT nested)
    assert hits[1]["shape"] == "bearer-token"


def test_ordinary_prose_and_near_misses_are_not_flagged():
    for benign in ("the sk- prefix is what an OpenAI key starts with",
                   "AKIA is the AWS prefix",
                   "Bearer with no token",
                   "eyJ is how a JWT starts",
                   "ghp_short",
                   ""):
        assert redact.scan_credential_shapes(benign) == [], benign


def test_scan_tolerates_a_non_string():
    assert redact.scan_credential_shapes(None) == []
    assert redact.scan_credential_shapes(42) == []


# ----------------------------------------------------- THE stated-coverage-limit test

def test_a_medical_and_personal_probe_is_invisible_and_the_stated_limit_says_so():
    """THE MOST IMPORTANT TEST HERE. The scanner sees credential SHAPES and nothing else,
    so a name, a date of birth, a diagnosis, a drug and an address are all invisible to
    it. That is acceptable ONLY because the warning states its own blindness, so a
    "no credential shapes found" result can never be read as "safe to share"."""
    assert redact.scan_credential_shapes(MEDICAL) == []

    limit = redact.CREDENTIAL_SHAPE_COVERAGE_LIMIT.lower()
    assert "shape" in limit                             # names WHAT it detects
    assert "personal" in limit and "medical" in limit    # names what it is BLIND to
    assert "blind" in limit
    assert "does not mean" in limit                     # kills the "clean == safe" read
    # NOT described as a personal-information tool anywhere on this surface
    for text in (redact.CREDENTIAL_SHAPE_COVERAGE_LIMIT,
                 redact.scan_credential_shapes.__doc__,
                 redact.scrub_credential_shapes.__doc__):
        assert "pii" not in text.lower()
        assert "personally identifiable" not in text.lower()


# ------------------------------------------------------------- scrub is OPT-IN, never
#                                                               a side effect of scanning

def test_scanning_never_mutates_and_scrubbing_is_a_separate_call():
    text = "before " + AWS + " after"
    assert redact.scan_credential_shapes(text) and text == "before " + AWS + " after"
    scrubbed = redact.scrub_credential_shapes(text)
    assert scrubbed != text
    assert AWS not in scrubbed
    assert scrubbed.startswith("before ") and scrubbed.endswith(" after")


def test_scrub_placeholder_names_the_shape_and_the_scan_goes_quiet_afterwards():
    """BOTH STATES: the same scanner FIRES on the raw text and is SILENT on the scrubbed
    text. A detector silent in both states would prove nothing."""
    text = "k=" + GITHUB + " j=" + JWT
    assert len(redact.scan_credential_shapes(text)) == 2        # fires BEFORE
    scrubbed = redact.scrub_credential_shapes(text)
    assert redact.scan_credential_shapes(scrubbed) == []        # silent AFTER
    assert "[redacted:github-token]" in scrubbed
    assert "[redacted:jwt]" in scrubbed


def test_scrub_tolerates_a_non_string_and_leaves_clean_text_byte_identical():
    assert redact.scrub_credential_shapes(None) == ""
    assert redact.scrub_credential_shapes(MEDICAL) == MEDICAL   # nothing to scrub


# --------------------------------------------------------------- path relativization

@pytest.mark.parametrize("raw,home,expected", [
    (r"C:\Users\someone\.codex\sessions\r.jsonl", r"C:\Users\someone",
     "~/.codex/sessions/r.jsonl"),
    ("/home/someone/work/repo", "/home/someone", "~/work/repo"),
    (r"c:\users\SOMEONE\x", r"C:\Users\someone", "~/x"),          # case-insensitive
    (r"C:\Users\someone", r"C:\Users\someone", "~"),              # home itself
    (r"C:\Users\someone\\", r"C:\Users\someone", "~"),            # trailing separator
    (r"D:\work\client\repo", r"C:\Users\someone", r"D:\work\client\repo"),  # not under
    ("", r"C:\Users\someone", ""),
    # an EMPTY home root relativizes nothing — rewriting every absolute path to `~/...`
    # would be a lie, so the path is returned verbatim
    ("/etc/passwd", "", "/etc/passwd"),
])
def test_relativize_home(raw, home, expected):
    assert redact.relativize_home(raw, home=home) == expected


def test_relativize_home_does_not_match_a_sibling_directory_by_prefix():
    """`C:\\Users\\someone2` starts with `C:\\Users\\someone` as a STRING but is a
    different directory; a naive startswith would rewrite it to `~2`."""
    assert redact.relativize_home(r"C:\Users\someone2\x", home=r"C:\Users\someone") == \
        r"C:\Users\someone2\x"


def test_relativize_home_defaults_to_the_process_home():
    inside = os.path.join(os.path.expanduser("~"), "probe.txt")
    assert redact.relativize_home(inside) == "~/probe.txt"


def test_relativize_home_tolerates_a_non_string():
    assert redact.relativize_home(None) == ""


# -------------------------------------------------------- shareable thread projection

def _full_thread():
    return corpus.ThreadMeta(
        id="t1", title="Refactor the parser", model_provider="openai", tokens_used=17,
        created_at_ms=1000, updated_at_ms=2000, git_branch="feature/x",
        cwd=r"C:\Users\someone\src\repo", agent_role="impl", agent_nickname="brisk-heron",
        preview="I need help tapering my prescription, my doctor said",
        rollout_path=r"C:\Users\someone\.codex\sessions\rollout-1.jsonl",
        adapter="codex")


def test_shareable_thread_drops_preview_entirely_and_relativizes_paths():
    out = redact.shareable_thread(_full_thread(), home=r"C:\Users\someone")
    assert out.preview == ""                                    # DROPPED, not truncated
    assert out.cwd == "~/src/repo"
    assert out.rollout_path == "~/.codex/sessions/rollout-1.jsonl"
    assert "someone" not in out.cwd and "someone" not in out.rollout_path


def test_shareable_thread_keeps_structure_titles_and_repo_branch():
    """The owner explicitly accepted keeping repo/branch and titles. Pinned so a later
    change to that trade is a deliberate, visible one."""
    out = redact.shareable_thread(_full_thread(), home=r"C:\Users\someone")
    assert out.id == "t1"
    assert out.title == "Refactor the parser"
    assert out.git_branch == "feature/x"
    assert (out.model_provider, out.adapter) == ("openai", "codex")
    assert (out.tokens_used, out.created_at_ms, out.updated_at_ms) == (17, 1000, 2000)
    assert (out.agent_role, out.agent_nickname) == ("impl", "brisk-heron")


def test_shareable_thread_is_pure():
    meta = _full_thread()
    before = (meta.preview, meta.cwd, meta.rollout_path)
    redact.shareable_thread(meta, home=r"C:\Users\someone")
    assert (meta.preview, meta.cwd, meta.rollout_path) == before


def test_shareable_thread_decides_every_threadmeta_field_explicitly():
    """BY CONSTRUCTION, like `MetadataView`: the projection names every ThreadMeta field,
    so ADDING a field to ThreadMeta turns this red and forces a keep/drop/relativize
    decision instead of a silent passthrough of whatever the new field carries."""
    assert {f.name for f in fields(corpus.ThreadMeta)} == redact.SHAREABLE_DECIDED_FIELDS


def test_shareable_thread_defaults_home_to_the_process_home():
    inside = os.path.join(os.path.expanduser("~"), "src")
    out = redact.shareable_thread(corpus.ThreadMeta(id="t", cwd=inside))
    assert out.cwd == "~/src"
