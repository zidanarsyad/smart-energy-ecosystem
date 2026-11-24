"""LLM reasoning & communication module with a free-tier provider chain: Groq first, then Gemini, then OpenRouter.

The LLM is an *auxiliary* reasoning/communication module (course doc: "LLM dapat ditambahkan sebagai modul
reasoning ... tetapi tidak wajib menjadi inti seluruh agen"). Binding decisions stay with the deterministic
agents (LP, BDI, MPC, rules); the LLM explains, deliberates, answers questions and translates, in any language.

Keys are read from the project's .env file (never logged). Set SEE_LLM=off to disable all calls (tests do this);
every caller has a deterministic fallback, so the ecosystem keeps working without network or quota.

Free-tier rate limits: Groq meters tokens per minute per model (8,000 TPM on this key) with a continuously
refilling bucket. On a 429 the client reads the provider's "try again in ..." delay: up to SEE_LLM_MAX_WAIT seconds
(default 4) it pauses and retries the same model, so each agent keeps its own LLM; a longer delay puts that model in
a cool-down and the request moves down the chain. Stored error texts are sanitised (no account/organisation ids).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field

import httpx

from common import multilingual as ml
from common.config import ROOT


def load_dotenv(path=None) -> None:
    path = path or ROOT / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

PROVIDERS = {
    "groq": {"url": "https://api.groq.com/openai/v1/chat/completions", "key_env": "GROQ_API_KEY", "timeout": 20.0},
    "gemini": {"url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
               "key_env": "GEMINI_API_KEY", "timeout": 30.0},
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions", "key_env": "OPENROUTER_API_KEY",
                   "timeout": 25.0},
}

# Free-tier models verified on 2026-11-18. Groq is preferred and first in every fallback chain.
GROQ_REASONING = "openai/gpt-oss-120b"
GROQ_FAST = "qwen/qwen3.8-27b"
GROQ_COMPACT = "openai/gpt-oss-20b"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Provider-qualified model specs ("provider:model"). Like real companies, every enterprise runs its own LLM from its
# own vendor; the shared chain behind it only covers free-tier quota exhaustion and outages.
QWEN = "groq:" + GROQ_FAST                                  # Alibaba Qwen
GPT_OSS_120B = "groq:" + GROQ_REASONING                     # OpenAI open-weight (self-hostable)
GPT_OSS_20B = "groq:" + GROQ_COMPACT                        # OpenAI open-weight, smaller
GEMINI_FLASH_LITE = "gemini:gemini-3.5-flash-lite"          # Google
GEMINI_31_FLASH_LITE = "gemini:gemini-3.1-flash-lite"       # Google, previous generation (small, low latency)
NEMOTRON = "openrouter:nvidia/nemotron-3.5-super-vl:free"   # NVIDIA

LANGUAGES = {"auto": None, **{code: english for code, (english, _) in ml.LANGUAGES.items()}}


def default_chain() -> list[tuple[str, str]]:
    return [("groq", GROQ_REASONING), ("groq", GROQ_FAST), ("groq", GROQ_COMPACT), ("gemini", GEMINI_MODEL),
            ("openrouter", os.environ.get("OPENROUTER_MODEL", "")),
            ("openrouter", os.environ.get("OPENROUTER_FALLBACK_MODEL", ""))]


def parse_spec(spec: str | None) -> tuple[str, str] | None:
    """'gemini:gemini-3.1-flash-lite' -> ('gemini', ...); a bare model id is looked up in the default chain."""
    if not spec:
        return None
    provider, sep, model = spec.partition(":")
    if sep and provider in PROVIDERS:
        return provider, model
    return next((c for c in default_chain() if c[1] == spec), ("groq", spec))


def chain_for(prefer: str | None) -> list[tuple[str, str]]:
    chain = default_chain()
    first = parse_spec(prefer)
    if first:
        chain = [first] + [c for c in chain if c != first]
    return [c for c in chain if c[1]]


def agent_llm(eid: str, default: str | None) -> str | None:
    """An enterprise's own LLM choice; SEE_LLM_<ENTERPRISE>=provider:model overrides it (e.g. SEE_LLM_POWERGRID)."""
    return os.environ.get(f"SEE_LLM_{eid.upper()}") or default


def short_model(spec: str | None) -> str:
    parsed = parse_spec(spec)
    return parsed[1].split("/")[-1] if parsed else "-"


def language_instruction(language: str | None) -> str:
    if not language or language == "auto":
        return ("Reply in the language of the user's MESSAGE (English in, English out; Bahasa Indonesia in, Bahasa "
                "Indonesia out). The facts may use Bahasa Indonesia field names; they never change the reply language.")
    return f"Reply in {LANGUAGES.get(language, language)}."


def language_reminder(language: str | None) -> str:
    """Last line of a user turn: models weigh the end of the prompt most, so the language rule is repeated there."""
    if not language or language == "auto":
        return "Answer in the same language as the MESSAGE above, not in the language of the facts."
    return f"Answer in {LANGUAGES.get(language, language)}."


