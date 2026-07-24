"""Build a SMALL, KNOWN synthetic corpus index for the Rust cargo e2e round-trip test.

SYNTHETIC ONLY -- every thread id, edge and conversation below is made up; this NEVER
touches the real $CODEX_HOME. Invoked by the Rust test as::

    python build_synth_index.py <out_index_path>

(with the repo root on PYTHONPATH so ``import aisr`` resolves). It writes a corpus of:

  * 3 threads   -- root-a (codex), child-b (codex), child-c (claude)
  * 2 edges     -- root-a -> child-b, root-a -> child-c   => one root (root-a), fan-out 2
  * 3 convs     -- conv-1/2 codex (2 + 1 turns), conv-3 claude (3 turns)

so the sidecar answers, deterministically:
  corpus.stats => conversations=3, threads=3, edges=2, records=6 (SUM turn_count),
                  providers={"codex":2, "claude":1}
  graph.roots  => [root-a] with child_count 2, provider "codex"
The Rust e2e asserts exactly these values over the real stdio JSON-RPC transport.
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
            id="root-a", title="root alpha", model_provider="codex", created_at_ms=1000))
        corpus.upsert_thread(conn, ThreadMeta(
            id="child-b", title="child beta", model_provider="codex", created_at_ms=1100))
        corpus.upsert_thread(conn, ThreadMeta(
            id="child-c", title="child gamma", model_provider="claude", created_at_ms=1200))
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
