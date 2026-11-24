"""PowerGrid Distribution enterprise server (default port 8202).

Native interface (heterogeneous on purpose): a legacy SOAP 1.1 / XML web service exposed by
an on-premises EMS. Energy in MWh per interval, tariffs in IDR/MWh, timestamps as local
"dd/MM/yyyy HH:mm" strings without offset, upper-case relational schema in SQLite.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi import Request, Response

from common.config import SIM_DATE, WINDOW_HOURS, WINDOW_START_HOUR, data_dir
from common.enterprise_base import UIProvider, create_enterprise_app, rows_to_table
from common.llm import GPT_OSS_120B
from common.stores import AuditLog, SqliteStore
from common.timeutil import now_iso
from enterprises.powergrid import loadforecast
from enterprises.powergrid.rules import GridSafetyAgent
from enterprises.powergrid.soap import NS, attr, envelope, esc, fault, parse_request, text

EID = "powergrid"
DATA = data_dir(EID)
audit = AuditLog(DATA / "audit.jsonl")
db = SqliteStore(DATA / "powergrid_ems.db")
LEGACY_FMT = "%d/%m/%Y %H:%M"
# Convex loading-risk curve published with the headroom (share of headroom -> risk-kWh per kWh).
LOADING_RISK_CURVE = [(0.70, 0.0), (0.90, 0.05), (1.00, 1.0)]

PROFILE = {
    "id": EID,
    "name": "PowerGrid Distribution",
    "initials": "PG",
    "color": "#2563eb",
    "role": "Distribution system operator (feeder FDR-07, grid supply up to the 250 kW contract)",
    "description": "DSO that supplies grid energy, monitors feeder load and enforces network safety limits.",
    "platform": "On-premises legacy SCADA/EMS (private data centre)",
    "database": "SQLite relational EMS database (legacy upper-case schema)",
    "protocol": "SOAP 1.1 / XML web service (POST /soap/GridService)",
    "content_type": "text/xml",
    "semantics": {"quantity": "energy in MWh per interval", "resolution": "PT1H",
                  "timezone": "local time 'dd/MM/yyyy HH:mm' (no offset)", "price": "IDR per MWh",
                  "carbon": "kgCO2 per MWh"},
    "paradigm": "Reactive agent (condition-action safety rules with veto power)",
    "models": ["Weighted similar-day load forecasting (statistical)", "Rule engine R1-R6 (reactive)"],
    "agents": [
        {"id": "powergrid.grid_safety_agent", "name": "Grid Safety Agent", "paradigm": "Reactive rule-based"},
        {"id": "powergrid.load_forecast_agent", "name": "Load Forecast Agent", "paradigm": "Statistical forecasting"},
        {"id": "powergrid.headroom_agent", "name": "Headroom & Tariff Agent", "paradigm": "Constraint calculation"},
    ],
    "business_goal": "Serve the industrial estate within the 250 kW connection contract without "
                     "violating feeder thermal limits or the N-1 security margin.",
    "users": [
        {"role": "Grid Operator", "can": "monitor feeder, review schedule verdicts"},
        {"role": "Network Planner", "can": "edit rule parameters and derating"},
        {"role": "Key Account Manager", "can": "manage connection agreements and tariffs"},
    ],
    "modules": [
        {"name": "Load monitoring", "feature": "feeder background load history", "model": "SCADA historian"},
        {"name": "Load forecasting", "feature": "hourly background load forecast", "model": "weighted similar-day average"},
        {"name": "Headroom & tariff", "feature": "headroom = min(contract, usable - background)", "model": "constraint calculation"},
        {"name": "Constraint checking", "feature": "verdict on proposed import schedules", "model": "reactive rules R1-R6"},
        {"name": "Emergency capacity (CNP)", "feature": "respond to calls for proposals", "model": "rule R5"},
    ],
    "governance": [
        "Grid safety veto: a schedule with a VIOLATION is rejected and cannot be executed.",
        "Network topology and other customers' loads are never shared, only per-interval headroom.",
        "Non-firm emergency import is refused when feeder utilisation forecast exceeds 70% (rule R5).",
        "Real switching/control actions are outside the simulator (decision support only).",
    ],
    "language": "id",  # Indonesian distribution system operator
    "llm_model": GPT_OSS_120B,  # safety-critical veto role: the strongest reasoning model
    "llm_vendor": "OpenAI (gpt-oss-120b, open-weight)",
    "llm_why": "A regulated grid operator keeps its data on-premises, so it self-hosts an open-weight model "
               "(served by Groq in this MVP).",
    "private_data": "network topology, feeder background load of other customers and wholesale cost",
}

SKILLS = [
    {"id": "grid-headroom", "name": "Grid headroom & tariff", "description": "GetHeadroomRequest (SOAP)", "tags": ["grid"]},
    {"id": "grid-check", "name": "Schedule security check", "description": "CheckScheduleRequest (SOAP)", "tags": ["safety"]},
    {"id": "grid-cfp", "name": "Emergency capacity proposal", "description": "CallForProposalRequest (SOAP)", "tags": ["cnp"]},
    {"id": "grid-submit", "name": "Schedule submission", "description": "SubmitScheduleRequest (SOAP)", "tags": ["dispatch"]},
]

HIST = loadforecast.build_history(db.query("SELECT TS_LOCAL, LOAD_MWH FROM BG_LOAD_HIST WHERE FEEDER_ID='FDR-07'"))
MAPE = loadforecast.backtest_mape(HIST)
agent = GridSafetyAgent(db.query("SELECT * FROM GRID_RULE"))


def _feeder() -> dict:
    return db.query("SELECT * FROM FEEDER WHERE FEEDER_ID='FDR-07'")[0]


def _contract(cust_id: str) -> dict | None:
    rows = db.query("SELECT * FROM CONN_AGREEMENT WHERE CUST_ID=?", (cust_id,))
    return rows[0] if rows else None


def _intervals(frm: datetime, to: datetime) -> list[datetime]:
    out, t = [], frm
    while t < to:
        out.append(t)
        t += timedelta(hours=1)
    return out


def _grid_state(intervals: list[datetime], contract: dict) -> list[dict]:
    feeder = _feeder()
    env = app.state.env
    onset = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day, WINDOW_START_HOUR) + timedelta(
        hours=int(env.get("onset_slot", 0)))
    out = []
    for t in intervals:
        bg = loadforecast.similar_day_forecast(HIST, t.date())[t.hour]
        derate = float(env.get("feeder_derate_factor", 1.0)) if t >= onset else 1.0
        usable = feeder["THERMAL_LIMIT_KW"] * (1.0 - feeder["SECURITY_MARGIN"]) * derate
        headroom = max(0.0, min(contract["CONTRACT_KW"], usable - bg))
        out.append({"t": t, "label": t.strftime(LEGACY_FMT), "bg_kw": bg, "usable_kw": usable,
                    "headroom_kw": headroom, "derate": derate})
    return out


def _read_intervals(op) -> tuple[list[datetime], list[float]]:
    times, mwh = [], []
    for iv in op.findall("pg:Interval", NS):
        times.append(datetime.strptime(iv.get("start"), LEGACY_FMT))
        mwh.append(float(iv.get("importMWh", "0")))
    return times, mwh


# ------------------------------------------------------------------ dashboard
class GridUI(UIProvider):
    def _window(self) -> list[dict]:
        start = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day, WINDOW_START_HOUR)
        return _grid_state(_intervals(start, start + timedelta(hours=WINDOW_HOURS)), _contract("IND-PRIMA-01"))

    def _last_schedule(self) -> list[dict]:
        last = db.query("SELECT SCHED_ID FROM GRID_SCHEDULE ORDER BY RECEIVED_TS DESC LIMIT 1")
        if not last:
            return []
        return db.query("SELECT * FROM GRID_SCHEDULE WHERE SCHED_ID=? ORDER BY TS_LOCAL", (last[0]["SCHED_ID"],))

    def kpis(self) -> list[dict]:
        f, c, w = _feeder(), _contract("IND-PRIMA-01"), self._window()
        last = self._last_schedule()
        return [
            {"label": "Feeder thermal limit", "value": f"{f['THERMAL_LIMIT_KW']:.0f} kW (margin {f['SECURITY_MARGIN']:.0%})"},
            {"label": "Contract (IndustriPrima)", "value": f"{c['CONTRACT_KW']:.0f} kW"},
            {"label": "Window headroom", "value": f"{sum(x['headroom_kw'] for x in w) / 1000:.3f} MWh"},
            {"label": "Derating factor (env)", "value": str(app.state.env.get("feeder_derate_factor"))},
            {"label": "Load forecast MAPE (14 d)", "value": f"{MAPE} %"},
            {"label": "Tariff", "value": f"Rp{c['TARIFF_IDR_MWH']:,.0f}/MWh"},
            {"label": "Last schedule verdict", "value": last[0]["VERDICT"] if last else "-"},
            {"label": "Last scheduled import", "value": f"{sum(r['IMPORT_MWH'] for r in last):.3f} MWh"},
        ]

    def agents(self) -> list[dict]:
        recent = audit.tail(300)
        out = []
        for a in PROFILE["agents"]:
            last = next((r for r in recent if r["actor"] == a["id"]), None)
            out.append({**a, "status": "active" if last else "idle",
                        "detail": {"powergrid.grid_safety_agent": f"{len(agent.rules)} rules loaded from GRID_RULE",
                                   "powergrid.load_forecast_agent": f"similar-day average, backtest MAPE {MAPE}%",
                                   "powergrid.headroom_agent": "headroom = min(contract, usable - background)"}[a["id"]],
                        "last_event": last["event"] if last else None, "last_ts": last["ts"] if last else None})
        return out

    def tables(self) -> list[str]:
        return ["FEEDER", "CONN_AGREEMENT", "GRID_RULE", "GRID_SCHEDULE", "RULE_EVENT", "BG_LOAD_HIST"]

    def table(self, name: str, limit: int = 50) -> dict:
        order = {"GRID_SCHEDULE": " ORDER BY RECEIVED_TS DESC, TS_LOCAL", "RULE_EVENT": " ORDER BY EVT_TS DESC",
                 "BG_LOAD_HIST": " ORDER BY rowid DESC"}.get(name, "")
        return rows_to_table(db.query(f"SELECT * FROM {name}{order} LIMIT ?", (limit,)), limit)

    def charts(self) -> list[dict]:
        w = self._window()
        last = {r["TS_LOCAL"]: r["IMPORT_MWH"] * 1000 for r in self._last_schedule()}
        return [{"title": "Feeder FDR-07 window (local time)", "unit": "kW",
                 "labels": [x["t"].strftime("%H:%M") for x in w],
                 "series": [{"name": "Scheduled import", "type": "bar", "values": [round(last.get(x["label"], 0), 1) for x in w]},
                            {"name": "Contract headroom", "type": "line", "values": [round(x["headroom_kw"], 1) for x in w]},
                            {"name": "Background forecast", "type": "line", "values": [round(x["bg_kw"], 1) for x in w]},
                            {"name": "Usable capacity", "type": "line", "values": [round(x["usable_kw"], 1) for x in w]}]}]

    def agent_context(self, audience: str) -> dict:
        w, c, f = self._window(), _contract("IND-PRIMA-01"), _feeder()
        last = self._last_schedule()
        events = db.query("SELECT RULE_ID, SEVERITY, TS_LOCAL, MESSAGE FROM RULE_EVENT ORDER BY EVT_TS DESC LIMIT 6")
        headroom = {x["t"].strftime("%H:%M"): round(x["headroom_kw"], 1) for x in w}
        r5, r5_reason, _ = agent.evaluate_emergency_cfp([x["label"] for x in w], [x["bg_kw"] for x in w],
                                                        [x["usable_kw"] for x in w], c["CONTRACT_KW"])
        ctx = {
            "firm_offer_already_in_market_kwh": round(sum(headroom.values()), 1),
            "additional_firm_capacity_kwh": 0,
            "firm_capacity_note": "All contract headroom is already offered in the market: PowerGrid cannot add firm "
                                  "energy. Only non-firm emergency import could add more, and only if rule R5 allows.",
            "non_firm_emergency_import_available_now": r5 == "PROPOSE",
            "rule_R5_assessment": r5_reason,
            "summary": f"PowerGrid can import up to {sum(headroom.values()):,.0f} kWh to IndustriPrima in 08:00-16:00 "
                       f"(contract {c['CONTRACT_KW']:.0f} kW, tariff Rp{c['TARIFF_IDR_MWH'] / 1000:,.0f}/kWh, derating "
                       f"factor {app.state.env.get('feeder_derate_factor')}). Last verdict: "
                       f"{last[0]['VERDICT'] if last else 'none yet'}.",
            "can_contribute": f"FIRM (already in the market): grid import up to the hourly headroom "
                              f"({sum(headroom.values()):,.0f} kWh). ADDITIONAL: non-firm emergency import only if "
                              "feeder utilisation stays below 70% (rule R5)",
            "contract_kw": c["CONTRACT_KW"], "tariff_rp_per_kwh": c["TARIFF_IDR_MWH"] / 1000,
            "hourly_headroom_kw": headroom, "derating_factor": app.state.env.get("feeder_derate_factor"),
            "stress_threshold": "imports above 90% of headroom are in the stress zone",
            "safety_rules": [{"id": r["RULE_ID"], "rule": r["DESCRIPTION"], "severity": r["SEVERITY"]}
                             for r in agent.rules.values()],
            "last_schedule_verdict": last[0]["VERDICT"] if last else None,
            "last_scheduled_import_kwh": round(sum(r["IMPORT_MWH"] for r in last) * 1000, 1) if last else 0,
            "recent_rule_events": events,
        }
        if audience == "internal":
            ctx.update({"feeder_thermal_limit_kw": f["THERMAL_LIMIT_KW"], "security_margin": f["SECURITY_MARGIN"],
                        "background_load_forecast_kw": {x["t"].strftime("%H:%M"): round(x["bg_kw"], 1) for x in w},
                        "wholesale_cost_rp_per_kwh": c["WHOLESALE_IDR_MWH"] / 1000,
                        "load_forecast_mape_pct": MAPE})
        return ctx

    def native_examples(self) -> list[dict]:
        req = envelope('    <pg:GetHeadroomRequest>\n      <pg:CustomerId>IND-PRIMA-01</pg:CustomerId>\n'
                       '      <pg:From>18/11/2026 08:00</pg:From>\n      <pg:To>18/11/2026 16:00</pg:To>\n'
                       '    </pg:GetHeadroomRequest>')
        res = envelope('    <pg:GetHeadroomResponse>\n      <pg:Tariff currency="IDR" unit="IDR/MWh">1300000</pg:Tariff>\n'
                       '      <pg:Interval start="18/11/2026 08:00" end="18/11/2026 09:00" headroomMWh="0.250" '
                       'backgroundForecastMWh="0.381" usableMWh="0.900"/>\n    </pg:GetHeadroomResponse>')
        return [{"title": "SOAP request: GetHeadroomRequest", "format": "xml", "body": req},
                {"title": "SOAP response excerpt (MWh, legacy local time)", "format": "xml", "body": res}]


app = create_enterprise_app(PROFILE, skills=SKILLS, transport="SOAP/XML (legacy)", native_prefixes=["/soap/"],
                            env_defaults={"feeder_derate_factor": 1.0}, ui=GridUI(), audit=audit)


# ------------------------------------------------------------------ SOAP operations
def op_get_headroom(op) -> str:
    cust = text(op, "CustomerId")
    contract = _contract(cust)
    if contract is None:
        return fault("Client", f"Unknown customer {cust}")
    frm = datetime.strptime(text(op, "From"), LEGACY_FMT)
    to = datetime.strptime(text(op, "To"), LEGACY_FMT)
    state = _grid_state(_intervals(frm, to), contract)
    f = _feeder()
    lines = [f"    <pg:GetHeadroomResponse>",
             f"      <pg:CustomerId>{esc(cust)}</pg:CustomerId>",
             f"      <pg:Feeder id=\"FDR-07\" thermalLimitKW={attr(f['THERMAL_LIMIT_KW'])} "
             f"securityMargin={attr(f['SECURITY_MARGIN'])}/>",
             f"      <pg:Tariff currency=\"IDR\" unit=\"IDR/MWh\">{contract['TARIFF_IDR_MWH']:.0f}</pg:Tariff>",
             f"      <pg:EmissionFactor unit=\"kgCO2/MWh\">{contract['EF_KG_MWH']:.0f}</pg:EmissionFactor>",
             f"      <pg:StressThreshold>{agent.p('R2'):.2f}</pg:StressThreshold>",
             "      <pg:LoadingRiskCurve unit=\"risk-kWh per kWh\">"
             + "".join(f"<pg:Tier upToFraction=\"{u:.2f}\" risk=\"{r:.2f}\"/>" for u, r in LOADING_RISK_CURVE)
             + "</pg:LoadingRiskCurve>",
             f"      <pg:LoadForecastModel name=\"weighted similar-day average\" backtestMAPE={attr(MAPE)}/>"]
    for s in state:
        end = (s["t"] + timedelta(hours=1)).strftime(LEGACY_FMT)
        lines.append(f"      <pg:Interval start={attr(s['label'])} end={attr(end)} "
                     f"headroomMWh=\"{s['headroom_kw'] / 1000:.4f}\" "
                     f"backgroundForecastMWh=\"{s['bg_kw'] / 1000:.4f}\" usableMWh=\"{s['usable_kw'] / 1000:.4f}\" "
                     f"derating=\"{s['derate']:.2f}\"/>")
    lines.append("    </pg:GetHeadroomResponse>")
    audit.log("powergrid.headroom_agent", "headroom_published",
              {"customer": cust, "headroom_mwh": round(sum(s["headroom_kw"] for s in state) / 1000, 4),
               "derate": app.state.env.get("feeder_derate_factor")})
    return envelope("\n".join(lines))


def _check(cust: str, times: list[datetime], mwh: list[float]) -> tuple[str, list, list[dict]]:
    contract = _contract(cust)
    state = _grid_state(times, contract)
    verdict, findings = agent.check([s["label"] for s in state], [m * 1000 for m in mwh],
                                    [s["headroom_kw"] for s in state], [s["bg_kw"] for s in state],
                                    [s["usable_kw"] for s in state])
    ts = now_iso()
    db.executemany("INSERT INTO RULE_EVENT VALUES (?,?,?,?,?,?)",
                   [(ts, x.rule_id, x.severity, x.interval, x.message, cust) for x in findings])
    return verdict, findings, state


def op_check_schedule(op) -> str:
    cust = text(op, "CustomerId")
    if _contract(cust) is None:
        return fault("Client", f"Unknown customer {cust}")
    times, mwh = _read_intervals(op)
    verdict, findings, state = _check(cust, times, mwh)
    util = max(((s["bg_kw"] + m * 1000) / max(s["usable_kw"], 1e-6)) for s, m in zip(state, mwh)) if state else 0
    lines = ["    <pg:CheckScheduleResponse>", f"      <pg:Verdict>{verdict}</pg:Verdict>",
             f"      <pg:MaxFeederUtilisation>{util:.3f}</pg:MaxFeederUtilisation>"]
    for x in findings:
        lines.append(f"      <pg:Finding rule={attr(x.rule_id)} name={attr(x.name)} severity={attr(x.severity)} "
                     f"interval={attr(x.interval)}>{esc(x.message)}</pg:Finding>")
    lines.append("    </pg:CheckScheduleResponse>")
    audit.log("powergrid.grid_safety_agent", "schedule_checked",
              {"verdict": verdict, "findings": len(findings), "import_mwh": round(sum(mwh), 4)},
              level="WARN" if verdict != "APPROVED" else "INFO")
    return envelope("\n".join(lines))


def op_call_for_proposal(op) -> str:
    cust = text(op, "CustomerId")
    contract = _contract(cust)
    if contract is None:
        return fault("Client", f"Unknown customer {cust}")
    frm = datetime.strptime(text(op, "From"), LEGACY_FMT)
    to = datetime.strptime(text(op, "To"), LEGACY_FMT)
    state = _grid_state(_intervals(frm, to), contract)
    decision, reason, spare = agent.evaluate_emergency_cfp([s["label"] for s in state], [s["bg_kw"] for s in state],
                                                           [s["usable_kw"] for s in state], contract["CONTRACT_KW"])
    audit.log("powergrid.grid_safety_agent", f"cfp_{decision.lower()}", {"need_mwh": text(op, "NeedMWh"),
                                                                          "reason": reason})
    if decision == "REFUSE":
        return envelope(f"    <pg:RefuseResponse>\n      <pg:Rule>R5</pg:Rule>\n"
                        f"      <pg:Reason>{esc(reason)}</pg:Reason>\n    </pg:RefuseResponse>")
    lines = ["    <pg:ProposalResponse>",
             f"      <pg:PriceIDRPerMWh>{contract['EMERG_TARIFF_IDR_MWH']:.0f}</pg:PriceIDRPerMWh>",
             f"      <pg:Conditions>{esc(reason)}</pg:Conditions>"]
    for s, sp in zip(state, spare):
        lines.append(f"      <pg:Interval start={attr(s['label'])} maxMWh=\"{sp / 1000:.4f}\"/>")
    lines.append("    </pg:ProposalResponse>")
    return envelope("\n".join(lines))


def op_submit_schedule(op) -> str:
    cust = text(op, "CustomerId")
    contract = _contract(cust)
    if contract is None:
        return fault("Client", f"Unknown customer {cust}")
    conv = text(op, "ConversationId", "")
    times, mwh = _read_intervals(op)
    verdict, findings, state = _check(cust, times, mwh)
    sched_id = "PG-SCH-" + uuid.uuid4().hex[:6].upper()
    received = now_iso()
    db.executemany("INSERT INTO GRID_SCHEDULE VALUES (?,?,?,?,?,?,?)",
                   [(sched_id, received, cust, t.strftime(LEGACY_FMT), m, verdict, conv) for t, m in zip(times, mwh)])
    kwh = sum(mwh) * 1000
    stress = sum(max(0.0, m * 1000 - agent.p("R2") * s["headroom_kw"]) for s, m in zip(state, mwh))
    revenue = contract["TARIFF_IDR_MWH"] / 1000 * kwh
    cost = contract["WHOLESALE_IDR_MWH"] / 1000 * kwh
    risk = 150.0 * stress
    balancing = float(text(op, "BalancingChargeIDR", "0") or 0)  # DR compensation paid as balancing party
    utility = revenue - cost - risk - balancing
    acceptable = verdict != "REJECTED" and utility >= 0
    audit.log("powergrid.grid_safety_agent", "schedule_submitted",
              {"schedule_id": sched_id, "verdict": verdict, "import_mwh": round(sum(mwh), 4),
               "utility_idr": round(utility)})
    return envelope(
        f"    <pg:SubmitScheduleResponse>\n"
        f"      <pg:Ack scheduleId={attr(sched_id)} verdict={attr(verdict)} simulated=\"true\"/>\n"
        f"      <pg:LocalUtility revenueIDR=\"{revenue:.0f}\" costIDR=\"{cost:.0f}\" riskPenaltyIDR=\"{risk:.0f}\" "
        f"balancingChargeIDR=\"{balancing:.0f}\" utilityIDR=\"{utility:.0f}\" "
        f"acceptable=\"{str(acceptable).lower()}\"/>\n"
        f"    </pg:SubmitScheduleResponse>")


HANDLERS = {
    "GetHeadroomRequest": op_get_headroom,
    "CheckScheduleRequest": op_check_schedule,
    "CallForProposalRequest": op_call_for_proposal,
    "SubmitScheduleRequest": op_submit_schedule,
}


@app.post("/soap/GridService", tags=["native API (SOAP)"])
async def soap_service(request: Request) -> Response:
    """Single SOAP endpoint; the operation is the first element of the SOAP Body."""
    body = await request.body()
    try:
        name, op = parse_request(body)
    except Exception as exc:  # malformed XML
        return Response(fault("Client", f"Malformed SOAP envelope: {exc}"), status_code=500,
                        media_type="text/xml; charset=utf-8")
    handler = HANDLERS.get(name)
    if handler is None:
        return Response(fault("Client", f"Unknown operation {name}"), status_code=500,
                        media_type="text/xml; charset=utf-8")
    xml = handler(op)
    status = 500 if "<soap:Fault>" in xml else 200
    return Response(xml, status_code=status, media_type="text/xml; charset=utf-8")


@app.get("/soap/GridService.wsdl-lite", tags=["native API (SOAP)"])
def wsdl_lite() -> dict:
    """Human-readable operation list (stand-in for a WSDL)."""
    return {"endpoint": "/soap/GridService", "namespace": NS["pg"], "operations": list(HANDLERS),
            "time_format": "dd/MM/yyyy HH:mm (local, no offset)", "energy_unit": "MWh", "price_unit": "IDR/MWh"}