def strip_think(text: str) -> str:
    return re.sub(r"<(think|thought)>.*?</\1>", "", text or "", flags=re.S).strip()


def merge_system(messages: list[dict]) -> list[dict]:
    """For models without a system role (Gemma on the Gemini API): fold the system prompt into the first turn."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [dict(m) for m in messages if m["role"] != "system"]
    if system and rest:
        rest[0]["content"] = system + "\n\n" + rest[0]["content"]
    return rest or [{"role": "user", "content": system}]


def extract_json(text: str) -> dict | None:
    text = strip_think(text)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None


MAX_WAIT_S = float(os.environ.get("SEE_LLM_MAX_WAIT", "4"))
_RETRY_RE = re.compile(r"(?:try again|retry) in\s+([0-9hms.]+)", re.I)
_LIMIT_RE = re.compile(r"\(([A-Z]+)\):\s*Limit\s+(\d+),\s*Used\s+(\d+),\s*Requested\s+(\d+)")
_SCALE = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def retry_after_seconds(resp: httpx.Response) -> float | None:
    """Delay until a rate-limited model accepts the request again: the provider's message, else Retry-After."""
    match = _RETRY_RE.search(resp.text or "")
    if match:
        parts = re.findall(r"([\d.]+)(ms|h|m|s)", match.group(1))
        if parts:
            return sum(float(num) * _SCALE[unit] for num, unit in parts)
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None


def describe_error(status: int | None, text: str) -> str:
    """Short, sanitised failure reason: never echoes account, organisation or project identifiers."""
    if status == 429:
        m = _LIMIT_RE.search(text or "")
        return (f"rate limited ({m.group(1)}: used {m.group(3)} + requested {m.group(4)} > limit {m.group(2)})"
                if m else "rate limited")
    clean = re.sub(r"org[_-][A-Za-z0-9]+|projects/\d+", "***", text or "")
    return (f"HTTP {status}: " if status else "") + clean[:120]


@dataclass
class LLMReply:
    ok: bool
    text: str = ""
    data: dict | None = None
    provider: str = "none"
    model: str = ""
    latency_ms: float = 0.0
    tokens: int | None = None
    cached: bool = False
    attempts: list = field(default_factory=list)
    error: str | None = None
    waited_s: float = 0.0

    def meta(self) -> dict:
        return {"llm_ok": self.ok, "provider": self.provider, "model": self.model, "latency_ms": self.latency_ms,
                "tokens": self.tokens, "cached": self.cached, "fallback_attempts": self.attempts,
                "llm_error": self.error, "rate_limit_wait_s": self.waited_s}


