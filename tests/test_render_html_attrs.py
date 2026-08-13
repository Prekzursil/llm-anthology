"""Hidden-unicode neutralisation on the ATTRIBUTE surfaces, and their audit coverage.

Two surfaces were unprotected while `SECURITY.md` claimed otherwise:

* the code-fence LANGUAGE, emitted into `class="language-…"` (`render_html.py`), and
* a media `path`, emitted into BOTH `src=` and `alt=` of the local `<img>`.

Neither passes through `_badge_text_nodes` (that runs only over markdown output), so a
flagged codepoint reached the DOM raw. `audit.hidden_char_hits` also missed both, because
`_STR_KEYS` did not name `language`/`path` — so the forensic sidecar reported CLEAN for a
conversation that carried a payload, contradicting `SECURITY.md` §"Three treatments" item 2
and its audit-covers-every-surface claim.

BOTH-STATES DISCIPLINE. Each surface is asserted as a pair:

* the `*_is_inert_ncr` / `*_audited` tests FIRE in the broken state (they are the fix's
  proof) — measured failing before the change;
* the `control_*` tests PASS in BOTH states. They exercise the same codepoint through the
  already-working body-text path, so a green control proves the assertion machinery is
  live rather than vacuous. A control that could not distinguish the two states would only
  prove the module imports.

Codepoints deliberately span all three invisible channels, because a Cf/Co/Cn-only scan
misses the modern one: a TAG codepoint (U+E0061, Cf-adjacent TAG block), a VS16 not
following a pictograph (U+FE0F, category **Mn**), and a variation-selector-supplement
(U+E0100, also **Mn**). The Mn pair is why these tests route through the shared
`sanitize` helpers instead of a hand-rolled category filter.
"""
from llm_anthology import audit, ir, render_html

TAG = "\U000e0061"        # TAG LATIN SMALL LETTER A — TAG block 0xE0000..0xE007F
VS16 = "️"           # VARIATION SELECTOR-16, category Mn, no pictograph before it
VSSUP = "\U000e0100"      # VARIATION SELECTOR-17, category Mn (supplement block)


def _conv(blocks, title="clean"):
    return ir.Conversation(id="c1", title=title, provider="claude", account="a",
                           turns=[ir.Turn("assistant", blocks)])


def _code(lang):
    return _conv([ir.Block("code", text="print(1)", data={"language": lang})])


def _media(path):
    return _conv([ir.Block("media", data={"path": path})])


# --------------------------------------------------------------------------
# render: code-fence language
# --------------------------------------------------------------------------

def test_code_fence_language_invisible_is_inert_ncr():
    h = render_html.render_conversation_html(_code("py" + TAG + "thon"))
    assert 'class="language-py&#xE0061;thon"' in h
    assert TAG not in h


def test_code_fence_language_variation_selectors_are_inert_ncr():
    h = render_html.render_conversation_html(_code("j" + VS16 + "s" + VSSUP))
    assert 'class="language-j&#xFE0F;s&#xE0100;"' in h
    assert VS16 not in h and VSSUP not in h


def test_code_fence_language_still_renders_clean_language():
    """The neutraliser must not disturb an ordinary language token."""
    h = render_html.render_conversation_html(_code("python"))
    assert 'class="language-python"' in h


# --------------------------------------------------------------------------
# render: media src / alt
# --------------------------------------------------------------------------

def test_media_src_and_alt_invisible_are_inert_ncr():
    h = render_html.render_conversation_html(_media("img" + TAG + ".png"))
    assert 'src="../media/img&#xE0061;.png"' in h
    assert 'alt="img&#xE0061;.png"' in h
    assert TAG not in h


def test_media_src_and_alt_variation_selector_is_inert_ncr():
    h = render_html.render_conversation_html(_media("sub/i" + VSSUP + ".png"))
    assert 'src="sub/i&#xE0100;.png"' in h
    assert 'alt="sub/i&#xE0100;.png"' in h
    assert VSSUP not in h


def test_media_clean_path_unchanged():
    h = render_html.render_conversation_html(_media("shot.png"))
    assert 'src="../media/shot.png"' in h and 'alt="shot.png"' in h


# --------------------------------------------------------------------------
# audit: the same two surfaces must be scanned
# --------------------------------------------------------------------------

def test_audit_records_code_fence_language():
    assert audit.hidden_char_hits(_code("py" + TAG + "thon")) == ["U+E0061"]


def test_audit_records_media_path():
    assert audit.hidden_char_hits(_media("img" + TAG + ".png")) == ["U+E0061"]


def test_audit_records_remote_media_path_too():
    """Locality gates RENDERING, never the forensic record."""
    assert audit.hidden_char_hits(_media("https://x/" + TAG + ".png")) == ["U+E0061"]


# --------------------------------------------------------------------------
# CONTROLS — green in BOTH states; prove the assertions above are not vacuous
# --------------------------------------------------------------------------

def test_control_body_text_invisible_is_badged():
    h = render_html.render_conversation_html(_conv([ir.Block("text", text="bo" + TAG + "dy")]))
    assert 'data-cp="U+E0061"' in h
    assert TAG not in h


def test_control_body_text_invisible_is_audited():
    assert audit.hidden_char_hits(_conv([ir.Block("text", text="bo" + TAG + "dy")])) == ["U+E0061"]


def test_control_clean_conversation_audits_empty():
    assert audit.hidden_char_hits(_code("python")) == []
    assert audit.hidden_char_hits(_media("shot.png")) == []


def test_control_markdown_link_destination_is_percent_encoded_upstream():
    """Why the two UNWRAPPED attribute sites do not leak — measured, not inferred.

    Enumerating every `quote=True` site in `render_html.py` gives nine attribute values.
    Six are now wrapped in `ncr_invisibles` directly (the link `title`/`href`, the citation
    `href`, and the three this module fixes), one is stripped by `sanitize_for_copy` (the
    document `<title>`), and two carry no wrap of their own: the defanged
    `data-unsafe-href` and the defanged-image `href`.

    An earlier version of this test asserted those two were saved by `_badge_text_nodes`
    emitting an NCR. That was an OVERCLAIM about the mechanism and the assertion failed:
    markdown-it normalises a link destination by PERCENT-ENCODING it, so U+E0061 arrives
    as `%F3%A0%81%A1` before any of our code runs and there is no invisible left to
    encode. Percent-encoding is a fine outcome — the bytes are visible ASCII in the
    source and in the URL bar, not an invisible in the DOM — but it is a different
    mechanism, living in a dependency rather than in this repo.
    """
    h = render_html.render_conversation_html(
        _conv([ir.Block("text", text="![a](https://x/f" + TAG + ".png)")]))
    assert 'href="https://x/f%F3%A0%81%A1.png"' in h
    assert TAG not in h


def test_control_badge_text_nodes_ncrs_invisibles_inside_a_tag():
    """The in-tag safety net, pinned directly because no public input reaches it.

    `_badge_text_nodes` splits on tags and NCR-encodes only within them, which is what
    would protect the two unwrapped `href` sites if a markdown-it upgrade, an attrs
    plugin, or a non-markdown caller ever handed them a raw invisible. Percent-encoding
    upstream means the public API cannot exercise that path today, so the helper is
    asserted directly — otherwise the net is documented but unmeasured.
    """
    out = render_html._badge_text_nodes(
        '<a href="https://x/f' + TAG + '.png">t' + TAG + 'xt</a>')
    assert 'href="https://x/f&#xE0061;.png"' in out      # inside a tag -> inert NCR
    assert 'data-cp="U+E0061"' in out                    # text node   -> visible badge
    assert TAG not in out
