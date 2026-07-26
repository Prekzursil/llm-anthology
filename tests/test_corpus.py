"""corpus.py — the IR extension the cockpit consumes.

SYNTHETIC fixtures ONLY. The real corpus is PRIVATE medical/pharma data; nothing
here is a real conversation, thread id, path, or token count. Every fixture is a
made-up shape that mirrors the Phase-0 MEASURED schema (895 spawn edges / 1831
threads / 287 parents across 13,711 rollout files), never its content.

Two things are pinned:
  1. the in-memory graph contract — ThreadMeta / SpawnEdge / Corpus and the four
     graph helpers (roots / children_of / depth / fan_out) that render the spawn tree;
  2. the on-disk contract — the contentless FTS5 + threads + thread_spawn_edges +
     ingest_checkpoint schema, executed against a REAL sqlite so the DDL is proven
     valid rather than asserted as a string, and the row<->dataclass mapping the
     parallel build agents target.
"""
import sqlite3

import pytest

from llm_anthology import corpus, ir


# --------------------------------------------------------------------- fixtures

# Every sqlite connection opened by a test is tracked and closed when it ends, so the
# suite stays free of ResourceWarnings from connections the garbage collector reaps.
_OPEN = []


def _track(conn):
    _OPEN.append(conn)
    return conn


def _open(path):
    return _track(corpus.open_index(path))


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()

def _tm(tid="t1", **kw):
    return corpus.ThreadMeta(id=tid, **kw)


def _edge(parent, child, status="completed"):
    return corpus.SpawnEdge(parent_thread_id=parent, child_thread_id=child, status=status)


def _block(text="", btype="text"):
    return ir.Block(type=btype, text=text)


def _turn(blocks=None, role="human"):
    return ir.Turn(role=role, blocks=blocks if blocks is not None else [])


def _conv(cid="c1", title="A title", provider="codex", account="acct",
          turns=None, created="2026-01-01", updated="2026-01-02"):
    return ir.Conversation(id=cid, title=title, provider=provider,
                           turns=turns if turns is not None else [],
                           created_at=created, updated_at=updated, account=account)


