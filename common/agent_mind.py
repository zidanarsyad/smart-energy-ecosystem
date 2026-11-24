"""LLM-backed reasoning and multilingual communication for one enterprise agent.

Every enterprise agent keeps its deterministic decision model (ML, rules, BDI, LP, MPC). The "mind" adds:
* a working language: the agent writes its messages to other agents in its own language (SolarGreen in Mandarin,
  PowerGrid and IndustriPrima in Bahasa Indonesia, EnergyTrade and BatteryFlex in English);
* understanding: a message from another agent, in any language, is interpreted by THIS agent's own LLM into its
  own language (faithful translation + FIPA-ACL performative + intent). Every number is checked deterministically;
* deliberate() / crosstalk(): rounds 1 and 2 of the ecosystem round table;
* compose() / converse() / understand(): direct agent-to-agent conversation through the A2A layer;
* ask(): answers to internal users or other parties, in the language of the question.
Privacy by construction: for external audiences the LLM never sees private facts, so it cannot leak them.
"""
from __future__ import annotations

import json
from collections import deque
from typing import Callable

from common import multilingual as ml
from common.llm import LLMReply, get_client, language_instruction, language_reminder
from common.timeutil import now_iso

DELIBERATION_KEYS = ("assessment", "can_contribute", "constraints", "recommendation", "concerns", "message")
PERFORMATIVES = ("INFORM", "REQUEST", "PROPOSE", "QUERY", "AGREE", "REFUSE", "ACCEPT", "REJECT", "CFP", "CONFIRM")
_ALIASES = {"ACCEPT_PROPOSAL": "ACCEPT", "REJECT_PROPOSAL": "REJECT", "QUERY_IF": "QUERY", "QUERY_REF": "QUERY",
            "CALL_FOR_PROPOSAL": "CFP", "OFFER": "PROPOSE", "QUESTION": "QUERY"}


def performative(value, default: str = "INFORM") -> str:
    v = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    v = _ALIASES.get(v, v)
    return v if v in PERFORMATIVES else default


def _js(value, limit: int) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:limit]


