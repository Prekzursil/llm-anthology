"""Shared build layer: IR conversations -> html/ + md/ + index + reports.

This layer was extracted from the three build_*.py scripts so the package can ship
one tested implementation behind a console entry point. The invariants that matter:
a single bad conversation must never truncate the corpus, and the index must not be
an injection vector (titles are attacker-influenced content).
"""
import json
import os

from llm_anthology import build, ir, render_html


def _conv(title="T", n=1, text="hello", account="a@b"):
    return ir.Conversation(id="c%d" % n, title=title, provider="claude", account=account,
                           turns=[ir.Turn("human", [ir.Block("text", text=text)])])


def test_safe_name_indexes_and_strips_illegal_path_chars():
    name = build.safe_name('a/b:c*d?"e<f>g|h', 7)
    assert name.startswith("007-")
    for ch in '/\\:*?"<>|':
        assert ch not in name


def test_safe_name_falls_back_when_title_is_empty_or_none():
    assert build.safe_name(None, 1).endswith("untitled")
    assert build.safe_name("   ", 2).endswith("untitled")


def test_safe_name_truncates_long_titles():
    assert len(build.safe_name("x" * 500, 1)) < 100


def test_render_corpus_writes_html_md_index_and_reports(tmp_path):
    out = str(tmp_path)
    rep = build.render_corpus([_conv("First", 1), _conv("Second", 2)], out, provider="claude")

    assert rep["rendered"] == 2
    assert os.path.isfile(os.path.join(out, "index.html"))
    assert os.path.isfile(os.path.join(out, "_fidelity-report.json"))
    assert os.path.isfile(os.path.join(out, "_hidden-char-audit.json"))
    html_files = os.listdir(os.path.join(out, "html"))
    md_files = os.listdir(os.path.join(out, "md"))
    assert len(html_files) == 2 and len(md_files) == 2
    assert all(f.endswith(".html") for f in html_files)
    assert all(f.endswith(".md") for f in md_files)


def test_render_corpus_isolates_a_failing_conversation(tmp_path, monkeypatch):
    """One malformed conversation must not cost the rest of the corpus."""
    real = render_html.render_conversation_html

    def boom(conv):
        if conv.title == "BAD":
            raise ValueError("synthetic render failure")
        return real(conv)

    monkeypatch.setattr(render_html, "render_conversation_html", boom)
    out = str(tmp_path)
    rep = build.render_corpus([_conv("ok1", 1), _conv("BAD", 2), _conv("ok2", 3)], out,
                              provider="claude")

    assert rep["rendered"] == 2                      # the two good ones survived
    assert len(rep["errors"]) == 1
    assert rep["errors"][0]["stage"] == "render"
    with open(os.path.join(out, "_fidelity-report.json"), encoding="utf-8") as fh:
        report = json.load(fh)
    assert len(report["errors"]) == 1


def test_index_escapes_title_and_cannot_inject_markup(tmp_path):
    out = str(tmp_path)
    build.render_corpus([_conv('</a><script>alert(1)</script>', 1)], out, provider="claude")
    with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
        doc = fh.read()
    assert "<script>alert(1)</script>" not in doc


def test_index_carries_the_per_provider_meta_column(tmp_path):
    out = str(tmp_path)
    build.render_corpus([_conv("t", 1)], out, provider="chatgpt",
                        meta_of=lambda c: "proj-XYZ")
    with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
        doc = fh.read()
    assert "proj-XYZ" in doc


def test_load_errors_are_carried_into_the_report(tmp_path):
    out = str(tmp_path)
    rep = build.render_corpus([_conv("t", 1)], out, provider="claude",
                              load_errors=[{"file": "x.json", "stage": "parse", "error": "boom"}])
    assert any(e["stage"] == "parse" for e in rep["errors"])


def test_hidden_char_conversations_are_audited(tmp_path):
    out = str(tmp_path)
    rep = build.render_corpus([_conv("t", 1, text="a​b")], out, provider="claude")
    with open(os.path.join(out, "_hidden-char-audit.json"), encoding="utf-8") as fh:
        rows = json.load(fh)
    assert rep["hidden_char_conversations"] == 1 and len(rows) == 1


def test_unknown_provider_theme_falls_back_rather_than_crashing(tmp_path):
    out = str(tmp_path)
    rep = build.render_corpus([_conv("t", 1)], out, provider="not-a-provider")
    assert rep["rendered"] == 1
    assert os.path.isfile(os.path.join(out, "index.html"))
