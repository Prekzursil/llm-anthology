"""ChatGPT native export (conversations.json) -> IR.

UNLIKE the other two providers, ChatGPT stores a MESSAGE TREE, not a list:

  conversation{ title, create_time, current_node, mapping{ node_id: {id, message,
                parent, children} } }
  message{ author{role}, create_time, content{content_type, parts|text|thoughts},
           end_turn, metadata{is_visually_hidden_from_conversation, ...} }

The rendered thread is current_node -> parent -> ... -> root, REVERSED. Every other
child of a branching node is an abandoned regeneration and must not be rendered, or
the transcript silently mixes discarded replies into the conversation.

Content types handled: text, code, multimodal_text, thoughts / reasoning_recap
(collapsed thinking), execution_output. Multimodal PARTS handled: image_asset_pointer,
plus the three voice-mode types — audio_transcription (the spoken words, rendered as
readable text), audio_asset_pointer, and real_time_user_audio_video_asset_pointer
(whose usable pointer is nested). Anything else is passed through as an `unknown`
block carrying its raw payload rather than dropped.

VALIDATED against a real Data Export (2026-07-19): 1613 conversations / 56373 turns
parsed with 0 errors and 0 empty conversations. That export carried only text,
thoughts, multimodal_text and reasoning_recap at the message level — no canvas/canmore,
tool calls, or execution_output — so those paths remain synthetic-tested only.
"""
from llm_anthology import ir

_HIDDEN_ROLES = {"system"}
_THINKING_TYPES = {"thoughts", "reasoning_recap"}


def parse_export(data):
    convs = data if isinstance(data, list) else [data]
    return [parse_conversation(c) for c in convs if isinstance(c, dict)]


def parse_conversation(conv):
    mapping = conv.get("mapping") or {}
    turns = []
    for msg in _active_path(mapping, conv.get("current_node")):
        role = _role(msg)
        if role is None:
            continue
        blocks = _blocks_from_message(msg)
        if not blocks:
            continue
        # consecutive assistant nodes are ONE visual turn until end_turn
        if turns and turns[-1].role == role == "assistant" and not turns[-1].branch:
            turns[-1].blocks.extend(blocks)
        else:
            turns.append(ir.Turn(role=role, blocks=blocks, uuid=_s(msg.get("id")),
                                 timestamp=_ts(msg)))
    return ir.Conversation(
        id=_s(conv.get("id")) or _s(conv.get("conversation_id")),
        title=_s(conv.get("title")) or "(untitled)",
        provider="chatgpt", turns=turns,
        created_at=_ts_top(conv.get("create_time")),
        updated_at=_ts_top(conv.get("update_time")),
        meta={},
    )


def _s(x):
    return x if isinstance(x, str) else ""


def _ts(msg):
    return _ts_top((msg or {}).get("create_time"))


def _ts_top(v):
    if isinstance(v, (int, float)):
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return _s(v)


def _active_path(mapping, current_node):
    """current_node -> root, reversed. Cycle-safe.

    current_node is authoritative when usable, but real exports carry conversations
    whose current_node is null, absent, or points at a since-deleted node — the naive
    walk then returns [] and the whole conversation renders blank. Fall back to the
    newest leaf so a conversation is never silently emptied.
    """
    chain = _walk_up(mapping, current_node)
    if not chain:
        chain = _walk_up(mapping, _fallback_tip(mapping))
    return chain


def _walk_up(mapping, nid):
    chain, seen = [], set()
    while nid and nid in mapping and nid not in seen:
        seen.add(nid)
        node = mapping.get(nid) or {}
        msg = node.get("message")
        if isinstance(msg, dict):
            chain.append(msg)
        nid = node.get("parent")
    chain.reverse()
    return chain


def _fallback_tip(mapping):
    """The most likely active tip when current_node is unusable: prefer a leaf
    (no children) and, among candidates, the newest by create_time."""
    best = None                     # (is_leaf, create_time, nid)
    for nid, node in mapping.items():
        if not isinstance(node, dict) or not isinstance(node.get("message"), dict):
            continue
        ts = node["message"].get("create_time")
        cand = (not (node.get("children") or []),
                ts if isinstance(ts, (int, float)) else -1.0, nid)
        if best is None or cand[:2] > best[:2]:
            best = cand
    return best[2] if best else None


def _role(msg):
    """human/assistant, or None when the message must not be rendered."""
    role = _s((msg.get("author") or {}).get("role")).lower()
    if role in _HIDDEN_ROLES:
        return None
    if (msg.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
        return None
    if role == "user":
        return "human"
    return "assistant"          # assistant + tool both render on the model side


def _blocks_from_message(msg):
    content = msg.get("content") or {}
    ctype = _s(content.get("content_type"))
    blocks = []

    if ctype in _THINKING_TYPES:
        for th in (content.get("thoughts") or []):
            if isinstance(th, dict):
                body = _s(th.get("content")) or _s(th.get("summary"))
                if body:
                    blocks.append(ir.Block("thinking", text=body,
                                           data={"summary": _s(th.get("summary"))}))
        body = _s(content.get("content"))
        if body:
            blocks.append(ir.Block("thinking", text=body))
        return blocks

    if ctype == "code":
        text = _s(content.get("text"))
        return [ir.Block("code", text=text, data={"language": _s(content.get("language"))})] if text else []

    if ctype == "execution_output":
        text = _s(content.get("text"))
        return [ir.Block("tool_result", text="", data={"name": "execution_output",
                                                       "content": text})] if text else []

    if ctype in ("text", "multimodal_text", ""):
        for part in (content.get("parts") or []):
            if isinstance(part, str):
                if part.strip():
                    blocks.append(ir.Block("text", text=part))
            elif isinstance(part, dict):
                b = _part_block(part)
                if b is not None:
                    blocks.append(b)
        return blocks

    # never silently drop an unrecognised content type
    return [ir.Block("unknown", data={"orig_type": ctype, "x_raw": content})]


def _asset_block(ptr):
    """A file-service asset pointer -> media block."""
    return ir.Block("media", text=ptr,
                    data={"path": ptr.rsplit("/", 1)[-1], "pointer": ptr})


def _part_block(part):
    """One dict part of a multimodal message -> a block, or None to skip.

    Voice-mode conversations carry three audio part types. `audio_transcription`
    holds the SPOKEN WORDS — it is prose the reader must see, so it renders as text
    rather than a raw payload dump (694 of them sit in the real export).
    """
    pt = _s(part.get("content_type"))
    if pt in ("image_asset_pointer", "audio_asset_pointer"):
        return _asset_block(_s(part.get("asset_pointer")))
    if pt == "audio_transcription":
        text = _s(part.get("text"))
        return ir.Block("text", text=text) if text.strip() else None
    if pt == "real_time_user_audio_video_asset_pointer":
        # the usable pointer is NESTED one level down
        inner = part.get("audio_asset_pointer")
        ptr = _s(inner.get("asset_pointer")) if isinstance(inner, dict) else ""
        if ptr:
            return _asset_block(ptr)
    # never silently drop an unrecognised part
    return ir.Block("unknown", data={"orig_type": pt, "x_raw": part})
