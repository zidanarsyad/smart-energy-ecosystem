"""Shakti Tantra terminal demo: run the baseline and a disruption scenario through the orchestrator API.

    python scripts/demo_cli.py                                   # solar_drop_40, forecast mode
    python scripts/demo_cli.py --scenario grid_derating --mode reference
    python scripts/demo_cli.py --all                             # every scenario after the baseline
(requires `python scripts/run_all.py` running in another terminal)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.config import orchestrator_url  # noqa: E402


def show(run: dict) -> None:
    k = run.get("kpis", {})
    print(f"\n=== {run['run_id']}  {run['scenario']['label']}  [{run['mode']}]  -> {run['status']}")
    if run.get("error"):
        print("   ERROR:", run["error"])
        return
    for t in run["trace"]:
        print(f"   {t['step']:>2}. {t['phase']:<15} {t['summary']}")
    print("   dispatch kWh :", {r: round(v) for r, v in k.get("dispatch_kwh", {}).items()},
          "| unserved", round(k.get("unserved_kwh", 0)))
    print(f"   cost Rp{k.get('total_cost_rp', 0):,.0f} | J {k.get('J', 0):,.0f} | renewable "
          f"{k.get('renewable_share_pct', 0):.1f}% | emissions {k.get('emissions_kg', 0):,.0f} kg | "
          f"messages {run['message_stats']['messages']} | interop {run['interoperability_success_rate_pct']}%")
    print("   local utilities (Rp):", k.get("local_utilities_rp"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", default="solar_drop_40")
    p.add_argument("--mode", default="forecast", choices=["forecast", "reference"])
    p.add_argument("--onset", type=int, default=0)
    p.add_argument("--all", action="store_true")
    a = p.parse_args()
    client = httpx.Client(base_url=orchestrator_url(), timeout=120)
    if a.all:
        show(client.post("/runs", json={"scenario": "normal", "mode": a.mode}).json())
        for sc in client.get("/scenarios").json():
            if sc != "normal":
                show(client.post("/runs", json={"scenario": sc, "mode": a.mode, "onset_slot": a.onset}).json())
        client.post("/environment", json={"scenario": "normal"})
        return
    res = client.post("/runs/compare", json={"scenario": a.scenario, "mode": a.mode, "onset_slot": a.onset}).json()
    show(res["baseline"])
    show(res["scenario"])
    print("\n   Monitoring:", " | ".join(res["scenario"]["monitoring"]["explanation"]))
    client.post("/environment", json={"scenario": "normal"})


if __name__ == "__main__":
    main()
