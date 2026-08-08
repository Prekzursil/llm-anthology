"""Tests for llm_anthology.discover — provider auto-discovery.

Every fixture here is a SYNTHETIC directory tree built in tmp_path with invented
filenames and dummy bytes. Nothing reads, asserts on, or embeds real session data;
one test (`test_never_returns_file_content`) actively proves the scanner cannot leak
file content into its result.

No test depends on what happens to be on the machine running it — the candidate roots
are always injected via `discover.Roots`, never resolved from the real home.
"""
import json
import os
import sqlite3

import pytest

from llm_anthology import corpus, discover


# --------------------------------------------------------------------- helpers

def _touch(path, data=b"x"):
    """Create `path` (and parents) holding `data`. Returns the absolute path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _can_symlink():
    """Probe once: file symlinks need Developer Mode / elevation on Windows."""
    import tempfile
    d = tempfile.mkdtemp()
    target = os.path.join(d, "t")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("t")
    try:
        os.symlink(target, os.path.join(d, "l"))
        return True
    except OSError:                          # env capability probe, not app code
        return False


_SYMLINKS = _can_symlink()


def _corpus_index(path):
    """A real, minimal corpus index — the same schema corpus.open_index writes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = corpus.open_index(path)
    conn.close()
    return path


def _codex_home(base, jsonl=0, zst=0, state_db=True):
    """A synthetic $CODEX_HOME: state_5.sqlite + a date-nested rollout tree."""
    if state_db:
        _touch(os.path.join(base, "state_5.sqlite"), b"SQLite format 3\x00")
    day = os.path.join(base, "sessions", "2026", "08", "06")
    for i in range(jsonl):
        _touch(os.path.join(day, "rollout-2026-08-06T00-00-0%d-uuid%d.jsonl" % (i, i)))
    for i in range(zst):
        _touch(os.path.join(day, "rollout-2026-08-06T01-00-0%d-uuid%d.jsonl.zst" % (i, i)))
    return base


def _claude_home(base, projects=0, files_each=1):
    """A synthetic ~/.claude: projects/<slug>/*.jsonl transcripts."""
    for p in range(projects):
        for f in range(files_each):
            _touch(os.path.join(base, "projects", "C--proj-%d" % p, "sess-%d.jsonl" % f))
    return base


def _by(result, provider=None, kind=None):
    return [f for f in result.findings
            if (provider is None or f.provider == provider)
            and (kind is None or f.kind == kind)]


@pytest.fixture
def empty_roots(tmp_path):
    """Roots pointing at existing-but-empty dirs — the honest 'nothing here' baseline."""
    for name in ("codex", "claude", "user", "idx"):
        os.makedirs(str(tmp_path / name), exist_ok=True)
    return discover.Roots(codex_home=str(tmp_path / "codex"),
                          claude_home=str(tmp_path / "claude"),
                          user_dirs=(str(tmp_path / "user"),),
                          index_dirs=(str(tmp_path / "idx"),))


# ------------------------------------------------------- structure / contract

def test_finding_is_json_ready_and_ordered_deterministically(tmp_path, empty_roots):
    _corpus_index(str(tmp_path / "idx" / "a.sqlite"))
    _corpus_index(str(tmp_path / "idx" / "b.sqlite"))
    first = discover.discover(empty_roots)
    second = discover.discover(empty_roots)

    assert [f.path for f in first.findings] == [f.path for f in second.findings]
    payload = json.dumps(first.as_dict())          # must survive an RPC boundary
    assert "a.sqlite" in payload
    one = first.findings[0]
    assert set(one.as_dict()) == {"provider", "kind", "path", "count",
                                  "newest_mtime", "confidence", "detail"}
    assert os.path.isabs(one.path)


def test_empty_roots_yield_no_findings_but_real_stats(empty_roots):
    result = discover.discover(empty_roots)
    assert result.findings == ()
    assert result.stats.roots_scanned > 0
    assert result.stats.elapsed_seconds >= 0.0
    assert result.stats.budget_exhausted is False


