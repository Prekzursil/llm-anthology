"""Hidden-character forensic audit across EVERY text surface of a conversation.

Scanning only Block.text under-reported by roughly 5x: an injected payload is most
likely to sit in uploaded-document text (attachment.extracted_content), tool input
or output, a citation title, or the conversation TITLE — none of which were checked.
"""
import json

from llm_anthology import sanitize

_STR_KEYS = ("extracted_content", "file_name", "name", "integration_name")
_BLOB_KEYS = ("input", "content")


def audit_texts(conv):
    """Yield every string a hidden codepoint could hide in."""
    yield conv.title or ""
    yield conv.account or ""
    for turn in conv.turns:
        for b in turn.blocks:
            yield b.text or ""
            d = b.data or {}
            for k in _STR_KEYS:
                v = d.get(k)
                if isinstance(v, str):
                    yield v
            for k in _BLOB_KEYS:
                v = d.get(k)
                if isinstance(v, str):
                    yield v
                elif v is not None:
                    try:
                        yield json.dumps(v, ensure_ascii=False)
                    except (TypeError, ValueError):
                        pass
            for c in (b.citations or []):
                if isinstance(c, dict):
                    for k in ("title", "url"):
                        if isinstance(c.get(k), str):
                            yield c[k]


def hidden_char_hits(conv):
    """Every flagged invisible codepoint found anywhere in the conversation."""
    hits = []
    for s in audit_texts(conv):
        hits.extend(cp for _, cp in sanitize.scan_invisibles(s))
    return hits
