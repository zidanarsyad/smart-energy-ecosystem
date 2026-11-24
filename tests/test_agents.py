"""Heterogeneous agent paradigms: BDI (IndustriPrima), MPC (BatteryFlex), reactive rules (PowerGrid),
and the orchestrator's negotiation protocols."""
import pytest

from common.canonical import Weights
from enterprises.batteryflex.mpc import economic_plan, track_schedule
from enterprises.industriprima.bdi import allocate_to_processes, build_energy_agent, concession_ask
from enterprises.industriprima.datagen import FLEX_PROCESSES, POLICY
from enterprises.powergrid.datagen import RULES
from enterprises.powergrid.rules import GridSafetyAgent
from orchestrator.negotiation import AlternatingOffersBuyer, buyer_offer, contract_net_award

FLEX = [42.0, 50.0, 54.0, 52.0, 44.0, 52.0, 56.0, 50.0]


@pytest.fixture
def bdi():
    agent = build_energy_agent(dict(POLICY), [dict(p) for p in FLEX_PROCESSES])
    agent.beliefs["fleksibel_per_jam"] = list(FLEX)
    return agent


# ---------------------------------------------------------------- BDI
def test_bdi_proposes_full_dr_when_flexible(bdi):
    out = bdi.handle({"type": "cfp_dr", "conv": "c1", "kebutuhan_kwh": 100})
    assert out["keputusan"] == "USULAN" and out["kuantitas_kwh"] == 100 and out["harga_rp_per_kwh"] == 1950
    assert out["jejak_bdi"]["rencana_terpilih"] == "usulan_dr"


def test_bdi_partial_proposal_is_capped_by_flexible_load(bdi):
    out = bdi.handle({"type": "cfp_dr", "conv": "c2", "kebutuhan_kwh": 500})
    assert out["kuantitas_kwh"] == sum(FLEX) == 400


def test_bdi_urgent_order_overrides_dr(bdi):
    bdi.beliefs["pesanan_mendesak"] = True
    out = bdi.handle({"type": "cfp_dr", "conv": "c3", "kebutuhan_kwh": 100})
    assert out["keputusan"] == "MENOLAK"
    assert out["jejak_bdi"]["rencana_terpilih"] == "tolak_cfp_pesanan_mendesak"   # desire D1 has priority


def test_bargaining_converges_between_private_reservations(bdi):
    bdi.handle({"type": "cfp_dr", "conv": "c4", "kebutuhan_kwh": 100})

    def seller(t, price):
        r = bdi.handle({"type": "tawaran_dr", "conv": "c4", "putaran": t, "harga": price})
        return {"decision": {"MENERIMA": "ACCEPT_PROPOSAL", "TAWARAN_BALIK": "COUNTER_PROPOSE"}.get(
            r["keputusan"], "REJECT_PROPOSAL"), "price": r.get("harga_rp_per_kwh")}

    out = AlternatingOffersBuyer(b0=1300, b_max=2000).run(seller, lambda s: {})
    assert out["agreed"] and out["price"] == 1741 and len(out["rounds"]) == 4
    assert POLICY["harga_reservasi_dr_rp_per_kwh"] <= out["price"] <= 2000


def test_bdi_never_accepts_below_reservation_at_deadline(bdi):
    out = bdi.handle({"type": "tawaran_dr", "conv": "c5", "putaran": 5, "harga": 1400})
    assert out["keputusan"] == "MENOLAK"


def test_dr_event_auto_accept_vs_human_approval(bdi):
    small = bdi.handle({"type": "event_dr", "per_jam_kwh": [0, 0, 0, 0, 0, 50, 50, 0], "harga": 1741})
    big = bdi.handle({"type": "event_dr", "per_jam_kwh": [40] * 8, "harga": 1741})
    assert small["status_event"] == "ACCEPTED"
    assert big["status_event"] == "PENDING_APPROVAL"   # 320 kWh > 200 kWh auto-approval limit