class LLMClient:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(transport=transport, timeout=30.0)
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, LLMReply] = OrderedDict()
        self._cooldown: dict[tuple[str, str], float] = {}
        self._sleep = time.sleep
        self.recent: deque = deque(maxlen=60)
        self.stats = {"calls": 0, "ok": 0, "failed": 0, "cache_hits": 0, "rate_limited": 0,
                      "same_model_retries": 0, "by_model": {}}

    def _bump(self, counter: str) -> None:
        with self._lock:
            self.stats[counter] = self.stats.get(counter, 0) + 1

    def _cooldown_left(self, provider: str, model: str) -> float:
        with self._lock:
            return max(0.0, self._cooldown.get((provider, model), 0.0) - time.monotonic())

    def _post(self, cfg: dict, body: dict, headers: dict) -> tuple[httpx.Response | None, str | None]:
        try:
            return self.http.post(cfg["url"], headers=headers, json=body, timeout=cfg["timeout"]), None
        except httpx.HTTPError as exc:
            return None, type(exc).__name__

    @staticmethod
    def enabled() -> bool:
        if os.environ.get("SEE_LLM", "on").lower() in ("off", "0", "false", "no"):
            return False
        return any(os.environ.get(p["key_env"]) for p in PROVIDERS.values())

    def status(self) -> dict:
        with self._lock:
            now = time.monotonic()
            cooling = {f"{p}:{m}": round(until - now, 1) for (p, m), until in self._cooldown.items() if until > now}
        return {"enabled": self.enabled(),
                "configured_providers": [n for n, p in PROVIDERS.items() if os.environ.get(p["key_env"])],
                "chain": [f"{p}:{m}" for p, m in default_chain() if m], "stats": self.stats,
                "max_rate_limit_wait_s": MAX_WAIT_S, "cooling_down_s": cooling, "recent": list(self.recent)[-15:]}

    def chat(self, messages: list[dict], *, prefer: str | None = None, json_mode: bool = False,
             max_tokens: int = 700, temperature: float = 0.3, agent: str = "") -> LLMReply:
        if not self.enabled():
            return LLMReply(ok=False, error="LLM disabled or no API key configured")
        key = hashlib.sha256(json.dumps([messages, prefer, json_mode, max_tokens], sort_keys=True,
                                        ensure_ascii=False).encode()).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
            if hit:
                self.stats["cache_hits"] += 1
                return LLMReply(**{**asdict(hit), "cached": True})
        attempts, waited = [], 0.0
        for provider, model in chain_for(prefer):
            cfg = PROVIDERS[provider]
            api_key = os.environ.get(cfg["key_env"])
            if not api_key:
                continue
            left = self._cooldown_left(provider, model)
            if left > MAX_WAIT_S:
                attempts.append({"provider": provider, "model": model, "status": 429,
                                 "error": f"skipped: rate-limit cool-down, {left:.0f}s left"})
                continue
            if left > 0:
                self._sleep(left)
                waited += left
            body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
            if json_mode and provider != "openrouter":
                body["response_format"] = {"type": "json_object"}
            if provider == "groq" and model.startswith("openai/gpt-oss"):
                body["reasoning_effort"] = "low"
            elif provider == "groq" and "qwen" in model:
                body["reasoning_format"] = "hidden"
            elif provider == "gemini" and model.startswith("gemini"):
                # Thinking tokens count against the output limit; a long think once cut an explanation to 93 chars.
                body["reasoning_effort"] = "minimal"
                body["max_tokens"] = max(max_tokens, 2048)
            elif provider == "openrouter":
                # Free hybrid-reasoning models (Nemotron, ERNIE) otherwise think for thousands of tokens and get cut
                # off mid-JSON (measured: 14 s and truncated vs 2.7 s complete). The deterministic agents do the
                # decision reasoning; here the LLM communicates, so thinking is switched off.
                body["reasoning"] = {"enabled": False}
                body["max_tokens"] = max(max_tokens, 1800)
            if "gemma" in model:
                body["messages"] = merge_system(messages)
                body.pop("response_format", None)
            headers = {"Authorization": f"Bearer {api_key}"}
            if provider == "openrouter":
                headers["X-Title"] = "Shakti Tantra academic MVP"
            t0 = time.perf_counter()
            resp, failure = self._post(cfg, body, headers)
            if resp is not None and resp.status_code == 429:
                self._bump("rate_limited")
                wait = retry_after_seconds(resp)
                if wait is not None and wait <= MAX_WAIT_S:
                    # The token bucket refills continuously: a short pause keeps the agent on its own model.
                    self._sleep(wait + 0.25)
                    waited += wait + 0.25
                    self._bump("same_model_retries")
                    resp, failure = self._post(cfg, body, headers)
                if resp is not None and resp.status_code == 429:
                    cool = min(retry_after_seconds(resp) or 10.0, 3600.0)
                    with self._lock:
                        self._cooldown[(provider, model)] = time.monotonic() + cool
            latency = round((time.perf_counter() - t0) * 1000, 1)
            if failure:
                attempts.append({"provider": provider, "model": model, "error": failure})
                continue
            if resp.status_code != 200:
                attempts.append({"provider": provider, "model": model, "status": resp.status_code,
                                 "error": describe_error(resp.status_code, resp.text)})
                continue
            payload = resp.json()
            choice = (payload.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "length":    # cut off at the token limit: never show half an answer
                attempts.append({"provider": provider, "model": model, "error": "output cut off at the token limit"})
                continue
            text = strip_think(choice.get("message", {}).get("content") or "")
            data = extract_json(text) if json_mode else None
            if not text or (json_mode and data is None):
                attempts.append({"provider": provider, "model": model, "error": "empty or invalid JSON output"})
                continue
            reply = LLMReply(ok=True, text=text, data=data, provider=provider, model=model, latency_ms=latency,
                             tokens=(payload.get("usage") or {}).get("total_tokens"), attempts=attempts,
                             waited_s=round(waited, 2))
            with self._lock:
                self._cache[key] = reply
                while len(self._cache) > 256:
                    self._cache.popitem(last=False)
                self.stats["calls"] += 1
                self.stats["ok"] += 1
                self.stats["by_model"][f"{provider}:{model}"] = self.stats["by_model"].get(f"{provider}:{model}", 0) + 1
                self.recent.append({"agent": agent, "provider": provider, "model": model, "latency_ms": latency,
                                    "tokens": reply.tokens, "fallbacks": len(attempts),
                                    "rate_limit_wait_s": reply.waited_s,
                                    "fallback_reasons": [f"{a['model']}: {a['error']}" for a in attempts][:4]})
            return reply
        with self._lock:
            self.stats["calls"] += 1
            self.stats["failed"] += 1
            self.recent.append({"agent": agent, "provider": "none", "model": "", "error": "all providers failed",
                                "fallback_reasons": [f"{a['model']}: {a['error']}" for a in attempts][:6]})
        return LLMReply(ok=False, attempts=attempts, error="all providers failed", waited_s=round(waited, 2))


_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