def test_missing_roots_are_skipped_not_fatal(tmp_path):
    roots = discover.Roots(codex_home=str(tmp_path / "nope"),
                           claude_home=str(tmp_path / "also-nope"),
                           user_dirs=(str(tmp_path / "gone"),),
                           index_dirs=(str(tmp_path / "vanished"),))
    result = discover.discover(roots)
    assert result.findings == ()


# ---------------------------------------------------------------- codex store

def test_codex_store_detected_with_state_db_and_rollout_counts(tmp_path, empty_roots):
    _codex_home(str(tmp_path / "codex"), jsonl=2, zst=3)
    hits = _by(discover.discover(empty_roots), provider="codex",
               kind=discover.KIND_SESSION_STORE)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.path == str(tmp_path / "codex")
    assert hit.count == 5                               # every rollout on disk
    assert hit.confidence == discover.CONF_HIGH
    assert hit.detail["rollouts_jsonl"] == 2
    assert hit.detail["rollouts_zst"] == 3
    # ALL 5 are ingestable: codex_rollout.ingest_sessions globs BOTH rollout-*.jsonl and
    # rollout-*.jsonl.zst and transparently decompresses. This assertion previously read
    # `== 2` (plain only), correctly, while the reader handled only the uncompressed form.
    # It moved when that reader was fixed, exactly as the note on the codex StoreSpec said
    # it would — and leaving it at 2 would have kept the shipped panel reporting
    # "ingestable 0" for a live store whose 2024 compressed rollouts had just become
    # readable. The number here is the CAPABILITY claim the UI shows a user, so it has to
    # track the engine rather than lag it.
    assert hit.detail["ingestable"] == 5
    assert hit.detail["state_db"].endswith("state_5.sqlite")
    assert hit.newest_mtime > 0
    # loaders.load_corpus(sessions_root, index_path, codex_home) needs BOTH paths, and
    # they are NOT the same directory. `path` is the codex_home; the counted items live
    # under items_root, so a caller cannot wire the count to the wrong parameter.
    assert hit.detail["items_root"] == os.path.join(str(tmp_path / "codex"), "sessions")


def test_codex_store_without_state_db_still_found_from_rollouts(tmp_path, empty_roots):
    _codex_home(str(tmp_path / "codex"), jsonl=1, state_db=False)
    hit = _by(discover.discover(empty_roots), provider="codex")[0]
    assert hit.detail["state_db"] == ""
    assert hit.count == 1


def test_non_rollout_files_in_the_session_tree_are_not_counted(tmp_path, empty_roots):
    """The real ~/.codex/sessions holds `log` and `memories` alongside the rollouts;
    counting those would inflate the store size the UI reports."""
    _codex_home(str(tmp_path / "codex"), jsonl=1)
    _touch(str(tmp_path / "codex" / "sessions" / "log" / "notes.txt"))
    _touch(str(tmp_path / "codex" / "sessions" / "2026" / "08" / "06" / "other.json"))
    hit = _by(discover.discover(empty_roots), provider="codex")[0]
    assert hit.count == 1


def test_codex_dir_without_any_marker_is_not_a_match(tmp_path, empty_roots):
    _touch(str(tmp_path / "codex" / "config.toml"), b"# not a session store")
    assert _by(discover.discover(empty_roots), provider="codex") == []


# ---------------------------------------------------------- claude-code store

def test_claude_code_projects_store_counts_transcripts(tmp_path, empty_roots):
    _claude_home(str(tmp_path / "claude"), projects=3, files_each=2)
    hits = _by(discover.discover(empty_roots), provider="claude-code")
    assert len(hits) == 1
    assert hits[0].path == str(tmp_path / "claude" / "projects")
    assert hits[0].count == 6
    assert hits[0].detail["project_dirs"] == 3


