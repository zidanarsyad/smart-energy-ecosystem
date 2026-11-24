"""Synthetic data for BatteryFlex: JSON config + JSON Lines telemetry (epoch seconds, SOC %)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from common.config import SIM_DATE

CONFIG = {
    "asset_id": "BF-BESS-01",
    "chemistry": "LFP",
    "capacity_kwh_nominal": 1250.0,
    "p_max_kw": 250.0,
    "soc_min_pct": 20.0,
    "soc_emergency_min_pct": 10.0,
    "soc_initial_pct": 100.0,
    "round_trip_efficiency": 0.90,
    "charge_cost_idr_kwh": 650.0,
    "degradation_idr_kwh": 300.0,
    "offer_price_idr_kwh": 1100.0,
    "reserve_price_idr_kwh": 1900.0,
    "reserve_risk_per_kwh": 2.0,
    "deep_discharge_penalty_idr_kwh": 400.0,
    "note": "Charged overnight from off-peak supply; SOC 100% at 08:00 WIB on the simulation day.",
}
WIB = timezone(timedelta(hours=7))


def generate(out: Path, seed: int = 42) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed + 4)
    (out / "bess_config.json").write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
    for name in ("telemetry.jsonl", "schedules.jsonl"):
        (out / name).unlink(missing_ok=True)
    start = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day, tzinfo=WIB) - timedelta(days=30)
    cycles, soc, lines = 1800, 22.0, []
    for d in range(30):
        depth = float(rng.uniform(0.45, 0.8))
        for h in range(24):
            ts = start + timedelta(days=d, hours=h)
            if 0 <= h < 6:
                p = -min(250.0, (100.0 - soc) / 100 * 1250 / max(6 - h, 1))
            elif 8 <= h < 16:
                p = min(250.0, depth * 1000 / 8 * (1 + rng.normal(0, 0.1)))
            else:
                p = 0.0
            soc = float(np.clip(soc - p / 1250 * 100 * (1.0 if p > 0 else 0.95), 15.0, 100.0))
            if h == 0:
                cycles += 1
            lines.append(json.dumps({"ts": int(ts.timestamp()), "soc_pct": round(soc, 2), "p_kw": round(p, 1),
                                     "temp_c": round(27 + 4 * (p != 0) + rng.normal(0, 0.6), 1),
                                     "cycle_count": cycles}))
    (out / "telemetry.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "schedules.jsonl").touch()
    return {"telemetry_rows": len(lines)}
