"""Provider-agnostic conversation Intermediate Representation.

Every adapter (Claude / ChatGPT / Gemini) parses its native export into this one
shape; the HTML and Markdown renderers consume only this. Versioned so a future
schema change is explicit. Unknown provider payloads survive via Block.data['x_raw'].
"""
from dataclasses import dataclass, field
from typing import Optional

IR_VERSION = 1


@dataclass
class Block:
    """One typed content block within a turn."""
    type: str                                   # text|thinking|tool_use|tool_result|attachment|file|image|unknown
    text: str = ""                              # display text (body for text/thinking; label otherwise)
    data: dict = field(default_factory=dict)    # type-specific payload (tool name/input/output, file_name, src, ...)
    citations: list = field(default_factory=list)


@dataclass
class Turn:
    """One message turn on the active conversation path."""
    role: str                                   # human | assistant
    blocks: list = field(default_factory=list)  # list[Block]
    uuid: str = ""
    timestamp: str = ""
    branch: Optional[dict] = None               # {"index": int, "total": int} when the parent had siblings


@dataclass
class Conversation:
    id: str
    title: str
    provider: str                               # claude | chatgpt | gemini
    turns: list = field(default_factory=list)   # list[Turn], the active (latest) chain
    created_at: str = ""
    updated_at: str = ""
    account: str = ""
    meta: dict = field(default_factory=dict)    # provider extras, audit (hidden-char hits), etc.
    ir_version: int = IR_VERSION
