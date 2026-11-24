"""IndustriPrima enterprise server (default port 8203).

Native interface (heterogeneous on purpose): REST/JSON whose resources and fields are in
Bahasa Indonesia (`kebutuhan_kwh`, `beban_fleksibel_kwh`), local timestamps without offset
("2026-11-18 08:00"), energy in kWh, plus an OpenADR 3.0-inspired endpoint (`/openadr3/events`)
where the estate acts as a VEN receiving demand-response events.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

from fastapi import Body, HTTPException
from pydantic import BaseModel

from common.canonical import REFERENCE_CASE
from common.config import SIM_DATE, WINDOW_HOURS, WINDOW_START_HOUR, data_dir
from common.enterprise_base import UIProvider, create_enterprise_app, rows_to_table
from common.glossary import translate
from common.llm import GPT_OSS_20B
from common.stores import AuditLog, JsonDocStore
from common.timeutil import now_iso
from enterprises.industriprima.bdi import allocate_to_processes, build_energy_agent
from enterprises.industriprima.forecaster import DemandForecaster

EID = "industriprima"
DATA = data_dir(EID)
audit = AuditLog(DATA / "audit.jsonl")
store = JsonDocStore(DATA / "industriprima_docs.json")         # operational collections
history = JsonDocStore(DATA / "industriprima_history.json")    # hourly load history (read-mostly)
LOCAL_FMT = "%Y-%m-%d %H:%M"

PROFILE = {
    "id": EID,
    "name": "IndustriPrima",
    "initials": "IP",
    "color": "#059669",
    "role": "Industrial estate (energy consumer with flexible load / demand response)",
    "description": "Industrial estate that plans its demand, schedules flexible loads and negotiates demand response.",
    "platform": "Microsoft Azure (simulated: ERP/MES on Azure App Service)",
    "database": "JSON document store (NoSQL collections, Bahasa Indonesia)",
    "protocol": "REST/JSON in Bahasa Indonesia + OpenADR 3.0-inspired VEN endpoint",
    "content_type": "application/json",
    "semantics": {"quantity": "energy in kWh per hour", "resolution": "PT1H",
                  "timezone": "local WIB without offset ('YYYY-MM-DD HH:MM')", "price": "Rp per kWh",
                  "field_language": "Bahasa Indonesia (kebutuhan_kwh, beban_fleksibel_kwh, ...)"},
    "paradigm": "BDI agent (Belief-Desire-Intention practical reasoning)",
    "models": ["Ridge regression ARX demand forecast", "BDI plan library with Boulware concession tactic"],
    "agents": [
        {"id": "industriprima.bdi_energy_agent", "name": "BDI Energy Agent", "paradigm": "BDI practical reasoning"},
        {"id": "industriprima.demand_forecast_agent", "name": "Demand Forecast Agent", "paradigm": "Time-series regression (ARX)"},
    ],
    "business_goal": "Meet the production plan at the lowest energy cost, offer flexible load when it is "
                     "fairly compensated, and reach a 30% renewable share.",
    "users": [
        {"role": "Energy Manager", "can": "approve/reject demand-response events above 200 kWh"},
        {"role": "Production Planner", "can": "maintain production plan and urgent-order flag"},
        {"role": "Finance Analyst", "can": "review energy cost and DR compensation"},
    ],
    "modules": [
        {"name": "Demand planning", "feature": "hourly demand forecast from production plan", "model": "Ridge ARX"},
        {"name": "Flexible load scheduling", "feature": "allocate DR to flexible processes by loss value", "model": "greedy merit order"},
        {"name": "DR negotiation", "feature": "Contract Net participant + alternating offers", "model": "BDI + Boulware tactic"},
        {"name": "DR participation (VEN)", "feature": "receive OpenADR-style events, opt-in/opt-out", "model": "BDI plans"},
        {"name": "Approvals (human-in-the-loop)", "feature": "Energy Manager approval for large DR", "model": "policy rule"},
    ],
    "governance": [
        "Curtailment is capped at 10% of hourly load; critical load is never offered.",
        "DR events above 200 kWh require Energy Manager approval (human-in-the-loop).",
        "Reservation price and process loss values are private and never shared.",
        "Urgent production orders override demand response (opt-out).",
    ],
    "language": "id",  # Indonesian manufacturer: native schema and agent messages in Bahasa Indonesia
    "llm_model": GPT_OSS_20B,
    "llm_vendor": "OpenAI (gpt-oss-20b)",
    "llm_why": "An Azure customer uses an OpenAI model from the Azure AI model catalogue (served by Groq in this MVP).",
    "private_data": "DR reservation price, initial ask, process loss values and negotiation strategy",
}

SKILLS = [
    {"id": "rencana-beban", "name": "Demand plan", "description": "GET /api/v1/rencana-beban (kWh, Bahasa)", "tags": ["demand"]},
    {"id": "negosiasi-dr", "name": "DR negotiation", "description": "POST /api/v1/negosiasi/dr (CNP + bargaining)", "tags": ["negotiation"]},
    {"id": "openadr-ven", "name": "OpenADR-style VEN", "description": "POST /openadr3/events", "tags": ["demand-response"]},
    {"id": "jadwal-pasokan", "name": "Supply schedule", "description": "POST /api/v1/jadwal-pasokan", "tags": ["dispatch"]},
]

NOMINAL = {d["jenis_hari"]: d["beban_kwh_per_jam"] for d in store.all("profil_beban_nominal")}
POLICY = store.all("kebijakan_energi")[0]
PROCESSES = store.all("proses_fleksibel")
forecaster = DemandForecaster(NOMINAL)
forecaster.fit(history.all("riwayat_beban"))
agent = build_energy_agent(POLICY, PROCESSES)


class PesanNegosiasi(BaseModel):
    jenis_pesan: str                     # CFP | TAWARAN | TERIMA | TOLAK
    id_percakapan: str
    putaran: int = 0
    kebutuhan_kwh: float | None = None
    jam_diizinkan: list[str] | None = None
    harga_tawaran_rp_per_kwh: float | None = None
    kuantitas_kwh: float | None = None


class IntervalPasokan(BaseModel):
    jam: str
    surya_kwh: float = 0
    baterai_kwh: float = 0
    jaringan_kwh: float = 0
    cadangan_baterai_kwh: float = 0
    dr_kwh: float = 0
    tidak_terlayani_kwh: float = 0


class JadwalPasokan(BaseModel):
    id_percakapan: str
    tanggal: str
    interval: list[IntervalPasokan]
    biaya_energi_rp: float
    kompensasi_dr_rp: float = 0
    biaya_penyeimbangan_rp: float = 0  # causer-pays share of the balancing (flexibility) cost
    pangsa_terbarukan_persen: float = 0


def _window_hours() -> list[str]:
    start = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day, WINDOW_START_HOUR)
    return [(start + timedelta(hours=i)).strftime(LOCAL_FMT) for i in range(WINDOW_HOURS)]


def _demand_plan(tanggal: str, jam_mulai: int, durasi: int, mode: str) -> dict:
    env = app.state.env
    plan = store.find_one("rencana_produksi", tanggal=tanggal)
    if plan is None:
        raise HTTPException(404, f"Tidak ada rencana produksi untuk {tanggal}")
    if mode == "deklarasi" and jam_mulai == WINDOW_START_HOUR and durasi == WINDOW_HOURS:
        values = [float(v) for v in REFERENCE_CASE["demand_kwh"]]
        basis = "deklarasi (tabel Case Study 04)"
    else:
        fc = forecaster.forecast_day(date.fromisoformat(tanggal), plan["indeks_produksi"], plan["suhu_prakiraan_c"])
        values = fc[jam_mulai:jam_mulai + durasi]
        basis = "prakiraan Ridge ARX"
    onset = int(env.get("onset_slot", 0))
    factor = float(env.get("demand_factor", 1.0))
    values = [v * (factor if i >= onset else 1.0) for i, v in enumerate(values)]
    cap = POLICY["batas_kurtailmen_persen"] / 100.0
    start = datetime.fromisoformat(tanggal) + timedelta(hours=jam_mulai)
    rows = [{"jam": (start + timedelta(hours=i)).strftime(LOCAL_FMT), "kebutuhan_kwh": round(v, 2),
             "beban_kritis_kwh": round(v * (1 - cap), 2), "beban_fleksibel_kwh": round(v * cap, 2)}
            for i, v in enumerate(values)]
    agent.beliefs.update({"prakiraan_window_kwh": round(sum(values), 1),
                          "fleksibel_per_jam": [r["beban_fleksibel_kwh"] for r in rows],
                          "fleksibel_total_kwh": round(sum(r["beban_fleksibel_kwh"] for r in rows), 1),
                          "jam_window": [r["jam"] for r in rows],
                          "pesanan_mendesak": bool(env.get("urgent_order") or plan.get("pesanan_mendesak"))})
    return {"basis": basis, "rows": rows, "plan": plan}


def _ensure_beliefs() -> None:
    if not agent.beliefs.get("fleksibel_per_jam"):
        _demand_plan(SIM_DATE.isoformat(), WINDOW_START_HOUR, WINDOW_HOURS, "prakiraan")
    agent.beliefs["pesanan_mendesak"] = bool(app.state.env.get("urgent_order"))


# ------------------------------------------------------------------ dashboard
class EstateUI(UIProvider):
    def kpis(self) -> list[dict]:
        d = _demand_plan(SIM_DATE.isoformat(), WINDOW_START_HOUR, WINDOW_HOURS, "prakiraan")
        events = store.all("event_dr")
        pending = [a for a in store.all("persetujuan") if a["status"] == "MENUNGGU"]
        sched = store.all("jadwal_pasokan")
        return [
            {"label": "Window demand forecast", "value": f"{sum(r['kebutuhan_kwh'] for r in d['rows']):,.0f} kWh"},
            {"label": "Flexible load (<=10%)", "value": f"{sum(r['beban_fleksibel_kwh'] for r in d['rows']):,.0f} kWh"},
            {"label": "Forecast MAPE (30 d)", "value": f"{forecaster.metrics.get('mape_pct')} %"},
            {"label": "Last DR event", "value": f"{events[-1]['total_kwh']:.0f} kWh - {events[-1]['status']}" if events else "-"},
            {"label": "Pending approvals", "value": str(len(pending))},
            {"label": "Last energy cost", "value": f"Rp{sched[-1]['biaya_energi_rp']:,.0f}" if sched else "-"},
            {"label": "Renewable share (last)", "value": f"{sched[-1]['pangsa_terbarukan_persen']:.1f} %" if sched else "-"},
            {"label": "Urgent order (env)", "value": str(bool(app.state.env.get("urgent_order")))},
        ]

    def agents(self) -> list[dict]:
        last_trace = agent.traces[-1] if agent.traces else None
        return [
            {**PROFILE["agents"][0], "status": "active" if last_trace else "idle",
             "detail": f"{len(agent.plans)} plans, {len(agent.desires)} desires, {len(agent.intentions)} intentions so far",
             "last_event": last_trace["rencana_terpilih"] if last_trace else None,
             "last_ts": last_trace["waktu"] if last_trace else None,
             "bdi": {"beliefs": agent.snapshot(),
                     "desires": [{"id": d.id, "description": d.description, "priority": d.priority}
                                 for d in agent.desires.values()],
                     "intentions": agent.intentions[-5:]}},
            {**PROFILE["agents"][1], "status": "active",
             "detail": f"Ridge ARX, MAPE {forecaster.metrics.get('mape_pct')}%, R2 {forecaster.metrics.get('r2')}",
             "last_event": "forecast", "last_ts": None},
        ]

    def tables(self) -> list[str]:
        return ["jejak_bdi", "rencana_produksi", "proses_fleksibel", "kebijakan_energi", "negosiasi", "event_dr",
                "persetujuan", "jadwal_pasokan", "prakiraan", "riwayat_beban", "model_prakiraan"]

    def table(self, name: str, limit: int = 50) -> dict:
        if name == "jejak_bdi":
            rows = [{"waktu": t["waktu"], "event": t["event"], "opsi": ", ".join(o["rencana"] for o in t["opsi"]),
                     "rencana_terpilih": t["rencana_terpilih"], "alasan_pemilihan": t["alasan_pemilihan"],
                     "keputusan": t["tindakan"].get("keputusan"), "alasan": t["tindakan"].get("alasan"),
                     "alasan_en": t["tindakan"].get("alasan_en")}
                    for t in reversed(agent.traces)]
            return rows_to_table(rows, limit)
        if name == "model_prakiraan":
            return rows_to_table([{"metrik": k, "nilai": v} for k, v in forecaster.metrics.items()], 100)
        rows = history.all(name) if name == "riwayat_beban" else store.all(name)
        if name in ("riwayat_beban", "negosiasi", "event_dr", "persetujuan", "jadwal_pasokan", "prakiraan"):
            rows = list(reversed(rows))
        return rows_to_table(rows, limit)

    def charts(self) -> list[dict]:
        d = _demand_plan(SIM_DATE.isoformat(), WINDOW_START_HOUR, WINDOW_HOURS, "prakiraan")
        labels = [r["jam"][-5:] for r in d["rows"]]
        charts = [{"title": "Demand plan per hour (critical + flexible)", "unit": "kWh", "labels": labels,
                   "series": [{"name": "Critical load", "type": "bar", "values": [r["beban_kritis_kwh"] for r in d["rows"]]},
                              {"name": "Flexible load", "type": "bar", "values": [r["beban_fleksibel_kwh"] for r in d["rows"]]}]}]
        events = store.all("event_dr")
        if events:
            charts.append({"title": f"Last DR event {events[-1]['event_id']} ({events[-1]['status']})", "unit": "kWh",
                           "labels": labels, "series": [{"name": "Load reduction", "type": "bar",
                                                         "values": events[-1]["per_jam_kwh"]}]})
        return charts

    def native_examples(self) -> list[dict]:
        return [
            {"title": "GET /api/v1/rencana-beban?tanggal=2026-11-18&jam_mulai=8&durasi=8", "format": "json",
             "body": json.dumps({"perusahaan": "IndustriPrima", "satuan_energi": "kWh",
                                 "zona_waktu": "WIB lokal (tanpa offset)",
                                 "interval": [{"jam": "2026-11-18 08:00", "kebutuhan_kwh": 421.7,
                                               "beban_kritis_kwh": 379.5, "beban_fleksibel_kwh": 42.2}]}, indent=2)},
            {"title": "POST /api/v1/negosiasi/dr (counter-offer)", "format": "json",
             "body": json.dumps({"jenis_pesan": "TAWARAN", "id_percakapan": "RUN-0002-DR", "putaran": 1,
                                 "harga_tawaran_rp_per_kwh": 1512}, indent=2)},
            {"title": "POST /openadr3/events (excerpt)", "format": "json",
             "body": json.dumps({"objectType": "EVENT", "programID": "ECO-DR-2026",
                                 "intervalPeriod": {"start": "2026-11-18T08:00:00+07:00", "duration": "PT1H"},
                                 "intervals": [{"id": 0, "payloads": [{"type": "DISPATCH_SETPOINT_RELATIVE",
                                                                       "values": [-12.5]}]}]}, indent=2)},
        ]

    def agent_context(self, audience: str) -> dict:
        d = _demand_plan(SIM_DATE.isoformat(), WINDOW_START_HOUR, WINDOW_HOURS, "prakiraan")
        rows = d["rows"]
        total, flex = sum(r["kebutuhan_kwh"] for r in rows), sum(r["beban_fleksibel_kwh"] for r in rows)
        urgent = bool(agent.beliefs.get("pesanan_mendesak"))
        pending = [a for a in store.all("persetujuan") if a["status"] == "MENUNGGU"]
        sched = store.all("jadwal_pasokan")
        beliefs = {translate(k): (({translate(kk): translate(vv) for kk, vv in v.items()}) if isinstance(v, dict) else v)
                   for k, v in agent.snapshot().items()}
        ctx = {
            "summary": f"IndustriPrima needs {total:,.0f} kWh in 08:00-16:00 WIB; flexible (curtailable) load "
                       f"{flex:,.0f} kWh (10% cap); urgent order: {urgent}; pending DR approvals: {len(pending)}.",
            "can_contribute": (f"ADDITIONAL: up to {flex:,.0f} kWh of demand response from flexible processes "
                               "(compressor, HVAC pre-cooling, forklift charging, batch mixer), max 10% of each hour, "
                               "against fair compensation; critical load is never offered")
                              if not urgent else "ADDITIONAL: none today, an urgent order protects production",
            "hourly_demand_kwh": {r["jam"][-5:]: r["kebutuhan_kwh"] for r in rows},
            "hourly_flexible_kwh": {r["jam"][-5:]: r["beban_fleksibel_kwh"] for r in rows},
            "urgent_order": urgent, "auto_approval_limit_kwh": POLICY["batas_persetujuan_otomatis_kwh"],
            "curtailment_limit_pct": POLICY["batas_kurtailmen_persen"],
            "renewable_target_pct": POLICY["target_energi_terbarukan_persen"],
            "recent_dr_events": [{"event_id": e["event_id"], "total_kwh": e["total_kwh"],
                                  "price_rp_per_kwh": e["harga_rp_per_kwh"], "status": e["status"]}
                                 for e in store.all("event_dr")[-3:]],
            "pending_approvals": len(pending),
            "bdi_beliefs": beliefs,
            "bdi_desires": [{"id": x.id, "description": translate(x.description), "priority": x.priority}
                            for x in agent.desires.values()],
            "recent_bdi_decisions": [{"event": t["event"], "selected_plan": translate(t["rencana_terpilih"]),
                                      "decision": translate(t["tindakan"].get("keputusan")),
                                      "reason": t["tindakan"].get("alasan_en") or t["tindakan"].get("alasan")}
                                     for t in agent.traces[-5:]],
            "flexible_processes": [{"code": p["kode"], "name": translate(p["nama"]), "power_kw": p["daya_kw"]}
                                   for p in PROCESSES],
            "last_supply_schedule": ({"energy_cost_rp": sched[-1]["biaya_energi_rp"], "dr_kwh": sched[-1]["dr_kwh"],
                                      "renewable_share_pct": sched[-1]["pangsa_terbarukan_persen"]} if sched else None),
        }
        if audience == "internal":
            ctx.update({"dr_reservation_price_rp_per_kwh": POLICY["harga_reservasi_dr_rp_per_kwh"],
                        "dr_initial_ask_rp_per_kwh": POLICY["harga_awal_dr_rp_per_kwh"],
                        "process_loss_values_rp_per_kwh": {p["kode"]: p["nilai_kerugian_rp_per_kwh"] for p in PROCESSES},
                        "last_local_utility_rp": sched[-1]["utilitas_lokal_rp"] if sched else None})
        return ctx

    def approvals(self) -> list[dict]:
        return list(reversed(store.all("persetujuan")))

    def decide_approval(self, approval_id: str, decision: str, role: str, note: str) -> dict:
        if role != "Energy Manager":
            raise HTTPException(403, "Hanya peran Energy Manager yang dapat memutuskan persetujuan DR.")
        doc = store.find_one("persetujuan", _id=approval_id)
        if doc is None:
            raise HTTPException(404, "Persetujuan tidak ditemukan")
        if doc["status"] != "MENUNGGU":
            raise HTTPException(409, f"Sudah diputuskan: {doc['status']}")
        approved = decision.lower() in ("approve", "setuju", "disetujui")
        doc = store.update("persetujuan", approval_id, {"status": "DISETUJUI" if approved else "DITOLAK",
                                                       "diputuskan_oleh": role, "waktu_keputusan": now_iso(),
                                                       "catatan": note})
        ev = store.find_one("event_dr", event_id=doc["event_id"])
        if ev:
            store.update("event_dr", ev["_id"], {"status": "ACCEPTED" if approved else "OPT_OUT"})
        audit.log("human:Energy Manager", "dr_approval_decided",
                  {"event_id": doc["event_id"], "decision": doc["status"], "note": note})
        return doc


app = create_enterprise_app(PROFILE, skills=SKILLS, transport="HTTP+JSON (Bahasa) + OpenADR-style",
                            native_prefixes=["/api/", "/openadr3/"],
                            env_defaults={"demand_factor": 1.0, "urgent_order": False}, ui=EstateUI(), audit=audit)


# ------------------------------------------------------------------ native API
@app.get("/api/v1/rencana-beban", tags=["native API"])
def rencana_beban(tanggal: str = SIM_DATE.isoformat(), jam_mulai: int = WINDOW_START_HOUR,
                  durasi: int = WINDOW_HOURS, mode: str = "prakiraan") -> dict:
    d = _demand_plan(tanggal, jam_mulai, durasi, mode)
    store.insert("prakiraan", {"dibuat": now_iso(), "tanggal": tanggal, "mode": mode,
                               "total_kwh": round(sum(r["kebutuhan_kwh"] for r in d["rows"]), 2)})
    audit.log("industriprima.demand_forecast_agent", "rencana_beban_diterbitkan",
              {"mode": mode, "total_kwh": round(sum(r["kebutuhan_kwh"] for r in d["rows"]), 1),
               "faktor_permintaan": app.state.env.get("demand_factor")})
    return {
        "perusahaan": "IndustriPrima", "id_pelanggan_pln": "IND-PRIMA-01", "tanggal": tanggal,
        "satuan_energi": "kWh", "zona_waktu": "WIB lokal (tanpa offset)", "dasar": d["basis"],
        "interval": d["rows"],
        "harga_maksimum_rp_per_kwh": POLICY["nilai_beban_padam_rp_per_kwh"],
        "model_prakiraan": {k: forecaster.metrics.get(k) for k in ("model", "mape_pct", "r2")},
        "catatan": f"Beban fleksibel dibatasi {POLICY['batas_kurtailmen_persen']}% per jam (kebijakan internal).",
    }


@app.post("/api/v1/negosiasi/dr", tags=["native API"])
def negosiasi_dr(p: PesanNegosiasi) -> dict:
    _ensure_beliefs()
    types = {"CFP": "cfp_dr", "TAWARAN": "tawaran_dr", "TERIMA": "terima_dr", "TOLAK": "tolak_dr"}
    if p.jenis_pesan not in types:
        raise HTTPException(422, f"jenis_pesan harus salah satu dari {list(types)}")
    window = agent.beliefs.get("jam_window") or _window_hours()
    allowed = [window.index(j) for j in (p.jam_diizinkan or []) if j in window] or None
    event = {"type": types[p.jenis_pesan], "conv": p.id_percakapan, "putaran": p.putaran,
             "kebutuhan_kwh": p.kebutuhan_kwh, "slot_diizinkan": allowed, "harga": p.harga_tawaran_rp_per_kwh,
             "kuantitas": p.kuantitas_kwh}
    result = agent.handle(event)
    if "maks_per_jam_kwh" in result:
        result["maks_per_jam"] = [{"jam": j, "maks_kwh": v} for j, v in zip(window, result.pop("maks_per_jam_kwh"))]
    store.insert("negosiasi", {"waktu": now_iso(), "id_percakapan": p.id_percakapan, "jenis_pesan": p.jenis_pesan,
                               "putaran": p.putaran, "harga_masuk": p.harga_tawaran_rp_per_kwh,
                               "keputusan": result.get("keputusan"), "harga_keluar": result.get("harga_rp_per_kwh"),
                               "alasan": result.get("alasan"), "alasan_en": result.get("alasan_en")})
    audit.log("industriprima.bdi_energy_agent", f"negosiasi_{p.jenis_pesan.lower()}",
              {"conv": p.id_percakapan, "keputusan": result.get("keputusan"), "harga": result.get("harga_rp_per_kwh"),
               "rencana": result["jejak_bdi"]["rencana_terpilih"]})
    return {"id_percakapan": p.id_percakapan, "jenis_pesan_diterima": p.jenis_pesan, **result}


@app.post("/openadr3/events", tags=["native API (OpenADR-style VEN)"])
def openadr_event(evt: dict = Body(...)) -> dict:
    _ensure_beliefs()
    if evt.get("objectType") != "EVENT":
        raise HTTPException(422, "objectType must be EVENT")
    reductions = []
    for iv in sorted(evt.get("intervals", []), key=lambda x: x["id"]):
        sp = next((p for p in iv["payloads"] if p["type"] == "DISPATCH_SETPOINT_RELATIVE"), None)
        reductions.append(round(-float(sp["values"][0]), 3) if sp else 0.0)  # kW over 1 h = kWh
    price = float(evt.get("x-agreed-price-idr-per-kwh", 0))
    result = agent.handle({"type": "event_dr", "per_jam_kwh": reductions, "harga": price})
    event_id = evt.get("id") or "evt-" + uuid.uuid4().hex[:8]
    status = result.get("status_event", "OPT_OUT")
    doc = store.insert("event_dr", {"event_id": event_id, "diterima": now_iso(), "program": evt.get("programID"),
                                    "harga_rp_per_kwh": price, "per_jam_kwh": reductions,
                                    "total_kwh": round(sum(reductions), 2), "status": status,
                                    "rencana_proses": result.get("rencana_proses"),
                                    "nilai_kerugian_rp": result.get("nilai_kerugian_rp", 0.0),
                                    "id_percakapan": evt.get("x-conversation-id")})
    approval_id = None
    if status == "PENDING_APPROVAL":
        approval = store.insert("persetujuan", {"event_id": event_id, "dibuat": now_iso(),
                                                "total_kwh": doc["total_kwh"], "harga_rp_per_kwh": price,
                                                "status": "MENUNGGU", "peran_wajib": "Energy Manager",
                                                "alasan": result.get("alasan"), "alasan_en": result.get("alasan_en")})
        approval_id = approval["_id"]
    audit.log("industriprima.bdi_energy_agent", "event_dr_diproses",
              {"event_id": event_id, "status": status, "total_kwh": doc["total_kwh"]},
              level="WARN" if status != "ACCEPTED" else "INFO")
    return _report(doc, approval_id, result.get("alasan"), result.get("alasan_en"))


def _report(doc: dict, approval_id: str | None = None, reason: str | None = None,
            reason_en: str | None = None) -> dict:
    return {
        "objectType": "REPORT", "eventID": doc["event_id"], "clientName": "IndustriPrima-VEN",
        "reportName": "DR acknowledgement", "x-status": doc["status"], "x-approval-id": approval_id,
        "x-reason": reason, "x-reason-en": reason_en, "x-loss-value-idr": doc.get("nilai_kerugian_rp", 0.0),
        "resources": [{"resourceName": "IP-FLEX-LOADS", "intervals": [
            {"id": i, "payloads": [{"type": "DISPATCH_SETPOINT_RELATIVE", "values": [-v]}]}
            for i, v in enumerate(doc["per_jam_kwh"])]}],
        "x-process-plan": doc.get("rencana_proses"),
    }


@app.get("/openadr3/reports", tags=["native API (OpenADR-style VEN)"])
def openadr_reports(eventID: str | None = None) -> list[dict]:
    events = store.all("event_dr")
    if eventID:
        events = [e for e in events if e["event_id"] == eventID]
    approvals = {a["event_id"]: a for a in store.all("persetujuan")}
    return [_report(e, approvals.get(e["event_id"], {}).get("_id")) for e in events]


@app.post("/api/v1/jadwal-pasokan", tags=["native API"])
def jadwal_pasokan(j: JadwalPasokan) -> dict:
    _ensure_beliefs()
    served = sum(i.surya_kwh + i.baterai_kwh + i.jaringan_kwh + i.cadangan_baterai_kwh for i in j.interval)
    unserved = sum(i.tidak_terlayani_kwh for i in j.interval)
    dr = [i.dr_kwh for i in j.interval]
    _, dr_loss = allocate_to_processes(dr, PROCESSES)
    reference = POLICY["tarif_acuan_pln_rp_per_kwh"] * served
    utility = (reference - j.biaya_energi_rp) + j.kompensasi_dr_rp - dr_loss - j.biaya_penyeimbangan_rp \
        - POLICY["nilai_beban_padam_rp_per_kwh"] * unserved
    result = agent.handle({"type": "jadwal_pasokan", "pangsa_terbarukan_persen": j.pangsa_terbarukan_persen})
    store.insert("jadwal_pasokan", {"diterima": now_iso(), "id_percakapan": j.id_percakapan, "tanggal": j.tanggal,
                                    "energi_terlayani_kwh": round(served, 2), "dr_kwh": round(sum(dr), 2),
                                    "tidak_terlayani_kwh": round(unserved, 2), "biaya_energi_rp": j.biaya_energi_rp,
                                    "kompensasi_dr_rp": j.kompensasi_dr_rp,
                                    "pangsa_terbarukan_persen": j.pangsa_terbarukan_persen,
                                    "utilitas_lokal_rp": round(utility)})
    audit.log("industriprima.bdi_energy_agent", "jadwal_pasokan_dicatat",
              {"biaya_rp": j.biaya_energi_rp, "utilitas_rp": round(utility)})
    return {"status": "DITERIMA", "catatan": result.get("alasan"),
            "utilitas_lokal": {"penghematan_vs_tarif_rp": round(reference - j.biaya_energi_rp),
                               "kompensasi_dr_rp": round(j.kompensasi_dr_rp), "nilai_kerugian_dr_rp": round(dr_loss),
                               "biaya_penyeimbangan_rp": round(j.biaya_penyeimbangan_rp),
                               "penalti_tidak_terlayani_rp": round(POLICY["nilai_beban_padam_rp_per_kwh"] * unserved),
                               "utilitas_rp": round(utility), "dapat_diterima": utility >= 0}}
