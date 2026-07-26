"""Claude native account-export (claude.ai Data Export) -> IR.

Schema (probed from the real exports on disk):
  conversation: uuid, name, summary, created_at, updated_at, account, chat_messages[]
  message:      uuid, parent_message_uuid, sender(human|assistant), created_at,
                content[], text (flattened), attachments[], files[]
  content item: type in {text, thinking, tool_use, tool_result, token_budget, ...}
    text        -> text, citations[]
    thinking    -> thinking, summaries[], (thinking_)hidden
    tool_use    -> name, input, display_content, integration_name, icon_name, id
    tool_result -> name, content, display_content, is_error, integration_name, icon_name, tool_use_id
    token_budget-> hidden (dropped)
  attachment:   file_name, file_type, file_size, extracted_content (embedded doc text)
  file:         file_name, file_uuid (NO bytes in export -> rendered as a name chip)

Branches: messages form a tree via parent_message_uuid (91 real branch points on
disk). We render the ACTIVE (latest) path — at each node choose the child with the
max created_at — and annotate a turn with {index,total} when its parent had siblings.
"""
from llm_anthology import ir

_HIDDEN_CONTENT_TYPES = {"token_budget"}


def _s(x):
    """Coerce a display field to str. Claude's display_content is sometimes a
    structured list, not a string; the real content still lives in data{}."""
    return x if isinstance(x, str) else ""


def parse_export(data):
    """A whole export file (array of conversations, or a single dict) -> [Conversation]."""
    convs = data if isinstance(data, list) else [data]
    return [parse_conversation(c) for c in convs]


def is_design_chat(data):
    """A Claude 'design chat' (design_chats/*.json) rather than a conversations.json
    entry: messages[] with `role` + a content DICT, instead of chat_messages[] with
    `sender` + a content LIST."""
    return (isinstance(data, dict) and isinstance(data.get("messages"), list)
            and "chat_messages" not in data)


def parse_design_chat(data):
    """design_chats/*.json -> IR. A distinct shape, so parse_conversation cannot read
    it: feeding one through the normal path yields a silently EMPTY conversation."""
    turns = []
    for m in (data.get("messages") or []):
        if not isinstance(m, dict):
            continue
        role = "human" if _s(m.get("role")).lower() in ("user", "human") else "assistant"
        blocks = _design_blocks(m.get("content"))
        if blocks:
            turns.append(ir.Turn(role=role, blocks=blocks, uuid=_s(m.get("uuid")),
                                 timestamp=_s(m.get("created_at"))))
    return ir.Conversation(
        id=_s(data.get("uuid")),
        title=_s(data.get("title")) or "(untitled design chat)",
        provider="claude",
        turns=turns,
        created_at=_s(data.get("created_at")),
        updated_at=_s(data.get("updated_at")),
        meta={"kind": "design_chat"},
    )


def _design_blocks(content):
    """Text lives in content.contentBlocks[] when present, else the flat
    content.content string (which duplicates the blocks)."""
    if isinstance(content, str):
        return [ir.Block("text", text=content)] if content.strip() else []
    if not isinstance(content, dict):
        return []
    blocks = []
    for b in (content.get("contentBlocks") or []):
        if not isinstance(b, dict):
            continue
        btype = _s(b.get("type"))
        if btype == "text" and _s(b.get("text")).strip():
            blocks.append(ir.Block("text", text=_s(b.get("text"))))
        elif btype == "tool_call" and isinstance(b.get("toolCall"), dict):
            blocks.extend(_tool_call_blocks(b["toolCall"]))
        else:                                   # never silently drop an unknown block
            blocks.append(ir.Block("unknown", data={"orig_type": btype, "x_raw": b}))
    if blocks:
        return blocks
    flat = _s(content.get("content"))
    return [ir.Block("text", text=flat)] if flat.strip() else []


def _tool_call_blocks(tc):
    """A design-chat tool_call {id,name,input,output} -> tool_use (+ tool_result when it
    carries an output), so it renders like the regular chat_messages tool path rather
    than as a raw payload. 140 such blocks sit in the live corpus (2 design chats)."""
    out = [ir.Block("tool_use", text="",
                    data={"name": _s(tc.get("name")), "input": tc.get("input"),
                          "id": _s(tc.get("id"))})]
    output = tc.get("output")
    if _s(output).strip():
        out.append(ir.Block("tool_result", text="",
                            data={"name": _s(tc.get("name")), "content": output,
                                  "tool_use_id": _s(tc.get("id"))}))
    return out


def parse_conversation(conv):
    messages = conv.get("chat_messages") or []
    turns = []
    for msg, branch in _active_path(messages):
        role = "human" if msg.get("sender") == "human" else "assistant"
        turns.append(ir.Turn(
            role=role,
            blocks=_blocks_from_message(msg),
            uuid=msg.get("uuid") or "",
            timestamp=msg.get("created_at") or "",
            branch=branch,
        ))
    return ir.Conversation(
        id=conv.get("uuid") or "",
        title=conv.get("name") or "(untitled)",
        provider="claude",
        turns=turns,
        created_at=conv.get("created_at") or "",
        updated_at=conv.get("updated_at") or "",
        account=_account_str(conv.get("account")),
        meta={"summary": conv.get("summary") or ""},
    )


def _account_str(acc):
    if isinstance(acc, dict):
        return acc.get("uuid") or acc.get("email_address") or acc.get("email") or ""
    return acc or ""


