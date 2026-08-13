"""FIELD-level parity between the engine's `corpus.build` params and TypeScript `BuildParams`.

WHY THIS EXISTS. Every cross-boundary gate in this repo stops at the METHOD-NAME level, and
C-A hid for a whole release at exactly the level below it:

  * `cockpit/src-tauri/src/lib.rs` `every_engine_rpc_has_a_registered_tauri_command` diffs RPC
    NAMES against `generate_handler![...]`, and says so itself — "Only the registration list is
    searched here". `corpus.build` IS registered as `corpus_build`, so it was green throughout.
  * `tests/test_sidecar_build.py::test_the_export_params_match_the_loader_vocabulary` is the
    right SHAPE of gate but is Python-to-Python: it holds `_EXPORT_PARAMS` against
    `loaders.EXPORT_PROVIDERS` and stops at the language boundary.
  * `tests/test_citation_anchors.py` checks that `types.ts`' citations still point at live text.
    A citation can be perfectly accurate over an INCOMPLETE contract, and was: the `BuildParams`
    doc cited `sidecar.py:990-1037`, the exact range containing the export-key read it omitted.
  * `cockpit/src/ipc/mock.test.ts` and `discoveryPanel.test.ts` do assert export-key behaviour,
    but against HAND-WRITTEN literal lists (`const keys = ["chatgpt_export", ...]`). A seventh
    key added to the engine would not make any of them red.

So the engine read six parameters — `chatgpt_export`, `chatgpt_projects`, `claude_export`,
`codex_export`, `gemini_export`, `gemini_harvest` — that the TypeScript interface could not
express, four user-visible strings asserted the capability did not exist, and the whole suite
was green. U-CA1 propagated the fields; this gate is what makes the regression impossible
rather than merely fixed.

WHAT IT COMPARES, AND WHY IT IS TWO SIGNALS AND NOT ONE. The engine surface is assembled from
two mechanically different reads that cannot substitute for each other:

  1. an AST walk of the `_corpus_build` handler, which yields the LITERAL keys
     (`_opt_str(params, "sessions_root")` and friends) and cannot see the export keys at all,
     because they arrive through `{key: _opt_str(params, key) for key in _EXPORT_KEYS}`;
  2. the imported `sidecar._EXPORT_KEYS`, the runtime tuple, which yields exactly the keys the
     AST walk is blind to — and is admitted ONLY because the same AST walk independently
     proves the handler still iterates that constant.

Neither half alone is the surface, and a control asserts the two sets are DISJOINT, so a
collection-level mistake that made one half silently mirror the other would be loud rather
than reporting a comfortable parity.

The gate is deliberately scoped to `corpus.build` <-> `BuildParams`. That is the pair that
shipped the defect, and widening it to every RPC would make this file red whenever any other
in-flight unit touches an unrelated interface. Adding a second pair is a one-line table edit
below when someone wants it.

BOTH-STATES. Per `single-signal-verification` 3b a gate that is silent in both states measures
nothing, so the reconstructed pre-624c9da `types.ts` is fed through the same comparison and
must come back red with exactly the six fields — and each mutation asserts first that it
actually changed the text, because a no-op mutation makes a both-states test vacuous.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from llm_anthology import sidecar

REPO = Path(__file__).resolve().parents[1]
SIDECAR_PY = REPO / "llm_anthology" / "sidecar.py"
TYPES_TS = REPO / "cockpit" / "src" / "ipc" / "types.ts"

#: The gated pairs: (RPC method, its handler in `sidecar.py`, its interface in `types.ts`).
#: A table edit, exactly like `discover.py`'s provider table.
GATED_PAIRS = (("corpus.build", "_corpus_build", "BuildParams"),)

#: The six wire names the engine has accepted since the G-1 seam. Spelled out because the
#: finding this gate closes warned about the precise trap: `_EXPORT_PARAMS` holds the BARE
#: provider names (`chatgpt`, `claude`, ...), and an implementer who diffs those instead of
#: the derived `_EXPORT_KEYS` adds wrongly-named TS fields and calls the boundary fixed.
EXPECTED_EXPORT_KEYS = ("chatgpt_export", "chatgpt_projects", "claude_export",
                        "codex_export", "gemini_export", "gemini_harvest")

#: The four session-store roots, read as string literals by the handler. Anchors for the
#: detector control: a scraper that cannot find these known-present keys is broken, and its
#: silence about anything else means nothing.
EXPECTED_LITERAL_KEYS = ("sessions_root", "grok_root", "claude_root", "codex_home")

_PARAM_READERS = ("_opt_str", "_req_str")


# --------------------------------------------------------------------------------------
# Engine side: what `corpus.build` actually reads out of `params`.
# --------------------------------------------------------------------------------------
def _handler_node(source: str, handler: str) -> ast.FunctionDef:
    """The handler's AST node. Fails CLOSED — a rename must be loud, not silently empty."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == handler:
            return node
    raise AssertionError(
        "detector broken: no `def %s` in %s. This gate cannot report parity it did not "
        "measure -- re-point GATED_PAIRS at the new handler name." % (handler, SIDECAR_PY))


