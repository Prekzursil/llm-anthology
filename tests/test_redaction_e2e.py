"""AIRTIGHT-REDACTION end-to-end proof (Phase-4 privacy model, HARD §9).

Private medical/pharma conversation data lives in this corpus. A raw-content or PII
leak to any cloud/network path is a HARD-BAN, so the ONLY thing permitted to cross is a
STRUCTURAL METADATA/AGGREGATE projection built BY CONSTRUCTION from a STRICT ALLOWLIST
(conversation_id, provider, account, created_at/updated_at, turn_count, char_count,
thread_id, plus aggregate counts). Raw message bodies (Block.text / Turn content), any
PII, and ALL FREE TEXT (title / notes / tags / aliases) are FORBIDDEN to cross, ever.

WHY THE TITLE IS A CANARY HERE, not a control (owner decision 2026-07-25): an
adversarial leak-hunt proved a conversation `title` is DERIVED FROM RAW CONTENT — the
Codex adapter builds it from the first line of the first user message
(adapters/codex_rollout.py `title = _first_line(first_user, 80)`), and ChatGPT/Claude/
Gemini titles are provider auto-summaries of the content. The previous iteration of this
suite used a deliberately CLEAN title as its non-vacuity control, which is precisely why
it could not see the leak: every poison token lived in a field that never crossed. Here
the title is DIRTY and must not cross; non-vacuity is proven with a structural id.

This module drives the WHOLE research path end-to-end, on SYNTHETIC fixtures only
(never $CODEX_HOME), with canaries planted in the worst places a projection could
accidentally read them (a message body, a tool block's structured data payload, a local
rollout path, a thread preview, AND the free-text family):

  * RAWBODY_SECRET_DO_NOT_LEAK  — a raw message-body secret
  * 123-45-6789                 — PII: a fake SSN
  * patient.zero@example-clinic.test — PII: a fake email
  * Jane Q. Patient             — PII: a freeform name no regex reliably catches
  * Zynflaxen-250               — a fake medical/pharma term (a drug name)
  * Prekzursil                  — PII: a local-path component (defence-in-depth)

Two entry points into "the FULL research path (redact -> MetadataView -> research)":

  1. THROUGH THE SIDECAR — ``research.synthesize`` / ``research.extract_entities`` over a
     real sqlite index, with a ``MockBackend`` that RECORDS every prompt it receives.
     Both the recorded backend INPUT and the sidecar's ``research.*`` RESPONSE envelope
     are scanned; a *reflecting* responder deliberately echoes everything the backend
     saw back into the response, so a leak anywhere the backend touched would surface in
     the response too.
  2. THE redact->research COMPOSITION over live ``ir`` objects — ``corpus_metadata`` /
     ``metadata_payload`` (the whole crossing surface) fed straight into
     ``research.synthesize_over_metadata`` / ``extract_entities``.

BOTH-STATES DISCIPLINE (the crux): a single detector, :func:`_leaks_in`, is shared by
every assertion. It is SILENT on all real paths above; the control tests prove it FIRES
when a body-bearing OR a free-text-bearing view actually crosses. A detector silent in
both states proves nothing — if a control ever stops firing, none of the airtight
assertions may be cited.
"""
import json
from dataclasses import asdict, fields

import pytest

from aisr import corpus, ir, redact, research, sidecar

# --- KNOWN sensitive canary tokens (SYNTHETIC — never real corpus data) -----------
SECRET = "RAWBODY_SECRET_DO_NOT_LEAK"          # a raw message-body secret
SSN = "123-45-6789"                            # PII: fake SSN
EMAIL = "patient.zero@example-clinic.test"     # PII: fake email
NAME = "Jane Q. Patient"                       # PII: freeform name, regex-proof
PHARMA = "Zynflaxen-250"                       # a fake medical/pharma term (drug name)
USERNAME = "Prekzursil"                        # PII: a local-path component

# The full canary set the leak oracle hunts for. No element is a substring of another,
# so a per-token membership test is unambiguous.
SENSITIVE = (SECRET, SSN, EMAIL, NAME, PHARMA, USERNAME)

