"""BatteryFlex Storage enterprise server (default port 8205).

Native interface (heterogeneous on purpose): JSON-RPC 2.0 over HTTP (POST /rpc, method names
such as `bess.plan_offer`), power in kW per step, state of charge in *percent*, timestamps as
*Unix epoch seconds*, telemetry kept in an append-only JSON Lines store at the edge.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from common.config import SIM_DATE, WINDOW_HOURS, WINDOW_START_HOUR, data_dir
from common.enterprise_base import UIProvider, create_enterprise_app, rows_to_table
from common.llm import GEMINI_31_FLASH_LITE
from common.stores import AuditLog, JsonlStore
from common.timeutil import now_iso
from enterprises.batteryflex.mpc import economic_plan, track_schedule

EID = "batteryflex"
DATA = data_dir(EID)
audit = AuditLog(DATA / "audit.jsonl")
telemetry = JsonlStore(DATA / "telemetry.jsonl")
schedules = JsonlStore(DATA / "schedules.jsonl")
CFG = json.loads((DATA / "bess_config.json").read_text(encoding="utf-8"))
WIB = timezone(timedelta(hours=7))
WINDOW_T0 = int(datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day, WINDOW_START_HOUR, tzinfo=WIB).timestamp())

PROFILE = {
    "id": EID,
    "name": "BatteryFlex Storage",
    "initials": "BF",
    "color": "#db2777",
    "role": "Battery energy storage operator (1,250 kWh / 250 kW LFP BESS)",
    "description": "Storage operator that schedules battery discharge with model predictive control.",
    "platform": "Private cloud / edge controller (simulated)",
    "database": "JSON Lines append-only telemetry store + JSON config",
    "protocol": "JSON-RPC 2.0 over HTTP (POST /rpc)",
    "content_type": "application/json-rpc",
    "semantics": {"quantity": "power in kW per step (step_s seconds)", "state": "state of charge in % of capacity",
                  "timezone": "Unix epoch seconds", "price": "IDR per kWh"},
    "paradigm": "Model Predictive Control agent (receding-horizon optimisation)",
    "models": ["Economic MPC (LP) for the offer envelope", "Tracking MPC (receding-horizon LP) for dispatch"],
    "agents": [
        {"id": "batteryflex.mpc_agent", "name": "MPC Dispatch Agent", "paradigm": "Model Predictive Control"},
        {"id": "batteryflex.offer_agent", "name": "Storage Offer Agent", "paradigm": "Cost-based pricing"},
        {"id": "batteryflex.health_agent", "name": "Battery Health Monitor", "paradigm": "Reactive rules"},
    ],
    "business_goal": "Earn a margin above charging + degradation cost while never violating SOC limits.",
    "users": [
        {"role": "Storage Operator", "can": "monitor SOC, accept dispatch schedules"},
        {"role": "Asset Manager", "can": "set price and degradation parameters"},
    ],
    "modules": [
        {"name": "Battery SOC monitoring", "feature": "telemetry (SOC %, kW, temperature)", "model": "JSONL time series"},
        {"name": "Offer planning", "feature": "energy-limited offer at reservation-based price", "model": "economic MPC (LP)"},
        {"name": "Discharge scheduling", "feature": "track ecosystem dispatch, keep SOC >= 20%", "model": "receding-horizon MPC"},
        {"name": "Emergency reserve (CNP)", "feature": "10-20% SOC band only with grid authorisation", "model": "policy rule"},
        {"name": "Health monitoring", "feature": "availability / rack faults", "model": "reactive rules"},
    ],
    "governance": [
        "SOC must stay >= 20% in normal operation; 10-20% only as authorised emergency reserve.",
        "Maximum discharge 250 kW (reduced proportionally during rack faults).",
        "Offer price >= reservation price (charging cost / efficiency + degradation).",
    ],
    "language": "en",
    "llm_model": GEMINI_31_FLASH_LITE,
    "llm_vendor": "Google (Gemini 3.1 Flash-Lite)",
    "llm_why": "The edge fleet gateway needs a small, low-latency model.",
    "private_data": "reservation price, charging cost and degradation cost",
}

SKILLS = [
    {"id": "bess-state", "name": "Battery state", "description": "bess.get_state", "tags": ["telemetry"]},
    {"id": "bess-offer", "name": "Offer envelope (economic MPC)", "description": "bess.plan_offer", "tags": ["mpc", "offer"]},
    {"id": "bess-reserve", "name": "Emergency reserve proposal", "description": "bess.cfp_reserve", "tags": ["cnp"]},
    {"id": "bess-schedule", "name": "Dispatch schedule (tracking MPC)", "description": "bess.set_schedule", "tags": ["mpc"]},
]


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _reservation_price() -> float:
    return round(CFG["charge_cost_idr_kwh"] / CFG["round_trip_efficiency"] + CFG["degradation_idr_kwh"], 1)


def _step_limits(steps: int) -> tuple[list[float], float]:
    """Per-step power limit and window energy limit given the current (simulated) availability."""
    env = app.state.env
    avail = float(env.get("availability", 1.0))
    onset = int(env.get("onset_slot", 0))
    p_max = [CFG["p_max_kw"] * (avail if k >= onset else 1.0) for k in range(steps)]
    usable = (CFG["soc_initial_pct"] - CFG["soc_min_pct"]) / 100 * CFG["capacity_kwh_nominal"]
    used_before = 0.0
    last = _last_schedule()
    if onset > 0 and last:
        used_before = sum(last["discharge_kw"][:onset])
    energy = used_before + (usable - used_before) * (avail if onset < steps else 1.0)
    return p_max, round(energy, 3)


def _last_schedule() -> dict | None:
    rows = schedules.read()
    return rows[-1] if rows else None


def m_get_state(params: dict) -> dict:
    env = app.state.env
    avail = float(env.get("availability", 1.0))
    return {"asset_id": CFG["asset_id"], "ts": WINDOW_T0, "soc_pct": CFG["soc_initial_pct"],
            "capacity_kwh": round(CFG["capacity_kwh_nominal"] * avail, 1),
            "capacity_kwh_nominal": CFG["capacity_kwh_nominal"],
            "p_discharge_max_kw": round(CFG["p_max_kw"] * avail, 1), "soc_min_pct": CFG["soc_min_pct"],
            "soc_emergency_min_pct": CFG["soc_emergency_min_pct"], "availability": avail,
            "temperature_c": 27.5, "chemistry": CFG["chemistry"]}


def m_plan_offer(params: dict) -> dict:
    steps = int(params.get("steps", WINDOW_HOURS))
    step_s = int(params.get("step_s", 3600))
    prices = params.get("price_forecast_idr_kwh")
    if not prices or len(prices) != steps:
        raise RpcError(-32602, "price_forecast_idr_kwh must have `steps` values")
    p_max, energy = _step_limits(steps)
    plan = economic_plan(CFG["soc_initial_pct"], CFG["capacity_kwh_nominal"], CFG["soc_min_pct"], p_max,
                         [float(p) for p in prices], _reservation_price(), energy_limit_kwh=energy, dt=step_s / 3600)
    audit.log("batteryflex.mpc_agent", "offer_planned",
              {"energy_kwh": energy, "local_plan_kw": plan["discharge_kw"], "margin_idr": plan["expected_margin_idr"]})
    return {"t_start": int(params.get("t_start", WINDOW_T0)), "step_s": step_s,
            "offer": {"price_idr_kwh": CFG["offer_price_idr_kwh"], "reservation_idr_kwh": _reservation_price(),
                      "max_kw": [round(p, 2) for p in p_max], "energy_kwh": energy},
            "local_mpc_plan": plan, "availability": app.state.env.get("availability", 1.0)}


def m_cfp_reserve(params: dict) -> dict:
    steps = int(params.get("steps", WINDOW_HOURS))
    avail = float(app.state.env.get("availability", 1.0))
    if avail < 0.3:
        audit.log("batteryflex.health_agent", "cfp_reserve_refused", {"availability": avail})
        return {"decision": "REFUSE", "reason": f"availability {avail:.0%} too low for deep discharge"}
    energy = (CFG["soc_min_pct"] - CFG["soc_emergency_min_pct"]) / 100 * CFG["capacity_kwh_nominal"] * avail
    p_max, _ = _step_limits(steps)
    audit.log("batteryflex.offer_agent", "cfp_reserve_proposed", {"energy_kwh": energy})
    return {"decision": "PROPOSE", "energy_kwh": round(energy, 2), "price_idr_kwh": CFG["reserve_price_idr_kwh"],
            "max_kw": p_max, "risk_per_kwh": CFG["reserve_risk_per_kwh"],
            "conditions": "SOC band 10-20% (deep discharge); requires grid emergency authorisation"}


def m_set_schedule(params: dict) -> dict:
    d = [float(v) for v in params.get("discharge_kw", [])]
    reserve = [float(v) for v in params.get("reserve_kw", [0.0] * len(d))]
    if not d:
        raise RpcError(-32602, "discharge_kw required")
    steps = len(d)
    p_max, _ = _step_limits(steps)
    ref = [a + b for a, b in zip(d, reserve)]
    soc_floor = CFG["soc_emergency_min_pct"] if sum(reserve) > 0 else CFG["soc_min_pct"]
    cap = CFG["capacity_kwh_nominal"] * float(app.state.env.get("availability", 1.0)) \
        if int(app.state.env.get("onset_slot", 0)) == 0 else CFG["capacity_kwh_nominal"]
    track = track_schedule(CFG["soc_initial_pct"], cap, soc_floor, p_max, ref)
    rec = {"received": now_iso(), "conversation_id": params.get("conversation_id"),
           "t_start": int(params.get("t_start", WINDOW_T0)), "step_s": int(params.get("step_s", 3600)),
           "discharge_kw": d, "reserve_kw": reserve, "executed_kw": track["executed_kw"], "soc_pct": track["soc_pct"],
           "max_tracking_error_kw": track["max_tracking_error_kw"], "feasible": track["feasible"]}
    schedules.append(rec)
    energy, res_energy = sum(d), sum(reserve)
    unit_cost = CFG["charge_cost_idr_kwh"] / CFG["round_trip_efficiency"] + CFG["degradation_idr_kwh"]
    revenue = CFG["offer_price_idr_kwh"] * energy + CFG["reserve_price_idr_kwh"] * res_energy
    cost = unit_cost * (energy + res_energy)
    risk = CFG["deep_discharge_penalty_idr_kwh"] * res_energy
    charge = float(params.get("balancing_charge_idr", 0.0))  # causer-pays share (e.g. after a rack fault)
    utility = revenue - cost - risk - charge
    audit.log("batteryflex.mpc_agent", "schedule_tracked",
              {"energy_kwh": round(energy + res_energy, 1), "final_soc_pct": track["soc_pct"][-1],
               "max_tracking_error_kw": track["max_tracking_error_kw"]})
    return {"accepted": track["feasible"], "executed_kw": track["executed_kw"], "soc_pct": track["soc_pct"],
            "max_tracking_error_kw": track["max_tracking_error_kw"], "method": track["method"],
            "local_utility": {"revenue_idr": round(revenue), "cost_idr": round(cost), "risk_penalty_idr": round(risk),
                              "balancing_charge_idr": round(charge), "utility_idr": round(utility),
                              "acceptable": utility >= 0}}


def m_cfp_result(params: dict) -> dict:
    accepted = bool(params.get("accepted"))
    audit.log("batteryflex.offer_agent", "cfp_reserve_" + ("accepted" if accepted else "rejected"),
              {"conversation_id": params.get("conversation_id"), "energy_kwh": params.get("energy_kwh"),
               "reason": params.get("reason")})
    return {"acknowledged": True, "accepted": accepted}


METHODS = {"bess.get_state": m_get_state, "bess.plan_offer": m_plan_offer, "bess.cfp_reserve": m_cfp_reserve,
           "bess.cfp_result": m_cfp_result, "bess.set_schedule": m_set_schedule}


class StorageUI(UIProvider):
    def kpis(self) -> list[dict]:
        st = m_get_state({})
        last = _last_schedule()
        return [
            {"label": "SOC at window start", "value": f"{st['soc_pct']:.0f} %"},
            {"label": "Usable energy (SOC >= 20%)", "value": f"{_step_limits(WINDOW_HOURS)[1]:,.0f} kWh"},
            {"label": "Max discharge", "value": f"{st['p_discharge_max_kw']:.0f} kW"},
            {"label": "Availability (env)", "value": f"{st['availability']:.0%}"},
            {"label": "Offer / reservation price", "value": f"Rp{CFG['offer_price_idr_kwh']:,.0f} / Rp{_reservation_price():,.0f}"},
            {"label": "Last schedule energy", "value": f"{sum(last['discharge_kw']) + sum(last['reserve_kw']):,.0f} kWh" if last else "-"},
            {"label": "Final SOC (last schedule)", "value": f"{last['soc_pct'][-1]:.1f} %" if last else "-"},
            {"label": "MPC max tracking error", "value": f"{last['max_tracking_error_kw']:.2f} kW" if last else "-"},
        ]

    def agents(self) -> list[dict]:
        recent = audit.tail(300)
        details = {"batteryflex.mpc_agent": "economic + receding-horizon tracking MPC (HiGHS)",
                   "batteryflex.offer_agent": f"reservation = {CFG['charge_cost_idr_kwh']}/{CFG['round_trip_efficiency']} + "
                                              f"{CFG['degradation_idr_kwh']} = Rp{_reservation_price():,.0f}/kWh",
                   "batteryflex.health_agent": "availability & SOC-band rules"}
        out = []
        for a in PROFILE["agents"]:
            last = next((r for r in recent if r["actor"] == a["id"]), None)
            out.append({**a, "status": "active" if last else "idle", "detail": details[a["id"]],
                        "last_event": last["event"] if last else None, "last_ts": last["ts"] if last else None})
        return out

    def tables(self) -> list[str]:
        return ["schedules", "telemetry", "bess_config"]

    def table(self, name: str, limit: int = 50) -> dict:
        if name == "bess_config":
            return rows_to_table([{"key": k, "value": v} for k, v in CFG.items()], 100)
        store = schedules if name == "schedules" else telemetry
        return rows_to_table(store.tail(limit), limit)

    def charts(self) -> list[dict]:
        last = _last_schedule()
        labels = [f"{WINDOW_START_HOUR + i:02d}:00" for i in range(WINDOW_HOURS)]
        if not last:
            return [{"title": "No dispatch schedule received yet", "unit": "kW", "labels": labels,
                     "series": [{"name": "Discharge", "type": "bar", "values": [0] * WINDOW_HOURS}]}]
        ref = [a + b for a, b in zip(last["discharge_kw"], last["reserve_kw"])]
        return [{"title": "Ecosystem dispatch vs MPC execution", "unit": "kW", "labels": labels,
                 "series": [{"name": "Dispatched (reference)", "type": "bar", "values": ref},
                            {"name": "MPC executed", "type": "line", "values": last["executed_kw"]}]},
                {"title": "State of charge trajectory", "unit": "%", "labels": ["08:00"] + [f"{WINDOW_START_HOUR + i + 1:02d}:00"
                                                                                     for i in range(WINDOW_HOURS)],
                 "series": [{"name": "SOC", "type": "line", "values": last["soc_pct"]},
                            {"name": "SOC min", "type": "line", "values": [CFG["soc_min_pct"]] * (WINDOW_HOURS + 1)}]}]

    def agent_context(self, audience: str) -> dict:
        st = m_get_state({})
        p_max, energy = _step_limits(WINDOW_HOURS)
        last = _last_schedule()
        reserve = (CFG["soc_min_pct"] - CFG["soc_emergency_min_pct"]) / 100 * CFG["capacity_kwh_nominal"] * st["availability"]
        ctx = {
            "summary": f"BatteryFlex has {energy:,.0f} kWh usable above the 20% SOC floor, max {max(p_max):.0f} kW, "
                       f"availability {st['availability']:.0%}, offered at Rp{CFG['offer_price_idr_kwh']:,.0f}/kWh; "
                       f"emergency reserve {reserve:,.0f} kWh only with grid authorisation.",
            "can_contribute": f"FIRM (already in the market): {energy:,.0f} kWh at up to {max(p_max):.0f} kW per hour, "
                              f"Rp{CFG['offer_price_idr_kwh']:,.0f}/kWh. ADDITIONAL: {reserve:,.0f} kWh deep-discharge "
                              f"reserve at Rp{CFG['reserve_price_idr_kwh']:,.0f}/kWh, only if PowerGrid authorises an "
                              "emergency, sharing the same inverter power limit",
            "soc_pct_at_window_start": st["soc_pct"], "capacity_kwh": st["capacity_kwh"], "availability": st["availability"],
            "max_power_kw_per_hour": [round(p, 1) for p in p_max], "usable_energy_kwh": energy,
            "soc_floor_pct": CFG["soc_min_pct"], "emergency_band_pct": "10-20",
            "offer_price_rp_per_kwh": CFG["offer_price_idr_kwh"], "reserve_price_rp_per_kwh": CFG["reserve_price_idr_kwh"],
            "last_schedule": ({"dispatched_kwh": round(sum(last["discharge_kw"]) + sum(last["reserve_kw"]), 1),
                               "final_soc_pct": last["soc_pct"][-1],
                               "max_tracking_error_kw": last["max_tracking_error_kw"]} if last else None),
        }
        if audience == "internal":
            ctx.update({"reservation_price_rp_per_kwh": _reservation_price(),
                        "charge_cost_rp_per_kwh": CFG["charge_cost_idr_kwh"],
                        "degradation_cost_rp_per_kwh": CFG["degradation_idr_kwh"],
                        "round_trip_efficiency": CFG["round_trip_efficiency"]})
        return ctx

    def native_examples(self) -> list[dict]:
        return [{"title": "JSON-RPC request", "format": "json",
                 "body": json.dumps({"jsonrpc": "2.0", "id": 7, "method": "bess.plan_offer",
                                     "params": {"t_start": WINDOW_T0, "step_s": 3600, "steps": 8,
                                                "price_forecast_idr_kwh": [1180, 1220, 1260, 1280, 1250, 1270, 1290, 1270]}},
                                    indent=2)},
                {"title": "JSON-RPC response (excerpt)", "format": "json",
                 "body": json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"offer": {"price_idr_kwh": 1100,
                                                                                    "max_kw": [250] * 8,
                                                                                    "energy_kwh": 1000}}}, indent=2)}]


app = create_enterprise_app(PROFILE, skills=SKILLS, transport="JSONRPC", native_prefixes=["/rpc"],
                            env_defaults={"availability": 1.0}, ui=StorageUI(), audit=audit)


@app.post("/rpc", tags=["native API (JSON-RPC 2.0)"])
async def rpc(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None,
                             "error": {"code": -32600, "message": "Invalid Request"}})
    method = METHODS.get(body["method"])
    if method is None:
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"),
                             "error": {"code": -32601, "message": f"Method not found: {body['method']}"}})
    try:
        result = method(body.get("params") or {})
    except RpcError as exc:
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": exc.code, "message": exc.message}})
    except Exception as exc:  # pragma: no cover - defensive
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32000, "message": str(exc)}})
    return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
