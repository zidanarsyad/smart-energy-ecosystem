"""SolarGreen Energy enterprise server (default port 8201).

Native interface (heterogeneous on purpose): REST/JSON with Google AIP-style custom methods,
quantities as *average AC power in kW per 15-minute interval*, timestamps in *UTC* ("Z"),
prices in IDR/kWh and carbon intensity in gCO2/kWh.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import numpy as np
import pandas as pd
from pydantic import BaseModel

from common.config import SIM_DATE, WINDOW_HOURS, WINDOW_START_HOUR, data_dir
from common.enterprise_base import UIProvider, create_enterprise_app, rows_to_table
from common.llm import QWEN
from common.stores import AuditLog
from enterprises.solargreen.forecaster import SolarForecaster

EID = "solargreen"
DATA = data_dir(EID)
audit = AuditLog(DATA / "audit.jsonl")

PROFILE = {
    "id": EID,
    "name": "SolarGreen Energy",
    "initials": "SG",
    "color": "#d97706",
    "role": "Renewable generator (400 kWp solar plant supplying the industrial estate)",
    "description": "Solar IPP that forecasts PV generation with machine learning and offers renewable energy.",
    "platform": "Google Cloud (simulated: Vertex AI + Cloud Run)",
    "database": "CSV time-series data lake (pandas)",
    "protocol": "REST/JSON, AIP-style custom methods (POST /api/v1/forecasts:generate)",
    "content_type": "application/json",
    "semantics": {"quantity": "average AC power, kW per interval", "resolution": "PT15M",
                  "timezone": "UTC (Z suffix)", "price": "IDR per kWh", "carbon": "gCO2 per kWh"},
    "paradigm": "Learning agent (machine-learning predictive)",
    "models": ["Gradient-boosted quantile regression P10/P50/P90 (HistGradientBoosting, monotonic in irradiance)"],
    "agents": [
        {"id": "solargreen.forecast_agent", "name": "Solar Forecast Agent",
         "paradigm": "ML predictive (quantile GBM) + intraday nowcast"},
        {"id": "solargreen.offer_agent", "name": "Availability & Offer Agent", "paradigm": "Rule-based pricing"},
        {"id": "solargreen.dispatch_agent", "name": "Dispatch Agent", "paradigm": "Reactive set-point executor"},
    ],
    "business_goal": "Sell as much solar energy as possible at or above the Rp850/kWh price floor "
                     "while keeping forecast error (imbalance risk) low.",
    "users": [
        {"role": "Plant Operator", "can": "monitor generation, acknowledge dispatch set-points"},
        {"role": "Data Scientist", "can": "inspect model metrics, retrain forecast model"},
        {"role": "Commercial Manager", "can": "set offer price and price floor"},
    ],
    "modules": [
        {"name": "Weather intake (NWP)", "feature": "day-ahead irradiance and temperature forecast", "model": "data ingestion"},
        {"name": "Solar generation forecasting", "feature": "P10/P50/P90 PV forecast per 15 min", "model": "quantile gradient boosting"},
        {"name": "Intraday nowcast", "feature": "persist the SCADA-observed actual/forecast ratio (cloud events)", "model": "ratio-persistence correction"},
        {"name": "Availability & offer", "feature": "offer = P50 forecast at Rp900/kWh", "model": "rule-based pricing"},
        {"name": "Dispatch & curtailment", "feature": "receive set-points, compute local utility", "model": "reactive rules"},
        {"name": "Data explorer & audit", "feature": "CSV data lake browser, JSONL audit", "model": "-"},
    ],
    "governance": [
        "Shares only aggregated availability and forecast spread; raw SCADA data never leaves the data lake.",
        "Offer price may not go below the Rp850/kWh floor.",
        "Dispatch set-points are accepted only from the ecosystem orchestrator (simulation: no real plant control).",
    ],
    "language": "zh",  # owned by a Chinese PV group: the operations agent works in Mandarin
    "llm_model": QWEN,
    "llm_vendor": "Alibaba Cloud (Qwen 3.8 27B)",
    "llm_why": "The Chinese parent company standardises on Qwen, which is strongest in Mandarin.",
    "private_data": "O&M cost, imbalance penalty and raw SCADA measurements",
}

SKILLS = [
    {"id": "solar-forecast", "name": "Solar generation forecast",
     "description": "P10/P50/P90 PV forecast, kW per 15 min, UTC", "tags": ["forecast", "ml", "solar"]},
    {"id": "solar-offer", "name": "Energy offer quote",
     "description": "Availability offer at a fixed IDR/kWh price", "tags": ["market", "offer"]},
    {"id": "solar-dispatch", "name": "Dispatch set-points", "description": "Accept kW set-points per 15 min",
     "tags": ["dispatch"]},
]

site = json.loads((DATA / "site.json").read_text(encoding="utf-8"))
forecaster = SolarForecaster(DATA / "models")
if not forecaster.load():
    raise RuntimeError("SolarGreen models missing - run `python scripts/generate_data.py` first.")
NWP_SIM = pd.read_csv(DATA / "nwp_forecast_sim_day.csv")
NWP_SIM["ts"] = pd.to_datetime(NWP_SIM["ts_utc"], utc=True)
DECLARED = pd.read_csv(DATA / "declared_availability.csv")
DECLARED["ts"] = pd.to_datetime(DECLARED["ts_utc"], utc=True)


class TimeRange(BaseModel):
    start_utc: datetime
    end_utc: datetime
    resolution: str = "PT15M"


class OfferQuote(TimeRange):
    basis: str = "forecast"  # forecast | declared


class SetpointPoint(BaseModel):
    ts_utc: datetime
    setpoint_kw: float


class DispatchRequest(BaseModel):
    offer_ref: str
    conversation_id: str | None = None
    schedule: list[SetpointPoint]
    imbalance_charge_idr: float = 0.0  # causer-pays share of the ecosystem's balancing cost


class SolarUI(UIProvider):
    def kpis(self) -> list[dict]:
        fc = _forecast_window()
        last = _read_csv("dispatch_log.csv")
        last_energy = 0.0
        if not last.empty:
            last_ref = last["offer_ref"].iloc[-1]
            last_energy = float(last.loc[last["offer_ref"] == last_ref, "setpoint_kw"].sum() * 0.25)
        m = forecaster.metrics
        return [
            {"label": "Plant capacity", "value": f"{site['kwp']:.0f} kWp"},
            {"label": "Window forecast P50", "value": f"{fc['p50'].sum() * 0.25:,.0f} kWh"},
            {"label": "P10 - P90 band", "value": f"{fc['p10'].sum() * 0.25:,.0f} - {fc['p90'].sum() * 0.25:,.0f} kWh"},
            {"label": "Model nMAE (holdout)", "value": f"{m.get('nmae_pct_of_kwp', 0)} % of kWp"},
            {"label": "P10-P90 coverage", "value": f"{m.get('p10_p90_coverage', 0) * 100:.0f} %"},
            {"label": "Last dispatched energy", "value": f"{last_energy:,.0f} kWh"},
            {"label": "Offer price", "value": f"Rp{site['offer_price_idr_kwh']:,}/kWh"},
            {"label": "Nowcast factor (env)", "value": str(app.state.env.get("nowcast_factor"))},
        ]

    def agents(self) -> list[dict]:
        return _agent_status(PROFILE["agents"], {
            "solargreen.forecast_agent": f"Quantile GBM, R2={forecaster.metrics.get('r2')}, "
                                         f"MAE={forecaster.metrics.get('mae_kw')} kW",
            "solargreen.offer_agent": "Offer = P50 forecast, price Rp900/kWh, floor Rp850/kWh",
            "solargreen.dispatch_agent": "Executes set-points, reports local utility U_i",
        })

    def tables(self) -> list[str]:
        return ["nwp_forecast_sim_day", "declared_availability", "forecasts_issued", "dispatch_log",
                "scada_generation", "nwp_forecast_archive", "model_metrics", "site"]

    def table(self, name: str, limit: int = 50) -> dict:
        if name == "model_metrics":
            return rows_to_table([{"metric": k, "value": v} for k, v in forecaster.metrics.items()], 100)
        if name == "site":
            return rows_to_table([{"key": k, "value": v} for k, v in site.items()], 100)
        df = _read_csv(f"{name}.csv")
        if name in ("scada_generation", "nwp_forecast_archive", "forecasts_issued", "dispatch_log"):
            df = df.tail(limit).iloc[::-1]
        return rows_to_table(df.head(limit).to_dict("records"), limit)

    def charts(self) -> list[dict]:
        fc = _forecast_window()
        hourly = _hourly_kwh(fc)
        charts = [{"title": "Window forecast per hour (WIB) - current environment", "unit": "kWh",
                   "labels": hourly["label"].tolist(),
                   "series": [{"name": "P50 (offered)", "type": "bar", "values": hourly["p50"].round(1).tolist()},
                              {"name": "P10", "type": "line", "values": hourly["p10"].round(1).tolist()},
                              {"name": "P90", "type": "line", "values": hourly["p90"].round(1).tolist()}]}]
        last = _read_csv("dispatch_log.csv")
        if not last.empty:
            ref = last["offer_ref"].iloc[-1]
            d = last[last["offer_ref"] == ref].copy()
            d["ts"] = pd.to_datetime(d["ts_utc"], utc=True)
            d["p50"] = d["setpoint_kw"]
            dh = _hourly_kwh(d.assign(p10=0.0, p90=0.0))
            charts.append({"title": f"Last dispatch received ({ref})", "unit": "kWh", "labels": dh["label"].tolist(),
                           "series": [{"name": "Dispatched", "type": "bar", "values": dh["p50"].round(1).tolist()}]})
        return charts

    def agent_context(self, audience: str) -> dict:
        fc = _forecast_window()
        hourly = _hourly_kwh(fc)
        p10, p50, p90 = (float(fc[q].sum() * 0.25) for q in ("p10", "p50", "p90"))
        factor = float(app.state.env.get("nowcast_factor", 1.0))
        last = _read_csv("dispatch_log.csv")
        last_kwh = 0.0
        if not last.empty:
            ref = last["offer_ref"].iloc[-1]
            last_kwh = float(last.loc[last["offer_ref"] == ref, "setpoint_kw"].sum() * 0.25)
        ctx = {
            "summary": f"SolarGreen P50 forecast {p50:,.0f} kWh for 08:00-16:00 WIB (P10 {p10:,.0f}, P90 {p90:,.0f}) "
                       f"offered at Rp{site['offer_price_idr_kwh']}/kWh; intraday nowcast factor {factor}.",
            "can_contribute": f"FIRM (already in the market): {p50:,.0f} kWh of renewable energy (P50) at "
                              f"Rp{site['offer_price_idr_kwh']}/kWh. ADDITIONAL: none (output depends on the sky); "
                              "can re-forecast every hour as the cloud event evolves",
            "plant_kwp": site["kwp"], "window": "2026-11-18 08:00-16:00 WIB",
            "hourly_p50_kwh": dict(zip(hourly["label"], hourly["p50"].round(1))),
            "window_p10_p50_p90_kwh": [round(p10), round(p50), round(p90)],
            "nowcast_factor": factor, "nowcast_note": "observed production / day-ahead forecast since the onset",
            "offer_price_rp_per_kwh": site["offer_price_idr_kwh"], "price_floor_rp_per_kwh": site["price_floor_idr_kwh"],
            "forecast_model": {k: forecaster.metrics.get(k) for k in ("model", "nmae_pct_of_kwp", "r2", "p10_p90_coverage")},
            "last_dispatched_kwh": round(last_kwh, 1),
        }
        if audience == "internal":
            ctx.update({"om_cost_rp_per_kwh": site["om_cost_idr_kwh"],
                        "imbalance_penalty_rp_per_kwh": site["imbalance_penalty_idr_kwh"]})
        return ctx

    def native_examples(self) -> list[dict]:
        return [
            {"title": "POST /api/v1/offers:quote (request)", "format": "json",
             "body": json.dumps({"start_utc": "2026-11-18T01:00:00Z", "end_utc": "2026-11-18T09:00:00Z",
                                 "basis": "forecast"}, indent=2)},
            {"title": "Response excerpt (kW per 15 min, UTC)", "format": "json",
             "body": json.dumps({"offer_ref": "SG-OFF-1a2b3c", "quantity_unit": "kW", "resolution": "PT15M",
                                 "timezone": "UTC", "price": {"amount": 900, "currency": "IDR", "per": "kWh"},
                                 "points": [{"ts_utc": "2026-11-18T01:00:00Z", "available_kw": 152.3,
                                             "p10_kw": 128.0, "p90_kw": 171.9}]}, indent=2)},
        ]


app = create_enterprise_app(PROFILE, skills=SKILLS, transport="HTTP+JSON", native_prefixes=["/api/"],
                            env_defaults={"nowcast_factor": 1.0}, ui=SolarUI(), audit=audit)


# ------------------------------------------------------------------ helpers
def _read_csv(name: str) -> pd.DataFrame:
    path = DATA / name
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def _window_start_utc() -> pd.Timestamp:
    return (pd.Timestamp(SIM_DATE) + pd.Timedelta(hours=WINDOW_START_HOUR - 7)).tz_localize("UTC")


def _factor(ts: pd.Series) -> np.ndarray:
    """Intraday nowcast correction: SCADA shows production at `nowcast_factor` x forecast since the onset
    (e.g. a cloud bank); the Forecast Agent persists that ratio over the remaining intervals."""
    env = app.state.env
    onset = _window_start_utc() + pd.Timedelta(hours=int(env.get("onset_slot", 0)))
    return np.where(ts >= onset, float(env.get("nowcast_factor", 1.0)), 1.0)


def _to_utc(value: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")


def _forecast(start: datetime, end: datetime) -> pd.DataFrame:
    """Day-ahead ML forecast from the NWP inputs, then the intraday nowcast correction."""
    rows = NWP_SIM[(NWP_SIM["ts"] >= _to_utc(start)) & (NWP_SIM["ts"] < _to_utc(end))].copy()
    pred = forecaster.predict(rows["ts"], rows["ghi_fc_wm2"], rows["temp_fc_c"])
    pred.index = rows.index
    pred = pred.mul(_factor(rows["ts"]), axis=0)
    return pd.concat([rows[["ts", "ts_utc"]], pred], axis=1)


def _forecast_window() -> pd.DataFrame:
    start = _window_start_utc()
    return _forecast(start.to_pydatetime(), (start + pd.Timedelta(hours=WINDOW_HOURS)).to_pydatetime())


def _hourly_kwh(df: pd.DataFrame) -> pd.DataFrame:
    local = df["ts"].dt.tz_convert("UTC").dt.tz_localize(None) + pd.Timedelta(hours=7)
    g = df.assign(hour=local.dt.floor("h"))[["hour", "p10", "p50", "p90"]].groupby("hour").sum() * 0.25
    g["label"] = [h.strftime("%H:%M") for h in g.index]
    return g.reset_index(drop=True)


def _agent_status(agents: list[dict], descriptions: dict) -> list[dict]:
    recent = audit.tail(300)
    out = []
    for a in agents:
        last = next((r for r in recent if r["actor"] == a["id"]), None)
        out.append({**a, "status": "active" if last else "idle", "detail": descriptions.get(a["id"], ""),
                    "last_event": last["event"] if last else None, "last_ts": last["ts"] if last else None})
    return out


def _iso_z(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ native API
@app.post("/api/v1/forecasts:generate", tags=["native API"])
def generate_forecast(req: TimeRange) -> dict:
    df = _forecast(req.start_utc, req.end_utc)
    audit.log("solargreen.forecast_agent", "forecast_generated",
              {"intervals": len(df), "p50_kwh": round(float(df["p50"].sum() * 0.25), 1),
               "nowcast_factor": app.state.env.get("nowcast_factor")})
    return {
        "site_id": site["site_id"], "quantity_unit": "kW", "resolution": "PT15M", "timezone": "UTC",
        "model": {k: forecaster.metrics.get(k) for k in ("model", "mae_kw", "nmae_pct_of_kwp", "r2",
                                                          "p10_p90_coverage")},
        "points": [{"ts_utc": _iso_z(r.ts), "p10_kw": round(r.p10, 2), "p50_kw": round(r.p50, 2),
                    "p90_kw": round(r.p90, 2)} for r in df.itertuples()],
    }


@app.post("/api/v1/offers:quote", tags=["native API"])
def quote_offer(req: OfferQuote) -> dict:
    if req.basis == "declared":
        rows = DECLARED[(DECLARED["ts"] >= _to_utc(req.start_utc)) & (DECLARED["ts"] < _to_utc(req.end_utc))].copy()
        rows["available_kw"] = rows["available_kw"] * _factor(rows["ts"])
        rows["p10"] = rows["p50"] = rows["p90"] = rows["available_kw"]
        basis = "declared availability (Case Study 04 table)"
    else:
        rows = _forecast(req.start_utc, req.end_utc)
        rows["available_kw"] = rows["p50"]
        basis = "P50 of quantile GBM forecast"
    price = max(site["offer_price_idr_kwh"], site["price_floor_idr_kwh"])
    offer_ref = "SG-OFF-" + uuid.uuid4().hex[:6]
    issued = pd.Timestamp.now(tz="UTC")
    log = rows.assign(issued_at_utc=_iso_z(issued), offer_ref=offer_ref, basis=req.basis,
                      ts_utc=[_iso_z(t) for t in rows["ts"]])
    log = log.rename(columns={"p10": "p10_kw", "p50": "p50_kw", "p90": "p90_kw"})
    log[["issued_at_utc", "offer_ref", "basis", "ts_utc", "p10_kw", "p50_kw", "p90_kw", "available_kw"]].to_csv(
        DATA / "forecasts_issued.csv", mode="a", header=False, index=False)
    energy = float(rows["available_kw"].sum() * 0.25)
    audit.log("solargreen.offer_agent", "offer_quoted",
              {"offer_ref": offer_ref, "basis": req.basis, "energy_kwh": round(energy, 1), "price_idr_kwh": price})
    return {
        "offer_ref": offer_ref, "site_id": site["site_id"], "basis": basis,
        "quantity_unit": "kW", "resolution": "PT15M", "timezone": "UTC",
        "price": {"amount": price, "currency": "IDR", "per": "kWh"},
        "carbon_intensity_gco2_per_kwh": site["carbon_gco2_kwh"],
        "issued_at_utc": _iso_z(issued),
        "points": [{"ts_utc": _iso_z(r.ts), "available_kw": round(float(r.available_kw), 2),
                    "p10_kw": round(float(r.p10), 2), "p90_kw": round(float(r.p90), 2)} for r in rows.itertuples()],
    }


@app.post("/api/v1/dispatch", tags=["native API"])
def dispatch(req: DispatchRequest) -> dict:
    received = _iso_z(pd.Timestamp.now(tz="UTC"))
    rows = [{"received_at_utc": received, "offer_ref": req.offer_ref, "conversation_id": req.conversation_id,
             "ts_utc": _iso_z(_to_utc(p.ts_utc)), "setpoint_kw": round(p.setpoint_kw, 3)} for p in req.schedule]
    pd.DataFrame(rows).to_csv(DATA / "dispatch_log.csv", mode="a", header=False, index=False)
    energy = sum(p.setpoint_kw for p in req.schedule) * 0.25
    issued = _read_csv("forecasts_issued.csv")
    spread = 0.0
    if not issued.empty:
        mine = issued[issued["offer_ref"] == req.offer_ref]
        if not mine.empty and float(mine["p50_kw"].sum()) > 0:
            ratio = energy / float(mine["p50_kw"].sum() * 0.25)
            spread = float((mine["p50_kw"] - mine["p10_kw"]).clip(lower=0).sum() * 0.25) * ratio
    revenue = site["offer_price_idr_kwh"] * energy
    cost = site["om_cost_idr_kwh"] * energy
    risk = site["imbalance_penalty_idr_kwh"] * 0.4 * spread
    utility = revenue - cost - risk - req.imbalance_charge_idr
    audit.log("solargreen.dispatch_agent", "dispatch_accepted",
              {"offer_ref": req.offer_ref, "energy_kwh": round(energy, 1), "utility_idr": round(utility),
               "imbalance_charge_idr": round(req.imbalance_charge_idr)})
    return {"status": "ACCEPTED", "offer_ref": req.offer_ref, "energy_kwh": round(energy, 2),
            "note": "Simulated set-points recorded; no real plant control.",
            "local_utility": {"revenue_idr": round(revenue), "cost_idr": round(cost),
                              "risk_penalty_idr": round(risk), "imbalance_charge_idr": round(req.imbalance_charge_idr),
                              "utility_idr": round(utility), "acceptable": utility >= 0}}
