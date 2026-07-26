"""`llm-anthology` — render ChatGPT / Claude / Gemini session exports to faithful HTML + Markdown.

Fully local and offline: this tool never opens a network connection. Point it at an
export you already have on disk and it writes <out_dir>/html, <out_dir>/md, an index
and two reports (a text-exact fidelity gate and a hidden-unicode audit).

  llm-anthology claude   <export.json | dir>  <out_dir>
  llm-anthology chatgpt  <conversations.json> <out_dir> [--projects FILE]
  llm-anthology codex    <codex.json>         <out_dir>
  llm-anthology gemini   <transcript.json>    <out_dir> [--harvest FILE]
  llm-anthology demo     <out.html>
"""
import argparse
import os
import sys

from llm_anthology import build, demo, loaders, render_html


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
    return p


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