# Which canaries each both-states control can physically surface. The two leak classes
# are carried by DIFFERENT field families, so each control asserts exactly the set its
# own leak would expose — an over-broad expectation would make a control unsatisfiable
# and tempt someone to weaken it.
#   * body/path-borne: present in a message body, a tool payload, or the rollout path.
#   * free-text-borne: present in the content-derived title / notes / tags / aliases.
# NAME appears ONLY in free text (a body-bearing leak cannot expose it); SECRET and
# USERNAME appear only in body/path fields.
BODY_BORNE = (SECRET, SSN, EMAIL, PHARMA, USERNAME)
FREETEXT_BORNE = (SSN, NAME, PHARMA, EMAIL)

# A CONTENT-DERIVED title of exactly the shape codex_rollout.py produces. It carries
# three canaries and MUST NOT cross — this is the leak that shipped and was caught.
DIRTY_TITLE = f"Chemo plan {PHARMA} for {NAME} SSN {SSN}"

# The structural id that SHOULD cross — proves each prompt is non-vacuous (a real
# metadata prompt was built) rather than trivially empty.
CONV_ID = "conv-e2e-onc"
THREAD_ID = "th-e2e-onc"

# A local rollout path carrying two canaries (username + body secret) in the ONE
# off-allowlist column the conversations table actually persists.
_DIRTY_ROLLOUT = rf"C:\Users\{USERNAME}\sessions\{SECRET}.jsonl"

# The strict allowlist, stated INDEPENDENTLY of the module so drift in either direction
# (a new leaky field, a dropped one) turns this red. STRUCTURAL ONLY — no free text.
ALLOWLIST = {
    "conversation_id", "provider", "account", "created_at", "updated_at",
    "turn_count", "char_count", "thread_id",
}


# ------------------------------------------------------------------- the ONE oracle

def _leaks_in(haystack, tokens=SENSITIVE):
    """Return the SET of canary tokens present in ``haystack`` (a str, or any
    JSON-serializable object).

    This is the SINGLE detector shared by the airtight assertions AND the both-states
    controls, so "silent on the real path" and "fires on the leaky control" are the SAME
    instrument measured in two states.
    """
    blob = haystack if isinstance(haystack, str) else json.dumps(haystack, default=str)
    return {t for t in tokens if t in blob}


# --------------------------------------------------------------- connection tracking

_OPEN = []


def _track(conn):
    _OPEN.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _OPEN:
        _OPEN.pop().close()


# --------------------------------------------------------------- synthetic fixtures

def _dirty_conversation(cid=CONV_ID, thread_id=THREAD_ID):
    """A synthetic medical/pharma conversation poisoned on EVERY axis: the body and a
    tool block's structured ``data`` payload, the content-derived TITLE, the free-text
    meta (tags/aliases/notes), a body-snippet preview, and a local rollout path.

    Nothing here is clean except the structural identifiers — so any field family that
    crosses will be caught.
    """
    return ir.Conversation(
        id=cid,
        title=DIRTY_TITLE,                     # content-derived -> must NOT cross
        provider="claude",
        account="clinic-acct",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
        turns=[
            ir.Turn("human", [
                ir.Block("text", text=f"My SSN is {SSN}, email {EMAIL}. {SECRET}"),
            ]),
            ir.Turn("assistant", [
                ir.Block("text", text=f"Prescribed {PHARMA} once daily. {SECRET}"),
                # the secret also hidden in a tool block's structured payload
                ir.Block("tool_use", text="",
                         data={"raw": SECRET, "ssn": SSN, "rx": PHARMA}),
            ]),
        ],
        meta={
            "thread_id": thread_id,
            # free text: poisoned, because free text no longer crosses at all
            "tags": [f"mrn-{SSN}", PHARMA],
            "aliases": [NAME],
            "notes": f"clinician note — reach {EMAIL}",
            # NON-allowlisted: the projection must never read these
            "preview": f"user said {SECRET} {EMAIL}",
            "rollout_path": _DIRTY_ROLLOUT,
        },
    )


def _dirty_index():
    """An in-memory corpus index (the production sidecar entry point) holding one
    conversation whose FTS body carries the canaries, whose title column is
    content-derived, and whose row carries a local rollout_path with a username.
    Fully synthetic — never $CODEX_HOME."""
    conn = corpus.open_index(":memory:")
    conv = _dirty_conversation()
    corpus.add_conversation(conn, conv, thread_id=conv.meta["thread_id"],
                            rollout_path=_DIRTY_ROLLOUT)
    conn.commit()
    return conn


