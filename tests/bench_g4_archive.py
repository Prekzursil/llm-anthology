"""MEASUREMENT for DECISION G-4: what the self-contained archive actually costs, and what
the FTS rebuild actually costs in query latency.

NOT A TEST, and deliberately not named like one — `pyproject.toml` sets
`python_files` to pytest's default, so `bench_*.py` is never collected and this cannot
slow or gate the suite. It exists because G-4 shipped with two numbers that were NOT
measured, and a decision record carrying an unmeasured number is a guess with a citation:

  * SIZE. The decision said "11.5 MB -> an estimated 55-75 MB for a 122 MB corpus
    (~27 MB compressed bodies + a positional index). ESTIMATE, not measured — settle it by
    zstd-ing a corpus sample."
  * LATENCY. `detail=none` was chosen on a real measurement (p95 33 ms over 2.2M records,
    ~6x under a 200 ms budget). That measured SPEED, which was never the problem — but the
    rebuild has to be re-measured against the same budget, because `detail=full` stores
    positions and reads more of them.

Run it:

    python tests/bench_g4_archive.py            # ~1,071 conversations, ~122 MB of text
    python tests/bench_g4_archive.py --scale 8  # a fast eighth-scale smoke of the harness

WHAT IT BUILDS. A synthetic corpus SHAPED like the owner's real one, which is the only part
of the real corpus that may be copied: 1,071 conversations, ~122.1 MB of text, one outlier
holding a large share of it (the real one is 18.0 M chars). The text is assembled from a
fixed pool of invented prose and code lines with a seeded PRNG, so it carries the kind of
redundancy an agentic transcript carries — the property zstd's ratio depends on — and no
real conversation content appears anywhere.

WHAT IT COMPARES. The same corpus, indexed twice: once at the PRE-G-4 shape (contentless
`detail=none`, no bodies table) and once at the shipped shape. Two indexes built from one
generator is the only way the size delta is attributable to the change rather than to the
fixture.

WHAT IT MEASURED, 2026-08-10, SQLite 3.50.4, full scale (1,071 conversations, 117.0 MB of
FTS text, baseline calibrated at 6.1% of text against the real corpus's 9.4%):

    | shape                     | live index | settled | archive | bodies ratio | p95     |
    |---------------------------|-----------:|--------:|--------:|-------------:|--------:|
    | pre-G-4 (detail=none)     |     7.1 MB |  3.5 MB |       — |            — |  4-5 ms |
    | shipped, data-ratio 0.00  |    50.2 MB | 45.7 MB | 13.0 MB |       x12.45 |  111 ms |
    | shipped, data-ratio 0.45  |    87.8 MB | 83.2 MB | 51.0 MB |        x8.01 |  114 ms |

  * SIZE lands INSIDE the 55-75 MB estimate on the text-only shape and about 1.1x over its
    upper bound on the data-heavy one. The estimate did not account for `block.data` at all,
    and that is the parameter the total is most sensitive to — see `--data-ratio`.
  * LATENCY stays within the 200 ms budget in both, worst single sample 181 ms. But the
    headroom is now ~1.8x, against ~6x for the shape it replaced, so this is "within budget"
    and NOT "still comfortably fast". A corpus several times larger than the measured one
    would need re-measuring before anyone claims it holds.
  * THIS BENCHMARK CHANGED THE FORMAT ONCE ALREADY. The first implementation used one frame
    per TURN and measured 101.3 MB of archive at ratio x1.59 — 138.2 MB in total, 1.8x over
    the estimate. Byte-budgeted frames (`corpus._FRAME_BUDGET`) took the archive to 13.0 MB.
    Three of this file's own numbers were wrong before that: it summed the main database and
    an un-checkpointed WAL (double-counting pages), targeted its size on combined
    text+data instead of on the FTS text the real 122.1 MB figure counts, and drew every
    sentence from a pool of sixteen, which collapsed the `detail=none` baseline to 1.1% of
    text against the real 9.4%. Each is recorded at the code that fixes it.

PRIVACY: synthetic only. It never opens the owner's index and never reads a session store.
"""
import argparse
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_anthology import corpus, ir            # noqa: E402

