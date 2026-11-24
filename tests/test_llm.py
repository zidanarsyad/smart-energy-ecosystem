"""LLM reasoning module: Groq-first fallback chain, JSON mode, cache, privacy-by-construction (offline, mocked)."""
import json
import os

import httpx
import pytest

from common.agent_mind import AgentMind
from common.llm import LLMClient, extract_json, retry_after_seconds, strip_think

GROQ_429 = ("Rate limit reached for model `{m}` in organization `org_01abcdefghijklmnopqrstuvwx` service tier "
            "`on_demand` on tokens per minute (TPM): Limit 8000, Used 7100, Requested 2400. Please try again in {t}. "
            "Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing")


def rate_limited(model: str, wait: str) -> httpx.Response:
    return httpx.Response(429, json={"error": {"message": GROQ_429.format(m=model, t=wait), "type": "tokens",
                                               "code": "rate_limit_exceeded"}})


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("SEE_LLM", "on")
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(k, "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "or/model:free")


def ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 42}})


def test_falls_back_from_rate_limited_groq_model(keys):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.host, body["model"]))
        if body["model"] == "openai/gpt-oss-120b":
            return httpx.Response(429, json={"error": "rate limited"})
        return ok("Halo, saya agen.")

    client = LLMClient(transport=httpx.MockTransport(handler))
    reply = client.chat([{"role": "user", "content": "Halo"}])
    assert reply.ok and reply.provider == "groq" and reply.model == "qwen/qwen3.8-27b"
    assert seen[0] == ("api.groq.com", "openai/gpt-oss-120b") and reply.attempts[0]["status"] == 429


def test_short_rate_limit_waits_and_keeps_the_agents_own_model(keys):
    calls, sleeps = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return rate_limited(model, "750ms") if len(calls) == 1 else ok("Siap, tetap dengan model sendiri.")

    client = LLMClient(transport=httpx.MockTransport(handler))
    client._sleep = sleeps.append
    reply = client.chat([{"role": "user", "content": "x"}], prefer="qwen/qwen3.8-27b")
    assert reply.ok and reply.model == "qwen/qwen3.8-27b" and calls == ["qwen/qwen3.8-27b"] * 2
    assert sleeps == [pytest.approx(1.0)] and reply.attempts == [] and reply.waited_s == pytest.approx(1.0)
    assert client.stats["rate_limited"] == 1 and client.stats["same_model_retries"] == 1


def test_long_rate_limit_falls_back_cools_down_and_hides_account_ids(keys):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        return rate_limited(model, "1m6.5s") if model == "qwen/qwen3.8-27b" else ok("fallback answer")

    client = LLMClient(transport=httpx.MockTransport(handler))
    client._sleep = lambda seconds: pytest.fail("a long rate limit must not block the agent")
    first = client.chat([{"role": "user", "content": "a"}], prefer="qwen/qwen3.8-27b")
    assert first.ok and first.model == "openai/gpt-oss-120b"
    assert first.attempts[0]["error"] == "rate limited (TPM: used 7100 + requested 2400 > limit 8000)"
    assert "org_" not in json.dumps(first.meta())         # account ids never reach run records or the UI
    second = client.chat([{"role": "user", "content": "b"}], prefer="qwen/qwen3.8-27b")
    assert second.ok and calls.count("qwen/qwen3.8-27b") == 1   # cooling down: not called again
    assert second.attempts[0]["error"].startswith("skipped: rate-limit cool-down")
    assert "groq:qwen/qwen3.8-27b" in client.status()["cooling_down_s"]


def test_retry_after_parsing():
    def resp(text, headers=None):
        return httpx.Response(429, text=text, headers=headers or {})

    assert retry_after_seconds(resp("Please try again in 750ms. Need more")) == pytest.approx(0.75)
    assert retry_after_seconds(resp("Please try again in 1m6.5s. Need more")) == pytest.approx(66.5)
    assert retry_after_seconds(resp("Quota exceeded. Please retry in 23s.")) == pytest.approx(23.0)
    assert retry_after_seconds(resp("busy", {"retry-after": "3"})) == 3.0
    assert retry_after_seconds(resp("busy")) is None


def test_falls_through_to_gemini_then_openrouter(keys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return ok("from openrouter")
        return httpx.Response(503, json={"error": "down"})

    reply = LLMClient(transport=httpx.MockTransport(handler)).chat([{"role": "user", "content": "x"}])
    assert reply.ok and reply.provider == "openrouter" and len(reply.attempts) == 4  # 3 groq + 1 gemini


def test_json_mode_skips_invalid_output_and_parses_valid(keys):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        if request.url.host == "api.groq.com":
            assert body["response_format"] == {"type": "json_object"}
        return ok("not json at all") if calls["n"] == 1 else ok('```json\n{"message": "ok", "confidence": 0.8}\n```')

    reply = LLMClient(transport=httpx.MockTransport(handler)).chat([{"role": "user", "content": "j"}], json_mode=True)
    assert reply.ok and reply.data == {"message": "ok", "confidence": 0.8} and calls["n"] == 2


def test_cache_and_disabled_mode(keys, monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return ok("cached answer")

    client = LLMClient(transport=httpx.MockTransport(handler))
    msgs = [{"role": "user", "content": "same"}]
    assert client.chat(msgs).text == client.chat(msgs).text and calls["n"] == 1
    assert client.chat(msgs).cached
    monkeypatch.setenv("SEE_LLM", "off")
    assert not client.chat([{"role": "user", "content": "other"}]).ok and calls["n"] == 1


def test_think_tags_and_json_extraction():
    assert strip_think("<think>secret chain</think>Answer") == "Answer"
    assert extract_json('Sure! {"a": 1} hope that helps') == {"a": 1}
    assert extract_json("no json") is None


def test_agent_mind_uses_only_external_facts_and_falls_back_offline(monkeypatch):
    monkeypatch.setenv("SEE_LLM", "off")
    audiences = []

    def context(audience):
        audiences.append(audience)
        base = {"summary": "Test summary.", "can_contribute": "ADDITIONAL: 100 kWh"}
        return {**base, "reservation_price": 1500} if audience == "internal" else base

    profile = {"id": "t", "name": "TestCo", "role": "tester", "paradigm": "BDI", "business_goal": "test",
               "governance": ["rule one", "rule two"], "agents": [{"name": "Test Agent"}]}
    mind = AgentMind(profile, context, "qwen/qwen3.8-27b", "reservation price")
    out = mind.deliberate({"headline": "Deficit 100 kWh"}, "en")
    assert audiences == ["external"]                       # round tables never see private data
    assert out["llm_ok"] is False and out["can_contribute"] == "ADDITIONAL: 100 kWh"
    ans = mind.ask("Berapa harga reservasi?", "auto", "external", "other agent")
    assert audiences[-1] == "external" and "1500" not in ans["answer"]
    assert os.environ["SEE_LLM"] == "off"
