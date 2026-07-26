"""Build a SMALL, KNOWN synthetic corpus index for the Rust cargo e2e round-trip test.

SYNTHETIC ONLY -- every thread id, edge and conversation below is made up; this NEVER
touches the real $CODEX_HOME. Invoked by the Rust test as::

    python build_synth_index.py <out_index_path>

(with the repo root on PYTHONPATH so ``import aisr`` resolves). It writes a corpus of:

  * 3 threads   -- root-a (codex, born 1000, 100 tok), child-b (codex, 1100, 20 tok),
                   child-c (claude, 1200, 3 tok)   -- births SPAN TIME so the time-travel
                   methods have a non-trivial axis, and tokens differ so a rollup SUM is
                   meaningful (root-a subtree = 100+20+3 = 123).
  * 2 edges     -- root-a -> child-b, root-a -> child-c   => one root (root-a), fan-out 2
  * 3 convs     -- conv-1/2 codex (2 + 1 turns), conv-3 claude (3 turns); bodies
                   "alpha"/"beta"/"gamma" => char_count 5+4+5 = 14

so the sidecar answers, deterministically (asserted EXACTLY by the Rust e2e over the
real stdio JSON-RPC transport):
  corpus.stats   => conversations=3, threads=3, edges=2, records=6 (SUM turn_count),
                    providers={"codex":2, "claude":1}
  graph.roots    => [root-a] with child_count 2, provider "codex"
  graph.timeline => events=[1000,1100,1200], min=1000, max=1200, undated=0
  graph.at(1100) => nodes [child-b, root-a] (child-c not yet born); root-a child_count 1,
                    depth 0; child-b child_count 0, depth 1; one edge root-a->child-b
  graph.rollup   => root-a {self 100, subtree 123, subtree_count 3, max_depth 1, children 2};
                    child-b subtree 20; child-c subtree 3
  graph.diff(as_of_a=1000, as_of_b=1100)
                 => added_nodes ["child-b"], added_edges [root-a->child-b], no removes/changes
  export.plan    => node_count 3, edge_count 2, conversation_count 3, est_bytes 14
  export.run     => ok, graph_gate, transcript_gate all true; a real artifact on disk whose
                    graph re-parses to {child-b, child-c, root-a}
"""
import sys

from aisr import corpus, ir
from aisr.corpus import SpawnEdge, ThreadMeta


def _add_conv(conn, cid, provider, title, thread_id, nturns):
    conv = ir.Conversation(
        id=cid, title=title, provider=provider,
        turns=[ir.Turn("human", []) for _ in range(nturns)],
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        account="acct")
    corpus.add_conversation(conn, conv, body=title, thread_id=thread_id)


def build(path):
    conn = corpus.open_index(path)
    try:
        corpus.upsert_thread(conn, ThreadMeta(
            id="root-a", title="root alpha", model_provider="codex",
            created_at_ms=1000, tokens_used=100))
        corpus.upsert_thread(conn, ThreadMeta(
            id="child-b", title="child beta", model_provider="codex",
            created_at_ms=1100, tokens_used=20))
        corpus.upsert_thread(conn, ThreadMeta(
            id="child-c", title="child gamma", model_provider="claude",
            created_at_ms=1200, tokens_used=3))
        corpus.upsert_edge(conn, SpawnEdge("root-a", "child-b", "completed"))
        corpus.upsert_edge(conn, SpawnEdge("root-a", "child-c", "completed"))
        _add_conv(conn, "conv-1", "codex", "alpha", "root-a", 2)
        _add_conv(conn, "conv-2", "codex", "beta", "child-b", 1)
        _add_conv(conn, "conv-3", "claude", "gamma", "child-c", 3)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    build(sys.argv[1])