def _literal_param_keys(node: ast.AST) -> frozenset[str]:
    """Every `params` key the handler names as a STRING LITERAL.

    Covers the three spellings a handler in this file could use: `_opt_str(params, "k")` /
    `_req_str(params, "k")`, `params.get("k")` and `params["k"]`. Blind by construction to
    keys read through a variable -- that is what `_dynamic_key_sources` is for.
    """
    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func, args = sub.func, sub.args
            named = (isinstance(func, ast.Name) and func.id in _PARAM_READERS
                     and len(args) >= 2 and isinstance(args[0], ast.Name)
                     and args[0].id == "params")
            got = (isinstance(func, ast.Attribute) and func.attr == "get"
                   and isinstance(func.value, ast.Name) and func.value.id == "params"
                   and len(args) >= 1)
            if named and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
                keys.add(args[1].value)
            elif got and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                keys.add(args[0].value)
        elif (isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name)
              and sub.value.id == "params" and isinstance(sub.slice, ast.Constant)
              and isinstance(sub.slice.value, str)):
            keys.add(sub.slice.value)
    return frozenset(keys)


def _dynamic_key_sources(node: ast.AST) -> frozenset[str]:
    """Module-level constants the handler ITERATES to read params by name.

    Matches `... _opt_str(params, key) ... for key in <NAME>` in any comprehension or `for`.
    The returned names are what makes importing a runtime tuple legitimate rather than
    assumed: the AST proves the handler still consumes it.
    """
    sources: set[str] = set()
    for sub in ast.walk(node):
        generators = getattr(sub, "generators", None)
        if generators is not None:
            pairs = [(g.target, g.iter) for g in generators]
        elif isinstance(sub, ast.For):
            pairs = [(sub.target, sub.iter)]
        else:
            continue
        for target, iterator in pairs:
            if not (isinstance(target, ast.Name) and isinstance(iterator, ast.Name)):
                continue
            for call in ast.walk(sub):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id in _PARAM_READERS and len(call.args) >= 2
                        and isinstance(call.args[0], ast.Name) and call.args[0].id == "params"
                        and isinstance(call.args[1], ast.Name)
                        and call.args[1].id == target.id):
                    sources.add(iterator.id)
    return frozenset(sources)


def _resolve(constant: str) -> frozenset[str]:
    """A module-level string tuple on the imported engine, flattened. Fails CLOSED."""
    value = getattr(sidecar, constant, None)
    resolved = frozenset(v for v in (value or ()) if isinstance(v, str) and v)
    if not resolved:
        raise AssertionError(
            "detector broken: `sidecar.%s` resolved to no wire keys (%r). A gate that "
            "measures an empty set reports parity for free." % (constant, value))
    return resolved


def engine_param_keys(source: str, handler: str) -> frozenset[str]:
    """The full `params` surface of one handler: literal keys plus iterated constants."""
    node = _handler_node(source, handler)
    keys = set(_literal_param_keys(node))
    for constant in sorted(_dynamic_key_sources(node)):
        keys |= _resolve(constant)
    if not keys:
        raise AssertionError(
            "detector broken: `%s` reads no params at all. Fail closed rather than "
            "reporting parity against an empty engine surface." % handler)
    return frozenset(keys)


