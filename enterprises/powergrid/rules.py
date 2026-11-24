"""PowerGrid Grid Safety Agent: a *reactive* agent (condition -> action rules, no planning).

Rules are stored in the EMS database (GRID_RULE) so grid engineers can tune parameters
without code changes. The agent has veto power: a VIOLATION rejects the schedule.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Finding:
    rule_id: str
    name: str
    severity: str
    interval: str
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


class GridSafetyAgent:
    def __init__(self, rules: list[dict]):
        self.rules = {r["RULE_ID"]: r for r in rules}

    def p(self, rule_id: str) -> float:
        return float(self.rules[rule_id]["PARAM"])

    def check(self, labels: list[str], import_kw: list[float], headroom_kw: list[float],
              bg_kw: list[float], usable_kw: list[float]) -> tuple[str, list[Finding]]:
        f: list[Finding] = []
        prev = None
        for lab, imp, head, bg, usable in zip(labels, import_kw, headroom_kw, bg_kw, usable_kw):
            if imp > head + self.p("R1"):
                f.append(Finding("R1", "CONTRACT_HEADROOM", "VIOLATION", lab,
                                 f"import {imp:.0f} kW > headroom {head:.0f} kW"))
            elif head > 0 and imp > self.p("R2") * head + 1e-6:
                f.append(Finding("R2", "STRESS_ZONE", "WARNING", lab,
                                 f"import {imp:.0f} kW is {imp / head:.0%} of headroom"))
            if prev is not None and abs(imp - prev) > self.p("R3"):
                f.append(Finding("R3", "RAMP_RATE", "WARNING", lab, f"ramp {imp - prev:+.0f} kW/h"))
            if usable > 0 and (bg + imp) / usable > self.p("R4"):
                f.append(Finding("R4", "FEEDER_THERMAL", "WARNING", lab,
                                 f"feeder loading {(bg + imp) / usable:.0%} of usable capacity"))
            if imp < self.p("R6"):
                f.append(Finding("R6", "NO_EXPORT", "VIOLATION", lab, f"reverse flow {imp:.0f} kW"))
            prev = imp
        if any(x.severity == "VIOLATION" for x in f):
            verdict = "REJECTED"
        elif f:
            verdict = "APPROVED_WITH_WARNINGS"
        else:
            verdict = "APPROVED"
        return verdict, f

    def evaluate_emergency_cfp(self, labels: list[str], bg_kw: list[float], usable_kw: list[float],
                               contract_kw: float) -> tuple[str, str, list[float]]:
        """Contract-Net participant behaviour for non-firm emergency import."""
        util = [(bg + contract_kw) / u if u > 0 else 9.9 for bg, u in zip(bg_kw, usable_kw)]
        limit = self.p("R5")
        if max(util) > limit:
            worst = labels[util.index(max(util))]
            return ("REFUSE", f"R5 EMERGENCY_IMPORT: feeder utilisation forecast {max(util):.0%} at {worst} "
                              f"exceeds {limit:.0%}; N-1 security margin must be kept", [])
        spare = [max(0.0, limit * u - bg - contract_kw) for bg, u in zip(bg_kw, usable_kw)]
        return "PROPOSE", "Non-firm capacity available within R5 utilisation limit", spare
