"""Generate js/src/generated/themes.ts from llm_anthology/themes/*.css.

The rendered page inlines its CSS (no remote fetch, CSP default-src 'none'), so the
JS rail needs the same stylesheet bytes the Python rail uses. Embedding it as a
generated module means it ships inside the npm package with no file I/O at runtime
AND cannot drift from the Python copy — regenerate instead of editing.

    python tools/gen-themes.py
"""
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "llm_anthology", "themes")
OUT = os.path.join(ROOT, "js", "src", "generated", "themes.ts")


def main():
    entries = []
    for path in sorted(glob.glob(os.path.join(SRC, "*.css"))):
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as fh:
            entries.append((name, fh.read()))

    body = ",\n  ".join("%s: %s" % (json.dumps(n), json.dumps(css)) for n, css in entries)
    src = (
        "// GENERATED FILE - do not edit by hand.\n"
        "// Produced by tools/gen-themes.py from llm_anthology/themes/*.css so both rails inline\n"
        "// byte-identical CSS. Edit the .css file and re-run the generator.\n\n"
        "export const THEMES: Readonly<Record<string, string>> = {\n  %s,\n};\n\n"
        "/** Missing theme -> empty string, mirroring the Python loader's OSError path. */\n"
        "export function loadTheme(name: string): string {\n"
        "  return THEMES[name] ?? \"\";\n"
        "}\n" % body
    )
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(src)
    print("WROTE", OUT, os.path.getsize(OUT), "bytes | themes:",
          ", ".join(n for n, _ in entries))


if __name__ == "__main__":
    main()