def test_claude_projects_dir_with_no_transcripts_is_not_a_match(tmp_path, empty_roots):
    os.makedirs(str(tmp_path / "claude" / "projects" / "empty-proj"), exist_ok=True)
    assert _by(discover.discover(empty_roots), provider="claude-code") == []


# ----------------------------------------------------------------- built index

def test_built_corpus_index_detected_with_row_counts(tmp_path, empty_roots):
    path = _corpus_index(str(tmp_path / "idx" / "corpus.sqlite"))
    hits = _by(discover.discover(empty_roots), kind=discover.KIND_BUILT_INDEX)
    assert len(hits) == 1
    assert hits[0].path == path
    assert hits[0].confidence == discover.CONF_HIGH
    assert hits[0].detail["conversations"] == 0        # a real, empty index


@pytest.mark.parametrize("name", [
    "Chat%20Export.sqlite",      # exactly what a browser download produces
    "a%41b.sqlite",              # %41 decodes to 'A' — the path SQLite opens is not this
    "100%.sqlite",               # a bare % not followed by hex digits
    "%25already.sqlite",         # a literal %25 must not be double-unescaped
])
def test_a_percent_in_the_filename_does_not_hide_a_real_index(tmp_path, empty_roots,
                                                              name):
    """A `%` in the path must not make a real index INVISIBLE to discovery.

    The URI the shape probe builds is opened with ``uri=True``, and URI mode decodes
    ``%HH``. Escaping only ``?`` and ``#`` therefore hands SQLite a path that is not the
    path on disk: ``Chat%20Export.sqlite`` resolves to ``Chat Export.sqlite``, which does
    not exist.

    The failure is SILENT and total. `_match_index` passes the suffix gate and the 16-byte
    magic gate — both use a plain ``open()``, which does no decoding — and only the
    connection fails. `tables is None`, the finding is dropped, and first-run discovery
    reports "No AI session data found in the usual places" while the index sits in the
    folder it just scanned. The only trace is a counted error the panel renders as
    "1 location was skipped".

    `100%` and `%25already` are the CONTROLS: a bare `%` is not a valid escape so it
    already opened, and `%25` must survive exactly one round of escaping rather than being
    re-escaped into `%2525`. Keeping them here means a fix that over-escapes fails too.
    """
    path = _corpus_index(str(tmp_path / "idx" / name))
    hits = _by(discover.discover(empty_roots), kind=discover.KIND_BUILT_INDEX)
    assert [h.path for h in hits] == [path]
    assert hits[0].confidence == discover.CONF_HIGH


def test_a_percent_in_a_PARENT_DIRECTORY_does_not_hide_a_real_index(tmp_path):
    """The same defect one level up — the escape sees the whole path, not just the name.

    Called out separately because the filename cases above would pass while this failed if
    a fix only sanitised the basename. `Downloads\\Chat%20Exports\\anthology.db` is an
    entirely ordinary shape: a browser-named FOLDER holding a corpus.
    """
    holder = tmp_path / "idx" / "Chat%20Exports"
    path = _corpus_index(str(holder / "anthology.sqlite"))
    for name in ("codex", "claude", "user"):
        os.makedirs(str(tmp_path / name), exist_ok=True)
    roots = discover.Roots(codex_home=str(tmp_path / "codex"),
                           claude_home=str(tmp_path / "claude"),
                           user_dirs=(str(tmp_path / "user"),),
                           index_dirs=(str(tmp_path / "idx"),))
    hits = _by(discover.discover(roots), kind=discover.KIND_BUILT_INDEX)
    assert [h.path for h in hits] == [path]