#: Measured shape of the real corpus (from the G-4 decision record).
REAL_CONVERSATIONS = 1071
REAL_TEXT_BYTES = 122_100_000
REAL_INDEX_BYTES = 11_500_000
REAL_LARGEST_CHARS = 18_000_000
BUDGET_MS = 200

_PROSE = [
    "the assistant proposed a refactor and explained the trade it makes",
    "the user asked why the previous attempt failed on the windows leg",
    "reading the file first, then editing the one function that moved",
    "the test was written before the implementation and watched to fail",
    "coverage is enforced at one hundred percent on this rail",
    "the index is rebuilt because the option set is fixed at create time",
    "a phrase query needs positions, which detail none does not store",
    "the seek table records each frame's compressed and decompressed size",
]
_CODE = [
    "def _resolve(self, path, *, strict=False):",
    "    return os.path.abspath(os.path.expanduser(path))",
    "    if row is None:  # nothing stored yet, fall back",
    "        raise ValueError('unreadable marker %r' % value)",
    "conn.execute('SELECT rowid FROM conversations WHERE conversation_id=?', (cid,))",
    "assert reader.decompressed_size == row['text_bytes']",
    "    for index, entry in enumerate(self._entries):",
    "        out += chunk[lo:hi]",
]


def _hapax(rng):
    """A high-cardinality token — an identifier, a hex blob, a path, a version.

    WHY THE VOCABULARY HAS TO SCALE WITH THE CORPUS. The first version of this generator drew
    every sentence from a pool of sixteen, and the resulting `detail=none` index came out at
    0.5 MB for 45 MB of text — 1.1%, against the 9.4% measured on the real corpus (11.5 MB over
    122.1 MB). That is not a small discrepancy, it is the wrong shape: a contentless
    `detail=none` posting list costs one entry per DISTINCT (term, document) pair, so a corpus
    that repeats sixteen sentences produces a couple of hundred posting lists no matter how
    large it gets, and the baseline the whole comparison divides by collapses. `detail=full`
    is not distorted the same way — it stores one entry per token OCCURRENCE — so keeping the
    small pool would have flattered the new shape by shrinking only the old one.

    Real agentic transcripts are full of once-seen tokens: symbol names, file paths, hashes,
    timestamps, error strings. This mints them so distinct-term count grows with the corpus.
    """
    kind = rng.randint(0, 3)
    if kind == 0:
        return "sym_%s_%04x" % (rng.choice(("resolve", "emit", "walk", "bind")), rng.getrandbits(16))
    if kind == 1:
        return "%08x%08x" % (rng.getrandbits(32), rng.getrandbits(32))
    if kind == 2:
        return "pkg/mod%03d/file_%05d.py" % (rng.randint(0, 400), rng.getrandbits(17))
    return "v%d.%d.%d-rc%d" % (rng.randint(0, 9), rng.randint(0, 40), rng.getrandbits(9),
                               rng.randint(1, 9))


def _paragraph(rng, sentences, hapax_every):
    """Boilerplate prose salted with once-seen tokens, which is what a real transcript is.

    `hapax_every` is the CALIBRATION knob: one minted token per that many sentences. It is
    tuned against a measurement rather than to taste — the real corpus's `detail=none` index
    is 11.5 MB over 122.1 MB of text (9.4%), and this benchmark prints the synthetic corpus's
    own ratio next to that so the reader can see how far the fixture is from the thing it
    stands in for. At one hapax per sentence the synthetic ratio measured 22%, i.e. a
    vocabulary far denser than reality; at sixteen fixed sentences and no hapaxes it measured
    1.1%. Neither is usable, and the ratio being PRINTED is what makes that visible instead
    of assumed.
    """
    parts = []
    for i in range(sentences):
        parts.append(rng.choice(_PROSE))
        if i % hapax_every == 0:
            parts.append(_hapax(rng))
    return " ".join(parts)


