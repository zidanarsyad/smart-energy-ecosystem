"""Multilingual agent communication: every agent speaks its own language and runs its own LLM; the RECEIVER's own
LLM interprets foreign messages, and a deterministic check verifies that numbers survive (offline, mocked)."""
import json

import httpx
import pytest

import common.agent_mind as agent_mind_mod
from common import multilingual as ml
from common.agent_mind import AgentMind
from common.llm import LLMClient, agent_llm, chain_for, parse_spec, short_model

PROFILE = {"id": "powergrid", "name": "PowerGrid", "role": "DSO", "paradigm": "reactive rules",
           "business_goal": "Keep the feeder safe.", "governance": ["Rule R5 limits non-firm import."],
           "agents": [{"name": "Grid Safety Agent"}], "language": "id"}


def facts(audience):
    return {"summary": "Feeder forecast at 81%.", "can_contribute": "ADDITIONAL: none (R5)"}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("SEE_LLM", "on")
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(k, "test-key")


def llm_json(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
                                     "usage": {"total_tokens": 99}})


def mind_with(handler, monkeypatch) -> AgentMind:
    client = LLMClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(agent_mind_mod, "get_client", lambda: client)
    return AgentMind(dict(PROFILE), facts, "groq:openai/gpt-oss-120b", "network topology")


def test_number_fidelity_across_languages_and_locales():
    zh = "由于云层覆盖，08:00–16:00发电量下降40%，约600千瓦时。"
    assert ml.number_fidelity(zh, "Karena awan, output turun 40% pada 08:00-16:00, sekitar 600 kWh.")["ok"]
    bad = ml.number_fidelity("Harga Rp1.741/kWh untuk 300 kWh", "Price Rp1,741/kWh for 30 kWh")
    assert not bad["ok"] and bad["missing"] == [300] and bad["added"] == [30]   # 1.741 (id) == 1,741 (en)
    assert ml.numbers_in("23,08% dan 1.500,5 kWh") == {23.08, 1500.5}
    assert ml.label("zh") == "中文 (Mandarin Chinese)" and ml.label("en") == "English"


def test_each_agent_has_its_own_llm_with_shared_backup(monkeypatch):
    assert parse_spec("openrouter:nvidia/nemotron-3.5-super-vl:free") == ("openrouter",
                                                                         "nvidia/nemotron-3.5-super-vl:free")
    chain = chain_for("gemini:gemini-3.1-flash-lite")
    assert chain[0] == ("gemini", "gemini-3.1-flash-lite") and chain[1][0] == "groq"   # own model, then Groq
    assert short_model("groq:qwen/qwen3.8-27b") == "qwen3.8-27b"
    monkeypatch.setenv("SEE_LLM_POWERGRID", "groq:openai/gpt-oss-20b")
    assert agent_llm("powergrid", "groq:openai/gpt-oss-120b") == "groq:openai/gpt-oss-20b"


def test_vendor_specific_request_shapes(keys):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.host, json.loads(request.content)))
        return llm_json({"ok": True})

    client = LLMClient(transport=httpx.MockTransport(handler))
    sysmsg = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    client.chat(sysmsg, prefer="openrouter:nvidia/nemotron-3.5-super-vl:free", max_tokens=600)
    host, body = bodies[-1]
    assert host == "openrouter.ai" and body["reasoning"] == {"enabled": False} and body["max_tokens"] == 1800
    client.chat(sysmsg, prefer="gemini:gemma-4-31b-it", json_mode=True)
    host, body = bodies[-1]
    assert "response_format" not in body and [m["role"] for m in body["messages"]] == ["user"]   # Gemma: no system


