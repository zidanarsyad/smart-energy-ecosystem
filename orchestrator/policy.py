"""Federated governance: ecosystem policies + each enterprise's own G_i enforced at the boundary.

The orchestrator never overrides an enterprise's governance; it records what each party's
policy decided (e.g. IndustriPrima's human approval, PowerGrid's veto) and derives the run status.
"""
from __future__ import annotations

from orchestrator.negotiation import HUB_PRICE_CAP


def evaluate(*, grid_verdict: str, prices_used: dict[str, float], dr_kwh: float, dr_ack: dict | None,
             unserved_kwh: float, reserve_kwh: float, acks: dict, degraded: list[dict], executed: bool,
             causers: list[str] | None = None) -> list[dict]:
    out: list[dict] = []

    def add(pid: str, name: str, owner: str, status: str, detail: str) -> None:
        out.append({"id": pid, "policy": name, "owner": owner, "status": status, "detail": detail})

    if grid_verdict in ("APPROVED", "APPROVED_WITH_WARNINGS"):
        add("G1", "Grid safety veto", "PowerGrid", "PASS", f"Schedule verdict {grid_verdict}")
    elif grid_verdict == "REJECTED":
        add("G1", "Grid safety veto", "PowerGrid", "FAIL", "PowerGrid rejected the schedule: execution blocked")
    elif grid_verdict == "UNAVAILABLE":
        add("G1", "Grid safety veto", "PowerGrid", "PENDING", "PowerGrid unreachable: operator must confirm safety")
    else:
        add("G1", "Grid safety veto", "PowerGrid", "N/A", "No grid import in the plan")

    over = {k: v for k, v in prices_used.items() if v > HUB_PRICE_CAP}
    add("G2", "Market price cap Rp2,000/kWh", "EnergyTrade Hub", "FAIL" if over else "PASS",
        f"Above cap: {over}" if over else f"Highest accepted price Rp{max(prices_used.values(), default=0):,.0f}/kWh")

    if dr_kwh <= 0.5:
        add("G3", "Human approval for large DR", "IndustriPrima", "N/A", "No demand response dispatched")
    elif dr_ack is None or dr_ack.get("status") in (None, "FAILED"):
        add("G3", "Human approval for large DR", "IndustriPrima", "PENDING", "DR event not acknowledged")
    elif dr_ack["status"] == "ACCEPTED":
        how = "approved by the Energy Manager" if dr_ack.get("approved_by_human") else \
            "accepted by the BDI agent within its delegated authority"
        add("G3", "Human approval for large DR", "IndustriPrima", "PASS", f"{dr_kwh:,.0f} kWh {how}")
    elif dr_ack["status"] == "PENDING_APPROVAL":
        add("G3", "Human approval for large DR", "IndustriPrima", "PENDING",
            f"{dr_kwh:,.0f} kWh exceeds IndustriPrima's auto-approval limit: waiting for the Energy Manager")
    else:
        add("G3", "Human approval for large DR", "IndustriPrima", "WARN",
            f"IndustriPrima opted out ({dr_ack.get('reason')}): replanning required")

    if unserved_kwh > 0.5:
        add("G4", "No automated load shedding", "Ecosystem", "PENDING",
            f"{unserved_kwh:,.0f} kWh cannot be served: ecosystem operator must authorise load shedding")
    else:
        add("G4", "No automated load shedding", "Ecosystem", "PASS", "All demand served or flexibly shifted")

    if reserve_kwh > 0.5:
        add("G5", "Emergency reserve authorisation", "BatteryFlex / PowerGrid", "PENDING",
            f"{reserve_kwh:,.0f} kWh deep discharge (SOC 10-20%) requires PowerGrid emergency authorisation")
    else:
        add("G5", "Emergency reserve authorisation", "BatteryFlex / PowerGrid", "N/A", "Reserve band not used")

    bad = [eid for eid, a in acks.items() if isinstance(a, dict) and a.get("local_utility")
           and not a["local_utility"]["acceptable"]]
    causers = set(causers or [])
    unfair = [e for e in bad if e not in causers]
    if not executed:
        add("G6", "Individual rationality (U_i >= 0)", "Ecosystem", "N/A", "Plan not executed")
    elif unfair:
        add("G6", "Individual rationality (U_i >= 0)", "Ecosystem", "WARN",
            f"Local utility negative for non-causing parties {unfair}: the plan is not acceptable to them")
    elif bad:
        add("G6", "Individual rationality (U_i >= 0)", "Ecosystem", "PASS",
            f"All non-causing parties have U_i >= 0; {bad} bear their own imbalance cost (causer pays)")
    else:
        add("G6", "Individual rationality (U_i >= 0)", "Ecosystem", "PASS",
            "Every enterprise reports an acceptable local utility")

    add("G7", "Data minimisation", "Ecosystem", "PASS",
        "Only shared fields were exchanged; reservation prices, loss values and network topology stayed private")
    add("G8", "Simulation boundary", "Ecosystem", "PASS",
        "All dispatch instructions are simulated (no real plant, grid or load control)")
    if degraded:
        add("G9", "Data quality / availability", "Ecosystem", "WARN",
            "; ".join(f"{d['enterprise']} {d['kind']}: {d['fallback']}" for d in degraded))
    else:
        add("G9", "Data quality / availability", "Ecosystem", "PASS", "All perception data live")
    return out


def run_status(policies: list[dict], failed_exec: list[str]) -> str:
    st = {p["id"]: p["status"] for p in policies}
    if st.get("G1") == "FAIL" or st.get("G2") == "FAIL":
        return "BLOCKED"
    if any(s == "PENDING" for s in st.values()):
        return "AWAITING_APPROVAL"
    if failed_exec or any(s in ("WARN", "FAIL") for s in st.values()):
        return "COMPLETED_WITH_WARNINGS"
    return "COMPLETED"
