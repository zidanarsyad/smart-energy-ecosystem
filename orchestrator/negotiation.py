"""Orchestrator-side negotiation protocols.

* FIPA Contract Net (initiator): CFP -> PROPOSE/REFUSE -> evaluate with the J(x) weights ->
  ACCEPT_PROPOSAL / REJECT_PROPOSAL (Smith 1980; FIPA SC00029).
* Alternating-offers bargaining (buyer side) with a time-dependent concession tactic
  (Faratin, Sierra & Jennings 1998):  b(t) = b0 + (b_max - b0) * (t / T)^(1/e),  e > 1 = conceder.
  The buyer accepts the seller's counter-offer s(t) when s(t) <= b(t+1).
"""
from __future__ import annotations

from typing import Callable

from common.canonical import Weights

HUB_PRICE_CAP = 2000.0


def proposal_score(p: dict, w: Weights) -> float:
    """Per-kWh contribution of a proposal to J(x): alpha*price + beta*service + gamma*risk."""
    return round(w.alpha * p["price_rp_per_kwh"] + w.beta * (1.0 if p.get("service_disruption") else 0.0)
                 + w.gamma * p.get("risk_per_kwh", 0.0), 2)


def rank_proposals(proposals: list[dict], w: Weights) -> list[dict]:
    return sorted((dict(p, score=proposal_score(p, w)) for p in proposals if p.get("decision") == "PROPOSE"),
                  key=lambda p: p["score"])


def contract_net_award(proposals: list[dict], need_kwh: float, w: Weights) -> dict:
    ranked = rank_proposals(proposals, w)
    awards, rejected, remaining = [], [], need_kwh
    for p in ranked:
        if remaining <= 1e-6:
            rejected.append(p)
            continue
        qty = round(min(p["energy_kwh"], remaining), 3)
        awards.append(dict(p, awarded_kwh=qty))
        remaining -= qty
    return {"ranked": ranked, "awards": awards, "rejected": rejected, "uncovered_kwh": round(max(0.0, remaining), 3)}


def buyer_offer(t: int, b0: float, b_max: float, deadline: int, e: float) -> float:
    frac = min(1.0, max(0.0, t / deadline))
    return float(round(b0 + (b_max - b0) * frac ** (1.0 / e)))  # whole rupiah


class AlternatingOffersBuyer:
    def __init__(self, b0: float, b_max: float, deadline: int = 6, e: float = 1.5):
        self.b0, self.b_max, self.deadline, self.e = b0, max(b0, b_max), deadline, e

    def schedule(self) -> list[float]:
        return [buyer_offer(t, self.b0, self.b_max, self.deadline, self.e) for t in range(self.deadline + 1)]

    def run(self, send_offer: Callable[[int, float], dict], accept_counter: Callable[[float], dict]) -> dict:
        rounds = []
        for t in range(self.deadline):
            b = buyer_offer(t, self.b0, self.b_max, self.deadline, self.e)
            resp = send_offer(t, b)
            entry = {"round": t, "buyer_offer": b, "seller_decision": resp["decision"],
                     "seller_price": resp.get("price"), "seller_reason": resp.get("reason"),
                     "seller_bdi_plan": resp.get("bdi_plan")}
            rounds.append(entry)
            if resp["decision"] == "ACCEPT_PROPOSAL":
                return {"agreed": True, "price": b, "rounds": rounds, "closed_by": "seller accepted the buyer's offer",
                        "buyer_schedule": self.schedule()}
            if resp["decision"] != "COUNTER_PROPOSE" or resp.get("price") is None:
                return {"agreed": False, "price": None, "rounds": rounds, "closed_by": f"seller {resp['decision']}",
                        "buyer_schedule": self.schedule()}
            counter = float(resp["price"])
            if counter <= buyer_offer(t + 1, self.b0, self.b_max, self.deadline, self.e):
                accept_counter(counter)
                entry["buyer_decision"] = "ACCEPT_PROPOSAL"
                return {"agreed": True, "price": counter, "rounds": rounds,
                        "closed_by": "buyer accepted the seller's counter-offer", "buyer_schedule": self.schedule()}
        return {"agreed": False, "price": None, "rounds": rounds, "closed_by": "deadline reached",
                "buyer_schedule": self.schedule()}