# --------------------------------------------------------------------------------------
# TypeScript side: the fields an interface actually declares.
# --------------------------------------------------------------------------------------
def _interface_body(text: str, name: str) -> str:
    """The brace-balanced body of `export interface <name>`.

    Balanced rather than a lazy `\\n\\}` match, because an inline object type (`detail: {
    ... }`) would truncate a regex body at the first inner close and silently shrink the
    field set -- which in this gate reads as "TypeScript is missing fields", a false red.
    """
    match = re.search(r"export\s+interface\s+%s\s*\{" % re.escape(name), text)
    if match is None:
        raise AssertionError(
            "detector broken: no `export interface %s` in %s. Silence here would let a "
            "renamed interface pass as parity." % (name, TYPES_TS))
    depth, start = 0, match.end() - 1
    for offset in range(start, len(text)):
        if text[offset] == "{":
            depth += 1
        elif text[offset] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:offset]
    raise AssertionError("detector broken: `interface %s` is never closed" % name)


def interface_fields(text: str, name: str) -> frozenset[str]:
    """Declared field names, with comments stripped FIRST so prose cannot inflate the set."""
    body = _interface_body(text, name)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    body = re.sub(r"//[^\n]*", "", body)
    fields = frozenset(
        re.findall(r"^\s*(?:readonly\s+)?([A-Za-z_$][A-Za-z_0-9$]*)\??\s*:", body, re.MULTILINE))
    if not fields:
        raise AssertionError(
            "detector broken: `interface %s` parsed to zero fields" % name)
    return fields


# --------------------------------------------------------------------------------------
# The comparison itself, as a pure function so both states run through the SAME code.
# --------------------------------------------------------------------------------------
def parity_violations(engine: frozenset[str], declared: frozenset[str]) -> dict[str, list[str]]:
    """`{"missing_in_ts": [...], "unknown_to_engine": [...]}` -- empty dict when identical."""
    missing = sorted(engine - declared)
    unknown = sorted(declared - engine)
    out: dict[str, list[str]] = {}
    if missing:
        out["missing_in_ts"] = missing
    if unknown:
        out["unknown_to_engine"] = unknown
    return out


def _sidecar_text() -> str:
    return SIDECAR_PY.read_text(encoding="utf-8")


def _types_text() -> str:
    return TYPES_TS.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# THE GATE
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(("method", "handler", "interface"), GATED_PAIRS)
def test_the_param_surface_is_field_identical_in_both_directions(method, handler, interface):
    """Neither side may carry a `corpus.build` param the other does not know about."""
    engine = engine_param_keys(_sidecar_text(), handler)
    declared = interface_fields(_types_text(), interface)
    assert parity_violations(engine, declared) == {}, (
        "%s param surface has drifted from `interface %s`.\n"
        "  engine reads   : %s\n"
        "  TypeScript has : %s\n"
        "A param absent from the interface is UNREACHABLE from the app even though the "
        "engine serves it -- that is exactly how C-A shipped. Add the field to "
        "cockpit/src/ipc/types.ts, or stop reading it in %s."
        % (method, interface, sorted(engine), sorted(declared), handler))


def test_the_six_export_keys_are_on_both_sides_by_name():
    """The specific six C-A hid behind, named rather than left to the set algebra above."""
    engine = engine_param_keys(_sidecar_text(), "_corpus_build")
    declared = interface_fields(_types_text(), "BuildParams")
    for key in EXPECTED_EXPORT_KEYS:
        assert key in engine, "`%s` is no longer read by the engine handler" % key
        assert key in declared, "`%s` is absent from `interface BuildParams`" % key


