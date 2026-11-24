"""Global Agentic Engine of the ecosystem (BAB 10, 10.3.B adaptive cycle):

ENVIRONMENT -> DISCOVERY -> PERCEPTION -> REASONING -> PLANNING (market LP) -> GRID SECURITY
-> NEGOTIATION (Contract Net + bargaining) -> REPLANNING -> DECISION -> EXECUTION
-> GOVERNANCE -> MONITORING & EVALUATION

The engine owns no enterprise data: everything it knows arrives through the enterprises'
heterogeneous native APIs and is translated by the interoperability layer (adapters).
"""
from __future__ import annotations

import threading
from typing import Callable

import httpx
from pydantic import BaseModel, Field

from common import multilingual as ml
from common.canonical import ClearingRequest, ClearingResult, DemandBid, SupplyOffer, Weights, WindowSpec
from common.config import ENTERPRISE_NAMES, ENTERPRISE_ORDER, enterprise_url
from common.llm import get_client
from common.timeutil import now_iso
from orchestrator import evaluation, policy
from orchestrator.adapters import ORCH, Adapters
from orchestrator.bus import EnterpriseUnavailable, MessageBus
from orchestrator.collab import EXPLAINER, FACILITATOR, Collaboration, situation_brief
from orchestrator.negotiation import HUB_PRICE_CAP, AlternatingOffersBuyer, rank_proposals
from orchestrator.scenarios import SCENARIOS
from orchestrator.store import OrchestratorStore

LABEL = evaluation.LABELS
RES_OWNER = {"solar": "solargreen", "grid": "powergrid", "battery": "batteryflex"}
FLEX_RESOURCES = ("demand_response", "battery_reserve", "grid_nonfirm")
FIRM_RESOURCES = ("solar", "battery", "grid")


class RunRequest(BaseModel):
    scenario: str = "normal"
    mode: str = "forecast"      # forecast = agents' own predictions | reference = Case Study 04 table values
    onset_slot: int = 0         # disruption starts at this slot; earlier slots keep the plan in execution
    weights: Weights = Field(default_factory=Weights)
    apply_environment: bool = True
    language: str = "en"        # viewer language: synthesis, explanation and the renderings shown to humans
    llm: bool = True            # use the LLM reasoning module (deterministic fallbacks when off/unavailable)
    deliberation: str = "auto"  # auto = when something deviates | always | off
    # What-if: make agents speak another working language this run, e.g. {"batteryflex": "ja"} (default: own language)
    agent_languages: dict[str, str] = Field(default_factory=dict)


class RunFailed(Exception):
    pass


def _to_cache(value):
    if isinstance(value, tuple):
        return {"model": value[0].model_dump(mode="json"), "detail": value[1]}
    return {"list": value}


def _from_cache(kind: str, data: dict):
    if kind == "prices":
        return data["list"]
    model = DemandBid(**data["model"]) if kind == "demand" else SupplyOffer(**data["model"])
    return model, data["detail"]


def _stale_offer(value, src_run: str):
    offer, detail = value
    o = offer.model_copy(update={
        "max_kwh": [round(v * 0.8, 3) for v in offer.max_kwh],
        "energy_limit_kwh": round(offer.energy_limit_kwh * 0.8, 3) if offer.energy_limit_kwh else None,
        "risk_per_kwh": offer.risk_per_kwh + 0.5,
        "notes": offer.notes + [f"STALE: cached from {src_run}, derated 20% because the enterprise is unreachable"]})
    return o, {**detail, "stale_from": src_run}


