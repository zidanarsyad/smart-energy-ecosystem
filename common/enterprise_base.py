"""Shared FastAPI shell for an enterprise server.

The shell only provides *internal* endpoints (health, agent card, dashboard data and the
simulation harness). Each enterprise adds its own *native* business API on top, and those
native APIs are deliberately heterogeneous (REST/JSON, SOAP/XML, JSON-RPC, Bahasa JSON).
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from common import multilingual as ml
from common.a2a import build_agent_card
from common.agent_mind import AgentMind
from common.glossary import GLOSSARY, ID_EN
from common.llm import agent_llm, get_client
from common.config import (BRAND_DIR, ECOSYSTEM_MEANING, ECOSYSTEM_NAME, ENTERPRISE_PORTS, FRONTEND_DIR,
                           ORCHESTRATOR_PORT, UI_PORT, browser_url, enterprise_url)
from common.stores import AuditLog


class UIProvider:
    """Interface each enterprise implements for its own dashboard (UI/UX per enterprise)."""

    def kpis(self) -> list[dict]:
        return []

    def agents(self) -> list[dict]:
        return []

    def tables(self) -> list[str]:
        return []

    def table(self, name: str, limit: int = 50) -> dict:
        return {"columns": [], "rows": []}

    def charts(self) -> list[dict]:
        return []

    def native_examples(self) -> list[dict]:
        return []

    def approvals(self) -> list[dict] | None:
        return None

    def decide_approval(self, approval_id: str, decision: str, role: str, note: str) -> dict:
        raise HTTPException(404, "This enterprise has no approval workflow")

    def agent_context(self, audience: str) -> dict:
        """Facts the LLM mind may use. audience 'internal' may include private data; 'external' must not."""
        return {"summary": "No context available."}


def rows_to_table(rows: list[dict], limit: int = 50) -> dict:
    rows = rows[:limit]
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return {"columns": cols, "rows": [[_cell(r.get(c)) for c in cols] for r in rows]}


def _cell(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 3)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)[:300]
    return v


def create_enterprise_app(profile: dict, *, skills: list[dict], transport: str,
                          native_prefixes: list[str], env_defaults: dict,
                          ui: UIProvider, audit: AuditLog) -> FastAPI:
    app = FastAPI(
        title=f"{profile['name']} - enterprise agent server",
        version="0.1.0-mvp",
        description=profile["description"] + f"  Native protocol: {profile['protocol']}.",
    )
    defaults = {"onset_slot": 0, "outage": False, **env_defaults}
    app.state.env = dict(defaults)
    app.state.env_defaults = dict(defaults)
    base_url = enterprise_url(profile["id"])
    port = ENTERPRISE_PORTS[profile["id"]]
    # Each enterprise chooses its own LLM and working language; deployments may override them per enterprise.
    profile["llm_model"] = agent_llm(profile["id"], profile.get("llm_model"))
    profile["language"] = os.environ.get(f"SEE_LANG_{profile['id'].upper()}") or profile.get("language", "en")
    mind = AgentMind(profile, ui.agent_context, profile.get("llm_model"),
                     profile.get("private_data", "internal costs, prices and strategies"))
    app.state.mind = mind
    blocked_prefixes = list(native_prefixes) + ["/a2a", "/agent/"]

    @app.middleware("http")
    async def outage_guard(request: Request, call_next):
        if app.state.env.get("outage") and any(request.url.path.startswith(p) for p in blocked_prefixes):
            audit.log("gateway", "request_rejected_outage", {"path": request.url.path}, level="WARN")
            return JSONResponse(status_code=503, content={
                "error": "SERVICE_UNAVAILABLE", "enterprise": profile["id"],
                "detail": f"Simulated outage of {profile['platform']}"})
        return await call_next(request)

    @app.get("/health", tags=["internal"])
    def health() -> dict:
        return {"status": "degraded" if app.state.env.get("outage") else "ok",
                "enterprise": profile["id"], "name": profile["name"], "port": port}

    @app.get("/.well-known/agent-card.json", tags=["discovery"])
    def agent_card() -> dict:
        return build_agent_card(profile, base_url, skills, transport)

    # ---------------- A2A-style conversational interface (homogeneous agent-communication layer) ----------------
    @app.post("/a2a", tags=["A2A agent communication"])
    def a2a(body: dict = Body(...)) -> JSONResponse:
        """JSON-RPC 2.0 `message/send` (subset of the A2A shape): text + data parts, any language.

        metadata.mode: 'ask' (question, answered in the question's language), 'deliberate' (round table, round 1),
        'crosstalk' (round 2: understand the other agents' messages and reply), 'compose' (write a message to another
        agent), 'converse' (understand a message from another agent and answer it), 'understand' (interpret only).
        The agent writes in its working language; metadata.language is the language of human-facing renderings."""
        rid = body.get("id")

        def error(code: int, message: str) -> JSONResponse:
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

        if body.get("jsonrpc") != "2.0" or body.get("method") != "message/send":
            return error(-32601, "Only method 'message/send' is supported")
        msg = (body.get("params") or {}).get("message") or {}
        parts = msg.get("parts") or []
        text = " ".join(p.get("text", "") for p in parts if p.get("kind") == "text").strip()
        data = next((p.get("data") for p in parts if p.get("kind") == "data"), None) or {}
        meta = msg.get("metadata") or {}
        mode, language = meta.get("mode", "ask"), meta.get("language", "auto")
        sender, use_llm = meta.get("sender", "unknown agent"), bool(meta.get("llm", True))
        native = meta.get("agent_language") or None      # simulation what-if: speak another working language
        if mode not in ("ask", "deliberate", "crosstalk", "compose", "converse", "understand"):
            return error(-32602, f"Unknown mode '{mode}'")
        if mode in ("ask", "compose", "converse", "understand") and not text:
            return error(-32602, "message needs a text part")
        if mode == "deliberate":
            out = mind.deliberate(data, language, native=native)
            reply_text, reply_lang = out["message"], out["language"]
        elif mode == "crosstalk":
            out = mind.crosstalk(data.get("messages") or [], data.get("headline", ""), data.get("my_position") or {},
                                 language, native=native, use_llm=use_llm)
            reply_text, reply_lang = out["reply"], out["reply_language"]
        elif mode == "compose":
            out = mind.compose(text, data.get("to_name") or "the ecosystem", language, native=native, use_llm=use_llm)
            reply_text, reply_lang = out["message"], out["language"]
        elif mode == "converse":
            out = mind.converse(text, data.get("source_language") or "und", data.get("from_name") or sender, language,
                                native=native, use_llm=use_llm)
            reply_text, reply_lang = out["reply"], out["reply_language"]
        elif mode == "understand":
            out = mind.understand(text, data.get("source_language") or "und", data.get("from_name") or sender,
                                  native=native, use_llm=use_llm)
            reply_text, reply_lang = out["understood"].get("translation") or text, out["language"]
        else:
            out = mind.ask(text, language, "external", sender, use_llm=use_llm)
            reply_text, reply_lang = out["answer"], language
        reply_data = {k: v for k, v in out.items() if k != "answer"}
        audit.log(f"{profile['id']}.a2a", f"a2a_{mode}", {"sender": sender, "language": reply_lang,
                                                           "provider": out.get("provider"), "model": out.get("model")})
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {
            "kind": "message", "role": "agent", "messageId": uuid.uuid4().hex, "contextId": msg.get("contextId"),
            "parts": [{"kind": "text", "text": reply_text}, {"kind": "data", "data": reply_data}],
            "metadata": {"agent": mind.agent_name, "enterprise": profile["name"], "language": reply_lang,
                         "provider": out.get("provider"), "model": out.get("model"),
                         "latency_ms": out.get("latency_ms")}}})

    @app.post("/agent/ask", tags=["dashboard"])
    def agent_ask(body: dict = Body(...)) -> dict:
        """Internal chat with this enterprise's agent (its own users): any language in, same language out."""
        question = (body.get("question") or "").strip()
        if not question:
            raise HTTPException(422, "question is required")
        return mind.ask(question, body.get("language", "auto"), "internal",
                        f"internal user ({body.get('role') or 'unknown role'})")

    @app.get("/agent/history", tags=["dashboard"])
    def agent_history() -> list[dict]:
        return list(reversed(mind.history))

    @app.get("/llm/status", tags=["internal"])
    def llm_status() -> dict:
        return {"agent": mind.agent_name, "preferred_model": profile.get("llm_model"),
                "vendor": profile.get("llm_vendor"), "working_language": profile.get("language", "en"),
                **get_client().status()}

    @app.get("/ui/glossary", tags=["dashboard"])
    def ui_glossary() -> dict:
        return {"translations": ID_EN, "glossary": GLOSSARY}

    @app.get("/sim/environment", tags=["simulation harness"])
    def get_environment() -> dict:
        return app.state.env

    @app.post("/sim/environment", tags=["simulation harness"])
    def set_environment(params: dict = Body(default_factory=dict)) -> dict:
        """Simulation harness only: changes the *world* this enterprise perceives
        (weather, faults, demand). Agents never call this; they perceive its effects."""
        if params.get("reset"):
            app.state.env = dict(app.state.env_defaults)
        for key, value in params.items():
            if key in app.state.env:
                app.state.env[key] = value
        audit.log("simulation-harness", "environment_changed", dict(app.state.env))
        return app.state.env

    # ---------------- dashboard (UI/UX per enterprise) ----------------
    # Shared ecosystem brand (favicon + "member of Shakti Tantra" mark); each enterprise keeps its own colour.
    app.mount("/brand", StaticFiles(directory=BRAND_DIR), name="brand")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        template = (FRONTEND_DIR / "enterprise.html").read_text(encoding="utf-8")
        boot = {"id": profile["id"], "name": profile["name"], "initials": profile["initials"],
                "color": profile["color"], "port": port, "ecosystem": ECOSYSTEM_NAME,
                "ecosystem_meaning": ECOSYSTEM_MEANING, "control_center": browser_url(UI_PORT),
                "orchestrator": browser_url(ORCHESTRATOR_PORT)}
        return template.replace("/*__BOOT__*/", "window.__BOOT__ = " + json.dumps(boot) + ";")

    @app.get("/ui/meta", tags=["dashboard"])
    def ui_meta() -> dict:
        return {"profile": profile, "environment": app.state.env, "port": port, "ecosystem": ECOSYSTEM_NAME,
                "language_label": ml.label(profile.get("language", "en")),
                "browser_url": browser_url(port), "docs_url": browser_url(port) + "/docs",
                "agent_card_url": browser_url(port) + "/.well-known/agent-card.json",
                "has_approvals": ui.approvals() is not None}

    @app.get("/ui/kpis", tags=["dashboard"])
    def ui_kpis() -> list[dict]:
        return ui.kpis()

    @app.get("/ui/agents", tags=["dashboard"])
    def ui_agents() -> list[dict]:
        return ui.agents()

    @app.get("/ui/tables", tags=["dashboard"])
    def ui_tables() -> list[str]:
        return ui.tables()

    @app.get("/ui/table/{name}", tags=["dashboard"])
    def ui_table(name: str, limit: int = 50) -> dict:
        if name not in ui.tables():
            raise HTTPException(404, f"Unknown table {name}")
        return ui.table(name, limit)

    @app.get("/ui/charts", tags=["dashboard"])
    def ui_charts() -> list[dict]:
        return ui.charts()

    @app.get("/ui/native", tags=["dashboard"])
    def ui_native() -> list[dict]:
        return ui.native_examples()

    @app.get("/ui/audit", tags=["dashboard"])
    def ui_audit(limit: int = 100) -> list[dict]:
        return audit.tail(limit)

    @app.get("/ui/approvals", tags=["dashboard"])
    def ui_approvals() -> list[dict]:
        return ui.approvals() or []

    @app.post("/ui/approvals/{approval_id}", tags=["dashboard"])
    def ui_decide(approval_id: str, body: dict = Body(default_factory=dict)) -> dict:
        return ui.decide_approval(approval_id, body.get("decision", ""), body.get("role", ""),
                                  body.get("note", ""))

    return app