# --------------------------------------------------------------------------------------
# DETECTOR CONTROLS -- each distinguishes the two hypotheses, per single-signal 3c.
# --------------------------------------------------------------------------------------
def test_the_two_engine_reads_are_disjoint_and_neither_is_empty():
    """The AST half and the runtime half must measure DIFFERENT things.

    If a collection-level mistake made one half mirror the other, parity would be reported
    for free. The literal walk must find the four roots and NONE of the export keys; the
    resolved constant must find the export keys and none of the roots.
    """
    node = _handler_node(_sidecar_text(), "_corpus_build")
    literals = _literal_param_keys(node)
    sources = _dynamic_key_sources(node)
    # Membership, not equality: a later unit may legitimately add a SECOND iterated table
    # (a roots table, say). What must never lapse is the export one, because the runtime
    # import of it is only a measurement for as long as the handler is proven to consume it.
    assert "_EXPORT_KEYS" in sources, (
        "the handler no longer iterates `_EXPORT_KEYS` to read params; importing that "
        "tuple would become an assumption rather than a measurement (found %s)" % sorted(sources))
    dynamic: frozenset[str] = frozenset()
    for constant in sorted(sources):
        dynamic |= _resolve(constant)
    assert set(EXPECTED_LITERAL_KEYS) <= literals, (
        "the AST half lost a known-present root; it is measuring nothing (found %s)"
        % sorted(literals))
    assert literals.isdisjoint(dynamic), (
        "the two engine reads OVERLAP on %s -- one half is mirroring the other, so their "
        "union is one signal wearing two hats" % sorted(literals & dynamic))


def test_export_keys_are_suffixed_wire_names_not_bare_provider_names():
    """`_EXPORT_PARAMS` holds `chatgpt`; the WIRE name is `chatgpt_export`. Do not confuse them."""
    assert _resolve("_EXPORT_KEYS") == frozenset(EXPECTED_EXPORT_KEYS)
    bare = {provider for provider, _second in sidecar._EXPORT_PARAMS}
    assert bare.isdisjoint(interface_fields(_types_text(), "BuildParams")), (
        "`interface BuildParams` declares a BARE provider name. The engine reads "
        "`<provider>_export` (sidecar.py:277), so a bare field is dead on the wire.")


def test_the_engine_scraper_fails_closed_on_a_renamed_handler():
    with pytest.raises(AssertionError, match="detector broken"):
        engine_param_keys(_sidecar_text(), "_corpus_build_renamed_by_a_refactor")


def test_the_engine_scraper_fails_closed_when_a_handler_reads_no_params():
    with pytest.raises(AssertionError, match="reads no params at all"):
        engine_param_keys("def _corpus_build(self, params):\n    return {}\n", "_corpus_build")


def test_the_engine_scraper_fails_closed_on_an_unresolvable_constant():
    with pytest.raises(AssertionError, match="resolved to no wire keys"):
        engine_param_keys(
            "def _corpus_build(self, params):\n"
            "    x = {k: _opt_str(params, k) for k in _NO_SUCH_TABLE}\n"
            "    return x\n", "_corpus_build")


def test_the_engine_scraper_reads_the_other_two_param_spellings():
    """`params.get("k")` and `params["k"]` count too, so a future handler cannot slip past."""
    source = ("def _h(self, params):\n"
              "    a = params.get(\"alpha\")\n"
              "    b = params[\"beta\"]\n"
              "    c = _req_str(params, \"gamma\")\n"
              "    return a, b, c\n")
    assert engine_param_keys(source, "_h") == frozenset({"alpha", "beta", "gamma"})


def test_the_engine_scraper_ignores_a_key_read_off_something_that_is_not_params():
    source = ("def _h(self, params):\n"
              "    a = _opt_str(params, \"real\")\n"
              "    b = _opt_str(other, \"decoy\")\n"
              "    c = other.get(\"decoy2\")\n"
              "    d = other[\"decoy3\"]\n"
              "    return a, b, c, d\n")
    assert engine_param_keys(source, "_h") == frozenset({"real"})


def test_the_interface_parser_fails_closed_on_a_missing_interface():
    with pytest.raises(AssertionError, match="detector broken"):
        interface_fields(_types_text(), "NoSuchInterfaceName")


def test_the_interface_parser_fails_closed_on_an_unterminated_interface():
    with pytest.raises(AssertionError, match="never closed"):
        interface_fields("export interface Half {\n  a?: string;\n", "Half")


def test_the_interface_parser_fails_closed_on_a_field_less_interface():
    with pytest.raises(AssertionError, match="zero fields"):
        interface_fields("export interface Empty {\n}\n", "Empty")