def _mem_index():
    """A schema-applied in-memory index with Row access (search reads by name)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return _track(corpus.init_index(conn))


# --------------------------------------------------------- dataclass surface

def test_threadmeta_defaults_are_schema_tolerant():
    """id is the only required field; everything else defaults so a partial row from
    an older schema (no updated_at_ms) still constructs. updated_at_ms and
    created_at_ms are NULLABLE (the legacy canonical DB lacks updated_at_ms)."""
    m = corpus.ThreadMeta(id="t1")
    assert m.title == "" and m.model_provider == "" and m.tokens_used == 0
    assert m.created_at_ms is None and m.updated_at_ms is None
    assert m.git_branch == "" and m.cwd == "" and m.agent_role == ""
    assert m.agent_nickname == "" and m.preview == "" and m.rollout_path == ""


def test_threadmeta_carries_every_contracted_field():
    m = corpus.ThreadMeta(id="t1", title="root", model_provider="codex",
                          tokens_used=42, created_at_ms=1000, updated_at_ms=2000,
                          git_branch="feat/x", cwd="/repo", agent_role="architect",
                          agent_nickname="Ada", preview="hello", rollout_path="/a.jsonl")
    assert (m.id, m.tokens_used, m.updated_at_ms, m.agent_nickname) == \
        ("t1", 42, 2000, "Ada")


def test_spawnedge_status_defaults_to_empty():
    e = corpus.SpawnEdge(parent_thread_id="p", child_thread_id="c")
    assert e.parent_thread_id == "p" and e.child_thread_id == "c" and e.status == ""


def test_empty_corpus_has_no_conversations_threads_or_edges():
    c = corpus.Corpus()
    assert c.conversations == [] and c.threads == {} and c.edges == []


def test_corpus_version_is_an_int():
    assert isinstance(corpus.CORPUS_VERSION, int)


# ------------------------------------------------------------ graph builders

def test_add_thread_keys_by_id_and_returns_it():
    c = corpus.Corpus()
    m = c.add_thread(_tm("x", title="hi"))
    assert c.threads == {"x": m} and m.title == "hi"


def test_add_edge_appends_and_returns_it():
    c = corpus.Corpus()
    e = c.add_edge(_edge("p", "q"))
    assert c.edges == [e]


# ---------------------------------------------------------------- roots()

def test_an_isolated_thread_is_its_own_root():
    c = corpus.Corpus()
    c.add_thread(_tm("solo"))
    assert c.roots() == ["solo"]


def test_a_child_is_not_a_root_but_its_parent_is():
    c = corpus.Corpus()
    c.add_thread(_tm("p"))
    c.add_thread(_tm("ch"))
    c.add_edge(_edge("p", "ch"))
    assert c.roots() == ["p"]


def test_roots_includes_a_parent_only_referenced_by_an_edge():
    """Schema-tolerant: an edge whose parent is ABSENT from the threads table is
    still a graph node, and having no parent of its own it is a root — so a dangling
    spawn tree still renders instead of vanishing."""
    c = corpus.Corpus()
    c.add_thread(_tm("known"))
    c.add_edge(_edge("ghost", "known"))
    assert c.roots() == ["ghost"]


def test_roots_are_sorted_for_deterministic_rendering():
    c = corpus.Corpus()
    for t in ("b", "a", "c"):
        c.add_thread(_tm(t))
    assert c.roots() == ["a", "b", "c"]


# ------------------------------------------------------------ children_of()

def test_children_of_returns_sorted_distinct_children():
    c = corpus.Corpus()
    c.add_edge(_edge("p", "z"))
    c.add_edge(_edge("p", "a"))
    c.add_edge(_edge("p", "a"))  # duplicate in-memory edge
    assert c.children_of("p") == ["a", "z"]


def test_children_of_a_leaf_is_empty():
    c = corpus.Corpus()
    c.add_thread(_tm("leaf"))
    assert c.children_of("leaf") == []


# ---------------------------------------------------------------- fan_out()

def test_fan_out_counts_distinct_children():
    c = corpus.Corpus()
    c.add_edge(_edge("p", "a"))
    c.add_edge(_edge("p", "b"))
    c.add_edge(_edge("p", "a"))  # duplicate must not inflate the count
    assert c.fan_out("p") == 2


def test_fan_out_is_zero_for_a_leaf():
    assert corpus.Corpus().fan_out("nope") == 0


# ------------------------------------------------------------------ depth()

def test_depth_of_a_root_is_zero():
    c = corpus.Corpus()
    c.add_thread(_tm("r"))
    assert c.depth("r") == 0


def test_depth_counts_the_parent_chain():
    c = corpus.Corpus()
    c.add_edge(_edge("r", "a"))
    c.add_edge(_edge("a", "b"))
    assert c.depth("a") == 1 and c.depth("b") == 2


def test_depth_takes_the_shortest_path_when_a_node_has_two_parents():
    """'c' is spawned BOTH from a root (depth 1) and from a depth-1 node (depth 2).
    The shorter path defines its depth, so the helper is min() — a first-parent or a
    max() implementation would return 2 here and both fail."""
    c = corpus.Corpus()
    c.add_edge(_edge("root", "mid"))   # mid -> depth 1
    c.add_edge(_edge("root", "c"))     # c via root  -> 1
    c.add_edge(_edge("mid", "c"))      # c via mid   -> 2
    assert c.depth("c") == 1


def test_depth_is_finite_on_a_cycle_never_infinite_recursion():
    """Corrupt/looped spawn data must terminate, not blow the stack: a<->b."""
    c = corpus.Corpus()
    c.add_edge(_edge("a", "b"))
    c.add_edge(_edge("b", "a"))
    assert c.depth("a") == 2


# ------------------------------------------------------- schema as a constant

def test_index_schema_declares_a_contentless_detail_none_fts5_over_records():
    s = corpus.INDEX_SCHEMA
    assert "conversations_fts" in s and "content=''" in s and "detail=none" in s


def test_index_schema_declares_the_threads_edges_and_checkpoint_tables():
    s = corpus.INDEX_SCHEMA
    for table in ("threads", "thread_spawn_edges", "ingest_checkpoint"):
        assert table in s
    # the resumable-ingest bookkeeping columns are the contract the builder writes
    for col in ("file", "offset", "content_hash"):
        assert col in s


# ---------------------------------------------------- schema executed for real

def test_init_index_creates_every_contract_table():
    conn = _track(corpus.init_index(sqlite3.connect(":memory:")))
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"conversations", "conversations_fts", "threads",
            "thread_spawn_edges", "ingest_checkpoint"} <= names


def test_open_index_enables_wal_on_a_file_db(tmp_path):
    conn = _open(str(tmp_path / "idx.sqlite"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_open_index_creates_the_file_on_disk(tmp_path):
    path = tmp_path / "idx.sqlite"
    _open(str(path))
    assert path.is_file()


# ------------------------------------------------------- thread persistence

def test_upsert_thread_round_trips_including_a_null_updated_at_ms(tmp_path):
    """The whole ThreadMeta survives a write/read, and a None updated_at_ms stays
    None through NULL rather than becoming 0 — the schema-tolerance guarantee for the
    legacy DB that lacks the column."""
    conn = _open(str(tmp_path / "i.sqlite"))
    m = corpus.ThreadMeta(id="t1", title="root", model_provider="codex",
                          tokens_used=1234, created_at_ms=1000, updated_at_ms=None,
                          git_branch="feat/x", cwd="/repo", agent_role="architect",
                          agent_nickname="Ada", preview="hello", rollout_path="/a/b.jsonl")
    corpus.upsert_thread(conn, m)
    got = corpus.load_corpus(conn).threads["t1"]
    assert got == m


def test_upsert_thread_replaces_rather_than_duplicating_the_same_id(tmp_path):
    conn = _open(str(tmp_path / "i.sqlite"))
    corpus.upsert_thread(conn, _tm("t1", title="v1"))
    corpus.upsert_thread(conn, _tm("t1", title="v2"))
    c = corpus.load_corpus(conn)
    assert list(c.threads) == ["t1"] and c.threads["t1"].title == "v2"


# --------------------------------------------------------- edge persistence

def test_upsert_edge_round_trips(tmp_path):
    conn = _open(str(tmp_path / "i.sqlite"))
    corpus.upsert_edge(conn, corpus.SpawnEdge("p", "c", "completed"))
    edges = corpus.load_corpus(conn).edges
    assert len(edges) == 1
    assert (edges[0].parent_thread_id, edges[0].child_thread_id, edges[0].status) == \
        ("p", "c", "completed")


def test_upsert_edge_is_idempotent_on_the_parent_child_pair(tmp_path):
    """A resumed build re-emitting the same edge must not create a second row —
    the (parent, child) PRIMARY KEY dedupes and the last status wins."""
    conn = _open(str(tmp_path / "i.sqlite"))
    corpus.upsert_edge(conn, corpus.SpawnEdge("p", "c", "completed"))
    corpus.upsert_edge(conn, corpus.SpawnEdge("p", "c", "failed"))
    edges = corpus.load_corpus(conn).edges
    assert len(edges) == 1 and edges[0].status == "failed"


def test_load_corpus_on_an_empty_index_is_empty(tmp_path):
    c = corpus.load_corpus(_open(str(tmp_path / "e.sqlite")))
    assert c.threads == {} and c.edges == [] and c.conversations == []


# -------------------------------------------------- conversation FTS indexing

def test_add_conversation_indexes_a_computed_body_for_fts_search():
    """With no explicit body the searchable text is the title plus every block's
    text; an empty-string block contributes nothing (the falsy filter) while the
    title and a real block are indexed."""
    conn = _mem_index()
    conv = _conv(cid="c1", title="Widgets",
                 turns=[_turn([_block("alpha beta"), _block("")]), _turn([])])
    corpus.add_conversation(conn, conv)
    rows = corpus.search(conn, "alpha")
    assert len(rows) == 1 and rows[0]["conversation_id"] == "c1"
    # the title column is searchable too
    assert corpus.search(conn, "Widgets")[0]["conversation_id"] == "c1"


def test_add_conversation_accepts_an_explicit_body_override():
    """A build agent may index a sanitized/truncated body instead of the raw turns;
    passing body= skips the computation entirely."""
    conn = _mem_index()
    corpus.add_conversation(conn, _conv(cid="c2", title="T"), body="zzz custom text")
    assert corpus.search(conn, "zzz")[0]["conversation_id"] == "c2"


def test_add_conversation_is_idempotent_and_does_not_double_index(tmp_path):
    """Re-adding the same conversation_id returns the SAME rowid and adds no second
    FTS posting, so a resumed ingest cannot inflate the search index."""
    conn = _open(str(tmp_path / "i.sqlite"))
    conv = _conv(cid="c3", title="Once", turns=[_turn([_block("gamma")])])
    r1 = corpus.add_conversation(conn, conv)
    r2 = corpus.add_conversation(conn, conv)
    assert r1 == r2
    assert len(corpus.search(conn, "gamma")) == 1


def test_search_returns_the_retrievable_columns_including_thread_linkage():
    conn = _mem_index()
    conv = _conv(cid="c9", title="Report", provider="codex", account="me",
                 turns=[_turn([_block("delta")])], created="2026-05-01",
                 updated="2026-05-02")
    corpus.add_conversation(conn, conv, body="delta", thread_id="t9",
                            rollout_path="/r/9.jsonl")
    row = corpus.search(conn, "delta")[0]
    assert row["conversation_id"] == "c9" and row["provider"] == "codex"
    assert row["account"] == "me" and row["title"] == "Report"
    assert row["created_at"] == "2026-05-01" and row["updated_at"] == "2026-05-02"
    assert row["thread_id"] == "t9" and row["rollout_path"] == "/r/9.jsonl"
    assert row["turn_count"] == 1 and row["char_count"] == len("delta")


def test_search_respects_the_limit():
    conn = _mem_index()
    for i in range(3):
        corpus.add_conversation(conn, _conv(cid="d%d" % i, title="t"), body="omega")
    assert len(corpus.search(conn, "omega", limit=2)) == 2


def test_search_with_no_match_returns_an_empty_list():
    conn = _mem_index()
    corpus.add_conversation(conn, _conv(cid="c1", title="t"), body="present")
    assert corpus.search(conn, "absenttoken") == []


# ---------------------------------------------------- ingest checkpoints

def test_get_checkpoint_is_none_before_any_write(tmp_path):
    conn = _open(str(tmp_path / "i.sqlite"))
    assert corpus.get_checkpoint(conn, "rollout.jsonl") is None


def test_checkpoint_round_trips_and_replaces_on_a_re_ingest(tmp_path):
    """The resumable-build contract: record how far into a file the builder got plus
    its content hash; re-recording the same file REPLACES (idempotent), so a changed
    file advances the offset instead of appending a second checkpoint row."""
    conn = _open(str(tmp_path / "i.sqlite"))
    corpus.set_checkpoint(conn, "rollout.jsonl", 100, "hash-a")
    assert corpus.get_checkpoint(conn, "rollout.jsonl") == (100, "hash-a")
    corpus.set_checkpoint(conn, "rollout.jsonl", 250, "hash-b")
    assert corpus.get_checkpoint(conn, "rollout.jsonl") == (250, "hash-b")


# --------------------------------------------------------------- end to end

def test_a_small_corpus_persists_and_reloads_as_a_navigable_graph(tmp_path):
    """The whole contract in one flow: write threads + spawn edges, reload into a
    Corpus, and walk it — roots, children, depth and fan-out all answer from the
    reloaded rows, which is exactly what the cockpit's spawn-tree render does."""
    conn = _open(str(tmp_path / "corpus.sqlite"))
    for tid in ("root", "childA", "childB", "grand"):
        corpus.upsert_thread(conn, _tm(tid))
    corpus.upsert_edge(conn, _edge("root", "childA"))
    corpus.upsert_edge(conn, _edge("root", "childB"))
    corpus.upsert_edge(conn, _edge("childA", "grand"))
    c = corpus.load_corpus(conn)
    assert c.roots() == ["root"]
    assert c.children_of("root") == ["childA", "childB"]
    assert c.fan_out("root") == 2 and c.fan_out("childA") == 1
    assert c.depth("grand") == 2 and c.depth("root") == 0
