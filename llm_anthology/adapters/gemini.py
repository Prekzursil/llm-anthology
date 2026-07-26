"""Google Takeout "Gemini Apps" activity -> IR.

Takeout is a FLAT activity log: each record is one EXCHANGE (prompt + response),
with NO conversation id. Grouping therefore has to come from outside — either the
live web-app harvest (true grouping, preferred) or a heuristic — and is passed in
as `groups`. The adapter itself only turns records into IR turns.

Schema (probed from the real transcript.json, 1060 records):
  idx, timestamp, timestamp_iso, tz, verb, gem, prompt, response_md,
  attachments[], media[], title, detail, zero_width[], source_order
Verbs on disk: Prompted 967 · Used 60 · Created Gemini Canvas 26 ·
               Gave feedback 6 · Selected 1
Only "Prompted" is a real prompt/response exchange; the rest are feature EVENTS
and are rendered as a single event turn rather than a forged model reply.
"""
from llm_anthology import ir

_PROMPT_VERB = "Prompted"


def parse_all(records, groups=None):
    """[Conversation] — one per group, or a single conversation if ungrouped."""
    if not groups:
        return [parse_conversation(records, list(range(len(records))),
                                   title="Gemini activity (ungrouped)", conv_id="all")]
    return [parse_conversation(records, g.get("turn_idxs") or [],
                               title=g.get("title") or "(untitled)",
                               conv_id=g.get("id") or "")
            for g in groups]


def parse_conversation(records, turn_idxs, title="", conv_id="", account=""):
    turns, gems = [], []
    first_ts = last_ts = ""
    for i in turn_idxs:
        if i < 0 or i >= len(records):
            continue
        r = records[i] or {}
        gem = r.get("gem")
        if gem and gem not in gems:
            gems.append(gem)
        ts = r.get("timestamp_iso") or r.get("timestamp") or ""
        first_ts = first_ts or ts
        last_ts = ts or last_ts
        turns.extend(_turns_from_record(r))
    return ir.Conversation(
        id=conv_id, title=title or "(untitled)", provider="gemini", turns=turns,
        created_at=first_ts, updated_at=last_ts, account=account,
        meta={"gems": gems} if gems else {},
    )


def _s(x):
    return x if isinstance(x, str) else ""


def _attachment_blocks(r):
    blocks = []
    for a in (r.get("attachments") or []):
        if isinstance(a, dict):
            blocks.append(ir.Block("attachment", text=_s(a.get("name")) or "attachment",
                                   data={"file_name": _s(a.get("name")),
                                         "path": _s(a.get("on_disk")),
                                         "resolved": bool(a.get("resolved"))}))
        else:
            blocks.append(ir.Block("attachment", text=_s(a) or "attachment",
                                   data={"file_name": _s(a)}))
    for m in (r.get("media") or []):
        path = _s(m.get("on_disk") or m.get("name")) if isinstance(m, dict) else _s(m)
        blocks.append(ir.Block("media", text=path, data={"path": path}))
    return blocks


def _turns_from_record(r):
    verb = _s(r.get("verb")) or _PROMPT_VERB
    ts = _s(r.get("timestamp_iso")) or _s(r.get("timestamp"))

    if verb != _PROMPT_VERB:
        # a feature event (Used / Created Gemini Canvas / Gave feedback / Selected).
        # Render it as an explicit event, never as a fabricated model reply.
        label = _s(r.get("title")) or _s(r.get("detail")) or verb
        return [ir.Turn("assistant",
                        [ir.Block("event", text=label, data={"name": verb, "detail": _s(r.get("detail"))})]
                        + _attachment_blocks(r),
                        timestamp=ts)]

    prompt = _s(r.get("prompt")) or _s(r.get("title")) or _s(r.get("detail"))
    human = ir.Turn("human", _attachment_blocks(r) + [ir.Block("text", text=prompt)], timestamp=ts)
    model = ir.Turn("assistant", [ir.Block("text", text=_s(r.get("response_md")))], timestamp=ts)
    return [human, model]
