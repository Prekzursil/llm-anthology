"""`llm-anthology` — render ChatGPT / Claude / Gemini session exports to faithful HTML + Markdown.

Fully local and offline: this tool never opens a network connection. Point it at an
export you already have on disk and it writes <out_dir>/html, <out_dir>/md, an index
and two reports (a text-exact fidelity gate and a hidden-unicode audit).

  llm-anthology claude   <export.json | dir>  <out_dir>
  llm-anthology chatgpt  <conversations.json> <out_dir> [--projects FILE]
  llm-anthology codex    <codex.json>         <out_dir>
  llm-anthology gemini   <transcript.json>    <out_dir> [--harvest FILE]
  llm-anthology demo     <out.html>

`index` is the odd one out: it writes no site, it builds the SQLite corpus index the
cockpit consumes (`python -m llm_anthology.sidecar --index <path>`).

  llm-anthology index    <sessions_root>      <out.sqlite> [--codex-home DIR]
"""
import argparse
import os
import sys

from llm_anthology import build, corpus, demo, index, loaders, render_html
from llm_anthology.adapters import codex_state

# How many ingest errors get a detail line before the rest are summarised. A tree of
# thousands of unreadable rollouts must not bury the counts under its own error log.
MAX_ERRORS_SHOWN = 10


def build_parser():
    p = argparse.ArgumentParser(
        prog="llm-anthology",
        description="Render AI session exports to faithful HTML + clean Markdown. "
                    "Fully offline — no network calls, ever.")
    sub = p.add_subparsers(dest="cmd")

    c = sub.add_parser("claude", help="Claude native export (a .json file or a directory of them)")
    c.add_argument("src")
    c.add_argument("out_dir")

    g = sub.add_parser("chatgpt", help="ChatGPT conversations.json (or a harvested array)")
    g.add_argument("src")
    g.add_argument("out_dir")
    g.add_argument("--projects", default=None,
                   help="optional second export whose records carry __project_id")

    x = sub.add_parser("codex", help="Codex task export (codex.json) — a THIRD shape, "
                                     "NOT readable by the chatgpt subcommand")
    x.add_argument("src")
    x.add_argument("out_dir")

    m = sub.add_parser("gemini", help="Google Takeout 'Gemini Apps' activity transcript.json")
    m.add_argument("src")
    m.add_argument("out_dir")
    m.add_argument("--harvest", default=None,
                   help="web-app harvest enabling TRUE conversation grouping "
                        "(without it, grouping is a labelled provisional heuristic)")

    d = sub.add_parser("demo", help="write a synthetic sample page (no real content)")
    d.add_argument("out_html")

    i = sub.add_parser("index", help="build the SQLite corpus index the cockpit reads "
                                     "(the ONLY supported way to produce one)")
    i.add_argument("src", help="the Codex sessions ROOT — the date-nested "
                               "YYYY/MM/DD/rollout-*.jsonl tree")
    i.add_argument("out_index", help="the SQLite index FILE to write; hand it to "
                                     "`sidecar --index <this>`")
    i.add_argument("--codex-home", default=None,
                   help="directory holding state_5.sqlite (the spawn graph). Omitted "
                        "means the LIVE store ($CODEX_HOME, else ~/.codex) — the "
                        "resolved path is always printed. Point this at a directory "
                        "with no state_5.sqlite to index the rollouts alone.")
    return p


