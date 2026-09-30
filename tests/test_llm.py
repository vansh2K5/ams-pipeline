import json
from pathlib import Path

import httpx
import pytest

from ams import llm as llm_mod
from ams.llm import Claude, Gemini, Groq, get_llm, load_dotenv, parse_json
from ams.mapping import build_plan
from ams.parsers import parse_service

EXAMPLES = Path(__file__).parent.parent / "examples"
KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "AMS_LLM_PROVIDER", "AMS_MODEL")


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def mock(handler):
    seen = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), seen


def test_gemini_request_and_json_reply():
    transport, seen = mock(lambda r: httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}))
    answer = Gemini("g-key", transport=transport).complete_json("sys", "user")
    assert answer == {"ok": True}
    req = seen[0]
    assert req.url.path == "/v1beta/models/gemini-flash-latest:generateContent"
    assert req.headers["x-goog-api-key"] == "g-key"
    body = json.loads(req.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "sys" in body["systemInstruction"]["parts"][0]["text"]


def test_groq_request_and_json_reply():
    transport, seen = mock(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": '```json\n{"pairs": []}\n```'}}]}))
    answer = Groq("q-key", transport=transport).complete_json("sys", "user")
    assert answer == {"pairs": []}
    req = seen[0]
    assert str(req.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer q-key"
    body = json.loads(req.content)
    assert body["model"] == "openai/gpt-oss-120b" and body["response_format"] == {"type": "json_object"}


def test_claude_request_and_text_reply():
    transport, seen = mock(lambda r: httpx.Response(200, json={"content": [{"type": "text", "text": "fixed code"}]}))
    assert Claude("a-key", transport=transport).complete_text("sys", "user") == "fixed code"
    assert seen[0].headers["x-api-key"] == "a-key"


def quiet(provider):
    waits = []
    provider._sleep = waits.append
    return provider, waits


def test_provider_errors_return_none_and_explain():
    transport, seen = mock(lambda r: httpx.Response(429, json={"error": {"message": "rate limited"}}))
    provider, waits = quiet(Groq("q-key", transport=transport))
    assert provider.complete_json("sys", "user") is None
    assert len(seen) == 3 and waits == [1.5, 3.0]  # two backoff retries, then give up
    assert provider.last_error.startswith("HTTP 429") and "rate limited" in provider.last_error
    assert "q-key" not in provider.last_error


def test_transient_error_then_success():
    replies = iter([httpx.Response(503, headers={"retry-after": "2"}, text="overloaded"),
                    httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})])
    transport, seen = mock(lambda r: next(replies))
    provider, waits = quiet(Groq("q-key", transport=transport))
    assert provider.complete_json("sys", "user") == {"ok": True}
    assert waits == [2.0] and provider.last_error == ""


def test_auth_errors_are_not_retried():
    transport, seen = mock(lambda r: httpx.Response(401, text="bad key"))
    provider, waits = quiet(Gemini("g-key", transport=transport))
    assert provider.complete_text("sys", "user") is None
    assert len(seen) == 1 and waits == []


def test_network_failure_returns_none():
    def boom(request):
        raise httpx.ConnectError("offline")

    provider, _ = quiet(Gemini("g-key", transport=httpx.MockTransport(boom)))
    assert provider.complete_text("sys", "user") is None
    assert "ConnectError" in provider.last_error


def test_selection_order_and_explicit_choice(monkeypatch):
    assert get_llm() is None
    monkeypatch.setenv("GROQ_API_KEY", "q")
    assert get_llm().name.startswith("groq:")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert get_llm().name.startswith("gemini:")  # gemini is checked first
    monkeypatch.setenv("AMS_LLM_PROVIDER", "groq")
    monkeypatch.setenv("AMS_MODEL", "llama-3.1-8b-instant")
    assert get_llm().name == "groq:llama-3.1-8b-instant"
    assert get_llm(disabled=True) is None
    monkeypatch.setenv("AMS_LLM_PROVIDER", "nope")
    with pytest.raises(ValueError):
        get_llm()


def test_dotenv_never_overrides_real_env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nGEMINI_API_KEY='from-file'\nexport GROQ_API_KEY=q-file\n")
    monkeypatch.setenv("GROQ_API_KEY", "from-shell")
    load_dotenv(env)
    assert llm_mod.os.environ["GEMINI_API_KEY"] == "from-file"
    assert llm_mod.os.environ["GROQ_API_KEY"] == "from-shell"
    monkeypatch.delenv("GEMINI_API_KEY")


def test_parse_json_tolerates_chatter():
    assert parse_json('Sure! {"a": 1} hope that helps') == {"a": 1}
    assert parse_json("[1, 2]") is None
    assert parse_json("") is None


def test_failed_provider_falls_back_to_heuristic_with_a_note():
    transport, _ = mock(lambda r: httpx.Response(401, text="invalid key"))
    prov, cons = parse_service(EXAMPLES / "order-service"), parse_service(EXAMPLES / "billing-service")
    plan = build_plan(prov, cons, prov.entity("Order"), cons.entity("OrderRecord"), Gemini("bad", transport=transport))
    assert plan.coverage == 1.0 and all(p.source == "heuristic" for p in plan.pairs)
    assert "HTTP 401" in plan.notes[0]


def test_engine_label_credits_what_actually_mapped():
    prov, cons = parse_service(EXAMPLES / "order-service"), parse_service(EXAMPLES / "billing-service")
    down, _ = mock(lambda r: httpx.Response(401, text="bad key"))
    plan = build_plan(prov, cons, prov.entity("Order"), cons.entity("OrderRecord"), Gemini("k", transport=down))
    assert plan.engine == "heuristic (fallback from gemini:gemini-flash-latest)"
    one = '{"pairs": [{"consumer_field": "client_id", "provider_field": "customerId", "confidence": 0.9}]}'
    partial, _ = mock(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": one}}]}))
    plan = build_plan(prov, cons, prov.entity("Order"), cons.entity("OrderRecord"), Groq("k", transport=partial))
    assert plan.engine == "groq:openai/gpt-oss-120b + heuristic"