def _dirty_corpus():
    """A ``Corpus`` of live ``ir`` objects for the redact->research composition path:
    one dirty conversation plus a thread whose title/preview/rollout_path carry
    canaries in off-allowlist fields."""
    cor = corpus.Corpus(conversations=[_dirty_conversation()])
    cor.add_thread(corpus.ThreadMeta(
        id=THREAD_ID, title=DIRTY_TITLE, preview=f"leak {SECRET} {EMAIL}",
        rollout_path=_DIRTY_ROLLOUT))
    return cor


# ============================================================ SIDECAR full research path

def test_sidecar_synthesize_airtight_input_and_response():
    """cloud ``research.synthesize`` over a dirty index: the recorded backend INPUT and
    the sidecar RESPONSE both carry no canary, yet the structural id DID cross."""
    conn = _track(_dirty_index())
    # a REFLECTING responder: the response echoes the entire prompt the backend saw, so
    # any leak the backend touched would also show up in the response we scan.
    backend = research.MockBackend(responder=lambda p: "DIGEST:: " + p)
    out = sidecar.Sidecar(conn, research_backend=backend).dispatch(
        "research.synthesize", {})

    assert len(backend.prompts) == 1                 # exactly one metadata-only prompt
    assert _leaks_in(backend.prompts) == set()       # recorded INPUT is clean
    assert _leaks_in(out) == set()                   # sidecar RESPONSE is clean
    # the content-derived title is absent on both sides ...
    assert DIRTY_TITLE not in backend.prompts[0]
    assert DIRTY_TITLE not in out["summary"]
    # ... and non-vacuous: the structural id really did cross (input + reflected response)
    assert CONV_ID in backend.prompts[0]
    assert CONV_ID in out["summary"]
    assert out["tier"] == "cloud" and out["conversation_count"] == 1


def test_sidecar_extract_entities_airtight_input_and_response():
    """cloud ``research.extract_entities`` over a dirty index: input + the parsed entity
    RESPONSE list both carry no canary."""
    conn = _track(_dirty_index())
    # reflect the WHOLE prompt as the entity payload — the strongest response to scan.
    backend = research.MockBackend(responder=lambda p: p)
    out = sidecar.Sidecar(conn, research_backend=backend).dispatch(
        "research.extract_entities", {})

    assert len(backend.prompts) == 1
    assert _leaks_in(backend.prompts) == set()       # recorded INPUT is clean
    assert _leaks_in(out) == set()                   # entity RESPONSE list is clean
    assert DIRTY_TITLE not in backend.prompts[0]
    assert CONV_ID in backend.prompts[0]
    assert any(CONV_ID in e for e in out["entities"])       # response non-vacuous
    assert out["conversation_count"] == 1


def test_sidecar_metadata_views_are_allowlist_only():
    """``_metadata_views`` is the ONLY value the corpus-blind research plane is handed.
    Even though each row carries a content-derived title and a local rollout_path (and
    sqlite a rowid) and the FTS holds the body, every view is the strict structural
    MetadataView and carries no canary."""
    conn = _track(_dirty_index())
    views = sidecar.Sidecar(conn)._metadata_views()
    assert views and all(isinstance(v, redact.MetadataView) for v in views)
    assert _leaks_in([asdict(v) for v in views]) == set()
    blob = json.dumps([asdict(v) for v in views])
    for column in ("rollout_path", "rowid", "title", "preview"):
        assert column not in blob


# ==================================================== redact -> research COMPOSITION (IR)

def test_redact_to_research_composition_airtight():
    """The FULL redact->MetadataView->research path over live ``ir`` objects: the
    projection, the whole crossing surface (``metadata_payload``), the recorded backend
    input, and both research responses are ALL free of every canary."""
    cor = _dirty_corpus()
    views = redact.corpus_metadata(cor)

    # the MetadataView projection itself carries no canary ...
    assert _leaks_in([asdict(v) for v in views]) == set()
    # ... nor does the ENTIRE object the research plane may send (per redact.py: "if a
    # token is absent here it cannot reach the cloud through this module") ...
    assert _leaks_in(redact.metadata_payload(cor)) == set()

    # ... and driving research over those views leaks nothing to the backend or back.
    sum_backend = research.MockBackend(responder=lambda p: "S:: " + p)
    summary = research.synthesize_over_metadata(views, sum_backend)
    ent_backend = research.MockBackend(responder=lambda p: p)
    entities = research.extract_entities(views, ent_backend)

    assert _leaks_in(sum_backend.prompts) == set()
    assert _leaks_in(summary) == set()
    assert _leaks_in(ent_backend.prompts) == set()
    assert _leaks_in(entities) == set()
    # non-vacuous: the structural id crossed on both the summary and entity paths
    assert CONV_ID in sum_backend.prompts[0]
    assert CONV_ID in summary


