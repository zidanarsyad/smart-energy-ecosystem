"""BatteryFlex Model Predictive Control (two linear programmes solved with HiGHS).

1. Economic MPC (local intelligence): maximise sum_t (price_t - c_res) * d_t * dt
   s.t. sum_t d_t * dt <= (SOC_0 - SOC_min) * C,  0 <= d_t <= P_t.
   -> the battery's *own* preferred plan and the offer envelope it submits to the market.

2. Tracking MPC (receding horizon): at every step k, with the *measured* SOC_k, solve
   min sum_{t>=k} |d_t - ref_t|   s.t. the same SOC / power constraints,
   apply only d_k, measure the new SOC (with sensor noise) and re-solve. This follows the
   ecosystem's dispatch schedule while guaranteeing that SOC_min is never violated.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog


def soc_trajectory(soc0_pct: float, cap_kwh: float, discharge_kw: list[float], dt: float = 1.0) -> list[float]:
    soc = [soc0_pct]
    for d in discharge_kw:
        soc.append(soc[-1] - d * dt / cap_kwh * 100.0)
    return [round(s, 3) for s in soc]


def economic_plan(soc0_pct: float, cap_kwh: float, soc_min_pct: float, p_max_kw: list[float],
                  prices: list[float], c_res: float, energy_limit_kwh: float | None = None, dt: float = 1.0) -> dict:
    T = len(prices)
    usable = max(0.0, (soc0_pct - soc_min_pct) / 100.0 * cap_kwh)
    if energy_limit_kwh is not None:
        usable = min(usable, energy_limit_kwh)
    c = [-(p - c_res) * dt for p in prices]
    res = linprog(c, A_ub=[[dt] * T], b_ub=[usable], bounds=[(0.0, pm) for pm in p_max_kw], method="highs")
    d = [round(max(0.0, float(v)), 3) for v in res.x] if res.success else [0.0] * T
    return {"discharge_kw": d, "soc_pct": soc_trajectory(soc0_pct, cap_kwh, d, dt),
            "expected_margin_idr": round(float(-res.fun), 0) if res.success else 0.0,
            "usable_energy_kwh": round(usable, 3), "solver": "HiGHS LP"}


def track_schedule(soc0_pct: float, cap_kwh: float, soc_min_pct: float, p_max_kw: list[float],
                   ref_kw: list[float], dt: float = 1.0, sensor_sigma_pct: float = 0.10, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    T = len(ref_kw)
    soc = soc0_pct
    executed, traj, horizon_plans = [], [round(soc0_pct, 3)], []
    for k in range(T):
        H = T - k
        c = np.r_[np.zeros(H), np.ones(H)]
        a_ub, b_ub = [], []
        for t in range(H):
            row = np.zeros(2 * H)
            row[t], row[H + t] = 1.0, -1.0
            a_ub.append(row)
            b_ub.append(ref_kw[k + t])
            row = np.zeros(2 * H)
            row[t], row[H + t] = -1.0, -1.0
            a_ub.append(row)
            b_ub.append(-ref_kw[k + t])
        energy = np.r_[np.full(H, dt), np.zeros(H)]
        a_ub.append(energy)
        b_ub.append(max(0.0, (soc - soc_min_pct) / 100.0 * cap_kwh))
        bounds = [(0.0, p_max_kw[k + t]) for t in range(H)] + [(0.0, None)] * H
        res = linprog(c, A_ub=np.array(a_ub), b_ub=b_ub, bounds=bounds, method="highs")
        plan = res.x[:H] if res.success else np.zeros(H)
        d_now = float(plan[0])
        horizon_plans.append([round(float(v), 2) for v in plan])
        measured_drop = d_now * dt / cap_kwh * 100.0
        # plant + sensor: the measured SOC deviates slightly from the model (feedback for the next solve)
        soc = soc - measured_drop + (float(rng.normal(0.0, sensor_sigma_pct)) if d_now > 0 else 0.0)
        executed.append(round(d_now, 3))
        traj.append(round(soc, 3))
    errors = [abs(e - r) for e, r in zip(executed, ref_kw)]
    return {"executed_kw": executed, "soc_pct": traj, "max_tracking_error_kw": round(max(errors) if errors else 0.0, 3),
            "energy_delivered_kwh": round(sum(executed) * dt, 3), "horizon_plans": horizon_plans,
            "feasible": max(errors, default=0.0) <= 0.02 * max(max(p_max_kw, default=0.0), 1.0),
            "method": "receding-horizon LP (HiGHS)"}
