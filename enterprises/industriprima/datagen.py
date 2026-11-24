"""Synthetic data for IndustriPrima's JSON document store (collections and fields in Bahasa Indonesia)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from common.config import SIM_DATE, WINDOW_HOURS, WINDOW_START_HOUR
from common.stores import JsonDocStore
from enterprises.industriprima.forecaster import DemandForecaster, daytype

TARGET_WINDOW_KWH = 4000.0  # Case Study 04: the industrial estate needs 4,000 kWh

# Nominal hourly load (kWh/h). Weekday 08:00-15:00 = Case Study 04 reference shape (sum 4,000).
NOMINAL = {
    "hari_kerja": [190, 185, 180, 180, 185, 210, 300, 380, 420, 500, 540, 520,
                   440, 520, 560, 500, 430, 330, 260, 230, 215, 205, 200, 195],
    "sabtu": [180, 175, 172, 172, 175, 190, 230, 260, 280, 300, 310, 300,
              260, 280, 290, 270, 240, 210, 200, 195, 190, 185, 182, 180],
    "minggu": [175, 172, 170, 170, 172, 175, 180, 185, 190, 195, 200, 200,
               195, 195, 195, 192, 190, 188, 186, 184, 182, 180, 178, 176],
}

FLEX_PROCESSES = [
    {"kode": "FX-01", "nama": "Kompresor udara cadangan lini B", "daya_kw": 20, "nilai_kerugian_rp_per_kwh": 1350,
     "keterangan": "dapat dimatikan sementara, tekanan dijaga tangki"},
    {"kode": "FX-02", "nama": "Pra-pendinginan HVAC gedung kantor", "daya_kw": 15, "nilai_kerugian_rp_per_kwh": 1420,
     "keterangan": "setpoint dinaikkan 2 derajat C"},
    {"kode": "FX-03", "nama": "Pengisian forklift listrik", "daya_kw": 12, "nilai_kerugian_rp_per_kwh": 1480,
     "keterangan": "digeser ke setelah pukul 16:00"},
    {"kode": "FX-04", "nama": "Mixer batch lini 3", "daya_kw": 25, "nilai_kerugian_rp_per_kwh": 1600,
     "keterangan": "batch dijadwal ulang, risiko keterlambatan kecil"},
]

POLICY = {
    "batas_kurtailmen_persen": 10,
    "batas_persetujuan_otomatis_kwh": 200,
    "harga_reservasi_dr_rp_per_kwh": 1500,
    "harga_awal_dr_rp_per_kwh": 1950,
    "taktik_konsesi_e": 0.6,
    "maks_putaran": 6,
    "target_energi_terbarukan_persen": 30,
    "nilai_beban_padam_rp_per_kwh": 5000,
    "tarif_acuan_pln_rp_per_kwh": 1300,
}


def _temps(rng: np.random.Generator, day_shift: float) -> list[float]:
    return [round(25.0 + 5.5 * max(0.0, np.sin(np.pi * (h - 6) / 14)) + day_shift + rng.normal(0, 0.3), 2)
            for h in range(24)]


def generate(out: Path, seed: int = 42) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed + 2)
    path, hist_path = out / "industriprima_docs.json", out / "industriprima_history.json"
    for p in (path, hist_path):
        p.unlink(missing_ok=True)
    store = JsonDocStore(path)              # operational collections (small, rewritten often)
    history = JsonDocStore(hist_path)       # 8,760 hourly load documents (read-mostly)
    start = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day) - timedelta(days=365)
    docs = []
    for d in range(365):
        day = start + timedelta(days=d)
        kind = daytype(day.date())
        prod = 1.0 if kind == "minggu" else float(np.clip(1.0 + rng.normal(0.0, 0.06 if kind == "hari_kerja" else 0.05),
                                                          0.8, 1.2))
        temps = _temps(rng, rng.normal(0.0, 1.0))
        for h in range(24):
            load = NOMINAL[kind][h] * prod * (1.0 + 0.012 * (temps[h] - 29.0)) * (1.0 + rng.normal(0.0, 0.025))
            docs.append({"jam": (day + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M"), "beban_kwh": round(load, 2),
                         "suhu_c": temps[h], "indeks_produksi": round(prod, 4), "jenis_hari": kind})
    history.replace_collection("riwayat_beban", docs)
    store.replace_collection("profil_beban_nominal", [{"jenis_hari": k, "beban_kwh_per_jam": v} for k, v in NOMINAL.items()])
    store.replace_collection("proses_fleksibel", FLEX_PROCESSES)
    store.replace_collection("kebijakan_energi", [POLICY])
    for col in ("event_dr", "persetujuan", "negosiasi", "jadwal_pasokan", "prakiraan"):
        store.replace_collection(col, [])

    forecaster = DemandForecaster(NOMINAL)
    metrics = forecaster.fit(docs)
    sim_temps = [round(25.0 + 5.5 * max(0.0, np.sin(np.pi * (h - 6) / 14)), 2) for h in range(24)]
    for h in range(WINDOW_START_HOUR, WINDOW_START_HOUR + WINDOW_HOURS):
        sim_temps[h] = 29.0  # overcast, mild day inside the window

    def window_kwh(p: float) -> float:
        fc = forecaster.forecast_day(SIM_DATE, p, sim_temps)
        return sum(fc[WINDOW_START_HOUR:WINDOW_START_HOUR + WINDOW_HOURS])

    lo, hi = 0.7, 1.3
    for _ in range(40):
        mid = (lo + hi) / 2
        if window_kwh(mid) < TARGET_WINDOW_KWH:
            lo = mid
        else:
            hi = mid
    prod_sim = round((lo + hi) / 2, 6)
    store.replace_collection("rencana_produksi", [{
        "tanggal": SIM_DATE.isoformat(), "indeks_produksi": prod_sim, "suhu_prakiraan_c": sim_temps,
        "pesanan_mendesak": False, "keterangan": "Rencana produksi normal 2 shift (data MES)"}])
    return {"demand_metrics": metrics, "production_index_sim": prod_sim,
            "sim_window_forecast_kwh": round(window_kwh(prod_sim), 2)}
