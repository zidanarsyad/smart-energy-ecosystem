"""Semantic mapping functions phi_i : D_i -> D* (BAB 10, section 10.2.C).

Each enterprise keeps its own units, clocks and vocabulary. These functions are the only
place where native semantics are converted to / from the canonical model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from common.canonical import WindowSpec
from common.timeutil import WIB

UTC = timezone.utc

MAPPINGS = [
    {"enterprise": "solargreen", "native": "points[].available_kw  (avg kW, PT15M, UTC 'Z')",
     "canonical": "SupplyOffer.max_kwh[h]  (kWh per hourly slot, WIB)",
     "phi": "sum of kW x 0.25 h over the 4 quarter-hours of slot h; UTC -> WIB (+7 h)"},
    {"enterprise": "solargreen", "native": "points[].p10_kw", "canonical": "SupplyOffer.uncertainty_kwh[h]",
     "phi": "sum of (available_kw - p10_kw) x 0.25 h  (forecast spread -> risk)"},
    {"enterprise": "solargreen", "native": "price.amount (IDR/kWh)", "canonical": "price_rp_per_kwh",
     "phi": "identity (IDR = Rp)"},
    {"enterprise": "solargreen", "native": "carbon_intensity_gco2_per_kwh", "canonical": "emission_kg_per_kwh",
     "phi": "/ 1000"},
    {"enterprise": "powergrid", "native": "Interval@headroomMWh", "canonical": "SupplyOffer.max_kwh[h]",
     "phi": "x 1000 (MWh -> kWh)"},
    {"enterprise": "powergrid", "native": "Tariff (IDR/MWh)", "canonical": "price_rp_per_kwh",
     "phi": "/ 1000"},
    {"enterprise": "powergrid", "native": "Interval@start 'dd/MM/yyyy HH:mm' (local, no offset)",
     "canonical": "slot start ISO 8601 +07:00", "phi": "strptime + attach WIB offset"},
    {"enterprise": "powergrid", "native": "EmissionFactor (kgCO2/MWh)", "canonical": "emission_kg_per_kwh",
     "phi": "/ 1000"},
    {"enterprise": "powergrid", "native": "StressThreshold (fraction)", "canonical": "stress_threshold_frac",
     "phi": "identity"},
    {"enterprise": "industriprima", "native": "interval[].kebutuhan_kwh", "canonical": "DemandBid.demand_kwh",
     "phi": "rename (Bahasa -> English)"},
    {"enterprise": "industriprima", "native": "beban_fleksibel_kwh / beban_kritis_kwh",
     "canonical": "DemandBid.flexible_kwh / critical_kwh", "phi": "rename"},
    {"enterprise": "industriprima", "native": "jam 'YYYY-MM-DD HH:MM' (local, no offset)",
     "canonical": "slot start ISO 8601 +07:00", "phi": "parse + attach WIB offset"},
    {"enterprise": "industriprima", "native": "harga_maksimum_rp_per_kwh", "canonical": "max_price_rp_per_kwh (VOLL)",
     "phi": "rename"},
    {"enterprise": "industriprima", "native": "OpenADR DISPATCH_SETPOINT_RELATIVE (-kW per PT1H)",
     "canonical": "demand_response kWh per slot", "phi": "kWh = -kW x 1 h"},
    {"enterprise": "batteryflex", "native": "result.offer.max_kw[] (kW per step_s)", "canonical": "SupplyOffer.max_kwh[h]",
     "phi": "kW x step_s / 3600"},
    {"enterprise": "batteryflex", "native": "soc_pct, capacity_kwh, soc_min_pct", "canonical": "energy_limit_kwh",
     "phi": "(soc - soc_min) / 100 x capacity (agent reports offer.energy_kwh)"},
    {"enterprise": "batteryflex", "native": "t_start (Unix epoch seconds)", "canonical": "slot start ISO 8601 +07:00",
     "phi": "datetime.fromtimestamp(t, WIB)"},
    {"enterprise": "energytrade", "native": "kWh, IDR/kWh, ISO 8601 +07:00", "canonical": "same",
     "phi": "identity (the hub adopted the canonical model D*)"},
]


def utc_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_z(s: str) -> datetime:
    return datetime.strptime(s.replace("+00:00", "Z"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def legacy_ts(dt: datetime) -> str:
    return dt.astimezone(WIB).strftime("%d/%m/%Y %H:%M")


def parse_legacy_ts(s: str) -> datetime:
    return datetime.strptime(s, "%d/%m/%Y %H:%M").replace(tzinfo=WIB)


def local_naive(dt: datetime) -> str:
    return dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M")


def parse_local_naive(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=WIB)


def epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def from_epoch(t: int) -> datetime:
    return datetime.fromtimestamp(int(t), WIB)


def slot_index(ts: datetime, window: WindowSpec) -> int | None:
    delta = (ts - window.start()).total_seconds() / 3600.0
    idx = int(delta // 1)
    return idx if 0 <= idx < window.hours else None


def kw_points_to_slot_kwh(points: list[dict], window: WindowSpec, value_key: str,
                          step_hours: float = 0.25, ts_key: str = "ts_utc") -> list[float]:
    out = [0.0] * window.hours
    for p in points:
        idx = slot_index(parse_utc_z(p[ts_key]), window)
        if idx is not None:
            out[idx] += float(p[value_key]) * step_hours
    return [round(v, 4) for v in out]


def slot_kwh_to_kw_points(kwh: list[float], window: WindowSpec, step_minutes: int = 15) -> list[dict]:
    """Inverse mapping for dispatch: constant average power inside each hourly slot."""
    points = []
    for start, energy in zip(window.slot_starts(), kwh):
        for q in range(60 // step_minutes):
            points.append({"ts_utc": utc_z(start + timedelta(minutes=step_minutes * q)),
                           "setpoint_kw": round(float(energy), 3)})
    return points


def mwh_to_kwh(x: float) -> float:
    return float(x) * 1000.0


def kwh_to_mwh(x: float) -> float:
    return float(x) / 1000.0


def per_mwh_to_per_kwh(x: float) -> float:
    return float(x) / 1000.0


def g_to_kg(x: float) -> float:
    return float(x) / 1000.0