class AgentMind:
    def __init__(self, profile: dict, context_fn: Callable[[str], dict], prefer_model: str | None,
                 private_note: str):
        self.profile = profile
        self.context_fn = context_fn
        self.prefer = prefer_model
        self.private_note = private_note
        self.agent_name = profile["agents"][0]["name"]
        self.language = profile.get("language", "en")
        self.history: deque = deque(maxlen=60)

    # ------------------------------------------------------------------ helpers
    def _identity(self, audience: str) -> str:
        p = self.profile
        if audience == "internal":
            who, privacy = "an internal user of your own enterprise", "You may use all facts, including private ones."
        else:
            who = "another agent of the ecosystem (an external party)"
            privacy = f"Never disclose private information ({self.private_note})."
        return (f"You are the {self.agent_name} of {p['name']} ({p['role']}), a member of Shakti Tantra, a "
                f"multi-enterprise energy ecosystem. This is an academic simulation: companies and data are fictional. "
                f"Your agent paradigm: {p['paradigm']}. Business goal: {p['business_goal']} "
                f"Governance rules you respect: {' '.join(p['governance'])} "
                f"You are talking with {who}. {privacy} "
                "Use only the JSON facts provided; never invent numbers; if a fact is missing say so. "
                "Binding decisions come from your deterministic decision models (optimisation, rules, BDI, MPC); "
                "you reason, explain, advise and communicate. Be concise and concrete. ")

    @staticmethod
    def _working(native: str) -> str:
        return (f"Your working language is {ml.name(native)}. You write every message to other agents in "
                f"{ml.name(native)}; the other agents read it in their own languages through their own LLMs. ")

    def _chat(self, messages: list[dict], use_llm: bool, **kw) -> LLMReply:
        if not use_llm:
            return LLMReply(ok=False, error="LLM switched off for this request")
        return get_client().chat(messages, prefer=self.prefer, agent=self.profile["id"], **kw)

    def _facts_brief(self) -> dict:
        facts = self.context_fn("external")
        return {k: facts[k] for k in ("summary", "can_contribute") if k in facts}

    def _who(self) -> dict:
        return {"agent": self.agent_name, "enterprise": self.profile["name"], "paradigm": self.profile["paradigm"]}

    def _log(self, **entry) -> None:
        self.history.append({"ts": now_iso(), **entry})

    @staticmethod
    def _viewer(language: str | None) -> str:
        return language if language and language != "auto" else "en"

    @staticmethod
    def _interpreted(source: str, item: dict, src: str | None = None, dst: str | None = None) -> dict:
        translation = str(item.get("translation") or "")
        return {"translation": translation, "performative": performative(item.get("performative")),
                "intent": str(item.get("intent") or ""), "numbers": ml.number_fidelity(source, translation),
                "translated": not ml.looks_untranslated(source, translation, src, dst)}

    def _translate_only(self, text: str, source_language: str, sender_name: str, native: str, use_llm: bool,
                        strict: bool = False) -> tuple[dict, LLMReply]:
        rule = (f"Your previous answer was not a {ml.name(native)} translation: write the translation ONLY in "
                f"{ml.name(native)} and never copy the original text. " if strict else "")
        prompt = (f"MESSAGE from {sender_name}, written in {ml.name(source_language)}:\n{text}\n\n{rule}Translate it "
                  f"faithfully into {ml.name(native)} (keep every number, unit, time and name exactly), and give "
                  f"its FIPA-ACL performative and its intent (at most 10 words, in {ml.name(native)}).\n"
                  'Return ONLY JSON: {"translation": "...", "performative": "...", "intent": "..."}')
        reply = self._chat([{"role": "system", "content": self._identity("external") + self._working(native)},
                            {"role": "user", "content": prompt}], use_llm, json_mode=True, max_tokens=600,
                           temperature=0.1)
        if reply.ok and reply.data:
            return self._interpreted(text, reply.data, source_language, native), reply
        return {"translation": None, "error": "not interpreted (LLM unavailable)"}, reply

    def _repair(self, text: str, source_language: str, sender_name: str, native: str, interp: dict,
                use_llm: bool) -> dict:
        """One targeted retry when the receiver's LLM skipped a message or returned it untranslated."""
        if interp.get("translation") and interp.get("translated", True):
            return interp
        retry, _ = self._translate_only(text, source_language, sender_name, native, use_llm, strict=True)
        if retry.get("translation") and retry.get("translated", True):
            return {**retry, "retried": True}
        return {**interp, "warning": "the receiver's LLM did not translate this message"}

    @staticmethod
    def _passthrough(text: str, perf: str | None = None) -> dict:
        return {"translation": text, "performative": performative(perf), "intent": "", "passthrough": True,
                "numbers": ml.number_fidelity(text, text)}

    # ------------------------------------------------------------------ questions (internal users or other parties)
    def ask(self, question: str, language: str = "auto", audience: str = "internal", sender: str = "user",
            use_llm: bool = True) -> dict:
        facts = self.context_fn(audience)
        messages = [{"role": "system", "content": self._identity(audience) + language_instruction(language)},
                    {"role": "user", "content": "FACTS (JSON):\n" + _js(facts, 9000)
                     + f"\n\nMESSAGE from {sender}:\n{question}\n\n({language_reminder(language)})"}]
        reply = self._chat(messages, use_llm, max_tokens=700)
        answer = reply.text if reply.ok else (
            "[LLM unavailable: deterministic summary] " + facts.get("summary", "No summary available."))
        self._log(mode="ask", audience=audience, sender=sender, language=language, question=question[:300],
                  understood="", answer=answer[:600], answer_language=language, model=reply.model)
        return {"answer": answer, **self._who(), **reply.meta()}

    # ------------------------------------------------------------------ round table, round 1: own position
    def deliberate(self, brief: dict, language: str = "en", use_llm: bool | None = None,
                   native: str | None = None) -> dict:
        use_llm = brief.get("llm", True) if use_llm is None else use_llm
        native, viewer = native or brief.get("agent_language") or self.language, self._viewer(language)
        facts = self.context_fn("external")
        messages = [
            {"role": "system", "content": self._identity("external") + self._working(native)},
            {"role": "user", "content":
                "The ecosystem facilitator opened a collaborative round table about the situation below. Reason from "
                "your enterprise's point of view. Your FIRM offer is already counted in the brief's supply; only "
                "propose ADDITIONAL contributions (from the brief's flexibility options) and say clearly if you have "
                "none.\n\nSITUATION BRIEF (JSON):\n" + _js(brief, 5000) + "\n\nYOUR FACTS (JSON):\n" + _js(facts, 6000)
                + "\n\nReturn ONLY a JSON object with keys: assessment (how the situation affects you, 1-2 sentences), "
                  "can_contribute (what you can offer, with numbers from your facts), constraints (limits you must "
                  "respect), recommendation (what the ecosystem should do), concerns (risks or conflicts with other "
                  f"parties); write these five in {ml.name(viewer)} for the human observers. message: your statement "
                  f"to the other agents, 1-2 sentences, at most 45 words, written in {ml.name(native)} (your working "
                  "language). performative: the FIPA-ACL act of message (INFORM, PROPOSE, REQUEST, REFUSE or AGREE). "
                  f"message_viewer: a faithful {ml.name(viewer)} rendering of message for the human observers. "
                  "confidence: number 0-1."}]
        reply = self._chat(messages, use_llm, json_mode=True, max_tokens=1000, temperature=0.2)
        if reply.ok and reply.data:
            out = {k: str(reply.data.get(k, "")) for k in DELIBERATION_KEYS}
            out.update(language=native, performative=performative(reply.data.get("performative")),
                       message_viewer=str(reply.data.get("message_viewer") or out["message"]))
            try:
                out["confidence"] = max(0.0, min(1.0, float(reply.data.get("confidence", 0.5))))
            except (TypeError, ValueError):
                out["confidence"] = 0.5
        else:   # deterministic fallback in the canonical language (English)
            message = "[LLM unavailable] " + facts.get("summary", "")
            out = {"assessment": facts.get("summary", ""), "can_contribute": facts.get("can_contribute", ""),
                   "constraints": "; ".join(self.profile["governance"][:2]), "recommendation": "", "concerns": "",
                   "message": message, "language": "en", "performative": "INFORM", "message_viewer": message,
                   "confidence": 0.0}
        out.update(**self._who(), **reply.meta())
        self._log(mode="deliberate", audience="external", sender="orchestrator.facilitator_agent", language="data",
                  question=brief.get("headline", "")[:300], understood="", answer=out["message"][:600],
                  answer_language=out["language"], model=reply.model)
        return out

    # ------------------------------------------------------------------ round table, round 2: cross-talk
    def crosstalk(self, messages: list[dict], headline: str, my_position: dict, language: str = "en",
                  native: str | None = None, use_llm: bool = True) -> dict:
        """Understand the other agents' round-1 messages (any language) and reply in the working language."""
        native, viewer = native or self.language, self._viewer(language)
        understood: dict[str, dict] = {}
        foreign = []
        for m in messages:
            if (m.get("language") or "en") == native:
                understood[m["id"]] = self._passthrough(m.get("text", ""), m.get("performative"))
            else:
                foreign.append(m)
        facts = self._facts_brief()
        incoming = [{"id": m["id"], "from": m.get("from_name") or m.get("from"), "language": ml.name(m.get("language")),
                     "text": m.get("text", "")} for m in messages]
        prompt = (f"ROUND TABLE, ROUND 2 (cross-talk). Situation: {headline}\nThe other agents posted these messages, "
                  f"each in its sender's own language:\n{_js(incoming, 4000)}\n\nYOUR EARLIER POSITION: "
                  f"{_js(my_position, 800)}\nYOUR FACTS: {_js(facts, 2500)}\n\n"
                  f"(1) For each message whose id is in {_js([m['id'] for m in foreign], 300)}: translate it "
                  f"faithfully into {ml.name(native)} (keep every number, unit, time and name exactly), give its "
                  f"FIPA-ACL performative and its intent (at most 10 words, in {ml.name(native)}). "
                  f"(2) Reply to the round table in {ml.name(native)}: 1-2 sentences, at most 50 words, reacting to "
                  "specific agents (agree, object, offer or ask), consistent with your facts and your earlier "
                  "position; never invent numbers or reveal private data.\n"
                  'Return ONLY JSON: {"understood": [{"id": "...", "translation": "...", "performative": "...", '
                  '"intent": "..."}], "reply": "...", "reply_performative": "AGREE|PROPOSE|INFORM|REQUEST|REFUSE", '
                  f'"addressed_to": ["agent names"], "reply_viewer": "faithful {ml.name(viewer)} rendering of reply '
                  'for human observers"}')
        reply = self._chat([{"role": "system", "content": self._identity("external") + self._working(native)},
                            {"role": "user", "content": prompt}], use_llm and bool(messages),
                           json_mode=True, max_tokens=1300, temperature=0.2)
        if reply.ok and reply.data:
            items = {str(u.get("id")): u for u in reply.data.get("understood") or [] if isinstance(u, dict)}
            for m in foreign:
                interp = (self._interpreted(m.get("text", ""), items[m["id"]], m.get("language"), native)
                          if m["id"] in items else {"translation": None, "error": "skipped by the LLM"})
                understood[m["id"]] = self._repair(m.get("text", ""), m.get("language"),
                                                   m.get("from_name") or m.get("from"), native, interp, use_llm)
            addressed = reply.data.get("addressed_to") or []
            out = {"reply": str(reply.data.get("reply") or ""), "reply_language": native,
                   "reply_performative": performative(reply.data.get("reply_performative")),
                   "addressed_to": [str(a) for a in addressed] if isinstance(addressed, list) else [str(addressed)],
                   "reply_viewer": str(reply.data.get("reply_viewer") or reply.data.get("reply") or "")}
        else:
            for m in foreign:
                understood[m["id"]] = {"translation": None, "error": "not interpreted (LLM unavailable)"}
            senders = ", ".join(sorted({str(m.get("from_name") or m.get("from")) for m in messages}))
            text = (f"[LLM unavailable] Noted {len(messages)} messages ({senders}). My position is unchanged: "
                    f"{facts.get('can_contribute', '')}")
            out = {"reply": text, "reply_language": "en", "reply_performative": "INFORM", "addressed_to": [],
                   "reply_viewer": text}
        out.update(understood=understood, language=native, **self._who(), **reply.meta())
        self._log(mode="crosstalk", audience="external", sender=f"{len(messages)} round-table agents",
                  language=", ".join(sorted({m.get("language") or "en" for m in messages})),
                  question=" | ".join(m.get("text", "")[:120] for m in messages)[:600],
                  understood=" | ".join((u.get("translation") or "?")[:120] for u in understood.values())[:600],
                  answer=out["reply"][:600], answer_language=out["reply_language"], model=reply.model)
        return out

    # ------------------------------------------------------------------ direct agent-to-agent conversation
    def compose(self, intent: str, to_name: str, language: str = "en", native: str | None = None,
                use_llm: bool = True) -> dict:
        """Write a message to another agent in the working language, from an operator instruction in any language."""
        native, viewer = native or self.language, self._viewer(language)
        prompt = (f"Your human operator asks you to send a message to {to_name}. The operator's instruction (it may be "
                  f"in any language): {intent}\nYOUR FACTS: {_js(self.context_fn('external'), 4000)}\n\nWrite that message "
                  f"as the {self.agent_name} in {ml.name(native)} (your working language): 1-3 sentences, at most 60 "
                  "words, grounded in your facts; never reveal private data or invent numbers.\n"
                  'Return ONLY JSON: {"message": "...", "performative": "QUERY|REQUEST|INFORM|PROPOSE", '
                  f'"message_viewer": "faithful {ml.name(viewer)} rendering of message for human observers"}}')
        reply = self._chat([{"role": "system", "content": self._identity("external") + self._working(native)},
                            {"role": "user", "content": prompt}], use_llm, json_mode=True, max_tokens=700,
                           temperature=0.3)
        if reply.ok and reply.data and reply.data.get("message"):
            out = {"message": str(reply.data["message"]), "language": native,
                   "performative": performative(reply.data.get("performative"), "QUERY"),
                   "message_viewer": str(reply.data.get("message_viewer") or reply.data["message"])}
        else:
            out = {"message": intent, "language": "und", "performative": "INFORM", "message_viewer": intent,
                   "note": "LLM unavailable: the operator's text is sent as written"}
        out.update(**self._who(), **reply.meta())
        self._log(mode="compose", audience="internal", sender="human operator", language="und", question=intent[:300],
                  understood="", answer=out["message"][:600], answer_language=out["language"], model=reply.model)
        return out

    def converse(self, text: str, source_language: str, sender_name: str, language: str = "en",
                 native: str | None = None, use_llm: bool = True) -> dict:
        """Understand a message from another agent (any language) and answer it in the working language."""
        native, viewer = native or self.language, self._viewer(language)
        same = (source_language or "") == native
        step1 = ("The message is already in your working language: copy it unchanged as translation. " if same else
                 f"Translate it faithfully into {ml.name(native)} (keep every number, unit, time and name exactly). ")
        facts = self.context_fn("external")       # full external facts: a direct question deserves a grounded answer
        prompt = (f"MESSAGE from {sender_name} (another enterprise's agent), written in {ml.name(source_language)}:\n"
                  f"{text}\n\nYOUR FACTS: {_js(facts, 5000)}\n\n(1) {step1}Give its FIPA-ACL performative and its "
                  f"intent (at most 10 words, in {ml.name(native)}). (2) Answer {sender_name} in {ml.name(native)}: "
                  "1-3 sentences, at most 60 words, grounded in your facts; never reveal private data or invent numbers.\n"
                  'Return ONLY JSON: {"understood": {"translation": "...", "performative": "...", "intent": "..."}, '
                  '"reply": "...", "reply_performative": "INFORM|AGREE|REFUSE|PROPOSE|QUERY", '
                  f'"reply_viewer": "faithful {ml.name(viewer)} rendering of reply for human observers"}}')
        reply = self._chat([{"role": "system", "content": self._identity("external") + self._working(native)},
                            {"role": "user", "content": prompt}], use_llm, json_mode=True, max_tokens=900,
                           temperature=0.2)
        if reply.ok and reply.data and reply.data.get("reply"):
            u = reply.data.get("understood") if isinstance(reply.data.get("understood"), dict) else {}
            understood = (self._passthrough(text, u.get("performative")) | {"intent": str(u.get("intent") or "")}
                          if same else self._repair(text, source_language, sender_name, native,
                                                    self._interpreted(text, u, source_language, native), use_llm))
            out = {"reply": str(reply.data["reply"]), "reply_language": native,
                   "reply_performative": performative(reply.data.get("reply_performative")),
                   "reply_viewer": str(reply.data.get("reply_viewer") or reply.data["reply"])}
        else:
            understood = (self._passthrough(text) if same
                          else {"translation": None, "error": "not interpreted (LLM unavailable)"})
            fallback = "[LLM unavailable] " + facts.get("summary", "")
            out = {"reply": fallback, "reply_language": "en", "reply_performative": "INFORM", "reply_viewer": fallback}
        out.update(understood=understood, language=native, **self._who(), **reply.meta())
        self._log(mode="converse", audience="external", sender=sender_name, language=source_language,
                  question=text[:300], understood=(understood.get("translation") or "")[:300],
                  answer=out["reply"][:600], answer_language=out["reply_language"], model=reply.model)
        return out

    def understand(self, text: str, source_language: str, sender_name: str, native: str | None = None,
                   use_llm: bool = True) -> dict:
        """Interpret a message from another agent into the working language (no reply)."""
        native = native or self.language
        if (source_language or "") == native:
            understood = self._passthrough(text)
            reply = LLMReply(ok=False, error="same language: no interpretation needed")
        else:
            understood, reply = self._translate_only(text, source_language, sender_name, native, use_llm)
            if reply.ok:
                understood = self._repair(text, source_language, sender_name, native, understood, use_llm)
        self._log(mode="understand", audience="external", sender=sender_name, language=source_language,
                  question=text[:300], understood=(understood.get("translation") or "")[:300], answer="",
                  answer_language="", model=reply.model)
        return {"understood": understood, "language": native, **self._who(), **reply.meta()}