def test_receiver_llm_interprets_foreign_messages_and_replies_in_its_language(keys, monkeypatch):
    prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["messages"][-1]["content"])
        return llm_json({"understood": [{"id": "r1-sg", "translation": "Output surya turun 40%, sekitar 600 kWh.",
                                         "performative": "inform", "intent": "memberi tahu kekurangan pasokan"}],
                         "reply": "Kami tidak dapat menambah impor non-firm (R5).", "reply_performative": "refuse",
                         "addressed_to": ["SolarGreen"], "reply_viewer": "We cannot add non-firm import (R5)."})

    mind = mind_with(handler, monkeypatch)
    msgs = [{"id": "r1-sg", "from": "solargreen", "from_name": "SolarGreen", "language": "zh",
             "text": "太阳能输出下降40%，约600千瓦时。"},
            {"id": "r1-ip", "from": "industriprima", "from_name": "IndustriPrima", "language": "id",
             "text": "Kami siap DR 100 kWh."}]
    out = mind.crosstalk(msgs, "Deficit 100 kWh", {"message": "Kami menjaga R5."}, "en")
    assert out["language"] == "id" and out["reply_language"] == "id" and out["reply_performative"] == "REFUSE"
    sg, ip = out["understood"]["r1-sg"], out["understood"]["r1-ip"]
    assert sg["performative"] == "INFORM" and sg["numbers"]["ok"] and sg["numbers"]["kept"] == 2
    assert ip["passthrough"] and ip["translation"] == "Kami siap DR 100 kWh."       # same language: delivered as is
    assert '["r1-sg"]' in prompts[0] and "into Indonesian" in prompts[0]            # only the foreign one is translated
    assert mind.history[-1]["mode"] == "crosstalk"


def test_untranslated_echo_is_detected_and_retried_once(keys, monkeypatch):
    zh = "请问贵方能否在14:00提供100 kWh的非保量进口电力？"
    assert ml.looks_untranslated(zh, zh, "zh", "id")                                   # copied, not translated
    assert ml.looks_untranslated(zh, "能否提供100 kWh电力", "zh", "id")                  # wrong script for Bahasa
    assert ml.looks_untranslated("Can you supply 100 kWh?", "Can you supply 100 kWh", "en", "zh")
    assert not ml.looks_untranslated(zh, "Bisakah Anda menyediakan 100 kWh pada 14:00?", "zh", "id")
    both = zh + "\n\nBisakah Anda menyediakan impor non-firm 100 kWh pada 14:00 untuk kami?"
    assert ml.looks_untranslated(zh, both, "zh", "id")                                 # original echoed inside (seen live)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["messages"][-1]["content"])
        if len(calls) == 1:   # the combined understand-and-answer call echoes the Mandarin text
            return llm_json({"understood": {"translation": zh, "performative": "query", "intent": "minta impor"},
                             "reply": "Tidak bisa, R5 berlaku.", "reply_performative": "refuse",
                             "reply_viewer": "No, R5 applies."})
        return llm_json({"translation": "Bisakah Anda menyediakan impor non-firm 100 kWh pada 14:00?",
                         "performative": "query", "intent": "meminta impor non-firm"})

    out = mind_with(handler, monkeypatch).converse(zh, "zh", "SolarGreen", "en")
    u = out["understood"]
    assert u["retried"] and u["translated"] and u["numbers"]["ok"] and len(calls) == 2
    assert "never copy the original text" in calls[1]                                   # the retry is stricter


def test_same_language_needs_no_llm_and_offline_fallback_is_explicit(monkeypatch):
    monkeypatch.setenv("SEE_LLM", "off")
    mind = AgentMind(dict(PROFILE), facts, None, "network topology")
    same = mind.understand("Beban feeder 81%.", "id", "IndustriPrima")
    assert same["understood"]["passthrough"] and same["language"] == "id"
    foreign = mind.understand("Feeder load is 81%.", "en", "EnergyTrade")
    assert foreign["understood"]["translation"] is None and "LLM unavailable" in foreign["understood"]["error"]
    pos = mind.deliberate({"headline": "Deficit 100 kWh", "llm": True}, "en")
    assert pos["language"] == "en" and pos["message"].startswith("[LLM unavailable]")   # canonical-language fallback
    sent = mind.compose("Tanya SolarGreen soal prakiraan", "SolarGreen")
    assert sent["message"] == "Tanya SolarGreen soal prakiraan" and sent["language"] == "und"
