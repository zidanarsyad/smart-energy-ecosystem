"""Monitoring & evaluation: KPIs, local utilities vs global objective, before/after comparison."""
from __future__ import annotations

from common.canonical import ClearingResult, SupplyOffer

PHYSICAL = ("solar", "battery", "grid", "grid_nonfirm", "battery_reserve")
FLEX = ("demand_response", "battery_reserve", "grid_nonfirm")
ORDER = ["solar", "battery", "grid", "grid_nonfirm", "battery_reserve", "demand_response"]
LABELS = {"solar": "SolarGreen solar", "battery": "BatteryFlex battery", "grid": "PowerGrid grid",
          "grid_nonfirm": "PowerGrid non-firm", "battery_reserve": "BatteryFlex reserve",
          "demand_response": "IndustriPrima demand response", "unserved": "Unserved (load shedding)"}


def _order(r: str) -> int:
    return ORDER.index(r) if r in ORDER else len(ORDER)


def compute_kpis(final: ClearingResult, offers: list[SupplyOffer], grid_headroom: list[float] | None,
                 acks: dict) -> dict:
    by_res = final.by_resource()
    totals = {r: round(sum(v), 2) for r, v in sorted(by_res.items(), key=lambda kv: _order(kv[0]))}
    cost_by_res: dict[str, float] = {}
    for oid, c in final.cost_by_offer_rp.items():
        r = final.resource_of[oid]
        cost_by_res[r] = round(cost_by_res.get(r, 0.0) + c, 2)
    physical = sum(totals.get(r, 0.0) for r in PHYSICAL)
    dr = totals.get("demand_response", 0.0)
    unserved = round(sum(final.unserved_kwh), 3)
    energy_cost = sum(cost_by_res.get(r, 0.0) for r in PHYSICAL)
    ef = {o.offer_id: o.emission_kg_per_kwh for o in offers}
    emissions = sum(sum(v) * ef.get(oid, 0.0) for oid, v in final.allocations.items())
    grid = [a + b for a, b in zip(by_res.get("grid", [0.0] * len(final.unserved_kwh)),
                                  by_res.get("grid_nonfirm", [0.0] * len(final.unserved_kwh)))]
    util = [g / h * 100 for g, h in zip(grid, grid_headroom or []) if h and h > 0]
    utilities = {eid: a["local_utility"]["utility_rp"] for eid, a in acks.items()
                 if isinstance(a, dict) and a.get("local_utility")}
    return {
        "demand_kwh": round(physical + dr + unserved, 2),
        "served_kwh": round(physical, 2),
        "dr_kwh": round(dr, 2),
        "unserved_kwh": unserved,
        "dispatch_kwh": totals,
        "cost_by_resource_rp": cost_by_res,
        "energy_cost_rp": round(energy_cost, 2),
        "firm_energy_cost_rp": round(sum(cost_by_res.get(r, 0.0) for r in ("solar", "battery", "grid")), 2),
        "flexibility_cost_rp": round(sum(cost_by_res.get(r, 0.0) for r in FLEX), 2),
        "dr_compensation_rp": round(cost_by_res.get("demand_response", 0.0), 2),
        "total_cost_rp": round(final.cost_rp, 2),
        "avg_energy_price_rp_per_kwh": round(energy_cost / physical, 2) if physical else 0.0,
        "renewable_share_pct": round(totals.get("solar", 0.0) / physical * 100, 2) if physical else 0.0,
        "emissions_kg": round(emissions, 1),
        "emission_intensity_kg_per_kwh": round(emissions / physical, 4) if physical else 0.0,
        "peak_grid_utilisation_pct": round(max(util), 1) if util else 0.0,
        "grid_stress_kwh": final.objective.get("stress_kwh", 0.0),
        "C": final.objective.get("C_cost_rp"), "T": final.objective.get("T_disruption_kwh"),
        "R": final.objective.get("R_risk_kwh"), "Q": final.objective.get("Q_renewable_kwh"),
        "J": final.objective.get("J"),
        "battery_final_soc_pct": (acks.get("batteryflex") or {}).get("final_soc_pct"),
        "local_utilities_rp": utilities,
        "ecosystem_welfare_rp": round(sum(utilities.values())) if utilities else None,
    }


def compare(totals: dict, unserved: float, kpis: dict, base: dict | None) -> dict:
    if base is None:
        return {"baseline_run": None, "decision_changed": False, "rows": [], "kpis": [],
                "explanation": ["First run for this mode: it becomes the baseline for later comparisons."]}
    bt = dict(base["decision"]["totals_kwh"])
    bt["unserved"] = base["kpis"]["unserved_kwh"]
    ct = dict(totals)
    ct["unserved"] = unserved
    keys = sorted(set(bt) | set(ct), key=lambda r: _order(r) if r != "unserved" else 99)
    rows = [{"resource": r, "label": LABELS.get(r, r), "baseline_kwh": round(bt.get(r, 0.0), 2),
             "current_kwh": round(ct.get(r, 0.0), 2), "delta_kwh": round(ct.get(r, 0.0) - bt.get(r, 0.0), 2)}
            for r in keys]
    kpi_keys = ["total_cost_rp", "firm_energy_cost_rp", "flexibility_cost_rp", "J", "renewable_share_pct",
                "emissions_kg", "peak_grid_utilisation_pct", "avg_energy_price_rp_per_kwh"]
    kpi_rows = []
    for k in kpi_keys:
        b, c = base["kpis"].get(k) or 0.0, kpis.get(k) or 0.0
        kpi_rows.append({"kpi": k, "baseline": b, "current": c, "delta": round(c - b, 2),
                         "delta_pct": round((c - b) / b * 100, 2) if b else None})
    changed = any(abs(r["delta_kwh"]) >= 1.0 for r in rows)
    explanation = []
    for r in rows:
        if abs(r["delta_kwh"]) >= 1.0:
            explanation.append(f"{r['label']}: {r['baseline_kwh']:,.0f} -> {r['current_kwh']:,.0f} kWh "
                               f"({r['delta_kwh']:+,.0f} kWh)")
    tc = next(x for x in kpi_rows if x["kpi"] == "total_cost_rp")
    if abs(tc["delta"]) >= 1:
        explanation.append(f"Total cost Rp{tc['baseline']:,.0f} -> Rp{tc['current']:,.0f} "
                           f"({tc['delta']:+,.0f}; {tc['delta_pct'] or 0:+.1f}%)")
    if not changed:
        explanation.append("Dispatch decision unchanged versus the baseline.")
    return {"baseline_run": base["run_id"], "baseline_scenario": base["scenario"]["id"], "decision_changed": changed,
            "rows": rows, "kpis": kpi_rows, "explanation": explanation}