class EcosystemEngine:
    def __init__(self, store: OrchestratorStore, urls: dict[str, str] | None = None,
                 http: httpx.Client | None = None):
        self.store = store
        self.urls = urls or {eid: enterprise_url(eid) for eid in ENTERPRISE_ORDER}
        self.http = http or httpx.Client(timeout=20.0, limits=httpx.Limits(max_connections=50,
                                                                           max_keepalive_connections=10,
                                                                           keepalive_expiry=5.0))
        self.adapters = Adapters(self.urls)
        self.collab = Collaboration(self.adapters)
        self._lock = threading.Lock()
        self._chat_lock = threading.Lock()
        self._chat_seq = 0

    # ------------------------------------------------------------------ simulation harness
    def apply_environment(self, scenario: str, onset_slot: int = 0) -> dict:
        sc = SCENARIOS[scenario]
        applied = {}
        for eid in ENTERPRISE_ORDER:
            params = {"reset": True, "onset_slot": onset_slot, **sc["env"].get(eid, {})}
            try:
                applied[eid] = self.http.post(self.urls[eid] + "/sim/environment", json=params, timeout=5).json()
            except httpx.HTTPError as exc:
                applied[eid] = {"error": str(exc)}
        self.store.log_environment(scenario, onset_slot, applied)
        return applied

    def registry(self) -> list[dict]:
        out = []
        for eid in ENTERPRISE_ORDER:
            entry = {"id": eid, "name": ENTERPRISE_NAMES[eid], "url": self.urls[eid], "online": False}
            try:
                entry["card"] = self.http.get(self.urls[eid] + "/.well-known/agent-card.json", timeout=3).json()
                entry["health"] = self.http.get(self.urls[eid] + "/health", timeout=3).json()
                entry["environment"] = self.http.get(self.urls[eid] + "/sim/environment", timeout=3).json()
                entry["online"] = True
            except (httpx.HTTPError, ValueError) as exc:
                entry["error"] = str(exc)
            out.append(entry)
        return out

    # ------------------------------------------------------------------ run
    def run(self, req: RunRequest) -> dict:
        if req.scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario '{req.scenario}'")
        if req.mode not in ("forecast", "reference"):
            raise ValueError("mode must be 'forecast' or 'reference'")
        with self._lock:
            return self._run(req)

    def _run(self, req: RunRequest) -> dict:
        window = WindowSpec()
        labels = window.labels()
        onset = max(0, min(int(req.onset_slot), window.hours - 1))
        run_id = self.store.next_run_id()
        bus = MessageBus(self.store, run_id, self.http)
        sc = SCENARIOS[req.scenario]
        plan_in_execution = self.store.latest_run(mode=req.mode) if onset > 0 else None
        baseline = self.store.latest_run(scenario="normal", mode=req.mode)
        trace: list[dict] = []
        degraded: list[dict] = []

        def step(phase: str, actor: str, summary: str, **detail) -> None:
            trace.append({"step": len(trace) + 1, "phase": phase, "actor": actor, "summary": summary,
                          "ts": now_iso(), "detail": detail})

        result = {"run_id": run_id, "created_at": now_iso(),
                  "scenario": {"id": req.scenario, "label": sc["label"], "description": sc["description"]},
                  "mode": req.mode, "onset_slot": onset, "onset_label": labels[onset], "weights": req.weights.model_dump(),
                  "window": {**window.model_dump(), "labels": labels}, "status": "RUNNING", "trace": trace,
                  "degraded": degraded}
        try:
            self._cycle(req, result, bus, window, onset, plan_in_execution, baseline, step, degraded)
        except RunFailed as exc:
            result["status"] = "FAILED"
            result["error"] = str(exc)
            step("FAILED", ORCH["decision"], str(exc))
        result["message_stats"] = dict(bus.stats)
        result["interoperability_success_rate_pct"] = round(
            100.0 * bus.stats["native_ok"] / max(bus.stats["native_calls"], 1), 1)
        result["summary"] = self._summary(result)
        self.store.save_run(result)
        return result

    # ------------------------------------------------------------------ the adaptive cycle
    def _cycle(self, req: RunRequest, result: dict, bus: MessageBus, window: WindowSpec, onset: int,
               plan_in_execution: dict | None, baseline: dict | None, step: Callable, degraded: list) -> None:
        A, H, labels, run_id, w = self.adapters, window.hours, window.labels(), result["run_id"], req.weights
        zeros = [0.0] * H

        if req.apply_environment:
            applied = self.apply_environment(req.scenario, onset)
            unreachable = [eid for eid, env in applied.items() if "error" in env]
            step("ENVIRONMENT", "simulation-harness",
                 f"World set to '{result['scenario']['label']}' from {labels[onset]}. "
                 f"Agents are not told the scenario; they must perceive its effects.",
                 changed_parameters=SCENARIOS[req.scenario]["env"], unreachable=unreachable)

        # 0. DISCOVERY ------------------------------------------------------------------
        online = {}
        for eid in ENTERPRISE_ORDER:
            try:
                A.agent_card(bus, eid)
                online[eid] = True
            except EnterpriseUnavailable:
                online[eid] = False
        step("DISCOVERY", ORCH["discovery"],
             f"{sum(online.values())}/{len(online)} enterprise agent cards discovered (A2A-style discovery).",
             online=online)

        # 1. PERCEPTION -----------------------------------------------------------------
        prices = self._perceive("energytrade", "prices", lambda: A.price_forecast(bus, window), run_id, degraded)
        prices = prices or [1250.0] * H
        solar = self._perceive("solargreen", "solar", lambda: A.solar_offer(bus, window, req.mode), run_id, degraded,
                               stale=_stale_offer)
        demand = self._perceive("industriprima", "demand", lambda: A.demand_bid(bus, window, req.mode, run_id),
                                run_id, degraded)
        grid = self._perceive("powergrid", "grid", lambda: A.grid_headroom(bus, window, run_id), run_id, degraded,
                              stale=_stale_offer)
        battery = self._perceive("batteryflex", "battery", lambda: A.battery_offer(bus, window, prices, run_id),
                                 run_id, degraded, stale=_stale_offer)
        if demand is None:
            raise RunFailed("IndustriPrima's demand plan is unavailable and no cached plan exists")
        bid, demand_detail = demand
        offers = {o.resource: o for o, _ in (x for x in (solar, grid, battery) if x)}
        result["perception"] = {
            "prices_rp_per_kwh": prices, "demand_kwh": bid.demand_kwh, "flexible_kwh": bid.flexible_kwh,
            "demand": demand_detail, "solar": solar[1] if solar else None, "grid": grid[1] if grid else None,
            "battery": battery[1] if battery else None,
            "offers": {r: {"offer_id": o.offer_id, "enterprise": o.enterprise, "price_rp_per_kwh": o.price_rp_per_kwh,
                           "max_kwh": o.max_kwh, "total_kwh": round(sum(o.max_kwh), 2),
                           "energy_limit_kwh": o.energy_limit_kwh, "notes": o.notes} for r, o in offers.items()}}
        step("PERCEPTION", ORCH["perception"],
             "Canonical view built from 4 native protocols: " + ", ".join(
                 f"{LABEL[r]} {min(sum(o.max_kwh), o.energy_limit_kwh or 1e12):,.0f} kWh @Rp{o.price_rp_per_kwh:,.0f}"
                 for r, o in offers.items()) + f"; demand {sum(bid.demand_kwh):,.0f} kWh "
             f"(flexible {sum(bid.flexible_kwh):,.0f} kWh).", degraded=list(degraded))

        # 2. REASONING ------------------------------------------------------------------
        reasoning = self._reason(bid, offers, baseline, degraded)
        result["reasoning"] = reasoning
        bus.internal(ORCH["reasoning"], ORCH["planning"], "INFORM", "situation_assessment",
                     {k: reasoning[k] for k in ("supply_total_kwh", "demand_kwh", "margin_kwh", "flexibility_needed",
                                                "findings")})
        step("REASONING", ORCH["reasoning"], reasoning["headline"], findings=reasoning["findings"])

        # 2b. DELIBERATION (collaboration round table over the A2A layer; advisory) ------
        use_llm = req.llm and get_client().enabled()
        result["llm"] = {"enabled": use_llm, "language": req.language}
        disturbed = reasoning["flexibility_needed"] or bool(reasoning.get("deviation_kwh")) or bool(degraded)
        if req.deliberation == "always" or (req.deliberation == "auto" and disturbed):
            brief = situation_brief(reasoning, result["perception"], degraded, labels)
            delib = self.collab.deliberate(bus, brief, online, req.language, use_llm, req.agent_languages)
            result["deliberation"] = delib
            syn, cs = delib["synthesis"], delib.get("stats") or {}
            langs = ", ".join(ml.label(code) for code in cs.get("languages", []))
            step("DELIBERATION", FACILITATOR,
                 f"Round table with {len(delib['participants'])} agents in {len(cs.get('languages', []))} "
                 f"language(s) ({langs}): {cs.get('messages', 0)} messages in {cs.get('rounds', 0)} rounds; "
                 f"{cs.get('understood', 0)}/{cs.get('interpretations', 0)} foreign-language messages understood by "
                 "the receivers' own LLMs"
                 + (f", numbers kept {cs['numbers_kept_pct']}%" if cs.get("numbers_kept_pct") is not None else "")
                 + (f". Facilitator ({syn.get('provider')}:{syn.get('model')})" if syn.get("llm_ok")
                    else ". Facilitator (deterministic)") + f": {syn.get('joint_assessment') or syn.get('summary')}",
                 recommended_actions=syn.get("recommended_actions"), conflicts=syn.get("conflicts"),
                 working_languages=delib.get("working_languages"), conversation_stats=cs)

        # 3. PLANNING -------------------------------------------------------------------
        locked = {}
        if plan_in_execution and onset > 0:
            prev = plan_in_execution["decision"]["plan_kwh"]
            locked = {r: list(v[:onset]) + [None] * (H - onset) for r, v in prev.items() if r in offers}
        rounds: list[dict] = []
        creq = ClearingRequest(session_id=f"{run_id}-R1", window=window, offers=list(offers.values()), demand=bid,
                               weights=w, locked=locked)
        final = self._clear(bus, creq, rounds, "R1", "initial market clearing with firm supply")
        step("PLANNING", ORCH["planning"],
             f"EnergyTrade Hub LP (R1): cost Rp{final.cost_rp:,.0f}, J={final.objective.get('J'):,.0f}, "
             f"unserved {sum(final.unserved_kwh):,.0f} kWh"
             + (f"; slots before {labels[onset]} locked to plan {plan_in_execution['run_id']}" if locked else ""),
             dispatch_kwh={r: round(sum(v), 1) for r, v in final.by_resource().items()},
             lot_adjustments=final.lot_adjustments)

        # 4. GRID SECURITY --------------------------------------------------------------
        final, verdict, creq = self._grid_security(bus, window, final, creq, offers, grid, rounds, step)

        # 5. NEGOTIATION + REPLANNING ----------------------------------------------------
        negotiation, flex_offers = None, []
        if sum(final.unserved_kwh) > 0.5:
            negotiation, flex_offers = self._negotiate(bus, window, final, offers, bid, w, onset, run_id, step)
            for label, purpose in (("R2", "adaptive re-dispatch with negotiated flexibility"),
                                   ("R3", "re-dispatch after the second Contract Net award pass")):
                if label == "R3":
                    if sum(final.unserved_kwh) <= 0.5 or not negotiation["rejected"]:
                        break
                    extra = self._award_backup(bus, negotiation, final, offers, onset, run_id, step)
                    if not extra:
                        break
                    flex_offers += extra
                if not flex_offers:
                    break
                creq = creq.model_copy(update={"session_id": f"{run_id}-{label}",
                                               "offers": list(offers.values()) + flex_offers})
                final = self._clear(bus, creq, rounds, label, purpose)
                final, verdict, creq = self._grid_security(bus, window, final, creq, offers, grid, rounds, step)
                step("REPLANNING", ORCH["planning"],
                     f"Re-dispatch ({label}): cost Rp{final.cost_rp:,.0f}, J={final.objective.get('J'):,.0f}, "
                     f"unserved {sum(final.unserved_kwh):,.0f} kWh",
                     dispatch_kwh={r: round(sum(v), 1) for r, v in final.by_resource().items()})
            self._send_rejections(bus, negotiation)
        result["negotiation"] = negotiation
        result["market_rounds"] = rounds
        result["grid_security"] = verdict

        # 6. DECISION -------------------------------------------------------------------
        all_offers = list(offers.values()) + flex_offers
        by_res = {r: v for r, v in sorted(final.by_resource().items(), key=lambda kv: evaluation._order(kv[0]))}
        totals = {r: round(sum(v), 2) for r, v in by_res.items()}
        result["decision"] = {
            "session_id": final.session_id, "plan_kwh": by_res, "unserved_kwh": final.unserved_kwh,
            "totals_kwh": totals, "allocations_by_offer": final.allocations,
            "offers": {o.offer_id: {"resource": o.resource, "enterprise": o.enterprise,
                                    "price_rp_per_kwh": o.price_rp_per_kwh} for o in all_offers},
            "marginal_price_rp_per_kwh": final.clearing_price_rp_per_kwh, "objective": final.objective,
            "cost_rp": final.cost_rp, "lot_adjustments": final.lot_adjustments, "solver": final.solver}
        result["local_vs_global"] = self._local_vs_global(by_res, battery, bid, final, offers, negotiation)
        bus.internal(ORCH["decision"], ORCH["execution"], "REQUEST", "execute_plan",
                     {"totals_kwh": totals, "cost_rp": final.cost_rp, "J": final.objective.get("J")})
        step("DECISION", ORCH["decision"],
             "Selected plan: " + ", ".join(f"{LABEL.get(r, r)} {v:,.0f} kWh" for r, v in totals.items() if v > 0.5)
             + (f", unserved {sum(final.unserved_kwh):,.0f} kWh" if sum(final.unserved_kwh) > 0.5 else "")
             + f"; energy cost Rp{final.cost_rp:,.0f}; J(x)={final.objective.get('J'):,.0f}",
             objective=final.objective)

        # 7. EXECUTION (pre-gate: PowerGrid veto) -----------------------------------------
        blocked = verdict.get("verdict") == "REJECTED"
        acks, failed = ({}, []) if blocked else self._execute(bus, window, final, by_res, all_offers, run_id,
                                                               reasoning["cost_allocation"])
        result["cost_allocation"] = acks.pop("_cost_allocation", None)
        result["execution"] = {"blocked": blocked, "acks": acks, "failed": failed}
        step("EXECUTION", ORCH["execution"],
             "Execution blocked by PowerGrid veto" if blocked else
             f"Instructions sent in each native protocol; {len(acks) - len(failed)}/{len(acks)} acknowledged"
             + (f" (failed: {', '.join(failed)})" if failed else ""),
             acks={k: (v.get("status") if isinstance(v, dict) else v) for k, v in acks.items()})

        # 8. GOVERNANCE -----------------------------------------------------------------
        gov_inputs = {"grid_verdict": verdict.get("verdict"),
                      "prices_used": {oid: final.price_of[oid] for oid, v in final.allocations.items() if sum(v) > 1e-6},
                      "dr_kwh": sum(by_res.get("demand_response", zeros)),
                      "unserved_kwh": sum(final.unserved_kwh), "reserve_kwh": sum(by_res.get("battery_reserve", zeros)),
                      "degraded": degraded, "executed": not blocked,
                      "causers": sorted(reasoning.get("deviation_kwh", {}))}
        policies = policy.evaluate(dr_ack=acks.get("industriprima_dr"), acks=acks, **gov_inputs)
        result["governance_inputs"] = gov_inputs
        result["policies"] = policies
        result["status"] = policy.run_status(policies, failed)
        step("GOVERNANCE", ORCH["policy"],
             f"{sum(p['status'] == 'PASS' for p in policies)} pass, {sum(p['status'] == 'PENDING' for p in policies)} "
             f"pending, {sum(p['status'] in ('WARN', 'FAIL') for p in policies)} warn/fail -> {result['status']}",
             policies=[f"{p['id']} {p['status']}" for p in policies])

        # 9. MONITORING & EVALUATION ------------------------------------------------------
        kpis = evaluation.compute_kpis(final, all_offers, grid[1]["headroom_kwh"] if grid else None, acks)
        result["kpis"] = kpis
        monitoring = evaluation.compare(totals, sum(final.unserved_kwh), kpis,
                                        baseline if baseline and baseline["run_id"] != run_id else None)
        result["monitoring"] = monitoring
        bus.internal(ORCH["monitoring"], ORCH["planning"], "INFORM", "evaluation",
                     {"decision_changed": monitoring["decision_changed"], "explanation": monitoring["explanation"],
                      "J": kpis["J"], "total_cost_rp": kpis["total_cost_rp"]})
        step("MONITORING", ORCH["monitoring"],
             (f"Compared with baseline {monitoring['baseline_run']}: " if monitoring["baseline_run"] else "")
             + "; ".join(monitoring["explanation"]), decision_changed=monitoring["decision_changed"])

        # 10. EXPLANATION (natural language, any language) --------------------------------
        explanation = self.collab.explain(result, req.language, use_llm)
        result["explanation"] = explanation
        step("EXPLANATION", EXPLAINER, explanation["text"][:400] + ("..." if len(explanation["text"]) > 400 else ""),
             provider=explanation.get("provider"), model=explanation.get("model"))

    # ------------------------------------------------------------------ helpers
    def _perceive(self, eid: str, kind: str, fn: Callable, run_id: str, degraded: list, stale: Callable | None = None):
        try:
            value = fn()
        except EnterpriseUnavailable as exc:
            cached = self.store.cache_get(eid, kind)
            if not cached:
                degraded.append({"enterprise": eid, "kind": kind, "error": exc.detail,
                                 "fallback": "excluded (no cached data)"})
                return None
            value = _from_cache(kind, cached["data"])
            if stale:
                value = stale(value, cached["run_id"])
            degraded.append({"enterprise": eid, "kind": kind, "error": exc.detail,
                             "fallback": f"stale data from {cached['run_id']}" + (" derated 20%" if stale else "")})
            return value
        self.store.cache_put(eid, kind, run_id, _to_cache(value))
        return value

    def _reason(self, bid: DemandBid, offers: dict[str, SupplyOffer], baseline: dict | None, degraded: list) -> dict:
        cap = {r: round(min(sum(o.max_kwh), o.energy_limit_kwh if o.energy_limit_kwh is not None else 1e12), 2)
               for r, o in offers.items()}
        supply, demand = sum(cap.values()), sum(bid.demand_kwh)
        gap = supply - demand
        findings, deviation = [], {}
        if baseline and baseline.get("reasoning"):
            b_cap = baseline["reasoning"]["supply_capacity_kwh"]
            for r, b in b_cap.items():
                v = cap.get(r, 0.0)
                if r in cap and b and abs(v - b) / b > 0.02:
                    cause = {"solar": "cloud event in SolarGreen's forecast/nowcast",
                             "grid": "reduced feeder headroom reported by PowerGrid",
                             "battery": "reduced BatteryFlex energy/power availability"}.get(r, "changed offer")
                    findings.append(f"{LABEL[r]} {v:,.0f} kWh is {(v - b) / b:+.1%} vs baseline {baseline['run_id']} "
                                    f"({b:,.0f} kWh) - cause: {cause}")
                elif r not in cap:
                    findings.append(f"{LABEL.get(r, r)} missing from perception (baseline {b:,.0f} kWh)")
                if b - v > 1.0 and r in RES_OWNER:
                    deviation[RES_OWNER[r]] = deviation.get(RES_OWNER[r], 0.0) + (b - v)
            bd = baseline["reasoning"].get("demand_kwh")
            if bd and abs(demand - bd) / bd > 0.02:
                findings.append(f"Demand {demand:,.0f} kWh is {(demand - bd) / bd:+.1%} vs baseline ({bd:,.0f} kWh)")
            if bd and demand - bd > 1.0:
                deviation["industriprima"] = deviation.get("industriprima", 0.0) + (demand - bd)
        total_dev = sum(deviation.values())
        allocation = ({e: round(d / total_dev, 4) for e, d in deviation.items()} if total_dev > 0
                      else {"powergrid": 1.0})
        if total_dev > 0:
            findings.append("Causer-pays: any balancing (flexibility) cost is charged to "
                            + ", ".join(f"{e} {s:.0%}" for e, s in allocation.items())
                            + " in proportion to their deviation from the baseline")
        for d in degraded:
            findings.append(f"Degraded perception: {d['enterprise']} {d['kind']} -> {d['fallback']}")
        if gap < 0:
            headline = (f"Deficit {-gap:,.0f} kWh: firm supply {supply:,.0f} kWh < demand {demand:,.0f} kWh "
                        f"-> flexibility must be procured")
        else:
            headline = (f"Supply margin {gap:,.0f} kWh ({gap / demand:.1%}) -> demand can be met from firm supply "
                        f"if hourly limits allow")
        return {"supply_capacity_kwh": cap, "supply_total_kwh": round(supply, 2), "demand_kwh": round(demand, 2),
                "flexible_kwh": round(sum(bid.flexible_kwh), 2), "margin_kwh": round(gap, 2), "headline": headline,
                "findings": findings, "flexibility_needed": gap < 0,
                "deviation_kwh": {e: round(v, 2) for e, v in deviation.items()}, "cost_allocation": allocation}

    def _clear(self, bus: MessageBus, creq: ClearingRequest, rounds: list, label: str, purpose: str) -> ClearingResult:
        try:
            res = self.adapters.clear(bus, creq, ORCH["planning"])
        except EnterpriseUnavailable as exc:
            raise RunFailed(f"EnergyTrade Hub unavailable, the market cannot be cleared ({exc.detail})") from exc
        if res.status != "OPTIMAL":
            raise RunFailed(f"Market clearing {label} failed: {res.solver}")
        rounds.append({"round": label, "session_id": creq.session_id, "purpose": purpose, "cost_rp": res.cost_rp,
                       "J": res.objective.get("J"), "unserved_kwh": round(sum(res.unserved_kwh), 2),
                       "dispatch_kwh": {r: round(sum(v), 2) for r, v in res.by_resource().items()},
                       "marginal_price_rp_per_kwh": res.clearing_price_rp_per_kwh,
                       "lot_adjustments": res.lot_adjustments, "rejected_offers": res.rejected_offers})
        return res

    def _grid_security(self, bus, window, final, creq, offers, grid, rounds, step):
        if "grid" not in offers or grid is None:
            return final, {"verdict": "NOT_APPLICABLE", "findings": []}, creq
        headroom = grid[1]["headroom_kwh"]
        verdict: dict = {}
        for attempt in range(3):
            imports = final.by_resource().get("grid", [0.0] * window.hours)
            try:
                verdict = self.adapters.grid_check(bus, window, imports, ORCH["planning"])
            except EnterpriseUnavailable as exc:
                verdict = {"verdict": "UNAVAILABLE", "findings": [], "error": exc.detail}
                break
            if verdict["verdict"] != "REJECTED" or attempt == 2:
                break
            caps = list(creq.caps.get("grid") or [1e9] * window.hours)
            for f in verdict["findings"]:
                if f["severity"] == "VIOLATION" and f["slot"] is not None:
                    caps[f["slot"]] = min(caps[f["slot"]], headroom[f["slot"]])
            step("GRID_SECURITY", "powergrid.grid_safety_agent",
                 "PowerGrid REJECTED the schedule (" + "; ".join(f"{f['rule']} {f['label']}: {f['message']}"
                                                                 for f in verdict["findings"]
                                                                 if f["severity"] == "VIOLATION")
                 + ") -> security-constrained re-dispatch", findings=verdict["findings"])
            creq = creq.model_copy(update={"caps": {**creq.caps, "grid": caps},
                                           "session_id": f"{creq.session_id}-S{attempt + 1}"})
            final = self._clear(bus, creq, rounds, creq.session_id.split("-", 2)[-1],
                                "security-constrained re-dispatch after PowerGrid veto")
        warn = [f for f in verdict.get("findings", []) if f["severity"] == "WARNING"]
        step("GRID_SECURITY", "powergrid.grid_safety_agent",
             f"Verdict {verdict.get('verdict')}"
             + (f", max feeder utilisation {verdict['max_feeder_utilisation']:.0%}" if "max_feeder_utilisation" in verdict else "")
             + (f", {len(warn)} warnings (" + "; ".join(f"{f['rule']} {f['label']}" for f in warn[:4]) + ")" if warn else ""),
             findings=verdict.get("findings", []))
        return final, verdict, creq

    def _negotiate(self, bus, window, final, offers, bid, w, onset, run_id, step):
        A, H = self.adapters, window.hours
        need = round(sum(final.unserved_kwh), 2)
        slots = list(range(onset, H))
        conv = f"{run_id}-FLEX"
        bus.internal(ORCH["planning"], ORCH["negotiation"], "REQUEST", "flexibility_procurement",
                     {"need_kwh": need, "slots": slots, "unserved_per_slot_kwh": final.unserved_kwh})
        proposals = []
        for eid, fn in (("industriprima", lambda: A.dr_cfp(bus, window, need, slots, conv)),
                        ("batteryflex", lambda: A.battery_reserve_cfp(bus, window, need, conv)),
                        ("powergrid", lambda: A.grid_cfp(bus, window, need, conv))):
            try:
                proposals.append(fn())
            except EnterpriseUnavailable as exc:
                proposals.append({"participant": eid, "decision": "NO_RESPONSE", "reason": exc.detail})
        ranked = rank_proposals(proposals, w)
        scores = {p["participant"]: p["score"] for p in ranked}
        step("NEGOTIATION", ORCH["negotiation"],
             f"Contract Net CFP for {need:,.0f} kWh -> " + "; ".join(
                 f"{p['participant']} {p['decision']}" + (f" {p['energy_kwh']:,.0f} kWh @Rp{p['price_rp_per_kwh']:,.0f} "
                                                         f"(score {scores[p['participant']]:,.0f})"
                                                         if p["decision"] == "PROPOSE" else "")
                 for p in proposals), proposals=proposals)

        grid_tariff = offers["grid"].price_rp_per_kwh if "grid" in offers else 1300.0
        remaining, awards, rejected, failed, bargaining = need, [], [], [], None
        for p in ranked:
            if remaining <= 1e-6:
                rejected.append(p)
                continue
            qty = round(min(p["energy_kwh"], remaining), 3)
            price = p["price_rp_per_kwh"]
            if p["participant"] == "industriprima":
                alternatives = [q["score"] for q in ranked if q["participant"] != "industriprima"]
                alt = min(alternatives) if alternatives else w.alpha * bid.max_price_rp_per_kwh + w.beta
                b_max = min(HUB_PRICE_CAP, (alt - w.beta) / w.alpha if w.alpha else HUB_PRICE_CAP)
                if price > grid_tariff:
                    buyer = AlternatingOffersBuyer(b0=grid_tariff, b_max=b_max, deadline=6, e=1.5)
                    bargaining = buyer.run(lambda t, b: A.dr_bargain_round(bus, conv, t, b, qty),
                                           lambda s: A.dr_close(bus, conv, True, s))
                    bargaining.update({"b0": grid_tariff, "b_max": round(b_max, 2), "e_buyer": 1.5, "deadline": 6,
                                       "initial_ask": price, "quantity_kwh": qty})
                    step("NEGOTIATION", ORCH["negotiation"],
                         "Alternating-offers bargaining with IndustriPrima's BDI agent: " + " | ".join(
                             f"r{r['round']}: offer Rp{r['buyer_offer']:,.0f} -> {r['seller_decision']}"
                             + (f" Rp{r['seller_price']:,.0f}" if r.get("seller_price") else "")
                             for r in bargaining["rounds"])
                         + (f" => agreed Rp{bargaining['price']:,.0f}/kWh ({bargaining['closed_by']})"
                            if bargaining["agreed"] else f" => no agreement ({bargaining['closed_by']})"),
                         bargaining=bargaining)
                    if not bargaining["agreed"]:
                        failed.append(p)
                        continue
                    price = bargaining["price"]
                else:
                    A.dr_close(bus, conv, True, price)
            elif p["participant"] == "batteryflex":
                A.battery_cfp_result(bus, conv, True, qty, "awarded by Contract Net")
            awards.append({**p, "awarded_kwh": qty, "agreed_price_rp_per_kwh": price})
            remaining -= qty
        flex_offers = [self._flex_offer(a, onset, run_id) for a in awards]
        negotiation = {"conversation_id": conv, "need_kwh": need, "slots": slots,
                       "proposals": [dict(p, score=scores.get(p["participant"])) for p in proposals],
                       "awards": awards, "rejected": rejected, "failed": failed,
                       "uncovered_kwh": round(max(0.0, remaining), 3), "bargaining": bargaining}
        step("NEGOTIATION", ORCH["negotiation"],
             "Awards: " + (", ".join(f"{a['participant']} {a['awarded_kwh']:,.0f} kWh @Rp{a['agreed_price_rp_per_kwh']:,.0f}"
                                     for a in awards) or "none")
             + (f"; held in reserve: {', '.join(p['participant'] for p in rejected)}" if rejected else "")
             + (f"; uncovered {remaining:,.0f} kWh" if remaining > 0.5 else ""))
        return negotiation, flex_offers

    @staticmethod
    def _flex_offer(a: dict, onset: int, run_id: str) -> SupplyOffer:
        caps = [v if h >= onset else 0.0 for h, v in enumerate(a["max_kwh"])]
        return SupplyOffer(
            offer_id=f"{a['participant'][:3].upper()}-FLEX-{run_id}", enterprise=a["participant"],
            agent=f"{a['participant']}." + ("bdi_energy_agent" if a["participant"] == "industriprima" else "offer_agent"),
            resource=a["resource"], price_rp_per_kwh=a["agreed_price_rp_per_kwh"], max_kwh=caps,
            energy_limit_kwh=a["awarded_kwh"], risk_per_kwh=a.get("risk_per_kwh", 0.0),
            service_disruption=a.get("service_disruption", False), power_group=a.get("power_group"),
            emission_kg_per_kwh=a.get("emission_kg_per_kwh", 0.0),
            notes=[f"Contract Net award, agreed Rp{a['agreed_price_rp_per_kwh']:,.0f}/kWh"])

    @staticmethod
    def _room_in_deficit(p: dict, final: ClearingResult, offers: dict[str, SupplyOffer]) -> float:
        """Energy a held-back proposal could still add in the hours that are still in deficit
        (respecting the power limit it shares with offers of the same asset)."""
        by_res = final.by_resource()
        H = len(final.unserved_kwh)
        used = list(by_res.get(p["resource"], [0.0] * H))
        if p.get("power_group"):
            for r, o in offers.items():
                if o.power_group == p["power_group"]:
                    used = [a + b for a, b in zip(used, by_res.get(r, [0.0] * H))]
        return sum(min(u, max(0.0, cap - x)) for u, cap, x in zip(final.unserved_kwh, p["max_kwh"], used) if u > 1e-6)

    def _award_backup(self, bus, negotiation: dict, final: ClearingResult, offers: dict[str, SupplyOffer], onset: int,
                      run_id: str, step) -> list[SupplyOffer]:
        """Second Contract Net pass: award proposals held in reserve when the re-dispatch still has a deficit,
        but only if they can actually deliver energy in the hours that are still short."""
        A, conv, extra = self.adapters, negotiation["conversation_id"], []
        need = remaining = round(sum(final.unserved_kwh), 2)
        skipped = []
        for p in list(negotiation["rejected"]):
            if remaining <= 1e-6:
                break
            room = self._room_in_deficit(p, final, offers)
            if room <= 0.5:
                skipped.append(f"{p['participant']} {p['resource']} (no capacity left in the deficit hours)")
                continue
            qty = round(min(p["energy_kwh"], remaining, room), 3)
            try:
                if p["participant"] == "industriprima":
                    A.dr_close(bus, conv, True, p["price_rp_per_kwh"])
                elif p["participant"] == "batteryflex":
                    A.battery_cfp_result(bus, conv, True, qty, "awarded in the second Contract Net pass")
            except EnterpriseUnavailable:
                continue
            award = {**p, "awarded_kwh": qty, "agreed_price_rp_per_kwh": p["price_rp_per_kwh"], "backup": True}
            negotiation["rejected"].remove(p)
            negotiation["awards"].append(award)
            extra.append(self._flex_offer(award, onset, run_id))
            remaining -= qty
        step("NEGOTIATION", ORCH["negotiation"],
             f"Deficit of {need:,.0f} kWh remains after re-dispatch -> second award pass: "
             + (", ".join(f"{o.enterprise} {o.energy_limit_kwh:,.0f} kWh @Rp{o.price_rp_per_kwh:,.0f}" for o in extra)
                or "nothing awardable")
             + (f"; skipped {', '.join(skipped)}" if skipped else ""))
        return extra

    def _send_rejections(self, bus, negotiation: dict) -> None:
        conv = negotiation["conversation_id"]
        for p in negotiation["rejected"]:
            try:
                if p["participant"] == "industriprima":
                    self.adapters.dr_close(bus, conv, False)
                elif p["participant"] == "batteryflex":
                    self.adapters.battery_cfp_result(bus, conv, False, p["energy_kwh"],
                                                     "a better-scoring proposal covered the need")
            except EnterpriseUnavailable:
                pass

    def _execute(self, bus, window, final, by_res, all_offers, run_id, allocation: dict[str, float]):
        A, H, conv = self.adapters, window.hours, f"{run_id}-EXEC"
        zeros = [0.0] * H
        acks, failed = {}, []

        def attempt(key: str, fn: Callable) -> None:
            try:
                acks[key] = fn()
            except EnterpriseUnavailable as exc:
                acks[key] = {"status": "FAILED", "error": exc.detail}
                failed.append(key)

        cost_by_res: dict[str, float] = {}
        for oid, c in final.cost_by_offer_rp.items():
            cost_by_res[final.resource_of[oid]] = cost_by_res.get(final.resource_of[oid], 0.0) + c
        # causer pays: flexibility bought through the Contract Net is a balancing cost charged to the parties
        # whose deviation from the baseline created the deficit (PowerGrid, the DSO, by default)
        balancing = sum(cost_by_res.get(r, 0.0) for r in FLEX_RESOURCES)
        charges = {e: round(balancing * s, 2) for e, s in allocation.items()}
        dr_comp = cost_by_res.get("demand_response", 0.0)
        solar_ids = [oid for oid, r in final.resource_of.items() if r == "solar"]
        if solar_ids and sum(by_res.get("solar", zeros)) > 0.5:
            attempt("solargreen", lambda: A.solar_dispatch(bus, window, solar_ids[0], by_res["solar"], conv,
                                                           charges.get("solargreen", 0.0)))
        if "grid" in by_res:
            grid_total = [a + b for a, b in zip(by_res.get("grid", zeros), by_res.get("grid_nonfirm", zeros))]
            attempt("powergrid", lambda: A.grid_submit(bus, window, grid_total, conv, charges.get("powergrid", 0.0)))
        if "battery" in by_res or "battery_reserve" in by_res:
            attempt("batteryflex", lambda: A.battery_schedule(bus, window, by_res.get("battery", zeros),
                                                              by_res.get("battery_reserve", zeros), conv,
                                                              charges.get("batteryflex", 0.0)))
        dr = by_res.get("demand_response", zeros)
        if sum(dr) > 0.5:
            dr_price = next(final.price_of[oid] for oid, r in final.resource_of.items() if r == "demand_response")
            attempt("industriprima_dr", lambda: A.dr_event(bus, window, dr, dr_price, conv))
        physical = sum(sum(by_res.get(r, zeros)) for r in evaluation.PHYSICAL)
        firm_cost = sum(cost_by_res.get(r, 0.0) for r in FIRM_RESOURCES)
        share = sum(by_res.get("solar", zeros)) / physical * 100 if physical else 0.0
        attempt("industriprima", lambda: A.supply_schedule(bus, window, by_res, final.unserved_kwh, firm_cost,
                                                           dr_comp, share, conv, charges.get("industriprima", 0.0)))
        lines = []
        starts = [s["start"] for s in window.slots()]
        for oid, alloc in final.allocations.items():
            res, seller = final.resource_of[oid], final.enterprise_of[oid]
            buyers = list(allocation.items()) if res in FLEX_RESOURCES else [("industriprima", 1.0)]
            for h, q in enumerate(alloc):
                for buyer, share_b in buyers:
                    if q * share_b > 1e-6 and buyer != seller:  # self-trades net out and are not settled
                        lines.append({"seller": seller, "buyer": buyer, "resource": res, "interval_start": starts[h],
                                      "quantity_kwh": round(q * share_b, 3), "price_idr_kwh": final.price_of[oid]})
        attempt("energytrade", lambda: A.settle(bus, final.session_id, lines))
        acks["_cost_allocation"] = {"rule": "causer pays (imbalance settlement)", "shares": allocation,
                                    "balancing_cost_rp": round(balancing, 2), "charges_rp": charges}
        return acks, failed

    def _local_vs_global(self, by_res, battery, bid, final, offers, negotiation) -> list[dict]:
        rows = []
        if battery:
            local = battery[1]["local_mpc_plan"]["discharge_kw"]
            rows.append({"enterprise": "BatteryFlex", "local_intelligence": "Economic MPC: discharge in the "
                         "highest-price hours", "local_plan": local,
                         "ecosystem_decision": [round(v, 1) for v in by_res.get("battery", [])],
                         "note": "The ecosystem spreads discharge to keep PowerGrid out of its stress zone."})
        if "solar" in offers:
            rows.append({"enterprise": "SolarGreen", "local_intelligence": "Sell the full P50 forecast",
                         "local_plan": [round(v, 1) for v in offers["solar"].max_kwh],
                         "ecosystem_decision": [round(v, 1) for v in by_res.get("solar", [])],
                         "note": "Cheapest, renewable resource: dispatched first."})
        rows.append({"enterprise": "IndustriPrima", "local_intelligence": "Protect production; offer DR only if "
                     "paid at least its private reservation price",
                     "local_plan": [0.0] * len(bid.demand_kwh),
                     "ecosystem_decision": [round(v, 1) for v in by_res.get("demand_response", [0.0] * len(bid.demand_kwh))],
                     "note": (f"DR bought at Rp{negotiation['awards'][0]['agreed_price_rp_per_kwh']:,.0f}/kWh after "
                              "negotiation") if negotiation and negotiation.get("awards") else "No DR needed."})
        if "grid" in offers:
            rows.append({"enterprise": "PowerGrid", "local_intelligence": "Keep import below 90% of headroom",
                         "local_plan": [round(v * (offers['grid'].stress_threshold_frac or 1.0), 1)
                                        for v in offers["grid"].max_kwh],
                         "ecosystem_decision": [round(v, 1) for v in by_res.get("grid", [])],
                         "note": f"Stress-zone energy accepted by the ecosystem: {final.objective.get('stress_kwh', 0):,.0f} kWh"})
        return rows

    def _summary(self, r: dict) -> dict:
        k = r.get("kpis", {})
        return {"scenario_label": r["scenario"]["label"], "status": r["status"], "total_cost_rp": k.get("total_cost_rp"),
                "J": k.get("J"), "dr_kwh": k.get("dr_kwh"), "unserved_kwh": k.get("unserved_kwh"),
                "renewable_share_pct": k.get("renewable_share_pct"), "dispatch_kwh": k.get("dispatch_kwh"),
                "decision_changed": (r.get("monitoring") or {}).get("decision_changed"),
                "messages": (r.get("message_stats") or {}).get("messages"), "error": r.get("error")}

    # ------------------------------------------------------------------ conversations (any language)
    def ask_run(self, run_id: str, question: str, language: str = "auto") -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return self.collab.ask_run(run, question, language)

    def ask_agent(self, eid: str, question: str, language: str = "auto") -> dict:
        """Talk to an enterprise agent through the A2A layer (the agent treats us as an external party)."""
        if eid not in self.urls:
            raise KeyError(eid)
        with self._chat_lock:
            self._chat_seq += 1
            conv = f"CHAT-{now_iso()[11:19].replace(':', '')}-{self._chat_seq:03d}"
        bus = MessageBus(self.store, conv, self.http)
        out = self.adapters.a2a_send(bus, eid, mode="ask", text=question, language=language,
                                     sender="human-via-control-center", task="conversation")
        return {"answer": out["text"], "conversation_id": conv, "agent": out["meta"].get("agent"),
                "enterprise": out["meta"].get("enterprise"), **{k: out["data"].get(k) for k in
                                                                 ("provider", "model", "latency_ms", "llm_ok")}}

    def converse(self, from_eid: str, to_eid: str, instruction: str, language: str = "en") -> dict:
        """Agent-to-agent conversation across languages (e.g. SolarGreen in Mandarin -> PowerGrid in Bahasa)."""
        for eid in (from_eid, to_eid):
            if eid not in self.urls:
                raise KeyError(eid)
        if from_eid == to_eid:
            raise ValueError("Choose two different agents")
        with self._chat_lock:
            self._chat_seq += 1
            conv = f"A2A-{now_iso()[11:19].replace(':', '')}-{self._chat_seq:03d}"
        bus = MessageBus(self.store, conv, self.http)
        out = self.collab.converse(bus, from_eid, to_eid, instruction, language, get_client().enabled())
        return {"conversation_id": conv, **out}

    # ------------------------------------------------------------------ human-in-the-loop sync
    def sync_approvals(self, run_id: str) -> dict:
        with self._lock:
            run = self.store.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            acks = run.get("execution", {}).get("acks", {})
            ack = acks.get("industriprima_dr")
            if not ack or ack.get("status") != "PENDING_APPROVAL":
                return run
            bus = MessageBus(self.store, run_id, self.http)
            bus.seq = len(self.store.messages(run_id))
            report = self.adapters.dr_report(bus, ack["event_id"])
            if report["status"] != ack["status"]:
                ack["status"] = report["status"]
                ack["approved_by_human"] = report["status"] == "ACCEPTED"
                run["policies"] = policy.evaluate(dr_ack=ack, acks=acks, **run["governance_inputs"])
                run["status"] = policy.run_status(run["policies"], run["execution"].get("failed", []))
                run["trace"].append({"step": len(run["trace"]) + 1, "phase": "HUMAN_APPROVAL",
                                     "actor": "human:Energy Manager (IndustriPrima)",
                                     "summary": f"DR event {ack['event_id']} -> {report['status']}; run status "
                                                f"{run['status']}", "ts": now_iso(), "detail": report})
                run["summary"] = self._summary(run)
                self.store.save_run(run)
            return run
