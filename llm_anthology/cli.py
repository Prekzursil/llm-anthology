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

  llm-anthology index    [sessions_root]      <out.sqlite> [--codex-home DIR]

The same four exports the render subcommands read are FIRST-CLASS corpus inputs (G-1), so
`index` can build a searchable archive from a plain downloaded export with no live session
store present at all — which is the only artifact most users have:

  llm-anthology index <out.sqlite> --chatgpt-export <conversations.json | export dir>
  llm-anthology index <out.sqlite> --claude-export  <export.json | export dir>
  llm-anthology index <out.sqlite> --codex-export   <codex.json | dir>
  llm-anthology index <out.sqlite> --gemini-export  <transcript.json>

That is why `sessions_root` is OPTIONAL: at least one source must be named, and an export
counts. Naming several builds ONE index from all of them.
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
    i.add_argument("src", nargs="?", default="",
                   help="the Codex sessions ROOT — the date-nested "
                        "YYYY/MM/DD/rollout-*.jsonl tree. OPTIONAL: an export flag is a "
                        "source in its own right, so omit this to import an export alone. "
                        "At least one source must be named either way; naming none is "
                        "refused before anything is written.")
    i.add_argument("out_index", help="the SQLite index FILE to write; hand it to "
                                     "`sidecar --index <this>`")
    i.add_argument("--codex-home", default=None,
                   help="directory holding state_5.sqlite (the spawn graph). OMITTED "
                        "MEANS NO SPAWN GRAPH IS MERGED — not 'go find one'. The help "
                        "here used to promise a fallback to the LIVE store ($CODEX_HOME, "
                        "else ~/.codex); loaders.py guards the merge on this argument "
                        "being named, so that fallback is gone and omission is the safe "
                        "choice. Whether the graph was merged is printed either way.")
    i.add_argument("--grok-root", default=None,
                   help="a Grok Build session store (<enc-cwd>/<session-id>/). OPT-IN "
                        "and never defaulted: a Grok store holds private material, so "
                        "omitting this reads none. Reachable from the cockpit before "
                        "this flag existed, and from nowhere on the command line.")
    i.add_argument("--claude-root", default=None,
                   help="a Claude Code store (the projects/ tree under a Claude home). "
                        "OPT-IN and never defaulted, for the same reason — ~/.claude is "
                        "private and omitting this reads nothing.")
    # The four DOWNLOADED exports, promoted to first-class corpus inputs by G-1. Same flag
    # style as --grok-root / --claude-root above, and the same rule: opt-in, never
    # defaulted, never guessed. Each takes a FILE or an export DIRECTORY (except gemini,
    # whose Takeout transcript is read directly and so must be a file).
    i.add_argument("--chatgpt-export", default=None,
                   help="a ChatGPT export: conversations.json, ONE of its "
                        "conversations-NNN.json shards, or the export DIRECTORY (every "
                        "shard is contributed)")
    i.add_argument("--chatgpt-projects", default=None,
                   help="a SECOND ChatGPT export whose project-tagged conversations join "
                        "the same dedup pool (the render path's --projects)")
    i.add_argument("--claude-export", default=None,
                   help="a Claude account export: conversations.json, a design_chats "
                        "document, or the export DIRECTORY")
    i.add_argument("--codex-export", default=None,
                   help="a Codex TASK export (codex.json), or a directory holding one. A "
                        "third shape, unrelated to the rollout tree in `src`")
    i.add_argument("--gemini-export", default=None,
                   help="a Google Takeout 'Gemini Apps' activity transcript.json")
    i.add_argument("--gemini-harvest", default=None,
                   help="web-app harvest enabling TRUE conversation grouping for "
                        "--gemini-export (without it, grouping is a labelled provisional "
                        "heuristic, and the mode used is printed either way)")
    return p


def _export_specs(args):
    """The `(provider, path, second_path)` specs the export flags name, in flag order.

    A provider with no `--<provider>-export` contributes nothing — an omitted source is
    not read, which is the same no-fallback rule --grok-root and --claude-root follow.
    """
    return [(provider, path, second or "") for provider, path, second in (
        ("chatgpt", args.chatgpt_export, args.chatgpt_projects),
        ("claude", args.claude_export, None),
        ("codex", args.codex_export, None),
        ("gemini", args.gemini_export, args.gemini_harvest),
    ) if path]