@pytest.mark.skipif(not _SYMLINKS, reason="file symlinks unavailable on this host")
def test_a_symlink_is_never_read_through_and_the_skip_is_recorded(tmp_path, empty_roots):
    """A symlinked FILE inside a scanned root must NOT be opened.

    `_is_dir` uses `follow_symlinks=False`, so a link is never DESCENDED — but before this
    fix it fell through to the file branch and was yielded, and every consumer opens BY
    PATH, which follows. Measured two independent ways: `discover()` returned two
    export_file findings for a link whose real content sat outside every scanned root, and
    `_head` on that link returned the outside file's bytes with zero errors noted.

    Why it matters beyond confinement: a link named `conversations.json` pointing at
    `\\\\host\\share\\x` makes that `open()` an outbound SMB/NTLM authentication — a
    credential leak from a first-run scan the user never aimed anywhere. The `except OSError`
    around the read prevents the crash, not the connection. This test uses a LOCAL outside
    target on purpose: the property under test is read-through, and pointing a test at a
    real UNC host would perform the very egress being prevented.

    Fixed at the WALK, not at `_head`: `_sqlite_shape` also opens by path, so a link to a
    `.db` would have stayed reachable through the index route. Skipping links once, where
    entries are produced, closes every consumer including future ones — and it makes the
    file side agree with the dir side, which had already decided not to traverse links.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "private-export.json"
    victim.write_text('[{"title": "must not be read"}]', encoding="utf-8")
    victim_db = _corpus_index(str(outside / "private.sqlite"))

    scanned = tmp_path / "user"
    os.symlink(str(victim), str(scanned / "conversations.json"))
    os.symlink(victim_db, str(tmp_path / "idx" / "linked.sqlite"))

    # The CONTROL: a real file beside the link must still be found, or a fix that simply
    # stopped detecting exports would pass this test.
    real = scanned / "real-dir" / "conversations.json"
    real.parent.mkdir()
    real.write_text('[{"title": "a genuine export"}]', encoding="utf-8")

    result = discover.discover(empty_roots)
    paths = [f.path.lower() for f in result.findings]
    assert not any("conversations.json" in p and "real-dir" not in p for p in paths), \
        "the symlinked export was read through: %s" % paths
    assert not any("linked.sqlite" in p for p in paths), \
        "the symlinked index was opened: %s" % paths
    assert any("real-dir" in p for p in paths), "the control export must still be found"
    # Not silent: the panel already renders a skip count, so the operator can see it.
    assert any("conversations.json" in e for e in result.stats.errors)


def test_unrelated_sqlite_file_is_not_a_built_index(tmp_path, empty_roots):
    other = str(tmp_path / "idx" / "browser.sqlite")
    os.makedirs(os.path.dirname(other), exist_ok=True)
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE bookmarks (id INTEGER)")
    conn.commit()
    conn.close()
    assert _by(discover.discover(empty_roots), kind=discover.KIND_BUILT_INDEX) == []


def test_non_sqlite_file_named_like_a_db_is_rejected_by_the_header_sniff(tmp_path,
                                                                        empty_roots):
    _touch(str(tmp_path / "idx" / "fake.sqlite"), b"this is plain text, not sqlite")
    assert _by(discover.discover(empty_roots), kind=discover.KIND_BUILT_INDEX) == []


def test_corrupt_sqlite_header_but_unreadable_body_does_not_crash(tmp_path, empty_roots):
    _touch(str(tmp_path / "idx" / "trunc.sqlite"), b"SQLite format 3\x00" + b"\x00" * 40)
    result = discover.discover(empty_roots)          # must not raise
    assert _by(result, kind=discover.KIND_BUILT_INDEX) == []


# ---------------------------------------------------------------- export files

def test_conversations_json_alone_is_ambiguous_between_chatgpt_and_claude(tmp_path,
                                                                          empty_roots):
    _touch(str(tmp_path / "user" / "export" / "conversations.json"), b'[{"a": 1}]')
    result = discover.discover(empty_roots)
    providers = sorted(f.provider for f in _by(result, kind=discover.KIND_EXPORT_FILE))
    assert providers == ["chatgpt", "claude"]
    for f in _by(result, kind=discover.KIND_EXPORT_FILE):
        assert f.confidence == discover.CONF_MEDIUM
        assert f.detail["ambiguous_with"]


def test_claude_sibling_files_resolve_the_ambiguity_to_claude_only(tmp_path, empty_roots):
    base = str(tmp_path / "user" / "claude-export")
    _touch(os.path.join(base, "conversations.json"), b'[{"a": 1}]')
    # grounded sibling set — loaders.py:39-42 names these as real Claude-export files
    _touch(os.path.join(base, "users.json"), b"{}")
    _touch(os.path.join(base, "design_chats", "d1.json"), b"{}")
    hits = _by(discover.discover(empty_roots), kind=discover.KIND_EXPORT_FILE)
    convs = [f for f in hits if f.path.endswith("conversations.json")]
    assert [f.provider for f in convs] == ["claude"]
    assert convs[0].confidence == discover.CONF_HIGH
    assert convs[0].detail.get("ambiguous_with", ()) == ()


def test_chatgpt_sibling_files_resolve_the_ambiguity_to_chatgpt_only(tmp_path,
                                                                      empty_roots):
    base = str(tmp_path / "user" / "chatgpt-export")
    _touch(os.path.join(base, "conversations.json"), b'[{"a": 1}]')
    _touch(os.path.join(base, "chat.html"), b"<html></html>")
    convs = [f for f in _by(discover.discover(empty_roots),
                            kind=discover.KIND_EXPORT_FILE)
             if f.path.endswith("conversations.json")]
    assert [f.provider for f in convs] == ["chatgpt"]
    assert convs[0].confidence == discover.CONF_HIGH


def test_chunked_conversations_export_is_detected(tmp_path, empty_roots):
    """A large ChatGPT history is split into conversations-000.json, -001.json ...
    A pattern that only matched the unsplit name misses the whole export."""
    base = str(tmp_path / "user" / "ChatGPT")
    for i in range(3):
        _touch(os.path.join(base, "conversations-%03d.json" % i), b"[]")
    hits = _by(discover.discover(empty_roots), provider="chatgpt")
    assert len(hits) == 3
    assert all(h.detail.get("ambiguous_with", ()) == () for h in hits)


def test_export_nested_four_deep_is_still_found(tmp_path, empty_roots):
    """The real landing shape is Downloads/<batch>/<provider>/<account>/export.json —
    an account-per-directory export sits FOUR levels below the landing dir."""
    deep = str(tmp_path / "user" / "AIs Conversations" / "Claude" / "a@example.com")
    _touch(os.path.join(deep, "conversations.json"), b"[]")
    _touch(os.path.join(deep, "users.json"), b"{}")
    hits = _by(discover.discover(empty_roots), provider="claude")
    assert [h.path for h in hits] == [os.path.join(deep, "conversations.json")]


def test_takeout_folder_anywhere_above_the_file_confirms_gemini(tmp_path, empty_roots):
    """The real transcript.json sits in `Gemini Apps/_converted/`, so only the
    IMMEDIATE parent is `_converted` — the confirming folder is an ancestor."""
    deep = str(tmp_path / "user" / "Gemini Apps" / "_converted")
    _touch(os.path.join(deep, "transcript.json"), b"[]")
    hits = _by(discover.discover(empty_roots), provider="gemini")
    assert [h.confidence for h in hits] == [discover.CONF_HIGH]


def test_the_apps_own_output_directory_is_never_scanned(tmp_path, empty_roots):
    """loaders.py:56-58 already refuses to ingest this app's own `_site` output; a
    discovery pass that offered it back would recreate that bug from the other end."""
    _touch(str(tmp_path / "user" / "_site" / "codex.json"), b"[]")
    assert _by(discover.discover(empty_roots), provider="codex") == []


def test_codex_json_export_detected(tmp_path, empty_roots):
    _touch(str(tmp_path / "user" / "codex.json"), b'{"threads": []}')
    hits = _by(discover.discover(empty_roots), provider="codex",
               kind=discover.KIND_EXPORT_FILE)
    assert len(hits) == 1
    assert hits[0].count == 1
    assert hits[0].detail["size_bytes"] > 0


def test_gemini_takeout_transcript_detected(tmp_path, empty_roots):
    _touch(str(tmp_path / "user" / "Gemini Apps" / "transcript.json"), b"[]")
    hits = _by(discover.discover(empty_roots), provider="gemini")
    assert [h.kind for h in hits] == [discover.KIND_EXPORT_FILE]


def test_bare_transcript_json_outside_a_takeout_folder_is_low_confidence(tmp_path,
                                                                         empty_roots):
    """`transcript.json` is a generic name; only the Takeout folder shape around it
    justifies calling it a Gemini export with confidence."""
    _touch(str(tmp_path / "user" / "misc" / "transcript.json"), b"[]")
    hits = _by(discover.discover(empty_roots), provider="gemini")
    assert [h.confidence for h in hits] == [discover.CONF_LOW]


def test_file_that_is_not_json_fails_the_structural_sniff(tmp_path, empty_roots):
    _touch(str(tmp_path / "user" / "conversations.json"), b"<html>not json</html>")
    assert _by(discover.discover(empty_roots), kind=discover.KIND_EXPORT_FILE) == []


def test_empty_file_fails_the_structural_sniff(tmp_path, empty_roots):
    _touch(str(tmp_path / "user" / "codex.json"), b"")
    assert _by(discover.discover(empty_roots), kind=discover.KIND_EXPORT_FILE) == []


def test_export_findings_are_capped_newest_first_per_provider(tmp_path, empty_roots):
    for i in range(8):
        p = _touch(str(tmp_path / "user" / ("d%d" % i) / "codex.json"), b"[]")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
    result = discover.discover(empty_roots, max_per_group=3)
    hits = _by(result, provider="codex", kind=discover.KIND_EXPORT_FILE)
    assert len(hits) == 3
    # the three NEWEST survive the cap (d5, d6, d7), not the three first-walked
    assert sorted(os.path.basename(os.path.dirname(h.path)) for h in hits) == \
        ["d5", "d6", "d7"]
    assert result.stats.truncated_groups == ("codex/export_file",)


# ------------------------------------------------------------------- bounding

def test_depth_limit_stops_the_walk(tmp_path, empty_roots):
    deep = str(tmp_path / "user")
    for level in range(6):
        deep = os.path.join(deep, "lvl%d" % level)
    _touch(os.path.join(deep, "codex.json"), b"[]")
    assert _by(discover.discover(empty_roots), provider="codex") == []


def test_pruned_directory_names_are_never_descended(tmp_path, empty_roots):
    _touch(str(tmp_path / "user" / "node_modules" / "codex.json"), b"[]")
    assert _by(discover.discover(empty_roots), provider="codex") == []


def test_file_budget_is_enforced_and_reported(tmp_path, empty_roots):
    for i in range(40):
        _touch(str(tmp_path / "user" / ("f%d.txt" % i)))
    _touch(str(tmp_path / "user" / "codex.json"), b"[]")
    result = discover.discover(empty_roots, file_budget=5)
    assert result.stats.budget_exhausted is True
    assert result.stats.files_examined <= 5 + len(empty_roots.user_dirs) + 8


def test_overlapping_roots_are_walked_only_once(tmp_path):
    """default_roots() deliberately uses the SAME dirs for exports and for indexes, so
    the de-duplication is on the real path, not a corner case."""
    shared = str(tmp_path / "shared")
    _touch(os.path.join(shared, "codex.json"), b"[]")
    roots = discover.Roots(user_dirs=(shared, shared), index_dirs=(shared,))
    result = discover.discover(roots)
    assert result.stats.dirs_visited == 1
    assert len(_by(result, provider="codex")) == 1


def test_an_index_spec_without_a_count_table_still_reports(tmp_path, empty_roots):
    _corpus_index(str(tmp_path / "idx" / "corpus.sqlite"))
    spec = discover.IndexSpec(provider="anthology", required_tables=("threads",))
    hits = _by(discover.discover(empty_roots, specs=(spec,)),
               kind=discover.KIND_BUILT_INDEX)
    assert len(hits) == 1
    assert hits[0].count == 0
    assert "conversations" not in hits[0].detail


def test_unreadable_file_is_recorded_and_the_scan_continues(tmp_path, empty_roots,
                                                            monkeypatch):
    locked = _touch(str(tmp_path / "user" / "codex.json"), b"[]")
    _touch(str(tmp_path / "user" / "ok" / "codex.json"), b"[]")
    real_open = open          # module attribute lookup does not fall back to builtins

    def blowing_open(path, *a, **kw):
        if os.path.abspath(str(path)) == os.path.abspath(locked):
            raise PermissionError("locked by another process")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(discover, "open", blowing_open, raising=False)
    result = discover.discover(empty_roots)
    assert [os.path.basename(os.path.dirname(f.path))
            for f in _by(result, provider="codex")] == ["ok"]
    assert any("locked by another process" in e for e in result.stats.errors)


def test_unreadable_directory_does_not_abort_the_scan(tmp_path, empty_roots, monkeypatch):
    _touch(str(tmp_path / "user" / "good" / "codex.json"), b"[]")
    bad = str(tmp_path / "user" / "bad")
    os.makedirs(bad, exist_ok=True)
    real_scandir = os.scandir

    def blowing_scandir(path):
        if os.path.abspath(str(path)) == os.path.abspath(bad):
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(discover.os, "scandir", blowing_scandir)
    result = discover.discover(empty_roots)
    assert _by(result, provider="codex")                # the good branch still found
    assert any("bad" in e for e in result.stats.errors)


def test_symlink_loop_terminates(tmp_path, empty_roots):
    base = str(tmp_path / "user" / "loop")
    os.makedirs(base, exist_ok=True)
    try:
        os.symlink(str(tmp_path / "user"), os.path.join(base, "back"),
                   target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):  # pragma: no cover
        pytest.skip("this platform/user cannot create a directory symlink")
    _touch(str(tmp_path / "user" / "codex.json"), b"[]")
    result = discover.discover(empty_roots)              # must terminate
    assert len(_by(result, provider="codex")) == 1       # counted exactly once


# --------------------------------------------------------- caller-path discipline

@pytest.mark.parametrize("bad", ["\\\\server\\share", "//server/share", "relative/dir"])
def test_nonlocal_or_relative_roots_are_rejected(bad):
    with pytest.raises(ValueError):
        discover.discover(discover.Roots(user_dirs=(bad,)))


def test_absolute_local_root_is_accepted(tmp_path):
    discover.discover(discover.Roots(user_dirs=(str(tmp_path),)))   # must not raise


# -------------------------------------------------------------------- read-only

def test_scan_mutates_nothing(tmp_path, empty_roots):
    _codex_home(str(tmp_path / "codex"), jsonl=1, zst=1)
    _corpus_index(str(tmp_path / "idx" / "corpus.sqlite"))
    _touch(str(tmp_path / "user" / "codex.json"), b"[]")

    def snapshot():
        seen = {}
        for root, _dirs, files in os.walk(str(tmp_path)):
            for name in files:
                p = os.path.join(root, name)
                st = os.stat(p)
                seen[p] = (st.st_size, st.st_mtime_ns)
        return seen

    before = snapshot()
    discover.discover(empty_roots)
    assert snapshot() == before


# ---------------------------------------------------------------------- privacy

def test_never_returns_file_content(tmp_path, empty_roots):
    canary = "CANARY_PRIVATE_TEXT_MUST_NOT_ESCAPE"
    _touch(str(tmp_path / "user" / "conversations.json"),
           json.dumps([{"name": canary}]).encode("utf-8"))
    _touch(str(tmp_path / "user" / "codex.json"),
           json.dumps({"title": canary}).encode("utf-8"))
    result = discover.discover(empty_roots)
    assert _by(result, kind=discover.KIND_EXPORT_FILE)      # the detector DID fire
    assert canary not in json.dumps(result.as_dict())
    assert canary not in repr(result)


# ------------------------------------------------------------------ extensibility

def test_a_new_provider_is_added_by_one_registry_entry(tmp_path, empty_roots):
    """Adding a provider must not require new scan code — only a table row."""
    _touch(str(tmp_path / "user" / "grokked.json"), b"[]")
    spec = discover.ExportSpec(provider="madeup", patterns=("grokked.json",),
                               confidence=discover.CONF_LOW)
    result = discover.discover(empty_roots, specs=discover.PROVIDERS + (spec,))
    hits = _by(result, provider="madeup")
    assert len(hits) == 1
    assert hits[0].confidence == discover.CONF_LOW


def test_a_new_store_provider_is_added_by_one_registry_entry(tmp_path, empty_roots):
    _touch(str(tmp_path / "claude" / "invented" / "s1.log"))
    spec = discover.StoreSpec(provider="madeup-store", root="claude_home",
                              subdir="invented", item_patterns=("*.log",))
    hits = _by(discover.discover(empty_roots, specs=discover.PROVIDERS + (spec,)),
               provider="madeup-store")
    assert len(hits) == 1 and hits[0].count == 1


def test_a_store_whose_items_sit_in_its_base_omits_items_root(tmp_path, empty_roots):
    """With no subdir the finding path IS the item root, so repeating it would be
    noise a caller has to reconcile."""
    _touch(str(tmp_path / "claude" / "s1.log"))
    spec = discover.StoreSpec(provider="madeup-store", root="claude_home",
                              item_patterns=("*.log",))
    hit = _by(discover.discover(empty_roots, specs=(spec,)),
              provider="madeup-store")[0]
    assert hit.path == str(tmp_path / "claude")
    assert "items_root" not in hit.detail


def test_registry_ships_only_grounded_providers():
    """Guards against inventing a filename pattern for a provider whose on-disk shape
    is unknown. Every shipped provider is one this repo has an adapter or loader for."""
    grounded = {"codex", "claude", "claude-code", "chatgpt", "gemini", "anthology",
                "grok"}
    assert {s.provider for s in discover.PROVIDERS} <= grounded


# --------------------------------------------------------------- default roots

def test_default_roots_prefers_codex_home_env(tmp_path):
    home = str(tmp_path / "home")
    explicit = str(tmp_path / "elsewhere")
    roots = discover.default_roots(home=home, env={"CODEX_HOME": explicit})
    assert roots.codex_home == explicit
    assert roots.claude_home == os.path.join(home, ".claude")


def test_default_roots_falls_back_to_dot_codex(tmp_path):
    home = str(tmp_path / "home")
    roots = discover.default_roots(home=home, env={})
    assert roots.codex_home == os.path.join(home, ".codex")
    assert any(d.endswith("Downloads") for d in roots.user_dirs)
    assert all(os.path.isabs(d) for d in roots.user_dirs)


def test_default_roots_uses_the_process_environment_when_none_given(monkeypatch,
                                                                    tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "envcodex"))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    assert discover.default_roots().codex_home == str(tmp_path / "envcodex")


def test_discover_with_no_roots_uses_the_defaults(monkeypatch, tmp_path):
    """The zero-argument call the UI will make must work without exploding."""
    monkeypatch.setattr(discover, "default_roots",
                        lambda: discover.Roots(user_dirs=(str(tmp_path),)))
    assert discover.discover().findings == ()