def test_flexible_processes_are_curtailed_in_merit_order():
    plan, loss = allocate_to_processes([30.0], FLEX_PROCESSES)
    assert [p["kode"] for p in plan[0]] == ["FX-01", "FX-02"]
    assert loss == 20 * 1350 + 10 * 1420


def test_concession_is_monotone_and_ends_at_reservation():
    asks = [concession_ask(t, 1950, 1500, 6, 0.6) for t in range(7)]
    assert asks == sorted(asks, reverse=True) and asks[-1] == 1500


# ---------------------------------------------------------------- MPC
PRICES = [1193, 1233, 1274, 1283, 1259, 1275, 1290, 1270]


def test_economic_mpc_uses_usable_energy_in_highest_price_hours():
    plan = economic_plan(100, 1250, 20, [250] * 8, PRICES, c_res=1022.2)
    assert abs(sum(plan["discharge_kw"]) - 1000) < 1e-6 and max(plan["discharge_kw"]) <= 250
    chosen = {i for i, d in enumerate(plan["discharge_kw"]) if d > 0}
    assert chosen == set(sorted(range(8), key=lambda i: -PRICES[i])[:4])
    assert plan["soc_pct"][-1] == pytest.approx(20.0)


def test_economic_mpc_idles_when_prices_below_reservation():
    plan = economic_plan(100, 1250, 20, [250] * 8, [900] * 8, c_res=1022.2)
    assert sum(plan["discharge_kw"]) == 0


def test_tracking_mpc_never_violates_soc_floor():
    out = track_schedule(100, 1250, 20, [250] * 8, [200] * 8)   # asks 1,600 kWh, only 1,000 usable
    assert sum(out["executed_kw"]) <= 1000 + 15        # sensor noise allowance
    assert not out["feasible"]


def test_tracking_mpc_follows_feasible_schedule():
    ref = [70, 130, 160, 130, 60, 110, 160, 180]
    out = track_schedule(100, 1250, 20, [250] * 8, ref)
    assert out["feasible"] and out["max_tracking_error_kw"] <= 5


# ---------------------------------------------------------------- reactive rules
@pytest.fixture
def grid_agent():
    return GridSafetyAgent([{"RULE_ID": r[0], "NAME": r[1], "DESCRIPTION": r[2], "SEVERITY": r[3], "PARAM": r[4]}
                            for r in RULES])


def test_reactive_rules_reject_headroom_violation(grid_agent):
    verdict, findings = grid_agent.check(["08:00", "09:00"], [210, 100], [206, 250], [400, 400], [900, 900])
    assert verdict == "REJECTED" and findings[0].rule_id == "R1"


def test_reactive_rules_warn_in_stress_zone(grid_agent):
    verdict, findings = grid_agent.check(["08:00"], [240], [250], [400], [900])
    assert verdict == "APPROVED_WITH_WARNINGS" and {f.rule_id for f in findings} == {"R2"}


def test_emergency_cfp_refused_when_feeder_is_loaded(grid_agent):
    decision, reason, _ = grid_agent.evaluate_emergency_cfp(["08:00"], [480], [900], 250)
    assert decision == "REFUSE" and "R5" in reason


# ---------------------------------------------------------------- Contract Net
def test_contract_net_awards_by_global_objective_score():
    props = [{"participant": "batteryflex", "decision": "PROPOSE", "price_rp_per_kwh": 1900, "energy_kwh": 125,
              "risk_per_kwh": 2.0},
             {"participant": "industriprima", "decision": "PROPOSE", "price_rp_per_kwh": 1950, "energy_kwh": 100,
              "service_disruption": True},
             {"participant": "powergrid", "decision": "REFUSE"}]
    out = contract_net_award(props, 100, Weights())
    assert [a["participant"] for a in out["awards"]] == ["industriprima"]   # 2,050 < 2,300
    assert [r["participant"] for r in out["rejected"]] == ["batteryflex"]


def test_buyer_concession_schedule():
    sched = [buyer_offer(t, 1300, 2000, 6, 1.5) for t in range(7)]
    assert sched[0] == 1300 and sched[-1] == 2000 and sched == sorted(sched)