def test_metadata_view_field_set_is_exactly_the_allowlist():
    """BY CONSTRUCTION the crossing surface cannot widen silently: the dataclass exposes
    EXACTLY the allowlist. A re-added ``title`` / ``notes`` / ``body`` / ``rollout_path``
    / ``preview`` field here turns this red immediately."""
    assert {f.name for f in fields(redact.MetadataView)} == ALLOWLIST


def test_body_is_counted_not_copied():
    """The body really carries content (so the airtight result is not vacuous because
    the corpus is empty), yet it is only COUNTED into ``char_count`` — never copied into
    any field."""
    conv = _dirty_conversation()
    view = redact.to_metadata_view(conv)
    body_len = sum(len(b.text) for t in conv.turns for b in t.blocks)
    assert body_len > 0                               # the body genuinely has content
    assert view.char_count == body_len                # counted ...
    assert _leaks_in(asdict(view)) == set()           # ... never copied


# ============================================================ BOTH-STATES controls (crux)

def _leaky_synthesize(conv, backend):
    """A DELIBERATELY BROKEN stand-in for ``research.synthesize_over_metadata``: it feeds
    a BODY-BEARING view (raw block text + the local rollout_path + a body preview, with
    NO allowlist projection) straight into the prompt.

    It exists ONLY to drive the both-states control below — the production research plane
    never does this. If this ever stops leaking, the leak oracle is no longer a real
    detector and the airtight assertions above become unciteable.
    """
    body = "\n".join(b.text for t in conv.turns for b in t.blocks)
    view = {
        "conversation_id": conv.id,
        "body": body,                                   # LEAK: raw message body
        "rollout_path": conv.meta.get("rollout_path"),  # LEAK: local path (username)
        "preview": conv.meta.get("preview"),            # LEAK: body snippet
    }
    prompt = "\n".join("%s: %s" % (k, v) for k, v in view.items())
    return backend.synthesize(prompt)


def _leaky_freetext_synthesize(conv, backend):
    """The control for the leak class that ACTUALLY SHIPPED: a prompt that renders the
    free-text family (the content-derived title + notes/tags/aliases) exactly as the
    pre-fix ``research._render_view`` did. The oracle must fire on this."""
    prompt = "\n".join((
        "  conversation_id: %s" % conv.id,
        "  title: %s" % conv.title,
        "  notes: %s" % conv.meta.get("notes", ""),
        "  tags: %s" % ", ".join(conv.meta.get("tags", ())),
        "  aliases: %s" % ", ".join(conv.meta.get("aliases", ())),
    ))
    return backend.synthesize(prompt)


def test_both_states_control_leaky_body_bearing_view_is_detected():
    """Both-states discipline: the SAME ``_leaks_in`` oracle that is SILENT on every real
    path above MUST FIRE when a body-bearing view actually crosses — in the recorded
    backend input AND in the (echoing) response. This proves the airtight results are a
    genuine detector reading zero, not a detector that can never read anything."""
    conv = _dirty_conversation()
    backend = research.MockBackend(responder=lambda p: "ECHO " + p)
    summary = _leaky_synthesize(conv, backend)

    # every BODY/PATH-borne canary is surfaced by the leaky path, on both sides. NAME
    # lives only in the free-text family, so a body-bearing leak cannot expose it —
    # that class is covered by the free-text control below.
    assert _leaks_in(backend.prompts) == set(BODY_BORNE)
    assert _leaks_in(summary) == set(BODY_BORNE)


def test_both_states_control_leaky_free_text_view_is_detected():
    """The control for the CONFIRMED regression: rendering the free-text family (as the
    pre-fix code did) must make the oracle fire on the title-borne canaries. Without
    this control, "no free text crossed" could be a detector that simply cannot see
    free text — which is exactly how the original leak survived a green suite."""
    conv = _dirty_conversation()
    backend = research.MockBackend(responder=lambda p: "ECHO " + p)
    summary = _leaky_freetext_synthesize(conv, backend)

    for token in FREETEXT_BORNE:
        assert token in backend.prompts[0], f"oracle blind to {token!r} in free text"
        assert token in summary
