"""Provider-agnostic LLM layer: Gemini, Groq or Claude, over plain HTTPS (httpx).

The pipeline only ever asks for small, structured JSON answers over the normalized
schema (never free-form code over a whole repo). Every call returns None on failure
(bad key, rate limit, network) and records why in `last_error`, so callers fall back
to the deterministic path instead of crashing.

Provider selection: AMS_LLM_PROVIDER=gemini|groq|anthropic, otherwise the first key
found among GEMINI_API_KEY (or GOOGLE_API_KEY), GROQ_API_KEY, ANTHROPIC_API_KEY.
AMS_MODEL overrides the provider's default model.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Protocol

import httpx

JSON_ONLY = "\nReply with a single JSON object and nothing else."
TRANSIENT = {429, 500, 502, 503, 504}


class LLM(Protocol):
    name: str
    last_error: str

    def complete_json(self, system: str, user: str) -> dict | None: ...

    def complete_text(self, system: str, user: str) -> str | None: ...


def parse_json(raw: str | None) -> dict | None:
    """First JSON object in a model reply (tolerates code fences and chatter)."""
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class _HTTPProvider:
    provider = ""
    default_model = ""

    def __init__(self, api_key: str, model: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.api_key = api_key
        self.model = model or os.getenv("AMS_MODEL") or self.default_model
        self.name = f"{self.provider}:{self.model}"
        self.last_error = ""
        self.retries, self.backoff, self._sleep = 2, 1.5, time.sleep
        self._http = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), transport=transport)

    def _call(self, system: str, user: str, want_json: bool) -> str | None:
        raise NotImplementedError

    def _post(self, url: str, headers: dict, body: dict) -> dict | None:
        """POST with backoff on transient failures (rate limits, overload, network)."""
        for attempt in range(self.retries + 1):
            last = attempt == self.retries
            try:
                response = self._http.post(url, headers=headers, json=body)
            except httpx.HTTPError as e:
                self.last_error = f"{type(e).__name__}: {e}"
                if last:
                    return None
                self._sleep(self.backoff * 2**attempt)
                continue
            if response.status_code < 400:
                self.last_error = ""  # an earlier attempt may have failed; this one did not
                try:
                    return response.json()
                except ValueError as e:
                    self.last_error = f"invalid JSON from provider: {e}"
                    return None
            # keep the provider's message but never echo request headers (they carry the key)
            self.last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code not in TRANSIENT or last:
                return None
            wait = self.backoff * 2**attempt
            retry_after = response.headers.get("retry-after", "")
            if retry_after.replace(".", "", 1).isdigit():
                wait = min(float(retry_after), 10.0)
            self._sleep(wait)
        return None

    def complete_text(self, system: str, user: str) -> str | None:
        self.last_error = ""
        return self._call(system, user, want_json=False)

    def complete_json(self, system: str, user: str) -> dict | None:
        self.last_error = ""
        data = parse_json(self._call(system + JSON_ONLY, user, want_json=True))
        if data is None and not self.last_error:
            self.last_error = "reply was not a JSON object"
        return data


class Gemini(_HTTPProvider):
    provider = "gemini"
    default_model = "gemini-flash-latest"  # alias Google keeps pointed at the current Flash model

    def _call(self, system: str, user: str, want_json: bool) -> str | None:
        body: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0},
        }
        if want_json:
            body["generationConfig"]["responseMimeType"] = "application/json"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        data = self._post(url, {"x-goog-api-key": self.api_key}, body)
        try:
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]) if data else None
        except (KeyError, IndexError, TypeError):
            self.last_error = f"unexpected Gemini response: {str(data)[:200]}"
            return None


class Groq(_HTTPProvider):
    provider = "groq"
    default_model = "openai/gpt-oss-120b"

    def _call(self, system: str, user: str, want_json: bool) -> str | None:
        body: dict = {
            "model": self.model, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if want_json:
            body["response_format"] = {"type": "json_object"}
        data = self._post("https://api.groq.com/openai/v1/chat/completions",
                          {"Authorization": f"Bearer {self.api_key}"}, body)
        try:
            return data["choices"][0]["message"]["content"] if data else None
        except (KeyError, IndexError, TypeError):
            self.last_error = f"unexpected Groq response: {str(data)[:200]}"
            return None


class Claude(_HTTPProvider):
    provider = "anthropic"
    default_model = "claude-sonnet-5-5"

    def _call(self, system: str, user: str, want_json: bool) -> str | None:
        body = {"model": self.model, "max_tokens": 4096, "temperature": 0, "system": system,
                "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        data = self._post("https://api.anthropic.com/v1/messages", headers, body)
        try:
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text") if data else None
        except (KeyError, TypeError):
            self.last_error = f"unexpected Anthropic response: {str(data)[:200]}"
            return None


PROVIDERS: dict[str, tuple[type[_HTTPProvider], tuple[str, ...]]] = {
    "gemini": (Gemini, ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    "groq": (Groq, ("GROQ_API_KEY",)),
    "anthropic": (Claude, ("ANTHROPIC_API_KEY",)),
}


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=value lines); never overrides variables already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip().removeprefix("export ").strip(), value.strip().strip("'\""))


def get_llm(disabled: bool = False) -> LLM | None:
    if disabled:
        return None
    wanted = os.getenv("AMS_LLM_PROVIDER", "").strip().lower()
    order = [wanted] if wanted else list(PROVIDERS)
    for name in order:
        if name not in PROVIDERS:
            raise ValueError(f"unknown AMS_LLM_PROVIDER {name!r}; use one of {', '.join(PROVIDERS)}")
        cls, env_vars = PROVIDERS[name]
        key = next((os.environ[v] for v in env_vars if os.getenv(v)), None)
        if key:
            return cls(key)
    return None
