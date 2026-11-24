"""EnergyTrade Hub enterprise server (default port 8204).

Native interface: modern REST/JSON (OpenAPI v2 resources) that already speaks the ecosystem's
canonical model (kWh per hourly slot, IDR/kWh, ISO 8601 with +07:00). The hub is the market
operator: it clears orders with an LP, publishes clearing prices and settles trades.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from pydantic import BaseModel

from common.canonical import ClearingRequest, ClearingResult, WindowSpec
from common.config import SIM_DATE, WINDOW_HOURS, WINDOW_START_HOUR, data_dir
from common.enterprise_base import UIProvider, create_enterprise_app, rows_to_table
from common.llm import NEMOTRON
from common.stores import AuditLog, SqliteStore
from common.timeutil import now_iso
from enterprises.energytrade.market import PRICE_CAP, clear_market

EID = "energytrade"
DATA = data_dir(EID)
audit = AuditLog(DATA / "audit.jsonl")
db = SqliteStore(DATA / "energytrade_market.db")

PROFILE = {
    "id": EID,
    "name": "EnergyTrade Hub",
    "initials": "ET",
    "color": "#7c3aed",
    "role": "Energy trader / market operator (bid matching, clearing prices, settlement)",
    "description": "Market hub that matches energy offers and bids with linear programming and settles trades.",
    "platform": "Amazon Web Services (simulated: Lambda + RDS)",
    "database": "SQLite order book and ledger (snake_case relational schema)",
    "protocol": "REST/JSON v2 (OpenAPI), canonical ecosystem model",
    "content_type": "application/json",
    "semantics": {"quantity": "energy in kWh per slot", "resolution": "PT1H",
                  "timezone": "ISO 8601 with +07:00 offset", "price": "IDR per kWh"},
    "paradigm": "Optimisation / market-based agent (LP + uniform-price auction)",
    "models": ["Multi-period linear programme (HiGHS) minimising J(x)", "Uniform-price auction (dual prices)",
               "Moving-average price forecast"],
    "agents": [
        {"id": "energytrade.market_clearing_agent", "name": "Market Clearing Agent", "paradigm": "LP optimisation"},
        {"id": "energytrade.price_agent", "name": "Price Forecast Agent", "paradigm": "Moving-average forecasting"},
        {"id": "energytrade.settlement_agent", "name": "Settlement Agent", "paradigm": "Deterministic ledger"},
    ],
    "business_goal": "Clear the market efficiently and transparently, earn a Rp10/kWh fee and keep a correct ledger.",
    "users": [
        {"role": "Energy Trader", "can": "inspect order book and clearing results"},
        {"role": "Market Analyst", "can": "review prices and merit order"},
        {"role": "Settlement Officer", "can": "review settlement statements"},
    ],
    "modules": [
        {"name": "Order book", "feature": "offers and bids per session", "model": "SQLite"},
        {"name": "Bid matching / clearing", "feature": "dispatch minimising J(x)", "model": "Linear Programming (HiGHS)"},
        {"name": "Price optimisation", "feature": "system marginal price per slot", "model": "LP duals (uniform-price auction)"},
        {"name": "Price forecast", "feature": "expected price curve for storage MPC", "model": "14-day moving average"},
        {"name": "Settlement", "feature": "trades, fees and net positions (simulated)", "model": "ledger arithmetic"},
    ],
    "governance": [
        "Offers above the Rp2,000/kWh price cap are rejected.",
        "Quantities are normalised to 10 kWh lots (largest remainder, total preserving).",
        "Settlement status is SIMULATED_SETTLED: no real money moves.",
        "Flexibility (DR, reserve) is a balancing cost charged to the causing parties (causer pays).",
    ],
    "language": "en",  # regional market platform: English as the working language
    "llm_model": NEMOTRON,
    "llm_vendor": "NVIDIA (Nemotron 3.5 Super)",
    "llm_why": "An AWS-hosted market platform runs NVIDIA NIM models (served by OpenRouter's free tier in this MVP).",
    "private_data": "individual participants' settlement positions and full order books",
}

SKILLS = [
    {"id": "market-clear", "name": "Market clearing", "description": "POST /api/v2/market/clear (LP)", "tags": ["market", "lp"]},
    {"id": "price-forecast", "name": "Price forecast", "description": "GET /api/v2/market/price-forecast", "tags": ["price"]},
    {"id": "settlement", "name": "Settlement", "description": "POST /api/v2/settlement", "tags": ["ledger"]},
]

RULES = {r["rule_id"]: r["value"] for r in db.query("SELECT * FROM market_rules")}


class SettlementLine(BaseModel):
    seller: str
    buyer: str
    resource: str
    interval_start: str
    quantity_kwh: float
    price_idr_kwh: float


class SettlementRequest(BaseModel):
    session_id: str
    lines: list[SettlementLine]


def _price_forecast(day: str, start_hour: int, hours: int) -> list[dict]:
    d = datetime.fromisoformat(day)
    out = []
    for i in range(hours):
        h = (start_hour + i) % 24
        rows = db.query("SELECT clearing_price_idr_kwh AS p FROM price_history WHERE substr(interval_start, 12, 2) = ? "
                        "ORDER BY interval_start DESC LIMIT 14", (f"{h:02d}",))
        price = sum(r["p"] for r in rows) / len(rows) if rows else 1200.0
        out.append({"start": (d + timedelta(hours=start_hour + i)).strftime("%Y-%m-%dT%H:%M:%S+07:00"),
                    "price_idr_kwh": round(price, 1)})
    return out


class HubUI(UIProvider):
    def _last(self) -> dict | None:
        rows = db.query("SELECT * FROM clearing_results ORDER BY created_at DESC LIMIT 1")
        return rows[0] if rows else None

    def kpis(self) -> list[dict]:
        last = self._last()
        n = db.query("SELECT COUNT(*) AS n FROM clearing_results")[0]["n"]
        vol = db.query("SELECT COALESCE(SUM(quantity_kwh),0) AS v FROM trades")[0]["v"]
        fee = db.query("SELECT COALESCE(SUM(fee_idr),0) AS f FROM settlements")[0]["f"]
        prices = json.loads(last["prices_json"]) if last else []
        return [
            {"label": "Sessions cleared", "value": str(n)},
            {"label": "Last session", "value": last["session_id"] if last else "-"},
            {"label": "Last energy cost", "value": f"Rp{last['cost_idr']:,.0f}" if last else "-"},
            {"label": "Avg marginal price (last)", "value": f"Rp{sum(prices) / len(prices):,.0f}/kWh" if prices else "-"},
            {"label": "Traded volume (settled)", "value": f"{vol:,.0f} kWh"},
            {"label": "Hub fee revenue", "value": f"Rp{fee:,.0f}"},
            {"label": "Price cap / lot", "value": f"Rp{RULES['PRICE_CAP']:,.0f} / {RULES['LOT_SIZE']:.0f} kWh"},
        ]

    def agents(self) -> list[dict]:
        recent = audit.tail(300)
        details = {"energytrade.market_clearing_agent": "HiGHS LP over 8 hourly slots, minimises J(x)",
                   "energytrade.price_agent": "14-day moving average per hour",
                   "energytrade.settlement_agent": "pay-as-bid trades, Rp10/kWh fee, simulated settlement"}
        out = []
        for a in PROFILE["agents"]:
            last = next((r for r in recent if r["actor"] == a["id"]), None)
            out.append({**a, "status": "active" if last else "idle", "detail": details[a["id"]],
                        "last_event": last["event"] if last else None, "last_ts": last["ts"] if last else None})
        return out

    def tables(self) -> list[str]:
        return ["sessions", "orders", "clearing_results", "trades", "settlements", "market_rules", "participants",
                "price_history"]

    def table(self, name: str, limit: int = 50) -> dict:
        order = {"sessions": "created_at DESC", "clearing_results": "created_at DESC", "orders": "rowid DESC",
                 "trades": "rowid DESC", "settlements": "created_at DESC", "price_history": "interval_start DESC"}
        sql = f"SELECT * FROM {name}" + (f" ORDER BY {order[name]}" if name in order else "") + " LIMIT ?"
        return rows_to_table(db.query(sql, (limit,)), limit)

    def charts(self) -> list[dict]:
        last = self._last()
        labels = WindowSpec().labels()
        fc = [p["price_idr_kwh"] for p in _price_forecast(SIM_DATE.isoformat(), WINDOW_START_HOUR, WINDOW_HOURS)]
        charts = []
        if last:
            alloc = json.loads(last["allocations_json"])
            res_of = alloc.pop("__resource_of__", {})
            by_res: dict[str, list[float]] = {}
            for oid, vals in alloc.items():
                r = res_of.get(oid, oid)
                by_res[r] = [a + b for a, b in zip(by_res.get(r, [0.0] * len(vals)), vals)]
            series = [{"name": r, "type": "bar", "values": [round(v, 1) for v in vals]} for r, vals in by_res.items()]
            charts.append({"title": f"Cleared dispatch {last['session_id']}", "unit": "kWh", "labels": labels,
                           "series": series, "stacked": True})
            charts.append({"title": "System marginal price vs forecast", "unit": "Rp/kWh", "labels": labels,
                           "series": [{"name": "Marginal price (LP dual)", "type": "line",
                                       "values": json.loads(last["prices_json"])},
                                      {"name": "Price forecast", "type": "line", "values": fc}]})
        else:
            charts.append({"title": "Price forecast", "unit": "Rp/kWh", "labels": labels,
                           "series": [{"name": "Price forecast", "type": "line", "values": fc}]})
        return charts

    def agent_context(self, audience: str) -> dict:
        last = self._last()
        fc = _price_forecast(SIM_DATE.isoformat(), WINDOW_START_HOUR, WINDOW_HOURS)
        last_info = None
        if last:
            alloc = json.loads(last["allocations_json"])
            res_of = alloc.pop("__resource_of__", {})
            by_res: dict[str, float] = {}
            for oid, vals in alloc.items():
                by_res[res_of.get(oid, oid)] = round(by_res.get(res_of.get(oid, oid), 0.0) + sum(vals), 1)
            last_info = {"session": last["session_id"], "status": last["status"], "energy_cost_rp": last["cost_idr"],
                         "dispatch_kwh": by_res, "marginal_price_rp_per_kwh": json.loads(last["prices_json"]),
                         "unserved_kwh": round(sum(json.loads(last["unserved_json"])), 1)}
        ctx = {
            "summary": "EnergyTrade Hub clears the market with a linear programme minimising J(x); "
                       + (f"last session {last['session_id']} cost Rp{last['cost_idr']:,.0f}." if last else
                          "no session cleared yet."),
            "can_contribute": "market clearing (LP), marginal prices, settlement; the hub owns no energy",
            "market_rules": {k: v for k, v in RULES.items()},
            "price_forecast_rp_per_kwh": {p["start"][11:16]: p["price_idr_kwh"] for p in fc},
            "last_session": last_info,
            "sessions_cleared": db.query("SELECT COUNT(*) AS n FROM clearing_results")[0]["n"],
        }
        if audience == "internal":
            ctx["fee_revenue_rp"] = db.query("SELECT COALESCE(SUM(fee_idr),0) AS f FROM settlements")[0]["f"]
        return ctx

    def native_examples(self) -> list[dict]:
        return [{"title": "POST /api/v2/market/clear (canonical order book, excerpt)", "format": "json",
                 "body": json.dumps({"session_id": "RUN-0001-R1", "window": {"date": "2026-11-18", "start_hour": 8,
                                                                            "hours": 8},
                                     "offers": [{"offer_id": "SG-OFF-1a2b3c", "resource": "solar",
                                                 "price_rp_per_kwh": 900, "max_kwh": [170, 200, 220, 230, 220, 190, 160, 110]}],
                                     "demand": {"demand_kwh": [420, 500, 540, 520, 440, 520, 560, 500]},
                                     "weights": {"alpha": 1, "beta": 100, "gamma": 200, "delta": 50}}, indent=2)}]


app = create_enterprise_app(PROFILE, skills=SKILLS, transport="HTTP+JSON", native_prefixes=["/api/"],
                            env_defaults={}, ui=HubUI(), audit=audit)


@app.get("/api/v2/market/rules", tags=["native API"])
def market_rules() -> list[dict]:
    return db.query("SELECT * FROM market_rules")


@app.get("/api/v2/market/price-forecast", tags=["native API"])
def price_forecast(date: str = SIM_DATE.isoformat(), start_hour: int = WINDOW_START_HOUR,
                   hours: int = WINDOW_HOURS) -> dict:
    fc = _price_forecast(date, start_hour, hours)
    audit.log("energytrade.price_agent", "price_forecast_published", {"avg": round(sum(p["price_idr_kwh"] for p in fc) / len(fc), 1)})
    return {"currency": "IDR", "unit": "IDR/kWh", "method": "14-day moving average per hour", "intervals": fc}


@app.post("/api/v2/market/clear", response_model=ClearingResult, tags=["native API"])
def clear(req: ClearingRequest) -> ClearingResult:
    result = clear_market(req, price_cap=RULES.get("PRICE_CAP", PRICE_CAP), lot=RULES.get("LOT_SIZE", 10.0))
    now = now_iso()
    db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?)",
               (req.session_id, now, req.window.start().isoformat(), req.window.hours, result.status,
                req.weights.model_dump_json()))
    rows = [(o.offer_id, req.session_id, o.enterprise, "SELL", o.resource, o.price_rp_per_kwh, json.dumps(o.max_kwh),
             round(sum(o.max_kwh), 2), o.energy_limit_kwh,
             "REJECTED" if any(r["offer_id"] == o.offer_id for r in result.rejected_offers) else "ACCEPTED")
            for o in req.offers]
    rows.append((req.demand.bid_id, req.session_id, req.demand.enterprise, "BUY", "load",
                 req.demand.max_price_rp_per_kwh, json.dumps(req.demand.demand_kwh), round(sum(req.demand.demand_kwh), 2),
                 None, "ACCEPTED"))
    db.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    alloc = dict(result.allocations)
    alloc["__resource_of__"] = result.resource_of
    db.execute("INSERT OR REPLACE INTO clearing_results VALUES (?,?,?,?,?,?,?,?)",
               (req.session_id, now, result.status, result.cost_rp, json.dumps(result.objective), json.dumps(alloc),
                json.dumps(result.clearing_price_rp_per_kwh), json.dumps(result.unserved_kwh)))
    audit.log("energytrade.market_clearing_agent", "market_cleared",
              {"session": req.session_id, "status": result.status, "cost_idr": result.cost_rp,
               "unserved_kwh": round(sum(result.unserved_kwh), 2), "J": result.objective.get("J")})
    return result


@app.get("/api/v2/market/sessions/{session_id}", tags=["native API"])
def session(session_id: str) -> dict:
    s = db.query("SELECT * FROM sessions WHERE session_id=?", (session_id,))
    if not s:
        raise HTTPException(404, "unknown session")
    return {"session": s[0], "orders": db.query("SELECT * FROM orders WHERE session_id=?", (session_id,)),
            "result": (db.query("SELECT * FROM clearing_results WHERE session_id=?", (session_id,)) or [None])[0]}


@app.post("/api/v2/settlement", tags=["native API"])
def settle(req: SettlementRequest) -> dict:
    fee_rate = RULES.get("HUB_FEE", 10.0)
    now = now_iso()
    trades, pos = [], {}
    for line in req.lines:
        if line.quantity_kwh <= 1e-9:
            continue
        amount = round(line.quantity_kwh * line.price_idr_kwh, 2)
        trades.append(("TR-" + uuid.uuid4().hex[:8], req.session_id, line.seller, line.buyer, line.resource,
                       line.interval_start, round(line.quantity_kwh, 3), line.price_idr_kwh, amount))
        s = pos.setdefault(line.seller, {"receivable": 0.0, "payable": 0.0, "fee": 0.0})
        b = pos.setdefault(line.buyer, {"receivable": 0.0, "payable": 0.0, "fee": 0.0})
        s["receivable"] += amount
        s["fee"] += fee_rate * line.quantity_kwh
        b["payable"] += amount
    db.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)", trades)
    statements = []
    for pid, p in pos.items():
        net = p["receivable"] - p["payable"] - p["fee"]
        sid = "ST-" + uuid.uuid4().hex[:8]
        db.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?,?,?,?)",
                   (sid, req.session_id, pid, round(p["receivable"], 2), round(p["payable"], 2), round(p["fee"], 2),
                    round(net, 2), "SIMULATED_SETTLED", now))
        statements.append({"settlement_id": sid, "participant": pid, "receivable_idr": round(p["receivable"]),
                           "payable_idr": round(p["payable"]), "fee_idr": round(p["fee"]), "net_idr": round(net),
                           "status": "SIMULATED_SETTLED"})
    fee_total = sum(p["fee"] for p in pos.values())
    utility = fee_total - RULES.get("SESSION_COST", 5000.0)
    audit.log("energytrade.settlement_agent", "settlement_recorded",
              {"session": req.session_id, "trades": len(trades), "fee_idr": round(fee_total)})
    return {"session_id": req.session_id, "trades": len(trades), "statements": statements,
            "local_utility": {"fee_revenue_idr": round(fee_total), "operating_cost_idr": RULES.get("SESSION_COST", 5000.0),
                              "utility_idr": round(utility), "acceptable": utility >= 0}}