def _missing_source(args, specs):
    """The first CHECKED source path that does not exist, or None.

    A named source that is not there must be an error rather than an ingest of nothing:
    every adapter GLOBS or walks, so a typo'd path contributes zero documents AND zero
    errors, which is the "perfectly successful build of nothing" this whole path keeps
    being bitten by. `src` keeps exactly the check it had when `main` still owned it — it
    moved here, it did not relax — and the export paths get the same one. Refusing before
    anything is written also keeps a pure typo from leaving a stray sqlite file behind.

    TWO DELIBERATE OMISSIONS, both stated rather than quietly assumed:

      * `--gemini-harvest` is not a corpus, it is a grouping HINT, and `load_gemini`
        already reports a named-but-absent harvest as an error while falling back to the
        labelled provisional heuristic. Hard-failing here would make that documented,
        tested path unreachable from the command line. `--chatgpt-projects` gets no such
        exemption — it IS an export, and silently ingesting none of it loses conversations.
      * `--grok-root` and `--claude-root` are NOT checked, and that is a pre-existing gap
        this unit does not close. `sidecar._corpus_build` refuses either root unless it is
        an existing directory, so the CLI is the laxer of the two surfaces;
        `test_index_forwards_the_grok_and_claude_roots_it_now_accepts` deliberately passes
        roots that do not exist and requires exit 0, so tightening it here would change a
        contract that belongs to the unit which added those flags. Worth closing — with
        that test — as its own change.

    An export path that slips through anyway is still not silent: `ingest_exports` turns a
    source resolving to no file into a `resolve` error and the exit code becomes 3.
    """
    checked = [args.src, args.chatgpt_projects]
    checked += [path for _provider, path, _second in specs]
    for path in checked:
        if path and not os.path.exists(path):
            return path
    return None


