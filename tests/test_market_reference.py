"""EnergyTrade Hub LP reproduces the numbers of Case Study 04 ('Tugas Klas Ekosistem')."""
from common.canonical import REFERENCE_CASE, ClearingRequest, DemandBid, SupplyOffer, Weights, round_to_lots
from enterprises.energytrade.market import clear_market

REF = REFERENCE_CASE
CURVE = [[0.70, 0.0], [0.90, 0.05], [1.00, 1.0]]


def offers(solar_factor: float = 1.0, dr_price: float | None = None, reserve: bool = False) -> list[SupplyOffer]:
    out = [
        SupplyOffer(offer_id="SG", enterprise="solargreen", agent="t", resource="solar", price_rp_per_kwh=900,
                    max_kwh=[v * solar_factor for v in REF["solar_kwh"]], green=True),
        SupplyOffer(offer_id="PG", enterprise="powergrid", agent="t", resource="grid", price_rp_per_kwh=1300,
                    max_kwh=[250.0] * 8, stress_threshold_frac=0.9, risk_tiers=CURVE, emission_kg_per_kwh=0.87),
        SupplyOffer(offer_id="BF", enterprise="batteryflex", agent="t", resource="battery", price_rp_per_kwh=1100,
                    max_kwh=[250.0] * 8, energy_limit_kwh=1000, risk_per_kwh=0.05, power_group="BESS"),
    ]
    if dr_price is not None:
        out.append(SupplyOffer(offer_id="DR", enterprise="industriprima", agent="t", resource="demand_response",
                               price_rp_per_kwh=dr_price, max_kwh=[0.1 * d for d in REF["demand_kwh"]],
                               energy_limit_kwh=400, service_disruption=True))
    if reserve:
        out.append(SupplyOffer(offer_id="RS", enterprise="batteryflex", agent="t", resource="battery_reserve",
                               price_rp_per_kwh=1900, max_kwh=[250.0] * 8, energy_limit_kwh=125, risk_per_kwh=2.0,
                               power_group="BESS"))
    return out


def demand(factor: float = 1.0) -> DemandBid:
    d = [v * factor for v in REF["demand_kwh"]]
    return DemandBid(bid_id="B", enterprise="industriprima", agent="t", demand_kwh=d,
                     flexible_kwh=[0.1 * v for v in d], critical_kwh=[0.9 * v for v in d])


def totals(res) -> dict:
    return {r: round(sum(v), 3) for r, v in res.by_resource().items()}


def test_doc_reference_cost_only_objective():
    """'Dengan asumsi hanya meminimalkan biaya energi': solar 1,500 + battery 1,000 + grid 1,500 = Rp4,400,000."""
    res = clear_market(ClearingRequest(session_id="T1", offers=offers(), demand=demand(),
                                       weights=Weights(alpha=1, beta=0, gamma=0, delta=0)))
    assert res.status == "OPTIMAL"
    assert round(res.cost_rp) == REF["expected_cost_rp"] == 4_400_000
    assert totals(res) == {"solar": 1500, "grid": 1500, "battery": 1000}
    assert sum(res.unserved_kwh) == 0


def test_multi_objective_keeps_doc_dispatch_and_flattens_grid():
    res = clear_market(ClearingRequest(session_id="T2", offers=offers(), demand=demand(), weights=Weights()))
    assert round(res.cost_rp) == 4_400_000
    grid = res.by_resource()["grid"]
    assert max(grid) <= 225 + 1e-6, "convex loading-risk curve keeps grid below the 90% stress zone"
    assert res.objective["stress_kwh"] == 0


def test_solar_drop_40_creates_deficit_then_dr_closes_it():
    short = clear_market(ClearingRequest(session_id="T3", offers=offers(0.6), demand=demand()))
    assert round(sum(short.unserved_kwh)) == 100        # 4,000 - (900 + 1,000 + 2,000)
    fixed = clear_market(ClearingRequest(session_id="T4", offers=offers(0.6, dr_price=1741), demand=demand()))
    t = totals(fixed)
    assert sum(fixed.unserved_kwh) < 1e-6
    assert (t["solar"], t["battery"], t["grid"], t["demand_response"]) == (900, 1000, 2000, 100)
    assert round(fixed.cost_rp) == 900 * 900 + 1000 * 1100 + 2000 * 1300 + 100 * 1741  # Rp4,684,100


def test_battery_and_reserve_share_the_inverter_limit():
    res = clear_market(ClearingRequest(session_id="T5", offers=offers(0.6, reserve=True), demand=demand()))
    alloc = res.by_resource()
    combined = [a + b for a, b in zip(alloc["battery"], alloc.get("battery_reserve", [0] * 8))]
    assert max(combined) <= 250 + 1e-6
    assert sum(res.unserved_kwh) < 1e-6 and round(sum(alloc["battery_reserve"])) == 100


def test_price_cap_rejects_offer():
    bad = offers() + [SupplyOffer(offer_id="X", enterprise="x", agent="t", resource="grid_nonfirm",
                                  price_rp_per_kwh=2100, max_kwh=[10.0] * 8)]
    res = clear_market(ClearingRequest(session_id="T6", offers=bad, demand=demand()))
    assert [r["offer_id"] for r in res.rejected_offers] == ["X"]


def test_marginal_price_is_grid_tariff_in_normal_case():
    res = clear_market(ClearingRequest(session_id="T7", offers=offers(), demand=demand(),
                                       weights=Weights(alpha=1, beta=0, gamma=0, delta=0)))
    assert all(abs(p - 1300) < 1e-6 for p in res.clearing_price_rp_per_kwh)


def test_lot_rounding_preserves_total():
    q = round_to_lots([102, 120, 132, 138, 132, 114, 96, 66])
    assert sum(q) == 900 and q == [100, 120, 130, 140, 130, 110, 100, 70]
    assert sum(round_to_lots([419.7, 500.2, 539.9, 520.4, 440.1, 519.8, 560.3, 499.6])) == 4000