def _build_index(args):
    """`index` — build the cockpit's SQLite corpus index. Returns the exit code.

    Kept out of `main` because it shares nothing with the four render subcommands: it
    writes one SQLite FILE rather than a site directory, so it never reaches
    build.render_corpus / print_report.
    """
    out = os.path.abspath(args.out_index)
    # corpus.open_index is a bare sqlite3.connect — it creates the file but NOT its
    # parent directory, so do that here exactly as the demo branch has to.
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    # Announce before the work: a real sessions tree takes minutes and the call below
    # passes no `progress=`, so this line is the only thing standing between the user
    # and a silent terminal. NOT because there is no hook — `load_corpus` accepts one
    # (loaders.py:319) and forwards it to `index.build_index` after every committed
    # chunk (loaders.py:428). An earlier version of this comment denied that any such
    # hook existed at all; that was true when it was written and the forward has since
    # been added, so per-chunk CLI progress is a one-line change AT THIS CALL SITE
    # rather than a change in loaders. Whether to wire it is an OPEN decision, not a
    # settled one — nothing here records a reason for leaving it unwired.
    #
    # The dead phrasing is deliberately PARAPHRASED above rather than quoted back:
    # `tests/test_citation_anchors.py` bans it as a literal substring of this file, and
    # a gate that cannot tell a quotation from an assertion is the right trade — a
    # re-assertion must not be able to hide inside quote marks. Do not restore the
    # quote and then relax the gate.
    print("INDEX_BUILDING", args.src, "->", args.out_index, flush=True)
    # Disclose the state store BEFORE reading it. With no --codex-home, load_corpus
    # falls back to the LIVE Codex store ($CODEX_HOME, else ~/.codex — see
    # adapters/codex_state.py:129) and that read is otherwise SILENT: an absent or busy
    # DB is skipped without a word. Someone indexing an ARCHIVED sessions tree would get
    # this machine's live spawn graph merged in and never know. Resolved through
    # _db_path itself so the disclosure can never drift from the path actually read.
    print("CODEX_STATE_DB", codex_state._db_path(args.codex_home), flush=True)

    result, errors = loaders.load_corpus(args.src, out, codex_home=args.codex_home)

    conn = corpus.open_index(out)
    try:
        # Read the postcondition back OUT of the artifact instead of reporting the
        # in-memory objects. `result` is what load_corpus ASSEMBLED, which is not the
        # same thing as what reached disk -- reporting its thread/edge counts once
        # advertised a spawn graph the index did not contain. corpus.load_corpus is the
        # exact reader the sidecar uses to rebuild the cockpit's graph, so these are the
        # numbers the app will actually see.
        rows = index.count(conn)
        graph = corpus.load_corpus(conn)
    finally:
        conn.close()

    print("INGESTED_CONVERSATIONS", len(result.conversations))   # read out of the tree
    print("INDEX_ROWS", rows)                                    # ...and landed on disk
    print("INDEX_THREADS", len(graph.threads))
    print("INDEX_EDGES", len(graph.edges))
    print("INGEST_ERRORS", len(errors))
    # stderr is unbuffered while a piped stdout is block-buffered, so without this the
    # error detail below lands ABOVE the counts it belongs to whenever output is
    # redirected -- measured on a live run.
    sys.stdout.flush()
    for err in errors[:MAX_ERRORS_SHOWN]:
        # sorted(items) rather than named fields: an entry carries `line` only when it
        # came from a rollout, and a formatter that named fields would silently drop it.
        print("  INGEST_ERROR " + " ".join("%s=%s" % kv for kv in sorted(err.items())),
              file=sys.stderr)
    if len(errors) > MAX_ERRORS_SHOWN:
        print("  ... and %d more" % (len(errors) - MAX_ERRORS_SHOWN), file=sys.stderr)
    print("INDEX_WRITTEN", args.out_index)

    # Exit 3 on ANY ingest error -- partial as well as total. Same code, same rule as
    # the render path below: content went in and did not come out. The index is still
    # written and the build is idempotent, so re-running after fixing the bad file
    # completes it; but a caller scripting `index && open-the-cockpit` must not read a
    # half-ingested corpus as success. That silent-partial-success is the exact trap
    # this file (see the exit-3 note in main) and build.py:107 were already bitten by.
    # One code is enough -- the counts above already separate partial from total.
    return 3 if errors else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)

    if args.cmd == "demo":
        out = os.path.abspath(args.out_html)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        build.write_text(out, render_html.render_conversation_html(demo.demo_conversation()))
        print("DEMO_WRITTEN", args.out_html)
        return 0

    if args.cmd is None:              # pragma: no cover - empty argv is handled above and
        parser.print_help()           # argparse rejects any non-subcommand token before here
        return 2

    if not os.path.exists(args.src):
        print("ERROR: no such file or directory: %s" % args.src, file=sys.stderr)
        return 1

    if args.cmd == "index":
        return _build_index(args)

    if args.cmd == "claude":
        convs, errors = loaders.load_claude(args.src, args.out_dir)
        report = build.render_corpus(convs, args.out_dir, provider="claude", load_errors=errors)
    elif args.cmd == "chatgpt":
        convs, errors, proj_of = loaders.load_chatgpt(args.src, args.projects)
        report = build.render_corpus(convs, args.out_dir, provider="chatgpt", load_errors=errors,
                                     meta_of=lambda c: proj_of.get(c.id, ""))
    elif args.cmd == "codex":
        convs, errors = loaders.load_codex(args.src, args.out_dir)
        report = build.render_corpus(convs, args.out_dir, provider="codex", load_errors=errors)
    elif args.cmd == "gemini":
        convs, errors, extra = loaders.load_gemini(args.src, args.harvest)
        report = build.render_corpus(convs, args.out_dir, provider="gemini", load_errors=errors,
                                     extra=extra)
    else:                             # pragma: no cover - argparse constrains cmd to the four above
        parser.print_help()
        return 2

    build.print_report(report)
    # Exit 3 = "loaded, but produced nothing usable". Returning 0 here made a
    # wrong-provider or drifted export indistinguishable from a good run: the
    # corpus rendered blank pages and every automated caller saw success. A
    # genuinely empty input (0 conversations) is not an error -- there was
    # nothing to lose. Content that went in and did not come out is.
    if report["conversations"] and not report["turns"]:
        return 3
    return 0


if __name__ == "__main__":            # pragma: no cover
    raise SystemExit(main())