def test_the_interface_parser_does_not_count_a_field_named_only_in_a_comment():
    """The trap that would make this gate green over a stale contract."""
    text = ("export interface Sample {\n"
            "  /** a doc block mentioning phantom_block?: string; in prose */\n"
            "  // phantom_line?: string;\n"
            "  real_field?: string;\n"
            "}\n")
    assert interface_fields(text, "Sample") == frozenset({"real_field"})


def test_the_interface_parser_survives_a_nested_object_type():
    """A lazy `\\n\\}` body match truncates here and invents a missing field."""
    text = ("export interface Nested {\n"
            "  detail: {\n"
            "    inner?: string;\n"
            "  };\n"
            "  after?: string;\n"
            "}\n")
    assert {"detail", "after"} <= interface_fields(text, "Nested")


# --------------------------------------------------------------------------------------
# BOTH-STATES: the gate must FIRE on the state it was written to catch.
# --------------------------------------------------------------------------------------
def _strip_fields(text: str, fields: tuple[str, ...]) -> str:
    """Delete field DECLARATIONS from an interface body, leaving the prose alone.

    Deleting only the declaration is the honest reconstruction: pre-624c9da the doc comment
    did not mention exports either, but a mutation that also removed the prose would let a
    comment-blind parser pass for the wrong reason.
    """
    for field in fields:
        text = re.sub(r"^\s*%s\?: string;\n" % re.escape(field), "", text, flags=re.MULTILINE)
    return text


def test_the_gate_fires_on_the_pre_ca1_typescript():
    """STATE A: `BuildParams` before U-CA1 (624c9da). Must report exactly the six."""
    live = _types_text()
    broken = _strip_fields(live, EXPECTED_EXPORT_KEYS)
    # Mutation control: a no-op mutation would make the rest of this test vacuous.
    assert broken != live and len(broken) < len(live)
    engine = engine_param_keys(_sidecar_text(), "_corpus_build")
    assert parity_violations(engine, interface_fields(broken, "BuildParams")) == {
        "missing_in_ts": sorted(EXPECTED_EXPORT_KEYS)}
    # STATE B, through the same code path: the live contract is clean.
    assert parity_violations(engine, interface_fields(live, "BuildParams")) == {}


def test_the_gate_fires_when_typescript_declares_a_field_the_engine_never_reads():
    """The other direction -- a field the app can send and the engine will ignore."""
    live = _types_text()
    mutated = live.replace("  sessions_root?: string;",
                           "  sessions_root?: string;\n  phantom_root?: string;", 1)
    assert mutated != live
    engine = engine_param_keys(_sidecar_text(), "_corpus_build")
    assert parity_violations(engine, interface_fields(mutated, "BuildParams")) == {
        "unknown_to_engine": ["phantom_root"]}


def test_the_gate_fires_when_the_engine_gains_a_param_typescript_lacks():
    """The C-A shape itself, replayed: a seventh source the interface cannot express."""
    live = _sidecar_text()
    mutated = live.replace('sessions_root = _opt_str(params, "sessions_root")',
                           'sessions_root = _opt_str(params, "sessions_root")\n'
                           '        notion_export = _opt_str(params, "notion_export")', 1)
    assert mutated != live
    engine = engine_param_keys(mutated, "_corpus_build")
    assert parity_violations(engine, interface_fields(_types_text(), "BuildParams")) == {
        "missing_in_ts": ["notion_export"]}


def test_the_gate_fires_when_the_engine_stops_reading_the_export_table():
    """Deleting the dynamic read must be loud, not a quietly smaller engine surface."""
    live = _sidecar_text()
    mutated = live.replace("export = {key: _opt_str(params, key) for key in _EXPORT_KEYS}",
                           "export = {key: \"\" for key in _EXPORT_KEYS}", 1)
    assert mutated != live
    engine = engine_param_keys(mutated, "_corpus_build")
    assert parity_violations(engine, interface_fields(_types_text(), "BuildParams")) == {
        "unknown_to_engine": sorted(EXPECTED_EXPORT_KEYS)}