def _build_index(args):
    """`index` — build the cockpit's SQLite corpus index. Returns the exit code.

    Kept out of `main` because it shares nothing with the four render subcommands: it
    writes one SQLite FILE rather than a site directory, so it never reaches
    build.render_corpus / print_report.

    IT VALIDATES ITS OWN SOURCES, which the render subcommands do not have to. `main`
    checks that `args.src` exists before dispatching, and that check cannot serve this
    subcommand any more: `src` is OPTIONAL here (an export is a source in its own right),
    so an empty one is legal and every OTHER named source needs the same existence check.
    Both refusals happen before `os.makedirs` and before the index is opened, so a refused
    build writes nothing at all — that matters most for the shape argparse used to catch
    for free: `index <path>` with no second positional now parses, with that path read as
    the OUTPUT, so without the guard a typo'd sessions tree would get an sqlite file
    written over it.
    """
    specs = _export_specs(args)
    if not (args.src or args.grok_root or args.claude_root or specs):
        print("ERROR: name at least one source: a sessions ROOT positional, --grok-root, "
              "--claude-root, or one of --chatgpt-export / --claude-export / "
              "--codex-export / --gemini-export", file=sys.stderr)
        return 2
    missing = _missing_source(args, specs)
    if missing is not None:
        print("ERROR: no such file or directory: %s" % missing, file=sys.stderr)
        return 1

    out = os.path.abspath(args.out_index)
    # corpus.open_index is a bare sqlite3.connect — it creates the file but NOT its
    # parent directory, so do that here exactly as the demo branch has to.
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    # Announce before the work: a real sessions tree takes minutes and the call below
    # passes no `progress=`, so this line is the only thing standing between the user
    # and a silent terminal. NOT because there is no hook — `load_corpus` accepts one
    # (loaders.py:336) and forwards it to `index.build_index` after every committed
    # chunk (loaders.py:461). An earlier version of this comment denied that any such
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
    print("INDEX_BUILDING", args.src or "(no sessions root)", "->", args.out_index,
          flush=True)
    # Disclose the state store BEFORE reading it. The read is otherwise SILENT — an absent
    # or busy DB is skipped without a word — so someone indexing an ARCHIVED sessions tree
    # could get a live spawn graph merged in and never know. Resolved through _db_path
    # itself so the path can never drift from the one actually read.
    #
    # AND disclose whether it is read AT ALL, which is the half that was missing. This
    # printed the resolved live path unconditionally, on the premise that an omitted
    # --codex-home falls back to the LIVE store. `loaders.py` guards the entire state
    # merge on the argument being named, so that premise is dead: omitting the flag reads
    # nothing. The unconditional line therefore named the owner's real ~/.codex and
    # implied it had been opened — a false privacy alarm — while a user trusting the help
    # got an index with no spawn graph, which is the cockpit's primary view, and no error
    # to explain it. The path is still printed when one IS named, because the original
    # reason for printing it has not changed.
    #
    # The path shown in the NOT-read case is still resolved through the real resolution
    # order (`adapters/codex_state.py:129` — explicit home, else $CODEX_HOME, else
    # ~/.codex), so it names the store that WOULD be read. Printing it is what makes the
    # "not read" line meaningful: naming the store and then saying it was skipped is the
    # disclosure; suppressing the path entirely would leave a user unable to tell which
    # store they just failed to merge.
    merged = bool(args.codex_home)
    if merged:
        print("CODEX_STATE_DB", codex_state._db_path(args.codex_home), flush=True)
    else:
        print("CODEX_STATE_DB", codex_state._db_path(args.codex_home),
              "(NOT read — no --codex-home)", flush=True)
    print("STATE_GRAPH_MERGED", "yes" if merged else "no", flush=True)

    result, errors = loaders.load_corpus(args.src, out, codex_home=args.codex_home,
                                         grok_root=args.grok_root,
                                         claude_root=args.claude_root)

    # The EXPORT half of the ingest (G-1), against the same index. A second call rather
    # than a `load_corpus` argument because a streamed export must write and drop each
    # conversation — see the placement note in loaders — and both halves fold into ONE
    # error list, so the exit code below covers them together.
    #
    # Called unconditionally, with `specs` empty when no export flag was named: it then
    # reads nothing and the counts below print zeros, which is the same disclose-either-way
    # rule STATE_GRAPH_MERGED follows. Silence would be indistinguishable from a flag that
    # was accepted and ignored.
    # NOT named `export_files`: that is a public function in `loaders`, and a local of the
    # same name reads as a call site when it is a result.
    export_report, export_errors = loaders.ingest_exports(out, specs)
    errors = errors + export_errors
    for row in export_report:
        # PER FILE, because that is what makes a dead shard visible: a 17-shard export that
        # silently contributed 16 is exactly what a single total cannot show.
        line = ("EXPORT_INGEST provider=%(provider)s file=%(file)s "
                "conversations=%(conversations)d duplicates=%(duplicates)d" % row)
        if "grouping_mode" in row:
            # Gemini only, and never omitted: Takeout carries no conversation id, so a
            # corpus whose boundaries were INFERRED must not read like ground truth.
            line += " grouping_mode=" + row["grouping_mode"]
        print(line)
    print("EXPORT_CONVERSATIONS", sum(row["conversations"] for row in export_report))

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

    # The trailing note is not decoration: this line counts SESSION-STORE conversations
    # only, and since exports became a source (G-1) it can legitimately read 0 while
    # INDEX_ROWS below reads 3. Without saying which half it counts, a successful
    # export-only import looks like a failed one. The token and the number keep their
    # position, so `INGESTED_CONVERSATIONS <n>` still parses as before.
    print("INGESTED_CONVERSATIONS", len(result.conversations),   # read out of the tree
          "(session stores; exports are counted above)")
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

    # `index` VALIDATES ITS OWN SOURCES and so is dispatched BEFORE the check below. It has
    # several (an optional sessions root, two store roots, four exports) and the existence
    # rule has to cover each; the four render subcommands have exactly one, `src`, and it
    # is required, so the shared check still serves them unchanged.
    if args.cmd == "index":
        return _build_index(args)

    if not os.path.exists(args.src):
        print("ERROR: no such file or directory: %s" % args.src, file=sys.stderr)
        return 1

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
