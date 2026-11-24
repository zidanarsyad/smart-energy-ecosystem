"""Protocol adapters of the interoperability layer (one per enterprise).

REST/JSON (UTC, kW)  |  SOAP 1.1/XML (MWh, legacy time)  |  Bahasa REST + OpenADR-style  |
canonical REST v2  |  JSON-RPC 2.0 (kW, SOC %, epoch). Each method speaks the enterprise's
native protocol, translates the answer with the phi_i functions in `semantics.py`, and logs
both views of the exchange on the message bus.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import timedelta
from xml.sax.saxutils import escape

from common.canonical import ClearingRequest, ClearingResult, DemandBid, SupplyOffer, WindowSpec
from common.glossary import translate
from common.timeutil import iso
from orchestrator.bus import EnterpriseUnavailable, MessageBus
from orchestrator.semantics import (epoch, g_to_kg, kw_points_to_slot_kwh, kwh_to_mwh, legacy_ts, local_naive,
                                    mwh_to_kwh, parse_legacy_ts, parse_local_naive, per_mwh_to_per_kwh, slot_index,
                                    slot_kwh_to_kw_points, utc_z)

ORCH = {k: f"orchestrator.{k}_agent" for k in
        ("discovery", "perception", "reasoning", "planning", "negotiation", "decision", "policy", "execution",
         "monitoring")}
SOAP_NS = {"soap": "http://schemas.xmlsoap.org/soap/envelope/", "pg": "urn:powergrid:ems:2009"}
P_SG, P_PG = "REST/JSON (UTC, kW per PT15M)", "SOAP 1.1/XML (MWh, dd/MM/yyyy HH:mm)"
P_IP, P_ADR = "REST/JSON Bahasa (kWh, local time)", "OpenADR 3.0-style JSON (VTN -> VEN)"
P_ET, P_BF = "REST/JSON v2 (canonical)", "JSON-RPC 2.0 (kW, SOC %, epoch s)"
P_A2A = "A2A-style JSON-RPC message/send (natural language, any language)"


def _soap(op: str, inner: str) -> str:
    return (f'<?xml version="1.0" encoding="utf-8"?>\n<soap:Envelope xmlns:soap="{SOAP_NS["soap"]}" '
            f'xmlns:pg="{SOAP_NS["pg"]}">\n  <soap:Body>\n    <pg:{op}>\n{inner}\n    </pg:{op}>\n'
            f'  </soap:Body>\n</soap:Envelope>')


def _soap_body(text: str) -> ET.Element:
    return ET.fromstring(text).find("soap:Body", SOAP_NS)[0]


def _local_utility(utility: float, acceptable: bool, components: dict) -> dict:
    return {"utility_rp": round(float(utility)), "acceptable": bool(acceptable), "components": components}


class Adapters:
    def __init__(self, urls: dict[str, str]):
        self.urls = urls

    # ------------------------------------------------------------ A2A conversational layer (homogeneous)
    def a2a_send(self, bus: MessageBus, eid: str, *, mode: str, text: str = "", data: dict | None = None,
                 language: str = "en", sender: str = ORCH["negotiation"], task: str = "conversation",
                 agent_language: str | None = None, use_llm: bool = True, message_type: str | None = None) -> dict:
        """Natural-language message to an enterprise agent over the A2A-style endpoint (same for all enterprises).

        `language` is the language of human-facing renderings; each agent writes in its own working language
        (`agent_language` is a simulation what-if override). Relayed agent-to-agent messages keep their original
        sender (e.g. 'solargreen.agent') and pass through the interoperability layer (`via`)."""
        parts = ([{"kind": "text", "text": text}] if text else []) + ([{"kind": "data", "data": data}] if data else [])
        meta = {"mode": mode, "language": language, "sender": sender, "llm": use_llm}
        if agent_language:
            meta["agent_language"] = agent_language
        body = {"jsonrpc": "2.0", "id": f"{bus.conv}-{eid}-{mode}", "method": "message/send",
                "params": {"message": {"role": "user", "messageId": f"{bus.conv}-{eid}-{mode}", "contextId": bus.conv,
                                       "parts": parts, "metadata": meta}}}
        interaction = {"deliberate": "a2a-deliberation", "crosstalk": "a2a-crosstalk"}.get(mode, "a2a-conversation")
        mtype = message_type or {"deliberate": "REQUEST", "crosstalk": "INFORM", "compose": "REQUEST",
                                 "understand": "INFORM"}.get(mode, "QUERY")
        resp, req = bus.exchange(sender=sender, receiver=f"{eid}.agent", enterprise=eid, message_type=mtype, task=task,
                                 payload={"mode": mode, "text": text[:500],
                                          "text_language": (data or {}).get("source_language"),
                                          "viewer_language": language, "data_keys": sorted((data or {}).keys())},
                                 protocol_label=P_A2A, method="POST", url=self.urls[eid] + "/a2a", json_body=body,
                                 interaction=interaction)
        d = resp.json()
        if "error" in d:
            bus.reply(req, "FAILURE", {"error": d["error"]}, resp, P_A2A)
            raise EnterpriseUnavailable(eid, f"A2A error {d['error']}")
        res = d["result"]
        text_out = " ".join(p.get("text", "") for p in res.get("parts", []) if p.get("kind") == "text")
        data_out = next((p.get("data") for p in res.get("parts", []) if p.get("kind") == "data"), {}) or {}
        out = {"text": text_out, "data": data_out, "meta": res.get("metadata", {})}
        reply_type = (data_out.get("performative") if mode in ("deliberate", "compose")
                      else data_out.get("reply_performative")) or ("INFORM" if mode in ("ask", "understand") else "PROPOSE")
        bus.reply(req, reply_type,
                  {"text": text_out, "text_language": out["meta"].get("language"),
                   **{k: data_out.get(k) for k in ("message_viewer", "reply_viewer", "assessment", "can_contribute",
                                                   "recommendation", "concerns", "provider", "model") if k in data_out}},
                  resp, P_A2A)
        return out

    # ------------------------------------------------------------ discovery
    def agent_card(self, bus: MessageBus, eid: str) -> dict:
        resp, req = bus.exchange(sender=ORCH["discovery"], receiver=f"{eid}.gateway", enterprise=eid,
                                 message_type="QUERY", task="agent_discovery", payload={"query": "agent-card"},
                                 protocol_label="HTTP GET /.well-known/agent-card.json", method="GET",
                                 url=self.urls[eid] + "/.well-known/agent-card.json")
        card = resp.json()
        x = card.get("x-enterprise", {})
        bus.reply(req, "INFORM", {"name": card.get("name"), "skills": [s["id"] for s in card.get("skills", [])],
                                  "native_protocol": x.get("native_protocol"), "platform": x.get("platform"),
                                  "agent_paradigm": x.get("agent_paradigm")}, resp, "HTTP/JSON")
        return card

    # ------------------------------------------------------------ SolarGreen (REST/JSON, UTC, kW@15min)
    def solar_offer(self, bus: MessageBus, window: WindowSpec, mode: str) -> tuple[SupplyOffer, dict]:
        start = window.start()
        body = {"start_utc": utc_z(start), "end_utc": utc_z(start + timedelta(hours=window.hours)),
                "basis": "declared" if mode == "reference" else "forecast"}
        resp, req = bus.exchange(sender=ORCH["perception"], receiver="solargreen.offer_agent", enterprise="solargreen",
                                 message_type="REQUEST", task="solar_offer",
                                 payload={"window": window.model_dump(), "basis": body["basis"]},
                                 protocol_label=P_SG, method="POST",
                                 url=self.urls["solargreen"] + "/api/v1/offers:quote", json_body=body)
        d = resp.json()
        avail = kw_points_to_slot_kwh(d["points"], window, "available_kw")
        p10 = kw_points_to_slot_kwh(d["points"], window, "p10_kw")
        p90 = kw_points_to_slot_kwh(d["points"], window, "p90_kw")
        unc = [round(max(0.0, a - b), 4) for a, b in zip(avail, p10)]
        offer = SupplyOffer(offer_id=d["offer_ref"], enterprise="solargreen", agent="solargreen.offer_agent",
                            resource="solar", price_rp_per_kwh=float(d["price"]["amount"]), max_kwh=avail,
                            emission_kg_per_kwh=g_to_kg(d["carbon_intensity_gco2_per_kwh"]), green=True,
                            uncertainty_kwh=unc, notes=[d["basis"]],
                            provenance={"native": "kW avg per 15 min, UTC", "points": len(d["points"]),
                                        "phi": "sum(kW x 0.25 h) per WIB hour"})
        bus.reply(req, "PROPOSE", {"offer_id": offer.offer_id, "resource": "solar",
                                   "price_rp_per_kwh": offer.price_rp_per_kwh, "max_kwh": avail,
                                   "total_kwh": round(sum(avail), 2), "uncertainty_kwh": unc}, resp, P_SG)
        return offer, {"total_kwh": round(sum(avail), 2), "p10_kwh": round(sum(p10), 2),
                       "p90_kwh": round(sum(p90), 2), "basis": d["basis"]}

    def solar_dispatch(self, bus: MessageBus, window: WindowSpec, offer_id: str, kwh: list[float], conv: str,
                       imbalance_charge: float = 0.0) -> dict:
        body = {"offer_ref": offer_id, "conversation_id": conv, "schedule": slot_kwh_to_kw_points(kwh, window),
                "imbalance_charge_idr": round(imbalance_charge, 2)}
        resp, req = bus.exchange(sender=ORCH["execution"], receiver="solargreen.dispatch_agent", enterprise="solargreen",
                                 message_type="REQUEST", task="dispatch",
                                 payload={"offer_id": offer_id, "kwh": kwh, "total_kwh": round(sum(kwh), 2),
                                          "imbalance_charge_rp": round(imbalance_charge)},
                                 protocol_label=P_SG, method="POST", url=self.urls["solargreen"] + "/api/v1/dispatch",
                                 json_body=body)
        d = resp.json()
        u = d["local_utility"]
        ack = {"status": d["status"], "energy_kwh": d["energy_kwh"],
               "local_utility": _local_utility(u["utility_idr"], u["acceptable"], u)}
        bus.reply(req, "CONFIRM", ack, resp, P_SG)
        return ack

    # ------------------------------------------------------------ PowerGrid (SOAP/XML, MWh, legacy time)
    def grid_headroom(self, bus: MessageBus, window: WindowSpec, conv: str) -> tuple[SupplyOffer, dict]:
        start = window.start()
        xml = _soap("GetHeadroomRequest", f"      <pg:CustomerId>IND-PRIMA-01</pg:CustomerId>\n"
                                          f"      <pg:From>{legacy_ts(start)}</pg:From>\n"
                                          f"      <pg:To>{legacy_ts(start + timedelta(hours=window.hours))}</pg:To>")
        resp, req = bus.exchange(sender=ORCH["perception"], receiver="powergrid.headroom_agent", enterprise="powergrid",
                                 message_type="QUERY", task="grid_headroom",
                                 payload={"customer": "IND-PRIMA-01", "window": window.model_dump()},
                                 protocol_label=P_PG, method="POST", url=self.urls["powergrid"] + "/soap/GridService",
                                 content=xml, headers={"Content-Type": "text/xml; charset=utf-8",
                                                       "SOAPAction": "urn:powergrid:ems:2009#GetHeadroom"})
        op = _soap_body(resp.text)
        H = window.hours
        head, bg, usable = [0.0] * H, [0.0] * H, [0.0] * H
        for iv in op.findall("pg:Interval", SOAP_NS):
            i = slot_index(parse_legacy_ts(iv.get("start")), window)
            if i is not None:
                head[i] = mwh_to_kwh(iv.get("headroomMWh"))
                bg[i] = mwh_to_kwh(iv.get("backgroundForecastMWh"))
                usable[i] = mwh_to_kwh(iv.get("usableMWh"))
        tariff = per_mwh_to_per_kwh(op.find("pg:Tariff", SOAP_NS).text)
        ef = float(op.find("pg:EmissionFactor", SOAP_NS).text) / 1000.0
        stress = float(op.find("pg:StressThreshold", SOAP_NS).text)
        curve = [[float(t.get("upToFraction")), float(t.get("risk"))]
                 for t in op.findall("pg:LoadingRiskCurve/pg:Tier", SOAP_NS)] or None
        offer = SupplyOffer(offer_id=f"PG-HR-{conv}", enterprise="powergrid", agent="powergrid.headroom_agent",
                            resource="grid", price_rp_per_kwh=tariff, max_kwh=[round(h, 3) for h in head],
                            emission_kg_per_kwh=ef, stress_threshold_frac=stress, stress_risk_per_kwh=1.0,
                            risk_tiers=curve,
                            provenance={"native": "headroomMWh per hour, IDR/MWh", "phi": "x1000, /1000"})
        detail = {"headroom_kwh": offer.max_kwh, "background_kw": [round(b, 1) for b in bg],
                  "usable_kw": [round(u, 1) for u in usable], "tariff_rp_per_kwh": tariff, "stress_threshold": stress,
                  "total_kwh": round(sum(head), 2)}
        bus.reply(req, "INFORM", {"resource": "grid", "price_rp_per_kwh": tariff, "max_kwh": offer.max_kwh,
                                  "total_kwh": detail["total_kwh"], "emission_kg_per_kwh": ef,
                                  "stress_threshold": stress}, resp, P_PG)
        return offer, detail

    def _intervals_xml(self, window: WindowSpec, kwh: list[float]) -> str:
        return "\n".join(f'      <pg:Interval start="{legacy_ts(s)}" importMWh="{kwh_to_mwh(v):.5f}"/>'
                         for s, v in zip(window.slot_starts(), kwh))

    def _findings(self, op: ET.Element, window: WindowSpec) -> list[dict]:
        out = []
        for f in op.findall("pg:Finding", SOAP_NS):
            ts = parse_legacy_ts(f.get("interval"))
            out.append({"rule": f.get("rule"), "name": f.get("name"), "severity": f.get("severity"),
                        "slot": slot_index(ts, window), "label": ts.strftime("%H:%M"), "message": f.text})
        return out

    def grid_check(self, bus: MessageBus, window: WindowSpec, grid_kwh: list[float], sender: str) -> dict:
        xml = _soap("CheckScheduleRequest", "      <pg:CustomerId>IND-PRIMA-01</pg:CustomerId>\n"
                    + self._intervals_xml(window, grid_kwh))
        resp, req = bus.exchange(sender=sender, receiver="powergrid.grid_safety_agent", enterprise="powergrid",
                                 message_type="REQUEST", task="grid_security_check",
                                 payload={"grid_import_kwh": grid_kwh, "total_kwh": round(sum(grid_kwh), 2)},
                                 protocol_label=P_PG, method="POST", url=self.urls["powergrid"] + "/soap/GridService",
                                 content=xml, headers={"Content-Type": "text/xml; charset=utf-8",
                                                       "SOAPAction": "urn:powergrid:ems:2009#CheckSchedule"})
        op = _soap_body(resp.text)
        verdict = {"verdict": op.find("pg:Verdict", SOAP_NS).text,
                   "max_feeder_utilisation": float(op.find("pg:MaxFeederUtilisation", SOAP_NS).text),
                   "findings": self._findings(op, window)}
        bus.reply(req, "INFORM", verdict, resp, P_PG)
        return verdict

    def grid_cfp(self, bus: MessageBus, window: WindowSpec, need_kwh: float, conv: str) -> dict:
        start = window.start()
        xml = _soap("CallForProposalRequest",
                    f"      <pg:CustomerId>IND-PRIMA-01</pg:CustomerId>\n      <pg:NeedMWh>{kwh_to_mwh(need_kwh):.4f}</pg:NeedMWh>\n"
                    f"      <pg:From>{legacy_ts(start)}</pg:From>\n"
                    f"      <pg:To>{legacy_ts(start + timedelta(hours=window.hours))}</pg:To>\n"
                    f"      <pg:ConversationId>{escape(conv)}</pg:ConversationId>")
        resp, req = bus.exchange(sender=ORCH["negotiation"], receiver="powergrid.grid_safety_agent",
                                 enterprise="powergrid", message_type="CFP", task="flexibility_procurement",
                                 payload={"need_kwh": round(need_kwh, 2), "product": "non-firm emergency import"},
                                 protocol_label=P_PG, method="POST", url=self.urls["powergrid"] + "/soap/GridService",
                                 content=xml, headers={"Content-Type": "text/xml; charset=utf-8"},
                                 interaction="fipa-contract-net")
        op = _soap_body(resp.text)
        if op.tag.endswith("RefuseResponse"):
            prop = {"participant": "powergrid", "decision": "REFUSE", "resource": "grid_nonfirm",
                    "reason": f"{op.find('pg:Rule', SOAP_NS).text}: {op.find('pg:Reason', SOAP_NS).text}"}
        else:
            H = window.hours
            caps = [0.0] * H
            for iv in op.findall("pg:Interval", SOAP_NS):
                i = slot_index(parse_legacy_ts(iv.get("start")), window)
                if i is not None:
                    caps[i] = mwh_to_kwh(iv.get("maxMWh"))
            prop = {"participant": "powergrid", "decision": "PROPOSE", "resource": "grid_nonfirm",
                    "price_rp_per_kwh": per_mwh_to_per_kwh(op.find("pg:PriceIDRPerMWh", SOAP_NS).text),
                    "max_kwh": caps, "energy_kwh": round(sum(caps), 2), "risk_per_kwh": 1.5,
                    "emission_kg_per_kwh": 0.87, "reason": op.find("pg:Conditions", SOAP_NS).text}
        bus.reply(req, prop["decision"], prop, resp, P_PG)
        return prop

    def grid_submit(self, bus: MessageBus, window: WindowSpec, grid_kwh: list[float], conv: str,
                    balancing_charge_rp: float) -> dict:
        xml = _soap("SubmitScheduleRequest",
                    f"      <pg:CustomerId>IND-PRIMA-01</pg:CustomerId>\n      <pg:ConversationId>{escape(conv)}</pg:ConversationId>\n"
                    f"      <pg:BalancingChargeIDR>{balancing_charge_rp:.0f}</pg:BalancingChargeIDR>\n"
                    + self._intervals_xml(window, grid_kwh))
        resp, req = bus.exchange(sender=ORCH["execution"], receiver="powergrid.grid_safety_agent",
                                 enterprise="powergrid", message_type="REQUEST", task="dispatch",
                                 payload={"grid_import_kwh": grid_kwh, "total_kwh": round(sum(grid_kwh), 2),
                                          "balancing_charge_rp": round(balancing_charge_rp)},
                                 protocol_label=P_PG, method="POST", url=self.urls["powergrid"] + "/soap/GridService",
                                 content=xml, headers={"Content-Type": "text/xml; charset=utf-8"})
        op = _soap_body(resp.text)
        ack_el, u = op.find("pg:Ack", SOAP_NS), op.find("pg:LocalUtility", SOAP_NS)
        comps = {k: float(v) for k, v in u.attrib.items() if k != "acceptable"}
        ack = {"status": "ACCEPTED" if ack_el.get("verdict") != "REJECTED" else "REJECTED",
               "schedule_id": ack_el.get("scheduleId"), "verdict": ack_el.get("verdict"),
               "local_utility": _local_utility(u.get("utilityIDR"), u.get("acceptable") == "true", comps)}
        bus.reply(req, "CONFIRM" if ack["status"] == "ACCEPTED" else "FAILURE", ack, resp, P_PG)
        return ack

    # ------------------------------------------------------------ IndustriPrima (Bahasa REST + OpenADR-style)
    def demand_bid(self, bus: MessageBus, window: WindowSpec, mode: str, conv: str) -> tuple[DemandBid, dict]:
        params = {"tanggal": window.date, "jam_mulai": window.start_hour, "durasi": window.hours,
                  "mode": "deklarasi" if mode == "reference" else "prakiraan"}
        resp, req = bus.exchange(sender=ORCH["perception"], receiver="industriprima.demand_forecast_agent",
                                 enterprise="industriprima", message_type="QUERY", task="demand_plan",
                                 payload={"window": window.model_dump(), "basis": params["mode"]},
                                 protocol_label=P_IP, method="GET", url=self.urls["industriprima"] + "/api/v1/rencana-beban",
                                 params=params)
        d = resp.json()
        H = window.hours
        dem, flex, crit = [0.0] * H, [0.0] * H, [0.0] * H
        for r in d["interval"]:
            i = slot_index(parse_local_naive(r["jam"]), window)
            if i is not None:
                dem[i], flex[i], crit[i] = r["kebutuhan_kwh"], r["beban_fleksibel_kwh"], r["beban_kritis_kwh"]
        bid = DemandBid(bid_id=f"IP-BID-{conv}", enterprise="industriprima", agent="industriprima.bdi_energy_agent",
                        demand_kwh=dem, flexible_kwh=flex, critical_kwh=crit,
                        max_price_rp_per_kwh=float(d["harga_maksimum_rp_per_kwh"]),
                        provenance={"native": "Bahasa fields, local time without offset", "basis": d["dasar"]})
        bus.reply(req, "INFORM", {"demand_kwh": dem, "flexible_kwh": flex, "total_kwh": round(sum(dem), 2),
                                  "max_price_rp_per_kwh": bid.max_price_rp_per_kwh}, resp, P_IP)
        return bid, {"total_kwh": round(sum(dem), 2), "flexible_kwh": round(sum(flex), 2), "basis": d["dasar"],
                     "model": d.get("model_prakiraan")}

    def _negotiate(self, bus: MessageBus, body: dict, message_type: str, payload: dict, interaction: str):
        return bus.exchange(sender=ORCH["negotiation"], receiver="industriprima.bdi_energy_agent",
                            enterprise="industriprima", message_type=message_type,
                            task="flexibility_procurement" if interaction == "fipa-contract-net" else "dr_price_bargaining",
                            payload=payload, protocol_label=P_IP, method="POST",
                            url=self.urls["industriprima"] + "/api/v1/negosiasi/dr", json_body=body,
                            interaction=interaction)

    def dr_cfp(self, bus: MessageBus, window: WindowSpec, need_kwh: float, slots: list[int], conv: str) -> dict:
        hours = [local_naive(window.slot_starts()[i]) for i in slots]
        body = {"jenis_pesan": "CFP", "id_percakapan": conv, "kebutuhan_kwh": round(need_kwh, 2), "jam_diizinkan": hours}
        resp, req = self._negotiate(bus, body, "CFP", {"need_kwh": round(need_kwh, 2), "allowed_slots": slots,
                                                       "product": "demand response (flexible load)"},
                                    "fipa-contract-net")
        d = resp.json()
        plan = d.get("jejak_bdi", {}).get("rencana_terpilih")
        if d["keputusan"] == "USULAN":
            caps = [0.0] * window.hours
            for r in d.get("maks_per_jam", []):
                i = slot_index(parse_local_naive(r["jam"]), window)
                if i is not None:
                    caps[i] = float(r["maks_kwh"])
            prop = {"participant": "industriprima", "decision": "PROPOSE", "resource": "demand_response",
                    "price_rp_per_kwh": float(d["harga_rp_per_kwh"]), "energy_kwh": float(d["kuantitas_kwh"]),
                    "max_kwh": caps, "risk_per_kwh": 0.0, "service_disruption": True,
                    "reason": d.get("alasan_en") or d["alasan"], "reason_native": d["alasan"], "bdi_plan": plan}
        else:
            prop = {"participant": "industriprima", "decision": "REFUSE", "resource": "demand_response",
                    "reason": d.get("alasan_en") or d["alasan"], "reason_native": d["alasan"], "bdi_plan": plan}
        bus.reply(req, prop["decision"], prop, resp, P_IP)
        return prop

    def dr_bargain_round(self, bus: MessageBus, conv: str, t: int, price: float, qty: float) -> dict:
        body = {"jenis_pesan": "TAWARAN", "id_percakapan": conv, "putaran": t, "harga_tawaran_rp_per_kwh": price,
                "kuantitas_kwh": qty}
        resp, req = self._negotiate(bus, body, "PROPOSE", {"round": t, "offer_rp_per_kwh": price, "kwh": qty},
                                    "alternating-offers")
        d = resp.json()
        decision = {"MENERIMA": "ACCEPT_PROPOSAL", "TAWARAN_BALIK": "COUNTER_PROPOSE",
                    "MENOLAK": "REJECT_PROPOSAL"}.get(d["keputusan"], "FAILURE")
        out = {"decision": decision, "price": d.get("harga_rp_per_kwh"), "reason": d.get("alasan_en") or d.get("alasan"),
               "reason_native": d.get("alasan"), "bdi_plan": d.get("jejak_bdi", {}).get("rencana_terpilih")}
        bus.reply(req, decision, out, resp, P_IP)
        return out

    def dr_close(self, bus: MessageBus, conv: str, accept: bool, price: float | None = None) -> dict:
        body = {"jenis_pesan": "TERIMA" if accept else "TOLAK", "id_percakapan": conv,
                "harga_tawaran_rp_per_kwh": price}
        resp, req = self._negotiate(bus, body, "ACCEPT_PROPOSAL" if accept else "REJECT_PROPOSAL",
                                    {"price_rp_per_kwh": price}, "alternating-offers" if accept else "fipa-contract-net")
        d = resp.json()
        bus.reply(req, "CONFIRM" if accept else "INFORM",
                  {"decision": d.get("keputusan"), "reason": d.get("alasan_en") or d.get("alasan")}, resp, P_IP)
        return d

    def dr_event(self, bus: MessageBus, window: WindowSpec, dr_kwh: list[float], price: float, conv: str) -> dict:
        evt = {"objectType": "EVENT", "id": f"evt-{conv}", "programID": "ECO-DR-2026",
               "eventName": "Ecosystem flexibility dispatch", "priority": 1,
               "payloadDescriptors": [{"objectType": "EVENT_PAYLOAD_DESCRIPTOR",
                                       "payloadType": "DISPATCH_SETPOINT_RELATIVE", "units": "KW"}],
               "intervalPeriod": {"start": iso(window.start()), "duration": "PT1H"},
               "intervals": [{"id": i, "payloads": [{"type": "DISPATCH_SETPOINT_RELATIVE", "values": [-round(v, 3)]}]}
                             for i, v in enumerate(dr_kwh)],
               "x-agreed-price-idr-per-kwh": price, "x-conversation-id": conv}
        resp, req = bus.exchange(sender=ORCH["execution"], receiver="industriprima.bdi_energy_agent",
                                 enterprise="industriprima", message_type="REQUEST", task="demand_response_event",
                                 payload={"dr_kwh": dr_kwh, "total_kwh": round(sum(dr_kwh), 2), "price_rp_per_kwh": price,
                                          "role": "VTN on behalf of PowerGrid (delegated)"},
                                 protocol_label=P_ADR, method="POST", url=self.urls["industriprima"] + "/openadr3/events",
                                 json_body=evt)
        d = resp.json()
        ack = {"event_id": d["eventID"], "status": d["x-status"], "approval_id": d.get("x-approval-id"),
               "reason": d.get("x-reason-en") or d.get("x-reason"), "loss_value_rp": d.get("x-loss-value-idr"),
               "process_plan": d.get("x-process-plan")}
        mtype = {"ACCEPTED": "AGREE", "PENDING_APPROVAL": "INFORM"}.get(ack["status"], "REFUSE")
        bus.reply(req, mtype, ack, resp, P_ADR)
        return ack

    def dr_report(self, bus: MessageBus, event_id: str) -> dict:
        resp, req = bus.exchange(sender=ORCH["monitoring"], receiver="industriprima.bdi_energy_agent",
                                 enterprise="industriprima", message_type="QUERY", task="demand_response_report",
                                 payload={"event_id": event_id}, protocol_label=P_ADR, method="GET",
                                 url=self.urls["industriprima"] + "/openadr3/reports", params={"eventID": event_id})
        reports = resp.json()
        status = reports[0]["x-status"] if reports else "UNKNOWN"
        bus.reply(req, "INFORM", {"event_id": event_id, "status": status}, resp, P_ADR)
        return {"event_id": event_id, "status": status}

    def supply_schedule(self, bus: MessageBus, window: WindowSpec, by_res: dict[str, list[float]],
                        unserved: list[float], energy_cost: float, dr_comp: float, renewable_share: float,
                        conv: str, balancing_charge: float = 0.0) -> dict:
        def g(r: str, i: int) -> float:
            return round(by_res.get(r, [0.0] * window.hours)[i], 3)

        intervals = [{"jam": local_naive(s), "surya_kwh": g("solar", i), "baterai_kwh": g("battery", i),
                      "jaringan_kwh": round(g("grid", i) + g("grid_nonfirm", i), 3),
                      "cadangan_baterai_kwh": g("battery_reserve", i), "dr_kwh": g("demand_response", i),
                      "tidak_terlayani_kwh": round(unserved[i], 3)} for i, s in enumerate(window.slot_starts())]
        body = {"id_percakapan": conv, "tanggal": window.date, "interval": intervals,
                "biaya_energi_rp": round(energy_cost, 2), "kompensasi_dr_rp": round(dr_comp, 2),
                "biaya_penyeimbangan_rp": round(balancing_charge, 2),
                "pangsa_terbarukan_persen": round(renewable_share, 2)}
        resp, req = bus.exchange(sender=ORCH["execution"], receiver="industriprima.bdi_energy_agent",
                                 enterprise="industriprima", message_type="INFORM", task="supply_schedule",
                                 payload={"energy_cost_rp": round(energy_cost), "dr_compensation_rp": round(dr_comp),
                                          "balancing_charge_rp": round(balancing_charge),
                                          "renewable_share_pct": round(renewable_share, 2)},
                                 protocol_label=P_IP, method="POST", url=self.urls["industriprima"] + "/api/v1/jadwal-pasokan",
                                 json_body=body)
        d = resp.json()
        u = d["utilitas_lokal"]
        ack = {"status": translate(d["status"]), "note": d.get("catatan"),
               "local_utility": _local_utility(u["utilitas_rp"], u["dapat_diterima"], u)}
        bus.reply(req, "CONFIRM", ack, resp, P_IP)
        return ack

    # ------------------------------------------------------------ EnergyTrade Hub (canonical REST v2)
    def price_forecast(self, bus: MessageBus, window: WindowSpec) -> list[float]:
        resp, req = bus.exchange(sender=ORCH["perception"], receiver="energytrade.price_agent", enterprise="energytrade",
                                 message_type="QUERY", task="price_forecast", payload={"window": window.model_dump()},
                                 protocol_label=P_ET, method="GET",
                                 url=self.urls["energytrade"] + "/api/v2/market/price-forecast",
                                 params={"date": window.date, "start_hour": window.start_hour, "hours": window.hours})
        prices = [p["price_idr_kwh"] for p in resp.json()["intervals"]]
        bus.reply(req, "INFORM", {"price_rp_per_kwh": prices}, resp, P_ET)
        return prices

    def clear(self, bus: MessageBus, creq: ClearingRequest, sender: str) -> ClearingResult:
        payload = {"session_id": creq.session_id, "offers": [{"offer_id": o.offer_id, "resource": o.resource,
                                                              "price_rp_per_kwh": o.price_rp_per_kwh,
                                                              "total_kwh": round(sum(o.max_kwh), 2),
                                                              "energy_limit_kwh": o.energy_limit_kwh}
                                                             for o in creq.offers],
                   "demand_kwh": round(sum(creq.demand.demand_kwh), 2), "weights": creq.weights.model_dump(),
                   "locked_resources": list(creq.locked)}
        resp, req = bus.exchange(sender=sender, receiver="energytrade.market_clearing_agent", enterprise="energytrade",
                                 message_type="REQUEST", task="market_clearing", payload=payload,
                                 protocol_label=P_ET, method="POST", url=self.urls["energytrade"] + "/api/v2/market/clear",
                                 json_body=creq.model_dump(mode="json"))
        res = ClearingResult(**resp.json())
        bus.reply(req, "INFORM", {"status": res.status, "cost_rp": res.cost_rp,
                                  "dispatch_kwh": {k: round(sum(v), 2) for k, v in res.by_resource().items()},
                                  "unserved_kwh": round(sum(res.unserved_kwh), 2), "J": res.objective.get("J"),
                                  "marginal_price_rp_per_kwh": res.clearing_price_rp_per_kwh}, resp, P_ET)
        return res

    def settle(self, bus: MessageBus, session_id: str, lines: list[dict]) -> dict:
        resp, req = bus.exchange(sender=ORCH["execution"], receiver="energytrade.settlement_agent",
                                 enterprise="energytrade", message_type="REQUEST", task="settlement",
                                 payload={"session_id": session_id, "lines": len(lines)}, protocol_label=P_ET,
                                 method="POST", url=self.urls["energytrade"] + "/api/v2/settlement",
                                 json_body={"session_id": session_id, "lines": lines})
        d = resp.json()
        u = d["local_utility"]
        ack = {"statements": d["statements"], "trades": d["trades"],
               "local_utility": _local_utility(u["utility_idr"], u["acceptable"], u)}
        bus.reply(req, "CONFIRM", ack, resp, P_ET)
        return ack

    # ------------------------------------------------------------ BatteryFlex (JSON-RPC 2.0)
    def _rpc(self, bus: MessageBus, method: str, params: dict, *, sender: str, receiver: str, message_type: str,
             task: str, payload: dict, interaction: str = "request-reply"):
        body = {"jsonrpc": "2.0", "id": bus.seq + 1, "method": method, "params": params}
        resp, req = bus.exchange(sender=sender, receiver=receiver, enterprise="batteryflex", message_type=message_type,
                                 task=task, payload=payload, protocol_label=P_BF, method="POST",
                                 url=self.urls["batteryflex"] + "/rpc", json_body=body, interaction=interaction)
        d = resp.json()
        if "error" in d:
            bus.reply(req, "FAILURE", {"error": d["error"]}, resp, P_BF)
            raise EnterpriseUnavailable("batteryflex", f"JSON-RPC error {d['error']}")
        return d["result"], resp, req

    def battery_offer(self, bus: MessageBus, window: WindowSpec, prices: list[float], conv: str) -> tuple[SupplyOffer, dict]:
        params = {"t_start": epoch(window.start()), "step_s": 3600, "steps": window.hours,
                  "price_forecast_idr_kwh": prices}
        res, resp, req = self._rpc(bus, "bess.plan_offer", params, sender=ORCH["perception"],
                                   receiver="batteryflex.mpc_agent", message_type="REQUEST", task="storage_offer",
                                   payload={"price_forecast_rp_per_kwh": prices})
        step_h = res["step_s"] / 3600.0
        offer = SupplyOffer(offer_id=f"BF-OFF-{conv}", enterprise="batteryflex", agent="batteryflex.offer_agent",
                            resource="battery", price_rp_per_kwh=float(res["offer"]["price_idr_kwh"]),
                            max_kwh=[round(kw * step_h, 3) for kw in res["offer"]["max_kw"]],
                            energy_limit_kwh=float(res["offer"]["energy_kwh"]), risk_per_kwh=0.05,
                            power_group="BF-BESS-01",
                            provenance={"native": "kW per step, SOC %, epoch seconds", "phi": "kW x step_s/3600"})
        bus.reply(req, "PROPOSE", {"offer_id": offer.offer_id, "resource": "battery",
                                   "price_rp_per_kwh": offer.price_rp_per_kwh, "max_kwh": offer.max_kwh,
                                   "energy_limit_kwh": offer.energy_limit_kwh,
                                   "local_mpc_plan_kw": res["local_mpc_plan"]["discharge_kw"]}, resp, P_BF)
        return offer, {"energy_kwh": offer.energy_limit_kwh, "local_mpc_plan": res["local_mpc_plan"],
                       "reservation_rp_per_kwh": res["offer"]["reservation_idr_kwh"],
                       "availability": res.get("availability")}

    def battery_reserve_cfp(self, bus: MessageBus, window: WindowSpec, need_kwh: float, conv: str) -> dict:
        params = {"t_start": epoch(window.start()), "step_s": 3600, "steps": window.hours, "need_kwh": need_kwh,
                  "conversation_id": conv}
        res, resp, req = self._rpc(bus, "bess.cfp_reserve", params, sender=ORCH["negotiation"],
                                   receiver="batteryflex.offer_agent", message_type="CFP",
                                   task="flexibility_procurement",
                                   payload={"need_kwh": round(need_kwh, 2), "product": "emergency reserve (SOC 10-20%)"},
                                   interaction="fipa-contract-net")
        if res["decision"] == "PROPOSE":
            prop = {"participant": "batteryflex", "decision": "PROPOSE", "resource": "battery_reserve",
                    "price_rp_per_kwh": float(res["price_idr_kwh"]), "energy_kwh": float(res["energy_kwh"]),
                    "max_kwh": [float(kw) for kw in res["max_kw"]], "risk_per_kwh": float(res["risk_per_kwh"]),
                    "power_group": "BF-BESS-01", "reason": res["conditions"]}
        else:
            prop = {"participant": "batteryflex", "decision": "REFUSE", "resource": "battery_reserve",
                    "reason": res["reason"]}
        bus.reply(req, prop["decision"], prop, resp, P_BF)
        return prop

    def battery_cfp_result(self, bus: MessageBus, conv: str, accepted: bool, energy_kwh: float, reason: str) -> dict:
        res, resp, req = self._rpc(bus, "bess.cfp_result", {"conversation_id": conv, "accepted": accepted,
                                                            "energy_kwh": energy_kwh, "reason": reason},
                                   sender=ORCH["negotiation"], receiver="batteryflex.offer_agent",
                                   message_type="ACCEPT_PROPOSAL" if accepted else "REJECT_PROPOSAL",
                                   task="flexibility_procurement", payload={"energy_kwh": energy_kwh, "reason": reason},
                                   interaction="fipa-contract-net")
        bus.reply(req, "CONFIRM" if accepted else "INFORM", res, resp, P_BF)
        return res

    def battery_schedule(self, bus: MessageBus, window: WindowSpec, discharge_kwh: list[float],
                         reserve_kwh: list[float], conv: str, balancing_charge: float = 0.0) -> dict:
        params = {"t_start": epoch(window.start()), "step_s": 3600, "discharge_kw": discharge_kwh,
                  "reserve_kw": reserve_kwh, "conversation_id": conv, "balancing_charge_idr": round(balancing_charge, 2)}
        res, resp, req = self._rpc(bus, "bess.set_schedule", params, sender=ORCH["execution"],
                                   receiver="batteryflex.mpc_agent", message_type="REQUEST", task="dispatch",
                                   payload={"discharge_kwh": discharge_kwh, "reserve_kwh": reserve_kwh,
                                            "total_kwh": round(sum(discharge_kwh) + sum(reserve_kwh), 2),
                                            "balancing_charge_rp": round(balancing_charge)})
        u = res["local_utility"]
        ack = {"status": "ACCEPTED" if res["accepted"] else "TRACKING_DEVIATION", "soc_pct": res["soc_pct"],
               "final_soc_pct": res["soc_pct"][-1], "executed_kw": res["executed_kw"],
               "max_tracking_error_kw": res["max_tracking_error_kw"],
               "local_utility": _local_utility(u["utility_idr"], u["acceptable"], u)}
        bus.reply(req, "CONFIRM" if res["accepted"] else "FAILURE", ack, resp, P_BF)
        return ack
