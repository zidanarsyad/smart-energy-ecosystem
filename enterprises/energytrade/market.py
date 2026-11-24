"""EnergyTrade Hub market clearing: a multi-period linear programme solved with HiGHS.

It generalises a uniform-price double auction (merit-order matching) to intertemporal
constraints such as the battery's energy limit, and it directly minimises the global
ecosystem objective of the course document:

    min_x  J(x) = alpha*C(x) + beta*T(x) + gamma*R(x) - delta*Q(x)

    s.t.   sum_r x[r,h] + u[h] = d[h]          for every hourly slot h   (energy balance)
           sum_h x[r,h] <= E[r]                for energy-limited offers  (battery)
           0 <= x[r,h,k] <= q[r,h,k]           offered quantity per tier k (grid: base / stress)

C = pay-as-bid energy cost + VOLL * unserved,  T = DR + unserved energy,
R = risk-kWh (offer risk + forecast spread + grid stress tier),  Q = renewable kWh.
The dual value of each balance constraint is the system marginal price of that slot.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import linprog

from common.canonical import ClearingRequest, ClearingResult, round_to_lots
from common.config import LOT_KWH

PRICE_CAP = 2000.0


def clear_market(req: ClearingRequest, price_cap: float = PRICE_CAP, lot: float = LOT_KWH) -> ClearingResult:
    H = req.window.hours
    w = req.weights
    accepted, rejected, notes = [], [], []
    for o in req.offers:
        if o.price_rp_per_kwh > price_cap:
            rejected.append({"offer_id": o.offer_id, "reason": f"price {o.price_rp_per_kwh} > cap {price_cap}"})
            continue
        if len(o.max_kwh) != H:
            rejected.append({"offer_id": o.offer_id, "reason": f"expected {H} slots, got {len(o.max_kwh)}"})
            continue
        q = round_to_lots(o.max_kwh, lot)
        if abs(sum(q) - sum(o.max_kwh)) > 1e-6:
            notes.append(f"{o.offer_id} ({o.resource}): {sum(o.max_kwh):,.2f} -> {sum(q):,.0f} kWh (10 kWh lots)")
        cap = req.caps.get(o.resource)
        if cap:
            q = [min(a, max(0.0, b)) for a, b in zip(q, cap)]
        limit = None
        if o.energy_limit_kwh is not None:
            limit = math.floor(o.energy_limit_kwh / lot + 1e-9) * lot
        accepted.append((o, q, limit))
    demand = round_to_lots(req.demand.demand_kwh, lot)
    if abs(sum(demand) - sum(req.demand.demand_kwh)) > 1e-6:
        notes.append(f"demand: {sum(req.demand.demand_kwh):,.2f} -> {sum(demand):,.0f} kWh (10 kWh lots)")
    voll = req.demand.max_price_rp_per_kwh

    meta: list[tuple[int, int, str, float]] = []  # (offer index or -1, slot, tier, risk per kWh)
    cost, bounds = [], []
    for i, (o, q, _) in enumerate(accepted):
        unc = o.uncertainty_kwh or [0.0] * H
        for h in range(H):
            unc_ratio = (unc[h] / o.max_kwh[h]) if o.max_kwh[h] > 1e-9 else 0.0
            risk = o.risk_per_kwh + unc_ratio
            base = (w.alpha * o.price_rp_per_kwh + w.beta * (1.0 if o.service_disruption else 0.0)
                    + w.gamma * risk - w.delta * (1.0 if o.green else 0.0))
            curve = o.risk_tiers or ([[o.stress_threshold_frac, 0.0], [1.0, o.stress_risk_per_kwh]]
                                     if o.stress_threshold_frac is not None else [[1.0, 0.0]])
            tiers, lo = [], 0.0
            for k, (upto, tier_risk) in enumerate(curve):
                stress = o.stress_threshold_frac is not None and lo >= o.stress_threshold_frac - 1e-9
                tiers.append(("stress" if stress else f"tier{k}", q[h] * (upto - lo), base + w.gamma * tier_risk,
                              risk + tier_risk))
                lo = upto
            for tier, ub, c, r in tiers:
                meta.append((i, h, tier, r))
                cost.append(c)
                bounds.append([0.0, max(0.0, ub)])
    for h in range(H):
        meta.append((-1, h, "unserved", 0.0))
        cost.append(w.alpha * voll + w.beta)
        bounds.append([0.0, demand[h]])

    for resource, values in req.locked.items():
        for h, v in enumerate(values[:H]):
            if v is None:
                continue
            rest = float(v)
            for k, (i, hh, tier, _) in enumerate(meta):
                if i >= 0 and hh == h and accepted[i][0].resource == resource:
                    take = min(rest, bounds[k][1])
                    bounds[k] = [take, take]
                    rest -= take

    n = len(cost)
    a_eq = np.zeros((H, n))
    for k, (_, h, _, _) in enumerate(meta):
        a_eq[h, k] = 1.0
    a_ub, b_ub = [], []
    for i, (_, _, limit) in enumerate(accepted):
        if limit is not None:
            row = np.zeros(n)
            for k, m in enumerate(meta):
                if m[0] == i:
                    row[k] = 1.0
            a_ub.append(row)
            b_ub.append(limit)
    # offers of the same physical asset (power_group) share its per-slot power limit
    groups: dict[str, list[int]] = {}
    for i, (o, _, _) in enumerate(accepted):
        if o.power_group:
            groups.setdefault(o.power_group, []).append(i)
    for members in (m for m in groups.values() if len(m) > 1):
        for h in range(H):
            row = np.zeros(n)
            for k, m in enumerate(meta):
                if m[0] in members and m[1] == h:
                    row[k] = 1.0
            a_ub.append(row)
            b_ub.append(max(accepted[i][1][h] for i in members))
    res = linprog(np.array(cost), A_ub=np.array(a_ub) if a_ub else None, b_ub=b_ub or None,
                  A_eq=a_eq, b_eq=np.array(demand), bounds=bounds, method="highs")

    offer_ids = [o.offer_id for o, _, _ in accepted]
    base_result = dict(session_id=req.session_id, resource_of={o.offer_id: o.resource for o, _, _ in accepted},
                       enterprise_of={o.offer_id: o.enterprise for o, _, _ in accepted},
                       price_of={o.offer_id: o.price_rp_per_kwh for o, _, _ in accepted},
                       lot_adjustments=notes, rejected_offers=rejected)
    if not res.success:
        return ClearingResult(status="FAILED", allocations={oid: [0.0] * H for oid in offer_ids},
                              unserved_kwh=list(demand), clearing_price_rp_per_kwh=[0.0] * H, cost_rp=0.0,
                              cost_by_offer_rp={}, objective={}, solver={"message": res.message}, **base_result)

    x = res.x
    alloc = {oid: [0.0] * H for oid in offer_ids}
    unserved = [0.0] * H
    risk_total = stress_kwh = 0.0
    for k, (i, h, tier, r) in enumerate(meta):
        if i < 0:
            unserved[h] += x[k]
        else:
            alloc[offer_ids[i]][h] += x[k]
            risk_total += r * x[k]
            if tier == "stress":
                stress_kwh += x[k]
    alloc = {oid: [round(v, 3) for v in vals] for oid, vals in alloc.items()}
    unserved = [round(v, 3) for v in unserved]
    cost_by_offer = {o.offer_id: round(o.price_rp_per_kwh * sum(alloc[o.offer_id]), 2) for o, _, _ in accepted}
    energy_cost = sum(cost_by_offer.values())
    disruption = sum(sum(alloc[o.offer_id]) for o, _, _ in accepted if o.service_disruption) + sum(unserved)
    green = sum(sum(alloc[o.offer_id]) for o, _, _ in accepted if o.green)
    c_total = energy_cost + voll * sum(unserved)
    j = w.alpha * c_total + w.beta * disruption + w.gamma * risk_total - w.delta * green
    marg = getattr(res, "eqlin", None)
    duals = list(marg.marginals) if marg is not None else [0.0] * H
    prices = [round(d / w.alpha, 2) if w.alpha else round(d, 2) for d in duals]
    return ClearingResult(
        status="OPTIMAL", allocations=alloc, unserved_kwh=unserved, clearing_price_rp_per_kwh=prices,
        cost_rp=round(energy_cost, 2), cost_by_offer_rp=cost_by_offer,
        objective={"C_cost_rp": round(c_total, 2), "T_disruption_kwh": round(disruption, 3),
                   "R_risk_kwh": round(risk_total, 3), "Q_renewable_kwh": round(green, 3),
                   "J": round(j, 2), "stress_kwh": round(stress_kwh, 3),
                   "unserved_kwh": round(sum(unserved), 3), "lost_load_cost_rp": round(voll * sum(unserved), 2),
                   "alpha": w.alpha, "beta": w.beta, "gamma": w.gamma, "delta": w.delta},
        solver={"name": "HiGHS via scipy.optimize.linprog", "status": res.status, "message": res.message,
                "variables": n, "constraints": H + len(a_ub), "objective_value": round(float(res.fun), 2)},
        **base_result)
