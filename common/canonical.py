"""Canonical ecosystem data model D* (BAB 10, 10.2.C: phi_i : D_i -> D*).

Every enterprise keeps its own native schema (units, time format, field names, protocol).
The orchestrator's interoperability layer translates each native payload into these
canonical objects:  energy in kWh per hourly slot, prices in Rp/kWh, timestamps in
ISO 8601 with the WIB offset (+07:00).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from pydantic import BaseModel, Field

from common.config import LOT_KWH, SIM_DATE, WINDOW_HOURS, WINDOW_START_HOUR
from common.timeutil import WIB, iso

# Case Study 04 reference table ("Tugas Klas Ekosistem", Case Study 04, sections 4-5).
# Totals are exactly the document's numbers; hourly shapes are synthetic but realistic
# (tropical clear-sky shape for solar, lunch dip for the industrial load).
REFERENCE_CASE = {
    "demand_kwh": [420, 500, 540, 520, 440, 520, 560, 500],          # total 4,000 kWh
    "solar_kwh": [170, 200, 220, 230, 220, 190, 160, 110],           # total 1,500 kWh @ Rp900
    "grid_kwh_per_slot": 250,                                         # total 2,000 kWh @ Rp1,300
    "battery_energy_kwh": 1000,                                       # 1,000 kWh @ Rp1,100
    "battery_power_kw": 250,
    "price": {"solar": 900, "grid": 1300, "battery": 1100},
    "expected_cost_rp": 4_400_000,
    "expected_dispatch_kwh": {"solar": 1500, "battery": 1000, "grid": 1500},
}


class WindowSpec(BaseModel):
    date: str = SIM_DATE.isoformat()
    start_hour: int = WINDOW_START_HOUR
    hours: int = WINDOW_HOURS

    def start(self) -> datetime:
        d = date.fromisoformat(self.date)
        return datetime(d.year, d.month, d.day, self.start_hour, tzinfo=WIB)

    def slot_starts(self) -> list[datetime]:
        s = self.start()
        return [s + timedelta(hours=i) for i in range(self.hours)]

    def labels(self) -> list[str]:
        return [s.strftime("%H:%M") for s in self.slot_starts()]

    def slots(self) -> list[dict]:
        return [{"index": i, "start": iso(s), "end": iso(s + timedelta(hours=1)),
                 "label": s.strftime("%H:%M")} for i, s in enumerate(self.slot_starts())]


class SupplyOffer(BaseModel):
    """A supply-side order in canonical units (one value per hourly slot)."""
    offer_id: str
    enterprise: str
    agent: str
    resource: str  # solar | grid | battery | demand_response | battery_reserve | grid_nonfirm
    price_rp_per_kwh: float
    max_kwh: list[float]
    energy_limit_kwh: float | None = None
    emission_kg_per_kwh: float = 0.0
    risk_per_kwh: float = 0.0            # risk-kWh per kWh dispatched (R(x) contribution)
    service_disruption: bool = False      # counts toward T(x) (demand not served on schedule)
    green: bool = False                   # counts toward Q(x) (renewable energy)
    stress_threshold_frac: float | None = None  # grid: above this share of capacity = stress
    stress_risk_per_kwh: float = 0.0
    risk_tiers: list[list[float]] | None = None  # convex loading-risk curve [[upto_frac, risk_per_kwh], ...]
    power_group: str | None = None  # offers of one physical asset share its per-slot limit (e.g. battery + reserve)
    uncertainty_kwh: list[float] | None = None  # forecast spread (P50 - P10) per slot
    notes: list[str] = Field(default_factory=list)
    provenance: dict = Field(default_factory=dict)


class DemandBid(BaseModel):
    """Demand-side bid of the industrial estate in canonical units."""
    bid_id: str
    enterprise: str
    agent: str
    demand_kwh: list[float]
    flexible_kwh: list[float]
    critical_kwh: list[float]
    max_price_rp_per_kwh: float = 5000.0  # declared value of lost load (VOLL)
    provenance: dict = Field(default_factory=dict)


class Weights(BaseModel):
    """Weights of the global objective J(x) = aC(x) + bT(x) + gR(x) - dQ(x)."""
    alpha: float = 1.0     # per Rp of cost C(x)
    beta: float = 100.0    # Rp per kWh of demand not served on schedule, T(x)
    gamma: float = 200.0   # Rp per risk-kWh, R(x)
    delta: float = 50.0    # Rp per kWh of renewable energy delivered, Q(x)


class ClearingRequest(BaseModel):
    session_id: str
    window: WindowSpec = Field(default_factory=WindowSpec)
    offers: list[SupplyOffer]
    demand: DemandBid
    weights: Weights = Field(default_factory=Weights)
    locked: dict[str, list[float | None]] = Field(default_factory=dict)  # resource -> fixed kWh
    caps: dict[str, list[float]] = Field(default_factory=dict)            # resource -> extra caps


class ClearingResult(BaseModel):
    session_id: str
    status: str
    allocations: dict[str, list[float]]
    resource_of: dict[str, str]
    enterprise_of: dict[str, str]
    price_of: dict[str, float]
    unserved_kwh: list[float]
    clearing_price_rp_per_kwh: list[float]
    cost_rp: float
    cost_by_offer_rp: dict[str, float]
    objective: dict[str, float]
    lot_adjustments: list[str] = Field(default_factory=list)
    rejected_offers: list[dict] = Field(default_factory=list)
    solver: dict = Field(default_factory=dict)

    def by_resource(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for oid, values in self.allocations.items():
            res = self.resource_of[oid]
            prev = out.get(res, [0.0] * len(values))
            out[res] = [round(a + b, 4) for a, b in zip(prev, values)]
        return out


def round_to_lots(values: list[float], lot: float = LOT_KWH) -> list[float]:
    """Largest-remainder rounding to lot multiples that preserves the (rounded) total."""
    if not values:
        return []
    clean = [max(0.0, float(v)) for v in values]
    total_lots = int(math.floor(sum(clean) / lot + 0.5 + 1e-9))
    floors = [int(math.floor(v / lot + 1e-9)) for v in clean]
    remainders = [v / lot - f for v, f in zip(clean, floors)]
    missing = total_lots - sum(floors)
    if missing > 0:
        for i in sorted(range(len(clean)), key=lambda k: (-remainders[k], k))[:missing]:
            floors[i] += 1
    elif missing < 0:
        for i in sorted(range(len(clean)), key=lambda k: (remainders[k], k)):
            if missing == 0:
                break
            if floors[i] > 0:
                floors[i] -= 1
                missing += 1
    return [f * lot for f in floors]