def _blocks(rng, n_lines, data_ratio, hapax_every):
    """One turn's blocks: a prose paragraph plus, with probability `data_ratio`, a tool_use
    block whose `data` carries code. `data` is archived but NEVER enters the FTS body, so it
    is the parameter the archive's size is most sensitive to — hence a knob, not a constant."""
    blocks = [ir.Block(type="text",
                       text=_paragraph(rng, max(1, n_lines // 4), hapax_every))]
    if rng.random() < data_ratio:
        code = "\n".join("%s  # %s" % (rng.choice(_CODE), _hapax(rng))
                         for _ in range(n_lines))
        blocks.append(ir.Block(type="tool_use", text="Edit",
                               data={"name": "Edit", "input": {"code": code}}))
    return blocks


def _conversation(rng, cid, target_chars, data_ratio, hapax_every):
    """A conversation whose FTS BODY — `block.text` only, which is what `char_count` counts
    and what "122.1 MB of text" measured — is roughly `target_chars` long.

    The first version accumulated `len(text) + len(str(data))` toward the target, so a
    data-heavy shape hit the target on its tool payloads and the FTS text landed at 45 MB
    instead of 122 MB. Targeting the same quantity the real measurement counted is what makes
    the two comparable at all.
    """
    turns, size = [], 0
    while size < target_chars:
        blocks = _blocks(rng, rng.randint(4, 40), data_ratio, hapax_every)
        turns.append(ir.Turn(
            role="human" if len(turns) % 2 == 0 else "assistant",
            uuid="u-%s-%d" % (cid, len(turns)),
            timestamp="2026-0%d-%02dT10:00:00Z" % (rng.randint(1, 8), rng.randint(1, 28)),
            blocks=blocks))
        size += sum(len(b.text) for b in blocks)
    return ir.Conversation(
        id=cid, title="synthetic session %s about a refactor" % cid, provider="codex",
        account="acct", turns=turns, created_at="2026-03-01T00:00:00Z",
        updated_at="2026-03-02T00:00:00Z",
        meta={"thread_id": "t-%s" % cid, "rollout_paths": ["/synthetic/%s.jsonl" % cid]}), size


def _corpus_records(scale, data_ratio, hapax_every):
    """Every synthetic conversation, sized so the total FTS text lands near the real corpus.
    One outlier carries the same share of the whole that the real 18.0 M-char session does."""
    count = max(4, REAL_CONVERSATIONS // scale)
    total = REAL_TEXT_BYTES // scale
    outlier = REAL_LARGEST_CHARS // scale
    rest = max(1, (total - outlier) // max(1, count - 1))
    rng = random.Random(20260810)
    for i in range(count):
        yield _conversation(rng, "c%05d" % i, outlier if i == 0 else rest, data_ratio,
                            hapax_every)


def _old_shape(conn):
    """Re-create the PRE-G-4 index shape on a fresh connection: contentless `detail=none`
    FTS, no bodies table. Built by mutating the current schema rather than by keeping a
    second copy of the DDL, so the comparison cannot drift from what shipped."""
    corpus.init_index(conn)
    conn.executescript(
        "DROP TABLE conversation_bodies;\n"
        "DROP TABLE conversations_fts;\n"
        "CREATE VIRTUAL TABLE conversations_fts USING fts5("
        "title, body, content='', detail=none, contentless_delete=1);")
    return conn


def _build(path, records, old):
    conn = sqlite3.connect(path)
    _old_shape(conn) if old else corpus.init_index(conn)
    started = time.perf_counter()
    for conv, _size in records:
        if old:
            # the pre-G-4 write: FTS + row only, no archived body
            body = corpus._conversation_body(conv)
            values = (conv.id, conv.provider, conv.account, conv.title, conv.created_at,
                      conv.updated_at, len(conv.turns), len(body), "", "")
            cur = conn.execute(
                "INSERT INTO conversations(%s) VALUES (%s)"
                % (",".join(corpus._CONV_COLS), ",".join("?" * len(corpus._CONV_COLS))),
                values)
            conn.execute(
                "INSERT INTO conversations_fts(rowid, title, body) VALUES (?,?,?)",
                (cur.lastrowid, conv.title, body))
        else:
            corpus.add_conversation(conn, conv, rollout_path="/synthetic/%s.jsonl" % conv.id)
    conn.commit()
    conn.execute("INSERT INTO conversations_fts(conversations_fts) VALUES('optimize')")
    conn.commit()
    elapsed = time.perf_counter() - started
    # CHECKPOINT BEFORE MEASURING. The first full-scale run summed the main database AND the
    # `-wal` file, which DOUBLE-COUNTS: the WAL holds new versions of pages whose old versions
    # are still in the main file, so an un-checkpointed index appears to occupy both copies.
    # Under WAL — which `init_index` turns on — that is most of a freshly built index, and it
    # is exactly the sort of inflation a headline "x29" number must not rest on.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return conn, elapsed


_QUERIES = [
    '"quick brown"', '"the assistant proposed"', "refactor AND windows",
    "NEAR(phrase positions, 6)", "title: synthetic", "coverage", "seek*",
    '"one hundred percent"', "detail AND none AND positions", "conn OR execute",
]


def _p95(conn, repeats):
    """Wall-clock per MATCH, mirroring `sidecar._run_search`: the COUNT then the ranked
    page, both over the same JOIN, because that pair is what one `search.query` costs."""
    samples = []
    frm = ("FROM conversations_fts JOIN conversations c "
           "ON c.rowid = conversations_fts.rowid WHERE conversations_fts MATCH ?")
    for _ in range(repeats):
        for query in _QUERIES:
            started = time.perf_counter()
            try:
                conn.execute("SELECT COUNT(*) " + frm, (query,)).fetchone()
                conn.execute(
                    "SELECT c.conversation_id, c.title " + frm
                    + " ORDER BY bm25(conversations_fts), c.conversation_id LIMIT 50",
                    (query,)).fetchall()
            except sqlite3.OperationalError as exc:
                samples.append((query, None, str(exc)))
                continue
            samples.append((query, (time.perf_counter() - started) * 1000.0, None))
    timed = [ms for _q, ms, _e in samples if ms is not None]
    refused = sorted({q for q, ms, _e in samples if ms is None})
    ordered = sorted(timed)
    return {
        "n": len(timed),
        "p50": statistics.median(ordered) if ordered else None,
        "p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))] if ordered else None,
        "max": max(ordered) if ordered else None,
        "refused": refused,
    }


def _mb(n):
    return n / 1024.0 / 1024.0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scale", type=int, default=1,
                    help="divide the real corpus size by this (default 1 = full scale)")
    ap.add_argument("--repeats", type=int, default=20,
                    help="query repeats per expression for the latency sample")
    ap.add_argument("--data-ratio", type=float, default=0.45,
                    help="probability a turn carries a tool_use `data` payload. THE MOST "
                         "SENSITIVE PARAMETER and an UNVERIFIED one: `data` is archived but "
                         "never enters the FTS body, so it inflates the archive without "
                         "moving the 'MB of text' figure. Its real value is unknown here — "
                         "measuring it means reading the owner's private index, which this "
                         "benchmark may not do. Run 0.0 for the text-only floor.")
    ap.add_argument("--hapax-every", type=int, default=5,
                    help="mint one once-seen token per N sentences. Calibration knob: the "
                         "printed synthetic index/text ratio should land near the real "
                         "corpus's measured 9.4%%. Swept at 1/32 scale — 2 -> 19.7%%, "
                         "3 -> 11.4%%, 5 -> 10.4%% — so 5 is the default.")
    args = ap.parse_args(argv)

    print("generating a synthetic corpus at 1/%d of the measured real shape "
          "(data-ratio %.2f)..." % (args.scale, args.data_ratio))
    started = time.perf_counter()
    records = list(_corpus_records(args.scale, args.data_ratio, args.hapax_every))
    text_bytes = sum(len(corpus._conversation_body(c).encode("utf-8")) for c, _s in records)
    print("  %d conversations, %.1f MB of text, generated in %.1fs"
          % (len(records), _mb(text_bytes), time.perf_counter() - started))

    tmp = tempfile.mkdtemp(prefix="g4bench-")
    results = {}
    for label, old in (("pre-G-4 (detail=none, no bodies)", True),
                       ("shipped  (detail=full + bodies)", False)):
        path = os.path.join(tmp, ("old" if old else "new") + ".sqlite")
        conn, ingest = _build(path, records, old)
        size = sum(os.path.getsize(path + suffix) for suffix in ("", "-wal", "-shm")
                   if os.path.exists(path + suffix))
        # TWO denominators, because using one of them produced a nonsense number in the
        # first run of this benchmark: it divided the archive by the FTS body size and
        # reported "128% of the text they hold", i.e. compression that made data bigger.
        # `_conversation_body` flattens `block.text` ONLY, so a tool_use block's `data`
        # payload — code, tool input/output, the bulk of an agentic transcript — is in the
        # ARCHIVE and not in that figure. The compression ratio has to be measured against
        # what the archive actually stores (`SUM(text_bytes)`, the frames' decompressed
        # size); the FTS text is the right denominator only for "a 122 MB corpus" framing.
        blobs, payload = 0, 0
        if not old:
            blobs, payload = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(archive)), 0), COALESCE(SUM(text_bytes), 0) "
                "FROM conversation_bodies").fetchone()
        latency = _p95(conn, args.repeats)
        # ...and a SETTLED size too. A freshly built index carries free pages the ingest left
        # behind; VACUUM is what a distributed/exported archive would actually weigh. Both are
        # printed because a user's live file is the first number, not the second.
        conn.execute("VACUUM")
        conn.commit()
        conn.close()
        settled = os.path.getsize(path)
        results[label] = dict(size=size, settled=settled, blobs=blobs, payload=payload,
                              ingest=ingest, latency=latency)
        print("\n%s" % label)
        print("  index on disk       %8.1f MB   (%.1f MB after VACUUM)"
              % (_mb(size), _mb(settled)))
        if not old:
            print("  of which bodies     %8.1f MB compressed" % _mb(blobs))
            print("    they store        %8.1f MB of turn JSON -> ratio x%.2f"
                  % (_mb(payload), payload / max(1.0, float(blobs))))
            print("    (that JSON is %.1fx the %.1f MB of FTS text, because block.data —"
                  % (payload / max(1.0, float(text_bytes)), _mb(text_bytes)))
            print("     tool input/output, code — is archived but never in the FTS body)")
            print("  everything else     %8.1f MB (row store + detail=full FTS)"
                  % _mb(size - blobs))
        if old:
            print("  CALIBRATION         %8.1f%% of the FTS text (real corpus: %.1f%%, "
                  "11.5 MB over 122.1 MB)"
                  % (100.0 * size / max(1, text_bytes),
                     100.0 * REAL_INDEX_BYTES / REAL_TEXT_BYTES))
        print("  ingest              %8.1f s" % ingest)
        print("  MATCH p50 / p95     %8.1f / %.1f ms   (budget %d ms, max %.1f)"
              % (latency["p50"], latency["p95"], BUDGET_MS, latency["max"]))
        if latency["refused"]:
            print("  REFUSED outright    %s" % ", ".join(latency["refused"]))

    old_r = results["pre-G-4 (detail=none, no bodies)"]
    new_r = results["shipped  (detail=full + bodies)"]
    print("\n---- verdict ----")
    print("index      %.1f MB -> %.1f MB  (x%.2f);  settled %.1f -> %.1f MB (x%.2f)"
          % (_mb(old_r["size"]), _mb(new_r["size"]),
             new_r["size"] / max(1.0, float(old_r["size"])),
             _mb(old_r["settled"]), _mb(new_r["settled"]),
             new_r["settled"] / max(1.0, float(old_r["settled"]))))
    print("           the G-4 estimate was 55-75 MB -> %s"
          % ("WITHIN the estimate" if _mb(new_r["settled"]) <= 75
             else "OVER the estimate by x%.1f" % (_mb(new_r["settled"]) / 75.0)))
    print("p95        %.1f ms -> %.1f ms  (budget %d ms) -> %s"
          % (old_r["latency"]["p95"], new_r["latency"]["p95"], BUDGET_MS,
             "WITHIN budget" if new_r["latency"]["p95"] <= BUDGET_MS else "OVER BUDGET"))
    print("worst      %.1f ms -> %.1f ms -> %s"
          % (old_r["latency"]["max"], new_r["latency"]["max"],
             "every sample within budget" if new_r["latency"]["max"] <= BUDGET_MS
             else "the SLOWEST sample EXCEEDS the budget"))
    print("capability the old shape could not answer at all: %s"
          % ", ".join(old_r["latency"]["refused"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