def _ts(m):
    return m.get("created_at") or ""


def _subtree_max_ts(messages, children):
    """Newest created_at anywhere inside each message's subtree.

    Iterative (a long chain would blow Python's recursion limit) and cycle-safe.
    Needed because the LOCALLY newest child is often an abandoned regeneration,
    while the live thread continues under an older sibling.
    """
    memo = {}
    for start in messages:
        if start.get("uuid") in memo:
            continue
        stack, onpath = [(start, False)], set()
        while stack:
            m, expanded = stack.pop()
            u = m.get("uuid")
            if expanded:
                best = _ts(m)
                for k in children.get(u, []):
                    v = memo.get(k.get("uuid"))
                    if v is not None and v > best:
                        best = v
                memo[u] = best
                onpath.discard(u)
                continue
            if u in memo or u in onpath:
                continue
            onpath.add(u)
            stack.append((m, True))
            for k in children.get(u, []):
                if k.get("uuid") not in memo:
                    stack.append((k, False))
    return memo


def _active_path(messages):
    """Walk root -> live leaf. Returns [(message, branch_or_None), ...]."""
    by_id = {m.get("uuid"): m for m in messages}
    children = {}
    for m in messages:
        children.setdefault(m.get("parent_message_uuid"), []).append(m)

    roots = [m for m in messages
             if m.get("parent_message_uuid") is None or m.get("parent_message_uuid") not in by_id]
    submax = _subtree_max_ts(messages, children)
    path, seen = [], set()
    # Walk EVERY root, chronologically. An orphaned parent link starts a second
    # root whose subtree is still real conversation content — dropping it loses
    # messages outright (measured: 42 messages / 18 conversations on real data).
    for root in sorted(roots, key=_ts):
        cur = root
        while cur is not None and cur.get("uuid") not in seen:
            seen.add(cur.get("uuid"))
            sibs = children.get(cur.get("parent_message_uuid"), [])
            branch = None
            if len(sibs) > 1:
                ordered = sorted(sibs, key=_ts)
                branch = {"index": ordered.index(cur) + 1, "total": len(sibs)}
            path.append((cur, branch))
            kids = children.get(cur.get("uuid"), [])
            # descend toward the subtree holding the globally newest message,
            # NOT merely the newest immediate child (that abandons live threads)
            cur = max(kids, key=lambda k: (submax.get(k.get("uuid"), _ts(k)), _ts(k))) if kids else None

    # Sweep ONLY nodes reachable from no root at all (e.g. an orphaned cycle).
    # Branch siblings are also "unvisited" but are intentionally off the active
    # path — sweeping those in would splice abandoned regenerations into the text.
    reachable, stack = set(), list(roots)
    while stack:
        m = stack.pop()
        u = m.get("uuid")
        if u in reachable:            # pragma: no cover - defensive: each message has a
            continue                  # single parent, so BFS cannot revisit a node
        reachable.add(u)
        stack.extend(children.get(u, []))
    orphans = [m for m in messages
               if m.get("uuid") not in reachable and m.get("uuid") not in seen]
    for m in sorted(orphans, key=_ts):
        if m.get("uuid") in seen:     # pragma: no cover - orphans are pre-filtered `not in seen`
            continue
        seen.add(m.get("uuid"))
        path.append((m, None))
    return path


def _blocks_from_message(m):
    blocks = []
    # uploaded files/attachments render above the message text in claude.ai
    for a in (m.get("attachments") or []):
        blocks.append(ir.Block(
            "attachment", text=a.get("file_name") or "attachment",
            data={"file_name": a.get("file_name"), "file_type": a.get("file_type"),
                  "file_size": a.get("file_size"), "extracted_content": a.get("extracted_content")}))
    for f in (m.get("files") or []):
        blocks.append(ir.Block(
            "file", text=f.get("file_name") or "file",
            data={"file_name": f.get("file_name"), "file_uuid": f.get("file_uuid")}))

    for item in (m.get("content") or []):
        t = item.get("type")
        if t in _HIDDEN_CONTENT_TYPES:
            continue
        if t == "text":
            blocks.append(ir.Block("text", text=_s(item.get("text")),
                                   citations=item.get("citations") or []))
        elif t == "thinking":
            blocks.append(ir.Block("thinking", text=_s(item.get("thinking")),
                                   data={"hidden": bool(item.get("thinking_hidden") or item.get("hidden")),
                                         "summaries": item.get("summaries") or []}))
        elif t == "tool_use":
            blocks.append(ir.Block("tool_use", text=_s(item.get("display_content")),
                                   data={"name": item.get("name") or "", "input": item.get("input"),
                                         "integration_name": item.get("integration_name"),
                                         "icon_name": item.get("icon_name"), "id": item.get("id")}))
        elif t == "tool_result":
            blocks.append(ir.Block("tool_result", text=_s(item.get("display_content")),
                                   data={"name": item.get("name") or "", "content": item.get("content"),
                                         "is_error": bool(item.get("is_error")),
                                         "integration_name": item.get("integration_name"),
                                         "icon_name": item.get("icon_name"),
                                         "tool_use_id": item.get("tool_use_id")}))
        else:
            # never silently drop an unknown block — pass it through for later handling
            blocks.append(ir.Block("unknown", text="", data={"orig_type": t, "x_raw": item}))
    return blocks
