"""End-to-end: five enterprise servers + the orchestrator as separate processes.

Uses spare ports (18200-18205) and a temporary data folder, so it does not disturb a running demo.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = 18200
SERVICES = [("energytrade", "enterprises.energytrade.app:app", 4), ("solargreen", "enterprises.solargreen.app:app", 1),
            ("powergrid", "enterprises.powergrid.app:app", 2), ("industriprima", "enterprises.industriprima.app:app", 3),
            ("batteryflex", "enterprises.batteryflex.app:app", 5)]

pytestmark = pytest.mark.integration


def _wait(port: int, proc: subprocess.Popen, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        assert proc.poll() is None, f"server on port {port} exited early"
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise TimeoutError(f"port {port} not healthy")


def _uvicorn(target: str, port: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", str(port),
                             "--log-level", "warning"], cwd=ROOT, env=env)


@pytest.fixture(scope="module")
def eco(tmp_path_factory):
    data = tmp_path_factory.mktemp("see-data")
    env = {**os.environ, "SEE_DATA_DIR": str(data), "SEE_PORT_BASE": str(BASE), "PYTHONPATH": str(ROOT)}
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_data.py")], cwd=ROOT, env=env, check=True,
                   capture_output=True)
    procs: list[subprocess.Popen] = []
    try:
        for _, target, off in SERVICES:
            procs.append(_uvicorn(target, BASE + off, env))
        for (_, _, off), proc in zip(SERVICES, procs):
            _wait(BASE + off, proc)
        procs.append(_uvicorn("orchestrator.app:app", BASE, env))
        _wait(BASE, procs[-1])
        with httpx.Client(base_url=f"http://127.0.0.1:{BASE}", timeout=180) as client:
            yield client
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()


def run(eco, **body) -> dict:
    r = eco.post("/runs", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_reference_baseline_reproduces_case_study_04(eco):
    res = run(eco, scenario="normal", mode="reference")
    assert res["status"] == "COMPLETED"
    assert res["kpis"]["total_cost_rp"] == 4_400_000
    assert res["decision"]["totals_kwh"] == {"solar": 1500.0, "battery": 1000.0, "grid": 1500.0}
    assert res["interoperability_success_rate_pct"] == 100.0
    msgs = eco.get(f"/runs/{res['run_id']}/messages").json()
    protocols = {(m.get("native") or {}).get("protocol", "").split(" (")[0] for m in msgs if m.get("native")}
    assert {"SOAP 1.1/XML", "JSON-RPC 2.0", "REST/JSON Bahasa", "REST/JSON v2"} <= protocols


def test_solar_drop_adapts_with_negotiated_demand_response(eco):
    res = run(eco, scenario="solar_drop_40", mode="reference")
    t = res["decision"]["totals_kwh"]
    assert (t["solar"], t["battery"], t["grid"], t["demand_response"]) == (900.0, 1000.0, 2000.0, 100.0)
    bargaining = res["negotiation"]["bargaining"]
    assert bargaining["agreed"] and bargaining["price"] == 1741
    assert res["kpis"]["total_cost_rp"] == 4_684_100
    assert res["monitoring"]["decision_changed"]
    assert res["cost_allocation"]["shares"] == {"solargreen": 1.0}
    assert not [p for p in res["policies"] if p["status"] == "FAIL"]


def test_forecast_mode_uses_the_agents_own_models(eco):
    res = run(eco, scenario="normal", mode="forecast")
    solar = res["perception"]["solar"]
    assert solar["p10_kwh"] < solar["total_kwh"] < solar["p90_kwh"]
    assert abs(res["kpis"]["total_cost_rp"] - 4_400_000) / 4_400_000 < 0.02
    drop = run(eco, scenario="solar_drop_40", mode="forecast")
    assert abs(drop["decision"]["totals_kwh"]["solar"] - 0.6 * res["decision"]["totals_kwh"]["solar"]) <= 10
    assert drop["kpis"]["dr_kwh"] > 0 and drop["monitoring"]["decision_changed"]


def test_large_dr_requires_energy_manager_approval(eco):
    run(eco, scenario="normal", mode="reference")
    res = run(eco, scenario="demand_surge_20", mode="reference")
    assert res["status"] == "AWAITING_APPROVAL"
    ack = res["execution"]["acks"]["industriprima_dr"]
    assert ack["status"] == "PENDING_APPROVAL"
    url = f"http://127.0.0.1:{BASE + 3}/ui/approvals/{ack['approval_id']}"
    assert httpx.post(url, json={"decision": "approve", "role": "Finance Analyst"}).status_code == 403
    assert httpx.post(url, json={"decision": "approve", "role": "Energy Manager"}).status_code == 200
    synced = eco.post(f"/runs/{res['run_id']}/sync-approvals").json()
    assert synced["execution"]["acks"]["industriprima_dr"]["status"] == "ACCEPTED"
    assert synced["status"] == "COMPLETED"
    assert synced["cost_allocation"]["shares"] == {"industriprima": 1.0}   # the causer pays for its own surge


def test_cloud_outage_degrades_gracefully(eco):
    res = run(eco, scenario="solar_cloud_outage", mode="reference")
    assert res["status"] != "FAILED"
    assert any(d["enterprise"] == "solargreen" for d in res["degraded"])
    assert "solargreen" in res["execution"]["failed"]


def test_collaboration_layer_round_table_explanation_and_a2a(eco):
    """LLM switched off (tests are offline): the collaboration protocol still runs with deterministic fallbacks."""
    base = run(eco, scenario="normal", mode="reference")
    assert "deliberation" not in base and base["explanation"]["text"]           # quiet day: no round table
    res = run(eco, scenario="solar_drop_40", mode="reference", language="id")
    d = res["deliberation"]
    assert set(d["participants"]) == {"solargreen", "powergrid", "industriprima", "energytrade", "batteryflex"}
    assert all("message" in r for r in d["responses"].values()) and d["advisory"]
    phases = [t["phase"] for t in res["trace"]]
    assert phases.index("REASONING") < phases.index("DELIBERATION") < phases.index("PLANNING")
    assert phases[-1] == "EXPLANATION" and res["explanation"]["llm_ok"] is False
    assert res["kpis"]["total_cost_rp"] == 4_684_100                            # advice never changes the decision
    msgs = eco.get(f"/runs/{res['run_id']}/messages").json()
    assert sum(1 for m in msgs if m["interaction"] == "a2a-deliberation") >= 10  # request + reply per agent
    # Multilingual round table: each agent keeps its own working language; round 2 = cross-talk between agents.
    assert d["working_languages"] == {"solargreen": "zh", "powergrid": "id", "industriprima": "id",
                                      "energytrade": "en", "batteryflex": "en"}
    assert [m["round"] for m in d["conversation"]].count(1) == 5 and [m["round"] for m in d["conversation"]].count(2) == 5
    assert all(len(m["understood_by"]) == 4 for m in d["conversation"] if m["round"] == 1)
    assert sum(1 for m in msgs if m["interaction"] == "a2a-crosstalk") >= 10
    conv = eco.post("/agents/converse", json={"from_agent": "solargreen", "to_agent": "powergrid",
                                              "message": "Ask about non-firm import at 14:00"}).json()
    assert [m["from"] for m in conv["messages"]] == ["solargreen", "powergrid"]
    assert "powergrid" in conv["messages"][0]["understood_by"]
    assert len(eco.get(f"/runs/{conv['conversation_id']}/messages").json()) >= 6  # compose, converse, understand
    assert eco.post("/agents/converse", json={"from_agent": "powergrid", "to_agent": "powergrid",
                                              "message": "x"}).status_code == 422
    card = httpx.get(f"http://127.0.0.1:{BASE + 1}/.well-known/agent-card.json").json()["x-enterprise"]
    assert card["working_language"]["code"] == "zh" and card["llm"]["model"].startswith("groq:qwen")
    ans = eco.post("/agents/industriprima/ask", json={"question": "Berapa harga reservasi DR anda?"}).json()
    assert "1500" not in ans["answer"].replace(",", "").replace(".", "")        # private data never leaves
    a2a = f"http://127.0.0.1:{BASE + 5}/a2a"
    r = httpx.post(a2a, json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
        "role": "user", "parts": [{"kind": "text", "text": "How much energy can you offer?"}],
        "metadata": {"mode": "ask"}}}}, timeout=30).json()
    assert r["result"]["role"] == "agent" and r["result"]["parts"][0]["kind"] == "text"
    assert httpx.post(a2a, json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get"}).json()["error"]["code"] == -32601
    bad = httpx.post(a2a, json={"jsonrpc": "2.0", "id": 3, "method": "message/send", "params": {"message": {
        "role": "user", "parts": [{"kind": "text", "text": "x"}], "metadata": {"mode": "dance"}}}}).json()
    assert bad["error"]["code"] == -32602
    assert eco.get("/glossary").json()["translations"]["kebutuhan_kwh"] == "demand_kwh"


def test_mid_window_disruption_keeps_executed_slots(eco):
    base = run(eco, scenario="normal", mode="reference")
    res = run(eco, scenario="solar_drop_40", mode="reference", onset_slot=4)
    for r in ("solar", "battery", "grid"):
        assert res["decision"]["plan_kwh"][r][:4] == base["decision"]["plan_kwh"][r][:4]
    assert res["decision"]["totals_kwh"]["solar"] < base["decision"]["totals_kwh"]["solar"]
    eco.post("/environment", json={"scenario": "normal"})
