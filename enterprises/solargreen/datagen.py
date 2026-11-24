"""Synthetic data for SolarGreen's own CSV data lake (+ training of its forecast model)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from common.canonical import REFERENCE_CASE
from common.config import SIM_DATE, WINDOW_HOURS, WINDOW_START_HOUR
from enterprises.solargreen import physics
from enterprises.solargreen.forecaster import SolarForecaster

TARGET_WINDOW_KWH = 1500.0  # Case Study 04: SolarGreen 1,500 kWh available


def _local(idx_utc: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return idx_utc.tz_convert("UTC").tz_localize(None) + pd.Timedelta(hours=7)


def _temperature(hour: np.ndarray, k: np.ndarray) -> np.ndarray:
    return 24.0 + 6.5 * np.clip(np.sin(np.pi * (hour - 7.0) / 14.0), 0.0, None) * (0.6 + 0.5 * k)


def generate(out: Path, seed: int = 42) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    days = 365
    sim_midnight_local = pd.Timestamp(SIM_DATE)
    start_utc = (sim_midnight_local - pd.Timedelta(days=days) - pd.Timedelta(hours=7)).tz_localize("UTC")
    idx = pd.date_range(start_utc, periods=days * 96, freq="15min")
    n = len(idx)
    mid = idx + pd.Timedelta(minutes=7.5)
    cosz = physics.cos_zenith(mid)
    ghi_cs = physics.clear_sky_ghi(cosz)
    local = _local(mid)
    hour = local.hour.to_numpy() + local.minute.to_numpy() / 60.0
    day_no = np.repeat(np.arange(days), 96)

    doy_daily = local.dayofyear.to_numpy()[::96]
    seasonal = 0.70 + 0.12 * np.cos(2 * np.pi * (doy_daily - 210) / 365.0)  # rainy season Nov-Mar
    z = np.zeros(days)
    for d in range(1, days):
        z[d] = 0.5 * z[d - 1] + rng.normal(0.0, 0.10)
    k_day = np.clip(seasonal + z, 0.25, 1.0)
    afternoon_drop = (1.0 - k_day) * 0.5
    e = np.zeros(n)
    noise = rng.normal(0.0, 0.04, n)
    for t in range(1, n):
        e[t] = 0.8 * e[t - 1] + noise[t]
    k_t = k_day[day_no] * (1.0 - afternoon_drop[day_no] / (1.0 + np.exp(-(hour - 13.5) / 0.8))) + e
    k_t = np.clip(k_t, 0.08, 1.05)
    ghi = ghi_cs * k_t
    temp = _temperature(hour, k_day[day_no]) + rng.normal(0.0, 0.8, days)[day_no]
    pv = physics.pv_ac_kw(ghi, temp) * (1.0 + rng.normal(0.0, 0.015, n))
    pv[cosz <= 0.02] = 0.0
    pv = np.clip(pv, 0.0, physics.AC_LIMIT_KW)
    bias = rng.normal(0.0, 0.06, days)[day_no]
    ghi_fc = np.clip(ghi * (1.0 + bias + rng.normal(0.0, 0.07, n)), 0.0, 1.15 * ghi_cs + 5.0)
    temp_fc = temp + rng.normal(0.0, 0.7, n)

    ts_str = idx.strftime("%Y-%m-%dT%H:%M:%SZ")
    nwp = pd.DataFrame({"ts_utc": ts_str, "ghi_fc_wm2": np.round(ghi_fc, 1), "temp_fc_c": np.round(temp_fc, 2)})
    scada = pd.DataFrame({"ts_utc": ts_str, "pv_kw": np.round(pv, 2), "ghi_meas_wm2": np.round(ghi, 1),
                          "temp_meas_c": np.round(temp, 2)})
    nwp.to_csv(out / "nwp_forecast_archive.csv", index=False)
    scada.to_csv(out / "scada_generation.csv", index=False)

    forecaster = SolarForecaster(out / "models")
    metrics = forecaster.fit(nwp, scada)
    forecaster.save()

    # Simulation-day NWP forecast: a partly cloudy day whose forecast matches Case Study 04.
    sim_idx = pd.date_range((sim_midnight_local - pd.Timedelta(hours=7)).tz_localize("UTC"), periods=96, freq="15min")
    sim_mid = sim_idx + pd.Timedelta(minutes=7.5)
    sim_cs = physics.clear_sky_ghi(physics.cos_zenith(sim_mid))
    sim_local = _local(sim_mid)
    sim_hour = sim_local.hour.to_numpy() + sim_local.minute.to_numpy() / 60.0
    in_window = (sim_hour >= WINDOW_START_HOUR) & (sim_hour < WINDOW_START_HOUR + WINDOW_HOURS)

    def weather(k: float):
        return sim_cs * k, _temperature(sim_hour, np.full(96, k))

    def window_kwh(k: float) -> float:
        g, t = weather(k)
        pred = forecaster.predict(sim_idx, g, t)
        return float((pred["p50"].to_numpy()[in_window] * 0.25).sum())

    lo, hi = 0.30, 1.10
    for _ in range(40):
        midk = (lo + hi) / 2
        if window_kwh(midk) < TARGET_WINDOW_KWH:
            lo = midk
        else:
            hi = midk
    k_sim = min((lo, hi), key=lambda k: abs(window_kwh(k) - TARGET_WINDOW_KWH))
    g_sim, t_sim = weather(k_sim)
    pd.DataFrame({"ts_utc": sim_idx.strftime("%Y-%m-%dT%H:%M:%SZ"), "ghi_fc_wm2": np.round(g_sim, 2),
                  "temp_fc_c": np.round(t_sim, 2)}).to_csv(out / "nwp_forecast_sim_day.csv", index=False)

    # Declared availability (reference mode = the document's table, kW per 15 min in UTC).
    win_start_utc = (sim_midnight_local + pd.Timedelta(hours=WINDOW_START_HOUR - 7)).tz_localize("UTC")
    rows = []
    for i, kwh in enumerate(REFERENCE_CASE["solar_kwh"]):
        for q in range(4):
            ts = win_start_utc + pd.Timedelta(hours=i, minutes=15 * q)
            rows.append({"ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "available_kw": float(kwh)})
    pd.DataFrame(rows).to_csv(out / "declared_availability.csv", index=False)

    pd.DataFrame(columns=["issued_at_utc", "offer_ref", "basis", "ts_utc", "p10_kw", "p50_kw", "p90_kw",
                          "available_kw"]).to_csv(out / "forecasts_issued.csv", index=False)
    pd.DataFrame(columns=["received_at_utc", "offer_ref", "conversation_id", "ts_utc", "setpoint_kw"]).to_csv(
        out / "dispatch_log.csv", index=False)

    site = {
        "site_id": "SG-PLANT-01", "name": "SolarGreen Kalasan PV plant (fictional)",
        "kwp": physics.PLANT_KWP, "ac_limit_kw": physics.AC_LIMIT_KW,
        "lat": physics.LAT_DEG, "lon": physics.LON_DEG,
        "offer_price_idr_kwh": 900, "price_floor_idr_kwh": 850, "om_cost_idr_kwh": 120,
        "imbalance_penalty_idr_kwh": 250, "carbon_gco2_kwh": 0,
        "sim_day_clearness_index": round(k_sim, 4),
        "sim_window_p50_kwh": round(window_kwh(k_sim), 2),
    }
    (out / "site.json").write_text(json.dumps(site, indent=2), encoding="utf-8")
    return {"forecast_metrics": metrics, "sim_window_p50_kwh": site["sim_window_p50_kwh"],
            "clearness": site["sim_day_clearness_index"]}
