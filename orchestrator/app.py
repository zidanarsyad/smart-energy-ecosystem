"""Ecosystem Orchestrator server (default port 8200).

Multi-Enterprise Orchestrator + Interoperability & Policy Layer + Global Planning, Negotiation and
Decision Engine + Monitoring/Evaluation (the global architecture of 'Tugas Klas Ekosistem', C.1).
The Global Agentic Control Center (Streamlit) only talks to this API.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from common.canonical import REFERENCE_CASE, Weights
from common.config import ECOSYSTEM_NAME, ECOSYSTEM_TAGLINE, ENTERPRISE_PORTS, browser_url, data_dir
from common.glossary import GLOSSARY, ID_EN
from common.llm import LANGUAGES, get_client
from orchestrator.bus import EnterpriseUnavailable
from orchestrator.collab import EXPLAINER_MODEL, FACILITATOR_MODEL
from orchestrator.engine import EcosystemEngine, RunRequest
from orchestrator.scenarios import SCENARIOS
from orchestrator.semantics import MAPPINGS
from orchestrator.store import OrchestratorStore

store = OrchestratorStore(data_dir("orchestrator") / "ecosystem.db")
engine = EcosystemEngine(store)

app = FastAPI(title=f"{ECOSYSTEM_NAME} Orchestrator",
              version="0.1.0-mvp",
              description=f"{ECOSYSTEM_TAGLINE}. Global agentic engine coordinating five heterogeneous enterprises.")


class EnvironmentRequest(BaseModel):
    scenario: str = "normal"
    onset_slot: int = 0


class AskRequest(BaseModel):
    question: str
    language: str = "auto"


class ConverseRequest(BaseModel):
    from_agent: str             # enterprise id of the sender, e.g. "solargreen" (writes in Mandarin)
    to_agent: str               # enterprise id of the receiver, e.g. "powergrid" (understands in Bahasa Indonesia)
    message: str                # what the sender should say: an instruction from the human operator, any language
    language: str = "en"        # viewer language of the renderings shown to humans


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "component": "orchestrator", "ecosystem": ECOSYSTEM_NAME,
            "runs": len(store.list_runs(1000))}


@app.get("/registry")
def registry() -> list[dict]:
    out = engine.registry()
    for e in out:
        e["dashboard_url"] = browser_url(ENTERPRISE_PORTS[e["id"]])
    return out


@app.get("/scenarios")
def scenarios() -> dict:
    return {k: {"label": v["label"], "description": v["description"], "env": v["env"]} for k, v in SCENARIOS.items()}


@app.get("/interop/mappings")
def mappings() -> list[dict]:
    return MAPPINGS


@app.get("/reference-case")
def reference_case() -> dict:
    return REFERENCE_CASE


@app.get("/weights/default")
def default_weights() -> dict:
    return Weights().model_dump()


@app.post("/environment")
def set_environment(req: EnvironmentRequest) -> dict:
    if req.scenario not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario {req.scenario}")
    return engine.apply_environment(req.scenario, req.onset_slot)


@app.post("/runs")
def create_run(req: RunRequest) -> dict:
    try:
        return engine.run(req)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/runs/compare")
def compare_runs(req: RunRequest) -> dict:
    """Run the normal baseline first, then the requested scenario (which is compared against it)."""
    try:
        base = engine.run(RunRequest(scenario="normal", mode=req.mode, weights=req.weights))
        scen = engine.run(req)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"baseline": base, "scenario": scen}


@app.get("/runs")
def list_runs(limit: int = 50) -> list[dict]:
    return store.list_runs(limit)


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@app.get("/runs/{run_id}/messages")
def run_messages(run_id: str) -> list[dict]:
    return store.messages(run_id)


@app.post("/runs/{run_id}/sync-approvals")
def sync_approvals(run_id: str) -> dict:
    try:
        return engine.sync_approvals(run_id)
    except KeyError as exc:
        raise HTTPException(404, "run not found") from exc


@app.get("/messages")
def latest_messages(limit: int = 200) -> list[dict]:
    return store.messages(None, limit)


@app.post("/runs/{run_id}/ask")
def ask_run(run_id: str, req: AskRequest) -> dict:
    """Ask the Explainer Agent about a run, in any language."""
    try:
        return engine.ask_run(run_id, req.question, req.language)
    except KeyError as exc:
        raise HTTPException(404, "run not found") from exc


@app.post("/agents/{eid}/ask")
def ask_agent(eid: str, req: AskRequest) -> dict:
    """Talk to an enterprise agent over the A2A layer, in any language (privacy rules for external parties apply)."""
    try:
        return engine.ask_agent(eid, req.question, req.language)
    except KeyError as exc:
        raise HTTPException(404, "unknown enterprise") from exc
    except EnterpriseUnavailable as exc:
        raise HTTPException(503, f"{eid} unavailable: {exc.detail}") from exc


@app.post("/agents/converse")
def converse(req: ConverseRequest) -> dict:
    """Agent-to-agent conversation across languages: the sender writes in its own working language, the receiver's
    own LLM understands it and answers in the receiver's language, and the sender's own LLM understands the answer."""
    try:
        return engine.converse(req.from_agent, req.to_agent, req.message, req.language)
    except KeyError as exc:
        raise HTTPException(404, f"unknown enterprise {exc}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except EnterpriseUnavailable as exc:
        raise HTTPException(503, f"agent unavailable: {exc.detail}") from exc


@app.get("/llm/status")
def llm_status() -> dict:
    return {**get_client().status(), "languages": LANGUAGES,
            "orchestrator_agents": {"Facilitator Agent": FACILITATOR_MODEL, "Explainer Agent": EXPLAINER_MODEL}}


@app.get("/glossary")
def glossary() -> dict:
    return {"glossary": GLOSSARY, "translations": ID_EN}
