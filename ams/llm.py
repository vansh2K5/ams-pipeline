"""Provider-agnostic LLM layer.

The pipeline only ever asks for small, structured JSON answers over the normalized
schema (never free-form code over a whole repo). Returns None when no provider is
configured, so every caller has a deterministic fallback.
"""

from __future__ import annotations

import json
import os
import re
from typing import Protocol


class LLM(Protocol):
    name: str

    def complete_json(self, system: str, user: str) -> dict | None: ...

    def complete_text(self, system: str, user: str) -> str | None: ...


class Claude:
    def __init__(self, model: str | None = None) -> None:
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model or os.getenv("AMS_MODEL", "claude-sonnet-5-5")
        self.name = f"anthropic:{self.model}"

    def complete_text(self, system: str, user: str) -> str | None:
        msg = self.client.messages.create(
            model=self.model, max_tokens=4096, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in msg.content if block.type == "text")

    def complete_json(self, system: str, user: str) -> dict | None:
        raw = self.complete_text(system + "\nReply with a single JSON object and nothing else.", user)
        if not raw:
            return None
        match = re.search(r"\{.*\}", raw, re.S)
        try:
            return json.loads(match.group(0)) if match else None
        except json.JSONDecodeError:
            return None


def get_llm(disabled: bool = False) -> LLM | None:
    if disabled or not os.getenv("ANTHROPIC_API_KEY"):
        return None
    return Claude()
