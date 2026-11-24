"""Collaboration & communication layer of the orchestrator (LLM-assisted and advisory).

* deliberate(): ecosystem round table over the A2A conversational layer, in two rounds. Round 1: every
  enterprise agent states its position, writing its message in its own working language (Mandarin, Bahasa
  Indonesia, English) with its own LLM. Round 2 (cross-talk): every agent's own LLM interprets the others'
  messages into its own language (translation + FIPA-ACL performative + intent, numbers checked) and the agent
  replies. The Facilitator Agent then synthesises agreements, conflicts and a joint recommendation.
* converse():   direct agent-to-agent conversation (e.g. SolarGreen in Mandarin <-> PowerGrid in Bahasa Indonesia).
* explain():    natural-language explanation of a run, in any language.
* ask_run():    grounded question answering about a run, in any language.
Binding decisions stay with the deterministic engine (LP market clearing, Contract Net, rules, BDI, MPC):
LLM output is advice and communication, so results remain reproducible and auditable.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from common import multilingual as ml
from common.config import ENTERPRISE_NAMES, ENTERPRISE_ORDER
from common.llm import GEMINI_FLASH_LITE, get_client, language_instruction, language_reminder
from orchestrator.adapters import ORCH, Adapters
from orchestrator.bus import EnterpriseUnavailable, MessageBus

FACILITATOR = "orchestrator.facilitator_agent"
EXPLAINER = "orchestrator.explainer_agent"
# The ecosystem operator runs its own agents on its own vendor's model (Google Gemini), like any other member.
FACILITATOR_MODEL = GEMINI_FLASH_LITE
EXPLAINER_MODEL = GEMINI_FLASH_LITE
GROUNDING = ("Each 'why' in negotiation.not_selected applies only to that participant: never combine the reasons "
             "or rules of different parties (e.g. PowerGrid's rule R5 limits only PowerGrid's own import). ")


def situation_brief(reasoning: dict, perception: dict, degraded: list, labels: list[str]) -> dict:
    """Shared (non-private) facts for the round table, in canonical units."""
    return {
        "headline": reasoning["headline"],
        "window": f"{labels[0]}-{int(labels[-1][:2]) + 1:02d}:00 WIB, 8 hourly slots",
        "demand_kwh": reasoning["demand_kwh"], "flexible_demand_kwh": reasoning["flexible_kwh"],
        "firm_supply_already_counted_kwh": reasoning["supply_capacity_kwh"],
        "note": "The firm offers above (solar, grid within contract, battery down to 20% SOC) are ALREADY counted. "
                "A deficit can only be covered by ADDITIONAL flexibility from the options below.",
        "flexibility_options": [
            "IndustriPrima demand response: curtail or shift flexible load, max 10% of each hour, paid compensation",
            "BatteryFlex deep-discharge reserve: SOC band 10-20%, needs PowerGrid emergency authorisation",
            "PowerGrid non-firm emergency import: only if feeder utilisation stays below 70% (rule R5)"],
        "decision_rules": "Binding decision by LP market clearing + Contract Net negotiation; the balancing cost of "
                          "flexibility is paid by the parties whose deviation caused the deficit (causer pays).",
        "margin_kwh": reasoning["margin_kwh"],
        "deviation_from_baseline_kwh": reasoning.get("deviation_kwh", {}),
        "findings": reasoning["findings"],
        "hourly_demand_kwh": dict(zip(labels, perception.get("demand_kwh", []))),
        "degraded_inputs": [f"{d['enterprise']} {d['kind']}: {d['fallback']}" for d in degraded],
        "question": "How does this affect you, what can you contribute, what must you protect, "
                    "and what should the ecosystem do?",
    }


def conversation_stats(conversation: list[dict]) -> dict:
    """How well did the agents understand each other across languages?"""
    interpretations = [u for m in conversation for u in m.get("understood_by", {}).values() if not u.get("passthrough")]
    understood = [u for u in interpretations if u.get("translation") and u.get("translated", True)]
    kept = sum((u.get("numbers") or {}).get("kept", 0) for u in understood)
    total = sum((u.get("numbers") or {}).get("total", 0) for u in understood)
    return {"messages": len(conversation), "rounds": len({m["round"] for m in conversation}),
            "languages": sorted({m["language"] for m in conversation if m.get("language")}),
            "interpretations": len(interpretations), "understood": len(understood),
            "same_language_deliveries": sum(1 for m in conversation for u in m.get("understood_by", {}).values()
                                            if u.get("passthrough")),
            "numbers_kept": kept, "numbers_total": total,
            "numbers_kept_pct": round(100 * kept / total, 1) if total else None}


def _model(d: dict) -> str:
    return f"{d.get('provider')}:{d.get('model')}" if d.get("llm_ok") else "deterministic"


def run_digest(run: dict) -> dict:
    k, d = run.get("kpis", {}), run.get("decision", {})
    neg = run.get("negotiation") or {}
    delib_all = run.get("deliberation") or {}
    delib = delib_all.get("synthesis") or {}
    return {
        "run_id": run["run_id"], "scenario": run["scenario"]["label"], "mode": run["mode"], "status": run["status"],
        "disruption_starts": run.get("onset_label"), "reasoning": (run.get("reasoning") or {}).get("findings"),
        "deliberation_summary": delib.get("joint_assessment"),
        "agent_positions": {ENTERPRISE_NAMES.get(e, e): r.get("message_viewer") or r.get("message") for e, r in
                            (delib_all.get("responses") or {}).items()},
        "round_table_conversation": [{"from": ENTERPRISE_NAMES.get(m["from"], m["from"]), "round": m["round"],
                                      "spoken_in": ml.name(m["language"]), "said_rendering": m["viewer_text"][:220]}
                                     for m in delib_all.get("conversation", [])][:10] or None,
        "negotiation": {"need_kwh": neg.get("need_kwh"),
                        "proposals": [{k2: p.get(k2) for k2 in ("participant", "decision", "energy_kwh",
                                                                 "price_rp_per_kwh", "score", "reason")}
                                      for p in neg.get("proposals", [])],
                        "awards": [{k2: a.get(k2) for k2 in ("participant", "awarded_kwh", "agreed_price_rp_per_kwh")}
                                   for a in neg.get("awards", [])],
                        "not_selected": [{"participant": p["participant"],
                                          "why": "not selected on cost alone: a proposal with a lower J(x) score per "
                                                 "kWh already covered the need. The offer was never dispatched, so "
                                                 "its conditions (e.g. an emergency authorisation) were never "
                                                 "requested, granted or refused."}
                                         for p in neg.get("rejected", [])]
                                        + [{"participant": p["participant"],
                                            "why": f"{p['participant']} declined to make an offer: {p.get('reason')}. "
                                                   f"This refusal concerns only {p['participant']}'s own supply."}
                                           for p in neg.get("proposals", []) if p.get("decision") == "REFUSE"],
                        "bargaining_rounds": [(r["buyer_offer"], r["seller_decision"], r.get("seller_price"))
                                              for r in ((neg.get("bargaining") or {}).get("rounds") or [])]} if neg else None,
        "decision_kwh": d.get("totals_kwh"), "unserved_kwh": k.get("unserved_kwh"),
        "kpis": {x: k.get(x) for x in ("total_cost_rp", "firm_energy_cost_rp", "flexibility_cost_rp", "J",
                                       "renewable_share_pct", "emissions_kg", "peak_grid_utilisation_pct")},
        "grid_security": (run.get("grid_security") or {}).get("verdict"),
        "policies": [f"{p['id']} {p['policy']}: {p['status']} ({p['detail']})" for p in run.get("policies", [])],
        "pending_human_approvals": [p["detail"] for p in run.get("policies", []) if p["status"] == "PENDING"] or "none",
        "balancing_cost": (run.get("cost_allocation") if (run.get("cost_allocation") or {}).get("balancing_cost_rp")
                           else "no balancing (flexibility) cost in this run, so nobody pays a balancing charge"),
        "local_utilities_rp": k.get("local_utilities_rp"),
        "change_vs_baseline": (run.get("monitoring") or {}).get("explanation"),
        "kpi_changes_vs_baseline": [{x["kpi"]: f"{x['baseline']} -> {x['current']} (delta {x['delta']:+})"}
                                    for x in (run.get("monitoring") or {}).get("kpis", [])],
    }


def deterministic_explanation(run: dict) -> str:
    parts = [f"{run['scenario']['label']} ({run['mode']} inputs) ended with status {run['status']}."]
    if run.get("reasoning"):
        parts.append(run["reasoning"]["headline"] + ".")
    neg = run.get("negotiation")
    if neg and neg.get("awards"):
        parts.append("Flexibility was procured by Contract Net: " + ", ".join(
            f"{a['participant']} {a['awarded_kwh']:,.0f} kWh at Rp{a['agreed_price_rp_per_kwh']:,.0f}/kWh"
            for a in neg["awards"]) + ".")
    parts.extend((run.get("monitoring") or {}).get("explanation", []))
    pending = [p["policy"] for p in run.get("policies", []) if p["status"] == "PENDING"]
    if pending:
        parts.append("Waiting for humans: " + "; ".join(pending) + ".")
    return " ".join(parts)


class Collaboration:
    def __init__(self, adapters: Adapters):
        self.adapters = adapters

    @staticmethod
    def _parallel(items: list, fn) -> dict:
        if not items:
            return {}
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            return dict(pool.map(fn, items))

    @staticmethod
    def _msg(mid: str, rnd: int, sender: str, to, language: str | None, perf: str | None, text: str,
             viewer_text: str | None, data: dict) -> dict:
        return {"id": mid, "round": rnd, "from": sender, "to": to, "language": language or "en",
                "performative": perf or "INFORM", "text": text, "viewer_text": viewer_text or text,
                "model": _model(data), "understood_by": {}}

    def deliberate(self, bus: MessageBus, brief: dict, online: dict, language: str, use_llm: bool = True,
                   agent_languages: dict | None = None) -> dict:
        participants = [e for e in ENTERPRISE_ORDER if online.get(e)]
        what_if = {e: lang for e, lang in (agent_languages or {}).items() if lang}
        bus.internal(FACILITATOR, "all enterprise agents", "CFP", "ecosystem_deliberation",
                     {"headline": brief["headline"], "participants": participants, "viewer_language": language})

        def send(eid: str, **kw):
            try:
                return eid, self.adapters.a2a_send(bus, eid, language=language, sender=FACILITATOR,
                                                   agent_language=what_if.get(eid), use_llm=use_llm, **kw)
            except EnterpriseUnavailable as exc:
                return eid, {"error": exc.detail}

        # Round 1: every agent states its position; its message is written in its own working language.
        r1 = self._parallel(participants, lambda eid: send(eid, mode="deliberate", text=brief["headline"],
                                                            data={**brief, "llm": use_llm},
                                                            task="ecosystem_deliberation"))
        responses = {eid: (r.get("data") or {"message": r.get("text", "")}) if "error" not in r else {"error": r["error"]}
                     for eid, r in r1.items()}
        conversation = [self._msg(f"r1-{eid}", 1, eid, ["all"], r.get("language"), r.get("performative"),
                                  r.get("message", ""), r.get("message_viewer"), r)
                        for eid, r in responses.items() if not r.get("error") and r.get("message")]

        # Round 2 (cross-talk): each receiver's OWN LLM interprets the others' messages, then the agent replies.
        speakers = [m["from"] for m in conversation]
        working = {eid: responses[eid].get("language") for eid in speakers}

        def round2(eid: str):
            incoming = [{"id": m["id"], "from": m["from"], "from_name": ENTERPRISE_NAMES.get(m["from"], m["from"]),
                         "language": m["language"], "performative": m["performative"], "text": m["text"]}
                        for m in conversation if m["from"] != eid]
            mine = responses[eid]
            return send(eid, mode="crosstalk", text=f"{len(incoming)} round-table messages",
                        data={"messages": incoming, "headline": brief["headline"],
                              "my_position": {"message": mine.get("message"),
                                              "can_contribute": mine.get("can_contribute")}},
                        task="ecosystem_crosstalk")

        r2 = self._parallel(speakers, round2) if len(speakers) > 1 else {}
        by_id = {m["id"]: m for m in conversation}
        for eid in speakers:
            r = r2.get(eid) or {}
            if not r or "error" in r:
                continue
            d = r.get("data") or {}
            working[eid] = d.get("language") or working[eid]
            for mid, u in (d.get("understood") or {}).items():
                if mid in by_id:
                    by_id[mid]["understood_by"][eid] = {**u, "language": d.get("language"), "model": _model(d)}
            if d.get("reply"):
                conversation.append(self._msg(f"r2-{eid}", 2, eid, d.get("addressed_to") or ["all"],
                                              d.get("reply_language"), d.get("reply_performative"), d["reply"],
                                              d.get("reply_viewer"), d))
        stats = conversation_stats(conversation)
        synthesis = self._facilitate(brief, responses, conversation, language, use_llm)
        bus.internal(FACILITATOR, ORCH["planning"], "INFORM", "deliberation_synthesis",
                     {"joint_assessment": synthesis.get("joint_assessment"),
                      "recommended_actions": synthesis.get("recommended_actions"), "conversation_stats": stats})
        return {"brief": brief, "participants": participants, "responses": responses, "conversation": conversation,
                "working_languages": working, "stats": stats, "synthesis": synthesis, "language": language,
                "advisory": True}

    def converse(self, bus: MessageBus, from_eid: str, to_eid: str, instruction: str, language: str = "en",
                 use_llm: bool = True) -> dict:
        """Direct agent-to-agent exchange: the sender writes in its language, the receiver's own LLM understands it
        and answers in the receiver's language, and the sender's own LLM understands the answer."""
        names = ENTERPRISE_NAMES
        sent = self.adapters.a2a_send(bus, from_eid, mode="compose", text=instruction,
                                      data={"to": to_eid, "to_name": names[to_eid]}, language=language,
                                      sender="human operator (Control Center)", task="agent_conversation",
                                      use_llm=use_llm)["data"]
        ans = self.adapters.a2a_send(bus, to_eid, mode="converse", text=sent.get("message") or instruction,
                                     data={"from": from_eid, "from_name": names[from_eid],
                                           "source_language": sent.get("language")},
                                     language=language, sender=f"{from_eid}.agent", task="agent_conversation",
                                     use_llm=use_llm, message_type=sent.get("performative") or "QUERY")["data"]
        back = {}
        if ans.get("reply"):
            back = self.adapters.a2a_send(bus, from_eid, mode="understand", text=ans["reply"],
                                          data={"from": to_eid, "from_name": names[to_eid],
                                                "source_language": ans.get("reply_language")},
                                          language=language, sender=f"{to_eid}.agent", task="agent_conversation",
                                          use_llm=use_llm, message_type=ans.get("reply_performative") or "INFORM")["data"]
        messages = [
            {"from": from_eid, "to": [to_eid], "language": sent.get("language"), "performative": sent.get("performative"),
             "text": sent.get("message"), "viewer_text": sent.get("message_viewer"), "model": _model(sent),
             "understood_by": {to_eid: {**(ans.get("understood") or {}), "language": ans.get("language"),
                                        "model": _model(ans)}}},
            {"from": to_eid, "to": [from_eid], "language": ans.get("reply_language"),
             "performative": ans.get("reply_performative"), "text": ans.get("reply"),
             "viewer_text": ans.get("reply_viewer"), "model": _model(ans),
             "understood_by": {from_eid: {**(back.get("understood") or {}), "language": back.get("language"),
                                          "model": _model(back) if back else "-"}}}]
        return {"instruction": instruction, "viewer_language": language, "messages": messages,
                "stats": conversation_stats([{**m, "round": 1} for m in messages])}

    def _facilitate(self, brief: dict, responses: dict, conversation: list[dict], language: str,
                    use_llm: bool) -> dict:
        positions = {ENTERPRISE_NAMES.get(e, e): {k: r.get(k) for k in ("assessment", "can_contribute", "constraints",
                                                                       "recommendation", "concerns")}
                     for e, r in responses.items() if not r.get("error")}
        dialogue = [{"from": ENTERPRISE_NAMES.get(m["from"], m["from"]), "round": m["round"],
                     "spoken_in": ml.name(m["language"]), "said": m["text"], "sender_rendering": m["viewer_text"]}
                    for m in conversation]
        reply = None
        if use_llm:
            messages = [
                {"role": "system", "content":
                    "You are the Facilitator Agent of Shakti Tantra, a multi-enterprise energy ecosystem (academic "
                    "simulation, fictional companies). You moderate a collaborative round table between enterprise "
                    "agents with different paradigms (ML forecaster, reactive safety agent, BDI agent, LP market, MPC "
                    "battery) that speak different languages and run different LLMs. Synthesise their positions "
                    "fairly: find agreements and conflicts of interest and propose a joint course of action for the "
                    "deterministic engine (LP market clearing, Contract Net negotiation, safety rules), which makes the "
                    "binding decision. Respect each party's constraints and privacy; never invent numbers. Rule R5 "
                    "limits only PowerGrid's own non-firm import; BatteryFlex's deep-discharge reserve needs a separate "
                    "emergency authorisation (policy G5, a human approval). " + language_instruction(language)},
                {"role": "user", "content":
                    "SITUATION BRIEF (JSON):\n" + json.dumps(brief, ensure_ascii=False, default=str)[:4500]
                    + "\n\nAGENT POSITIONS (JSON):\n" + json.dumps(positions, ensure_ascii=False, default=str)[:5000]
                    + "\n\nROUND-TABLE CONVERSATION (each agent spoke its own language; round 2 replies react to "
                      "round 1):\n" + json.dumps(dialogue, ensure_ascii=False, default=str)[:5000]
                    + "\n\nReturn ONLY a JSON object with keys: joint_assessment (2-3 sentences), agreements (list of "
                      "short strings), conflicts (list of short strings), recommended_actions (list of 2-5 concrete "
                      "actions that use ONLY the flexibility options in the brief; firm supply is already counted), "
                      "negotiation_guidance (1 sentence), summary (1 sentence). Do not invent prices or quantities "
                      "that are not in the facts. Write the text in the requested language."}]
            reply = get_client().chat(messages, prefer=FACILITATOR_MODEL, json_mode=True, max_tokens=1200,
                                      temperature=0.2, agent=FACILITATOR)
        if reply and reply.ok and reply.data:
            d = reply.data

            def as_list(v):
                return [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])

            out = {"joint_assessment": str(d.get("joint_assessment", "")), "agreements": as_list(d.get("agreements")),
                   "conflicts": as_list(d.get("conflicts")), "recommended_actions": as_list(d.get("recommended_actions")),
                   "negotiation_guidance": str(d.get("negotiation_guidance", "")), "summary": str(d.get("summary", ""))}
        else:
            deficit = brief["margin_kwh"] < 0 or bool(brief.get("deviation_from_baseline_kwh"))
            out = {"joint_assessment": brief["headline"],
                   "agreements": ["All parties keep their own safety and governance constraints."],
                   "conflicts": ["Flexibility has a price: the cost is charged to the causing parties (causer pays)."]
                   if deficit else [],
                   "recommended_actions": (["Clear the market with firm supply first",
                                            "Run a Contract Net for the remaining deficit (DR, battery reserve, "
                                            "non-firm import)", "Respect PowerGrid's safety rules and human approvals"]
                                           if deficit else ["Keep the firm dispatch"]),
                   "negotiation_guidance": "Prefer the proposal with the lowest J(x) contribution per kWh.",
                   "summary": "[LLM unavailable] deterministic synthesis of the round table."}
        out.update((reply.meta() if reply else {"llm_ok": False, "provider": "none", "model": ""}))
        return out

    def explain(self, run: dict, language: str, use_llm: bool = True) -> dict:
        reply = None
        if use_llm:
            messages = [
                {"role": "system", "content":
                    "You are the Explainer Agent of Shakti Tantra, a multi-enterprise agentic energy ecosystem "
                    "(academic simulation). Explain a run to a lecturer and to managers of the companies. Use only the "
                    "facts given; never invent numbers; plain prose without headings. " + GROUNDING
                    + language_instruction(language)},
                {"role": "user", "content":
                    "RUN FACTS (JSON):\n" + json.dumps(run_digest(run), ensure_ascii=False, default=str)[:9000]
                    + "\n\nIn 5-8 sentences: what changed in the environment, how the agents perceived and discussed it, "
                      "what was negotiated, what was decided and why (cost, risk, renewables), who pays the balancing "
                      "cost, and what still needs human approval. If round_table_conversation is present, say in one "
                      "sentence that the agents spoke different languages (name them) and understood each other "
                      "through their own LLMs. Mention human approvals ONLY if pending_human_approvals lists them; if "
                      "it is 'none', say nothing is pending."}]
            reply = get_client().chat(messages, prefer=EXPLAINER_MODEL, max_tokens=900, temperature=0.2,
                                      agent=EXPLAINER)
        text = reply.text if reply and reply.ok else deterministic_explanation(run)
        return {"text": text, "language": language,
                **(reply.meta() if reply else {"llm_ok": False, "provider": "none", "model": ""})}

    def ask_run(self, run: dict, question: str, language: str = "auto") -> dict:
        messages = [
            {"role": "system", "content":
                "You are the Explainer Agent of Shakti Tantra, a multi-enterprise agentic energy ecosystem (academic "
                "simulation). Answer questions about one run using only the facts given; if the facts do not contain "
                "the answer, say so. Be concise. " + GROUNDING + language_instruction(language)},
            {"role": "user", "content": "RUN FACTS (JSON):\n" + json.dumps(run_digest(run), ensure_ascii=False,
                                                                          default=str)[:9000]
                                        + f"\n\nQUESTION:\n{question}\n\n({language_reminder(language)})"}]
        reply = get_client().chat(messages, prefer=EXPLAINER_MODEL, max_tokens=800, agent=EXPLAINER)
        answer = reply.text if reply.ok else "[LLM unavailable] " + deterministic_explanation(run)
        return {"answer": answer, "agent": "Explainer Agent (orchestrator)", **reply.meta()}
