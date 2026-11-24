"""Global Agentic Control Center (Streamlit, default port 8501).

Shows ecosystem status, the heterogeneity of the five enterprises, agent communication,
negotiation, decisions, environment changes and KPIs. It only talks to the orchestrator API
(it never reads an enterprise database directly).

    streamlit run frontend/control_center.py --server.port 8501
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import altair as alt
import httpx
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import multilingual as ml  # noqa: E402
from common.config import (BRAND_DIR, ECOSYSTEM_MEANING, ECOSYSTEM_NAME, ECOSYSTEM_TAGLINE,  # noqa: E402
                           ENTERPRISE_NAMES, ENTERPRISE_PORTS, browser_url, orchestrator_url)

st.set_page_config(page_title=f"{ECOSYSTEM_NAME} Control Center", page_icon=str(BRAND_DIR / "favicon.svg"),
                   layout="wide")
st.logo(str(BRAND_DIR / "shakti-tantra-lockup.svg"), size="large",
        icon_image=str(BRAND_DIR / "shakti-tantra-mark.svg"))

API = orchestrator_url()
RES_COLORS = {"solar": "#f59e0b", "battery": "#db2777", "grid": "#2563eb", "grid_nonfirm": "#60a5fa",
              "battery_reserve": "#f472b6", "demand_response": "#059669", "unserved": "#dc2626"}
RES_LABELS = {"solar": "Solar (SolarGreen)", "battery": "Battery (BatteryFlex)", "grid": "Grid (PowerGrid)",
              "grid_nonfirm": "Grid non-firm", "battery_reserve": "Battery reserve",
              "demand_response": "Demand response (IndustriPrima)", "unserved": "Unserved"}
STATUS_ICON = {"COMPLETED": ":green[COMPLETED]", "COMPLETED_WITH_WARNINGS": ":orange[COMPLETED WITH WARNINGS]",
               "AWAITING_APPROVAL": ":orange[AWAITING APPROVAL]", "BLOCKED": ":red[BLOCKED]", "FAILED": ":red[FAILED]"}
PHASE_ICON = {"ENVIRONMENT": ":material/cloud:", "DISCOVERY": ":material/travel_explore:",
              "PERCEPTION": ":material/sensors:", "REASONING": ":material/psychology:",
              "DELIBERATION": ":material/groups:", "EXPLANATION": ":material/record_voice_over:",
              "PLANNING": ":material/calculate:", "GRID_SECURITY": ":material/shield:",
              "NEGOTIATION": ":material/handshake:", "REPLANNING": ":material/autorenew:",
              "DECISION": ":material/gavel:", "EXECUTION": ":material/send:", "GOVERNANCE": ":material/policy:",
              "MONITORING": ":material/monitoring:", "HUMAN_APPROVAL": ":material/person_check:",
              "FAILED": ":material/error:"}


def call(method: str, path: str, **kw):
    try:
        r = httpx.request(method, API + path, timeout=180, **kw)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        st.error(f"Orchestrator call failed ({method} {path}): {exc}. Is `python scripts/run_all.py` running?")
        return None


@st.cache_data(ttl=5, show_spinner=False)
def registry() -> list[dict]:
    return call("GET", "/registry") or []


@st.cache_data(ttl=60, show_spinner=False)
def scenarios() -> dict:
    return call("GET", "/scenarios") or {}


@st.cache_data(ttl=10, show_spinner=False)
def llm_status() -> dict:
    return call("GET", "/llm/status") or {}


@st.cache_data(ttl=3600, show_spinner=False)
def glossary() -> dict:
    return call("GET", "/glossary") or {"glossary": [], "translations": {}}


LANG_OPTIONS = {"en": "English", "id": "Bahasa Indonesia", "auto": "Same as the question", "jv": "Javanese",
                "ms": "Malay", "ja": "Japanese", "zh": "Mandarin Chinese (中文)", "ar": "Arabic"}
WORKING_LANGS = ["zh", "id", "en", "ja", "jv", "ms", "ar", "de", "fr", "es"]


def rp(v) -> str:
    return "-" if v is None else f"Rp{v:,.0f}"


def short(spec: str | None) -> str:
    """'openrouter:nvidia/nemotron-3.5-super-vl:free' -> 'nemotron-3.5-super-vl:free'."""
    return (spec or "-").split(":", 1)[-1].split("/")[-1]


def xent(e: dict) -> dict:
    return (e.get("card") or {}).get("x-enterprise", {})


def agent_lang(e: dict) -> str:
    return (xent(e).get("working_language") or {}).get("code", "en")


def safe(text) -> str:
    """Plain text for st.markdown: no accidental LaTeX ($) or emphasis from LLM output."""
    return str(text or "").replace("$", "\\$").replace("*", "\\*").replace("_", "\\_")


def render_message(m: dict, viewer: str, positions: dict | None = None) -> None:
    """One natural-language agent message: original text, the sender's rendering for humans, and how every
    receiver's own LLM understood it (in the receiver's own language), with the deterministic number check."""
    to = ", ".join(ENTERPRISE_NAMES.get(t, t) for t in (m.get("to") or ["all"]))
    st.markdown(f"**{ENTERPRISE_NAMES.get(m['from'], m['from'])}** → {to}  \n"
                f":blue-badge[{ml.label(m.get('language'))}] :gray-badge[{m.get('performative', 'INFORM')}] "
                f":violet-badge[{short(m.get('model'))}]")
    st.markdown(f"> {safe(m.get('text'))}")
    if m.get("viewer_text") and m.get("viewer_text") != m.get("text"):
        st.caption(f"Sender's rendering ({LANG_OPTIONS.get(viewer, viewer)}): {m['viewer_text']}")
    und = m.get("understood_by") or {}
    if und:
        with st.expander(f"How {len(und)} receiver(s) understood it, each in its own language"):
            for rid, u in und.items():
                who = f"**{ENTERPRISE_NAMES.get(rid, rid)}** :blue-badge[{ml.label(u.get('language'))}]"
                if u.get("passthrough"):
                    st.markdown(f"{who} :gray-badge[same language: delivered as is]")
                    continue
                num = u.get("numbers") or {}
                if not u.get("translation"):
                    check = ":red-badge[not interpreted]"
                elif u.get("warning") or u.get("translated") is False:
                    check = ":red-badge[not translated]"
                else:
                    check = (":green-badge[translated after 1 retry]" if u.get("retried")
                             else ":green-badge[translated]")
                numbers = (f":green-badge[numbers {num.get('kept', 0)}/{num.get('total', 0)}]" if num.get("ok", True)
                           else f":red-badge[numbers {num.get('kept', 0)}/{num.get('total', 0)}, missing "
                                f"{num.get('missing')}]") if num else ""
                st.markdown(f"{who} :gray-badge[{u.get('performative', '')}] {check} {numbers} "
                            f":violet-badge[{short(u.get('model'))}]")
                st.markdown(f"> {safe(u.get('translation') or u.get('error') or '-')}"
                            + (f"  \n> _intent: {safe(u['intent'])}_" if u.get("intent") else ""))
    if positions:
        with st.expander("Reasoning behind this position (for observers)"):
            for key, lab in (("assessment", "Assessment"), ("can_contribute", "Can contribute"),
                             ("constraints", "Constraints"), ("recommendation", "Recommendation"),
                             ("concerns", "Concerns")):
                if positions.get(key):
                    st.markdown(f"**{lab}:** {safe(positions[key])}")


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.caption(f"{ECOSYSTEM_TAGLINE}. Heterogeneous multi-enterprise agentic AI (MVP).  \n_{ECOSYSTEM_MEANING}_")
    health = call("GET", "/health")
    st.markdown(f"**Orchestrator** {':green[online]' if health else ':red[offline]'}  \n`{API}`")
    st.divider()
    st.markdown("**Enterprise dashboards**")
    for e in registry():
        mark = ":green[●]" if e.get("online") else ":red[●]"
        out = " (simulated outage)" if e.get("environment", {}).get("outage") else ""
        st.markdown(f"{mark} [{e['name']}]({e.get('dashboard_url', '#')}) `:{ENTERPRISE_PORTS[e['id']]}`{out}")
    st.divider()
    ls = llm_status()
    if ls:
        on, stats = ls.get("enabled"), ls.get("stats", {})
        st.markdown(f"**LLM communication** {':green[on]' if on else ':gray[off]'}")
        lines = [f"{e['name']}: {ml.label(agent_lang(e))}, `{short((xent(e).get('llm') or {}).get('model'))}`"
                 for e in registry() if xent(e)]
        lines += [f"{name}: viewer language, `{short(model)}`"
                  for name, model in (ls.get("orchestrator_agents") or {}).items()]
        st.caption("Each agent speaks its own language and runs its own LLM:  \n" + "  \n".join(lines))
        st.caption(f"Backup chain for every agent: Groq → Gemini → OpenRouter. Orchestrator calls: "
                   f"{stats.get('ok', 0)} ok, {stats.get('failed', 0)} failed, "
                   f"{stats.get('rate_limited', 0)} free-tier rate limits hit")
    st.divider()
    if st.button("Reset world to normal", icon=":material/restart_alt:", width="stretch"):
        call("POST", "/environment", json={"scenario": "normal"})
        registry.clear()
        st.toast("All enterprises reset to normal conditions")

st.title(f"{ECOSYSTEM_NAME}: Global Agentic Control Center")
st.caption(ECOSYSTEM_TAGLINE)
tab_eco, tab_sim, tab_msg, tab_ask, tab_eval, tab_gloss = st.tabs(
    [":material/hub: Ecosystem", ":material/bolt: Simulation", ":material/forum: Agent messages",
     ":material/chat: Ask the agents", ":material/analytics: Evaluation", ":material/translate: Glossary (EN)"])

# ------------------------------------------------------------------ ecosystem tab
with tab_eco:
    reg = registry()
    c1, c2 = st.columns([3, 2])
    with c1:
        st.subheader("Global architecture")
        nodes = "\n".join(
            f'"{e["id"]}" [label="{e["name"]}\\n:{ENTERPRISE_PORTS[e["id"]]}\\n'
            f'{(e.get("card") or {}).get("x-enterprise", {}).get("native_protocol", "?").split(" (")[0][:34]}", '
            f'fillcolor="{"#dcfce7" if e.get("online") else "#fee2e2"}"];' for e in reg)
        edges = "\n".join(f'"interop" -> "{e["id"]}" [dir=both];' for e in reg)
        dot = f"""digraph G {{ rankdir=LR; bgcolor="transparent"; nodesep=0.25; ranksep=0.5;
          node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10, fillcolor="#eef2ff", color="#94a3b8"];
          edge [color="#64748b", fontsize=9, fontname="Helvetica"];
          ui [label="Global Agentic Control Center\\nStreamlit :8501", fillcolor="#e0e7ff"];
          orch [label="Multi-Enterprise Orchestrator :8200\\n(discovery, perception, reasoning)"];
          engine [label="Global Planning, Negotiation\\n& Decision Engine (J(x), CNP, bargaining)"];
          interop [label="Interoperability & Policy Layer\\n(protocol adapters, phi_i mappings, A2A-lite, governance)"];
          mon [label="Monitoring, Evaluation\\n& Replanning"];
          ui -> orch; orch -> interop; interop -> engine; engine -> mon; mon -> orch [style=dashed, label="replan"];
          {nodes}
          {edges}
        }}"""
        st.graphviz_chart(dot, width="stretch")
    with c2:
        st.subheader("Why this ecosystem is heterogeneous")
        st.markdown(
            "Each enterprise differs on **six dimensions** (BAB 10, 10.2.B); the doc's minimum is two:\n"
            "- **Platform**: Google Cloud / on-prem legacy / Azure / AWS / edge (simulated)\n"
            "- **Database**: CSV data lake / SQLite EMS / JSON documents / SQLite ledger / JSON Lines\n"
            "- **Protocol**: REST+AIP / SOAP-XML / Bahasa REST + OpenADR-style / REST v2 / JSON-RPC 2.0\n"
            "- **Semantics**: kW@15 min UTC / MWh legacy time / kWh in Bahasa / canonical / SOC % + epoch\n"
            "- **Agent paradigm**: ML learning agent / reactive rules / BDI / LP market / MPC\n"
            "- **Governance**: each enterprise enforces its own G_i (veto, human approval, SOC limits, price cap)\n\n"
            "Plus two facets of real-world heterogeneity bridged by LLMs:\n"
            "- **Language** (part of semantics): 中文 / Bahasa Indonesia / Bahasa Indonesia / English / English. "
            "Each agent writes in its own language; the receiver's own LLM understands it\n"
            "- **LLM**: Qwen (Alibaba) / gpt-oss-120b and gpt-oss-20b (OpenAI) / Nemotron (NVIDIA) / Gemini "
            "(Google). Every company runs its own model")
    st.subheader("Heterogeneity matrix  E_i = (U_i, M_i, D_i, A_i, P_i, G_i)")
    rows = []
    for e in reg:
        x = (e.get("card") or {}).get("x-enterprise", {})
        rows.append({"Enterprise": e["name"], "Online": "yes" if e.get("online") else "no",
                     "Platform (T)": x.get("platform"), "Database (D)": x.get("database"),
                     "Native protocol (P)": x.get("native_protocol"),
                     "Data semantics": "; ".join(f"{k}: {v}" for k, v in (x.get("data_semantics") or {}).items()),
                     "Agent paradigm (A)": x.get("agent_paradigm"), "AI models": "; ".join(x.get("ai_models", [])),
                     "Working language": (x.get("working_language") or {}).get("label"),
                     "LLM (vendor)": (x.get("llm") or {}).get("vendor"),
                     "Users (U)": ", ".join(x.get("users", [])), "Governance (G)": len(x.get("governance", []))})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    with st.expander("Semantic mappings phi_i : D_i -> D* used by the interoperability layer"):
        st.dataframe(pd.DataFrame(call("GET", "/interop/mappings") or []), width="stretch", hide_index=True)
    with st.expander("Enterprise specifications (from each agent card)"):
        for e in reg:
            x = (e.get("card") or {}).get("x-enterprise", {})
            if not x:
                continue
            st.markdown(f"**{x['name']}**: {x['role']}  \nModules: {', '.join(x['modules'])}  \n"
                        f"Agents: {', '.join(a['name'] + ' (' + a['paradigm'] + ')' for a in x['agents'])}  \n"
                        f"Governance: " + " | ".join(x["governance"]))

# ------------------------------------------------------------------ simulation tab
with tab_sim:
    sc = scenarios()
    labels = [f"{h:02d}:00" for h in range(8, 16)]
    c1, c2, c3 = st.columns([3, 2, 2])
    scen = c1.selectbox("Environment scenario", list(sc), format_func=lambda k: sc[k]["label"])
    mode = c2.radio("Input source", ["forecast", "reference"], horizontal=True,
                    format_func=lambda m: "Agent forecasts (ML/MPC)" if m == "forecast" else "Case Study 04 table",
                    help="Forecast: every enterprise uses its own predictive model. Reference: the document's table "
                         "values (exact Rp4,400,000 baseline).")
    onset = c3.selectbox("Disruption starts at", list(range(8)), format_func=lambda i: labels[i],
                         help="Slots before this hour keep the plan already in execution (adaptive replanning).")
    if scen in sc:
        st.caption(sc[scen]["description"])
    l1, l2, l3 = st.columns([2, 2, 3])
    language = l1.selectbox("Viewer language (renderings, synthesis, explanation)",
                            [k for k in LANG_OPTIONS if k != "auto"], format_func=lambda k: LANG_OPTIONS[k], index=0,
                            help="Agents always write in their own working language (SolarGreen 中文, PowerGrid and "
                                 "IndustriPrima Bahasa Indonesia, EnergyTrade and BatteryFlex English) and understand "
                                 "each other through their own LLMs. This sets the language shown to you.")
    use_llm = l2.toggle("LLM reasoning & communication", value=True,
                        help="Each agent uses its own LLM, with Groq → Gemini → OpenRouter as backup. Off = "
                             "deterministic fallbacks only. Binding decisions never depend on the LLM.")
    deliberation = l3.radio("Agent round table", ["auto", "always", "off"], horizontal=True,
                            help="auto = when the agents perceive a deviation or deficit")
    defaults = {e["id"]: agent_lang(e) for e in registry() if xent(e)}
    with st.expander("Agent working languages (what-if)"):
        st.caption("Each enterprise has its own working language. Change one to see that the others still "
                   "understand it, e.g. make BatteryFlex speak Japanese.")
        lc = st.columns(max(1, len(defaults)))
        agent_languages = {}
        for col, (eid, code) in zip(lc, defaults.items()):
            opts = [code] + [c for c in WORKING_LANGS if c != code]
            pick = col.selectbox(ENTERPRISE_NAMES.get(eid, eid), opts, format_func=ml.label, key=f"wl-{eid}")
            if pick != code:
                agent_languages[eid] = pick
    with st.expander("Global objective weights:  J(x) = α·C(x) + β·T(x) + γ·R(x) − δ·Q(x)"):
        w1, w2, w3, w4 = st.columns(4)
        weights = {"alpha": w1.number_input("alpha (per Rp of cost)", value=1.0, step=0.1),
                   "beta": w2.number_input("beta (Rp per kWh not served on time)", value=100.0, step=10.0),
                   "gamma": w3.number_input("gamma (Rp per risk-kWh)", value=200.0, step=10.0),
                   "delta": w4.number_input("delta (Rp per renewable kWh)", value=50.0, step=10.0)}
    b1, b2, _ = st.columns([2, 2, 4])
    body = {"scenario": scen, "mode": mode, "onset_slot": onset, "weights": weights, "language": language,
            "llm": use_llm, "deliberation": deliberation, "agent_languages": agent_languages}
    if b1.button("Run agentic cycle", type="primary", icon=":material/play_arrow:", width="stretch"):
        with st.spinner("Agents are perceiving, negotiating and deciding ..."):
            run = call("POST", "/runs", json=body)
        if run:
            st.session_state["run_id"] = run["run_id"]
            registry.clear()
    if b2.button("Baseline + scenario (compare)", icon=":material/compare_arrows:", width="stretch"):
        with st.spinner("Running the normal baseline, then the scenario ..."):
            res = call("POST", "/runs/compare", json=body)
        if res:
            st.session_state["run_id"] = res["scenario"]["run_id"]
            registry.clear()

    runs = call("GET", "/runs") or []
    if not runs:
        st.info("No runs yet. Choose a scenario and press **Run agentic cycle** "
                "(tip: start with *Normal operation*, then *Solar production drops 40%*).")
    else:
        ids = [r["run_id"] for r in runs]
        current = st.session_state.get("run_id", ids[0])
        sel = st.selectbox("Showing run", ids, index=ids.index(current) if current in ids else 0,
                           format_func=lambda i: next(f"{r['run_id']} - {r['summary'].get('scenario_label')} "
                                                      f"[{r['mode']}] {r['status']}" for r in runs if r["run_id"] == i))
        st.session_state["run_id"] = sel
        run = call("GET", f"/runs/{sel}")
        if run:
            st.markdown(f"### {run['run_id']} - {run['scenario']['label']}  \n"
                        f"Status {STATUS_ICON.get(run['status'], run['status'])} - mode `{run['mode']}` - "
                        f"disruption from {run['onset_label']} - {run['message_stats']['messages']} messages - "
                        f"interoperability success {run['interoperability_success_rate_pct']}%")
            if run.get("error"):
                st.error(run["error"])
            k = run.get("kpis", {})
            base_k = {x["kpi"]: x for x in (run.get("monitoring") or {}).get("kpis", [])}

            def delta(key, fmt="{:+,.0f}"):
                return fmt.format(base_k[key]["delta"]) if key in base_k else None

            if k:
                m = st.columns(4)
                m[0].metric("Total cost", rp(k["total_cost_rp"]), delta("total_cost_rp"), delta_color="inverse", border=True)
                m[1].metric("Objective J(x)", f"{k['J']:,.0f}", delta("J"), delta_color="inverse", border=True)
                m[2].metric("Renewable share", f"{k['renewable_share_pct']:.1f}%", delta("renewable_share_pct", "{:+.1f}"), border=True)
                m[3].metric("Emissions", f"{k['emissions_kg']:,.0f} kg", delta("emissions_kg"), delta_color="inverse", border=True)
                m = st.columns(4)
                m[0].metric("Demand response", f"{k['dr_kwh']:,.0f} kWh", border=True)
                m[1].metric("Unserved (load shedding)", f"{k['unserved_kwh']:,.0f} kWh", border=True)
                m[2].metric("Peak grid use", f"{k['peak_grid_utilisation_pct']:.0f}%", border=True)
                m[3].metric("Flexibility cost", rp(k.get("flexibility_cost_rp")), border=True)

            exp = run.get("explanation")
            if exp:
                src = f"{exp.get('provider')}:{exp.get('model')}" if exp.get("llm_ok") else "deterministic fallback"
                st.info(f"**Explainer Agent** ({src})\n\n{exp['text']}", icon=":material/record_voice_over:")

            if run.get("decision"):
                d = run["decision"]
                order = list(RES_COLORS)
                recs = [{"slot": labels[i], "resource": RES_LABELS.get(r, r), "kwh": v,
                         "stack": order.index(r) if r in order else 99}
                        for r, vals in d["plan_kwh"].items() for i, v in enumerate(vals)]
                recs += [{"slot": labels[i], "resource": RES_LABELS["unserved"], "kwh": v, "stack": 99}
                         for i, v in enumerate(d["unserved_kwh"]) if v > 0]
                df = pd.DataFrame(recs)
                domain = [RES_LABELS[r] for r in RES_COLORS]
                bars = alt.Chart(df).mark_bar().encode(
                    x=alt.X("slot:N", sort=labels, title="Hour (WIB)"),
                    y=alt.Y("sum(kwh):Q", title="kWh"),
                    color=alt.Color("resource:N", scale=alt.Scale(domain=domain, range=list(RES_COLORS.values())),
                                    legend=alt.Legend(orient="bottom", title=None, columns=4, labelLimit=220)),
                    order=alt.Order("stack:Q", sort="ascending"),
                    tooltip=["slot", "resource", alt.Tooltip("kwh:Q", format=",.1f")])
                dem = pd.DataFrame({"slot": labels, "demand": run["perception"]["demand_kwh"]})
                line = alt.Chart(dem).mark_line(point=True, color="#111827", strokeDash=[4, 3]).encode(
                    x=alt.X("slot:N", sort=labels), y="demand:Q", tooltip=["slot", alt.Tooltip("demand:Q", format=",.1f")])
                cc1, cc2 = st.columns([3, 2])
                with cc1:
                    st.markdown("**Energy dispatch plan per hour** (dashed line = demand)")
                    st.altair_chart((bars + line).properties(height=330), width="stretch")
                with cc2:
                    st.markdown("**Decision totals**")
                    tot = [{"Resource": RES_LABELS.get(r, r), "kWh": v,
                            "Cost (Rp)": k.get("cost_by_resource_rp", {}).get(r, 0.0)} for r, v in d["totals_kwh"].items()]
                    if k.get("unserved_kwh"):
                        tot.append({"Resource": "Unserved", "kWh": k["unserved_kwh"], "Cost (Rp)": 0.0})
                    st.dataframe(pd.DataFrame(tot), width="stretch", hide_index=True,
                                 column_config={"kWh": st.column_config.NumberColumn(format="%,.1f"),
                                                "Cost (Rp)": st.column_config.NumberColumn(format="%,.0f")})
                    obj = d["objective"]
                    st.markdown(f"C = {rp(obj.get('C_cost_rp'))}, T = {obj.get('T_disruption_kwh', 0):,.1f} kWh, "
                                f"R = {obj.get('R_risk_kwh', 0):,.1f} risk-kWh, Q = {obj.get('Q_renewable_kwh', 0):,.0f} kWh  \n"
                                f"**J(x) = {obj.get('J', 0):,.0f}** (alpha={obj.get('alpha')}, beta={obj.get('beta')}, "
                                f"gamma={obj.get('gamma')}, delta={obj.get('delta')})")
                    if d.get("lot_adjustments"):
                        st.caption("Hub lot rule: " + "; ".join(d["lot_adjustments"]))

            mon = run.get("monitoring") or {}
            if mon.get("baseline_run"):
                st.markdown(f"#### Adaptive decision change vs baseline {mon['baseline_run']}  "
                            + (":green[decision changed]" if mon["decision_changed"] else ":gray[no change]"))
                st.dataframe(pd.DataFrame(mon["rows"])[["label", "baseline_kwh", "current_kwh", "delta_kwh"]],
                             width="stretch", hide_index=True)
                st.caption(" | ".join(mon["explanation"]))

            st.markdown("#### Decision trace (perception -> reasoning -> planning -> negotiation -> decision -> action)")
            for t in run.get("trace", []):
                with st.expander(f"{t['step']}. **{t['phase']}** - {t['summary'][:160]}",
                                 icon=PHASE_ICON.get(t["phase"], ":material/radio_button_checked:")):
                    st.caption(f"actor: {t['actor']} - {t['ts']}")
                    st.write(t["summary"])
                    if t.get("detail"):
                        st.json(t["detail"], expanded=False)

            delib = run.get("deliberation")
            if delib:
                syn = delib.get("synthesis") or {}
                cs = delib.get("stats") or {}
                viewer = delib.get("language", "en")
                src = f"{syn.get('provider')}:{syn.get('model')}" if syn.get("llm_ok") else "deterministic fallback"
                st.markdown(f"#### Collaboration: multilingual agent round table "
                            f"({len(delib.get('participants', []))} agents, advisory)")
                st.caption("Each agent writes in its own working language with its own LLM. In round 2 every receiver's "
                           "own LLM interprets the other agents' messages into its language (translation, FIPA-ACL act, "
                           "intent), and a deterministic check verifies that every number survives the translation. "
                           f"The Facilitator Agent ({src}) synthesised the result. Binding decisions stay with the LP "
                           "market, the Contract Net and the safety rules.")
                if cs:
                    q = st.columns(4)
                    q[0].metric("Languages spoken", len(cs.get("languages", [])),
                                help=", ".join(ml.label(c) for c in cs.get("languages", [])), border=True)
                    q[1].metric(f"Messages ({cs.get('rounds', 0)} rounds)", cs.get("messages", 0), border=True)
                    q[2].metric("Foreign messages understood", f"{cs.get('understood', 0)}/{cs.get('interpretations', 0)}",
                                help="Interpretations by the receivers' own LLMs; same-language messages are delivered "
                                     f"as they are ({cs.get('same_language_deliveries', 0)} deliveries).", border=True)
                    q[3].metric("Numbers kept in translation",
                                "-" if cs.get("numbers_kept_pct") is None else f"{cs['numbers_kept_pct']}%",
                                help=f"{cs.get('numbers_kept', 0)} of {cs.get('numbers_total', 0)} numbers survived",
                                border=True)
                s1, s2 = st.columns([3, 2])
                with s1:
                    st.markdown(f"**Joint assessment.** {syn.get('joint_assessment', '')}")
                    if syn.get("recommended_actions"):
                        st.markdown("**Recommended actions**\n" + "\n".join(f"- {a}" for a in syn["recommended_actions"]))
                    if syn.get("negotiation_guidance"):
                        st.markdown(f"**Negotiation guidance.** {syn['negotiation_guidance']}")
                with s2:
                    if syn.get("agreements"):
                        st.markdown("**Agreements**\n" + "\n".join(f"- {a}" for a in syn["agreements"]))
                    if syn.get("conflicts"):
                        st.markdown("**Conflicts of interest**\n" + "\n".join(f"- {a}" for a in syn["conflicts"]))
                responses = delib.get("responses") or {}
                for eid, r in responses.items():
                    if r.get("error"):
                        st.error(f"{ENTERPRISE_NAMES.get(eid, eid)}: {r['error']}")
                conv = delib.get("conversation") or []
                for rnd, title in ((1, "Round 1: positions (each agent in its own language)"),
                                   (2, "Round 2: cross-talk (each agent replies after its own LLM understood the others)")):
                    msgs = [m for m in conv if m["round"] == rnd]
                    if not msgs:
                        continue
                    st.markdown(f"**{title}**")
                    for start in range(0, len(msgs), 2):
                        cols = st.columns(2)
                        for col, m in zip(cols, msgs[start:start + 2]):
                            with col, st.container(border=True):
                                render_message(m, viewer, responses.get(m["from"]) if rnd == 1 else None)

            neg = run.get("negotiation")
            if neg:
                st.markdown("#### Negotiation")
                n1, n2 = st.columns([3, 2])
                with n1:
                    st.markdown(f"**Contract Net** for {neg['need_kwh']:,.0f} kWh (conversation `{neg['conversation_id']}`)")
                    st.dataframe(pd.DataFrame([{"Participant": p["participant"], "Reply": p["decision"],
                                                "Product": p.get("resource"), "kWh": p.get("energy_kwh"),
                                                "Ask (Rp/kWh)": p.get("price_rp_per_kwh"), "Score J/kWh": p.get("score"),
                                                "Reason": p.get("reason")} for p in neg["proposals"]]),
                                 width="stretch", hide_index=True)
                    st.markdown("Awarded: " + (", ".join(f"**{a['participant']}** {a['awarded_kwh']:,.0f} kWh at "
                                                         f"Rp{a['agreed_price_rp_per_kwh']:,.0f}/kWh" for a in neg["awards"])
                                               or "none")
                                + (f"; uncovered {neg['uncovered_kwh']:,.0f} kWh" if neg.get("uncovered_kwh") else ""))
                with n2:
                    bg = neg.get("bargaining")
                    if bg:
                        st.markdown(f"**Alternating offers** with IndustriPrima's BDI agent -> "
                                    f"{'agreed Rp{:,.0f}'.format(bg['price']) if bg['agreed'] else 'no agreement'}")
                        rows = []
                        for r in bg["rounds"]:
                            rows.append({"round": r["round"], "party": "Orchestrator (buyer)", "price": r["buyer_offer"]})
                            if r.get("seller_price"):
                                rows.append({"round": r["round"], "party": "IndustriPrima (seller)", "price": r["seller_price"]})
                        ch = alt.Chart(pd.DataFrame(rows)).mark_line(point=True).encode(
                            x=alt.X("round:O"), y=alt.Y("price:Q", scale=alt.Scale(zero=False), title="Rp/kWh"),
                            color=alt.Color("party:N", legend=alt.Legend(orient="bottom", title=None)),
                            tooltip=["round", "party", alt.Tooltip("price:Q", format=",.0f")])
                        st.altair_chart(ch.properties(height=220), width="stretch")
                        st.caption(f"Buyer tactic: b(t) = {bg['b0']:,.0f} + ({bg['b_max']:,.0f} - {bg['b0']:,.0f})(t/{bg['deadline']})^(1/{bg['e_buyer']}); "
                                   f"seller uses a private Boulware tactic. Closed: {bg['closed_by']}.")

            g1, g2 = st.columns(2)
            with g1:
                if run.get("policies"):
                    st.markdown("#### Governance (federated policies)")
                    st.dataframe(pd.DataFrame(run["policies"]), width="stretch", hide_index=True)
                    dr_ack = (run.get("execution") or {}).get("acks", {}).get("industriprima_dr")
                    if dr_ack and dr_ack.get("status") == "PENDING_APPROVAL":
                        st.warning("IndustriPrima's Energy Manager must approve this demand-response event in the "
                                   f"[IndustriPrima dashboard]({browser_url(ENTERPRISE_PORTS['industriprima'])}) "
                                   "(Approvals tab, role 'Energy Manager').")
                        if st.button("Sync approvals from IndustriPrima", icon=":material/sync:"):
                            call("POST", f"/runs/{run['run_id']}/sync-approvals")
                            st.rerun()
                gs = run.get("grid_security") or {}
                if gs.get("findings"):
                    st.markdown(f"#### PowerGrid security check: {gs.get('verdict')}")
                    st.dataframe(pd.DataFrame(gs["findings"]), width="stretch", hide_index=True)
            with g2:
                if k.get("local_utilities_rp"):
                    st.markdown("#### Local utility U_i = Revenue - Cost - RiskPenalty (reported by each enterprise)")
                    acks = run["execution"]["acks"]
                    st.dataframe(pd.DataFrame([{"Enterprise": ENTERPRISE_NAMES.get(e, e),
                                                "U_i (Rp)": v,
                                                "Acceptable": acks.get(e, {}).get("local_utility", {}).get("acceptable")}
                                               for e, v in k["local_utilities_rp"].items()]),
                                 width="stretch", hide_index=True,
                                 column_config={"U_i (Rp)": st.column_config.NumberColumn(format="%,.0f")})
                    st.caption(f"Ecosystem welfare (sum of U_i): {rp(k.get('ecosystem_welfare_rp'))}. A plan is "
                               "acceptable only if no enterprise is made worse off than its reservation (U_i >= 0).")
                ca = run.get("cost_allocation")
                if ca and ca.get("balancing_cost_rp"):
                    st.markdown(f"**Balancing cost {rp(ca['balancing_cost_rp'])}** ({ca['rule']}): "
                                + ", ".join(f"{ENTERPRISE_NAMES.get(e, e)} {s:.0%} = {rp(ca['charges_rp'].get(e))}"
                                            for e, s in ca["shares"].items()))
                if run.get("local_vs_global"):
                    st.markdown("#### Local enterprise intelligence vs global ecosystem decision")
                    st.dataframe(pd.DataFrame([{"Enterprise": r["enterprise"], "Local intelligence": r["local_intelligence"],
                                                "Local plan (kWh/slot)": ", ".join(f"{v:.0f}" for v in r["local_plan"]),
                                                "Ecosystem decision (kWh/slot)": ", ".join(f"{v:.0f}" for v in r["ecosystem_decision"]),
                                                "Note": r["note"]} for r in run["local_vs_global"]]),
                                 width="stretch", hide_index=True)
            if run.get("market_rounds"):
                with st.expander("Market clearing rounds (EnergyTrade Hub LP)"):
                    st.dataframe(pd.DataFrame([{**{kk: vv for kk, vv in r.items() if kk not in ("dispatch_kwh",
                                                                                                "marginal_price_rp_per_kwh",
                                                                                                "rejected_offers")},
                                                "dispatch_kwh": json.dumps(r["dispatch_kwh"]),
                                                "marginal_price": json.dumps(r["marginal_price_rp_per_kwh"])}
                                               for r in run["market_rounds"]]), width="stretch", hide_index=True)

# ------------------------------------------------------------------ messages tab
with tab_msg:
    runs = call("GET", "/runs") or []
    if not runs:
        st.info("No runs yet.")
    else:
        ids = [r["run_id"] for r in runs]
        cur = st.session_state.get("run_id", ids[0])
        mid = st.selectbox("Conversation (run)", ids, index=ids.index(cur) if cur in ids else 0, key="msg_run")
        msgs = call("GET", f"/runs/{mid}/messages") or []
        types = sorted({m["message_type"] for m in msgs})
        f1, f2 = st.columns(2)
        pick_types = f1.multiselect("Message types", types, default=types)
        query = f2.text_input("Filter by agent (sender or receiver contains)", "")
        view = [m for m in msgs if m["message_type"] in pick_types
                and (not query or query.lower() in (m["sender"] + m["receiver"]).lower())]
        st.caption(f"{len(view)} of {len(msgs)} A2A-lite messages. Each native exchange is logged as a request and a "
                   "reply, with the canonical payload and the native wire format (SOAP, JSON-RPC, Bahasa JSON, ...).")
        df = pd.DataFrame([{"seq": m["seq"], "time": m["ts"][11:23], "sender": m["sender"], "receiver": m["receiver"],
                            "type": m["message_type"], "interaction": m["interaction"], "task": m["task"],
                            "natural-language text (A2A)": ((m.get("payload") or {}).get("text") or "")[:120]
                            if m["interaction"].startswith("a2a") else "",
                            "protocol": (m.get("native") or {}).get("protocol", m["protocol"]), "status": m["status"]}
                           for m in view])
        st.dataframe(df, width="stretch", hide_index=True, height=360)
        if view:
            seq = st.selectbox("Inspect message", [m["seq"] for m in view],
                               format_func=lambda s: next(f"#{m['seq']} {m['message_type']} {m['sender']} -> {m['receiver']}"
                                                          for m in view if m["seq"] == s))
            m = next(x for x in view if x["seq"] == seq)
            i1, i2 = st.columns(2)
            with i1:
                st.markdown("**A2A-lite envelope (canonical payload)**")
                st.json({k: m[k] for k in ("message_id", "run_id", "seq", "ts", "sender", "receiver", "via",
                                           "message_type", "protocol", "interaction", "task", "in_reply_to",
                                           "reply_required", "status", "payload")}, expanded=True)
            with i2:
                st.markdown("**Native wire format (as sent / received)**")
                nat = m.get("native") or {}
                st.caption(f"{nat.get('protocol', 'internal message (no native protocol)')} "
                           f"{nat.get('method', '')} {nat.get('url', '')} "
                           f"{'HTTP ' + str(nat['status']) if nat.get('status') else ''} "
                           f"{str(nat.get('latency_ms')) + ' ms' if nat.get('latency_ms') else ''}")
                body = nat.get("request") or nat.get("response") or nat.get("error") or ""
                lang = "xml" if body.lstrip().startswith("<") else "json"
                st.code(body, language=lang)

# ------------------------------------------------------------------ ask tab (multilingual chat)
with tab_ask:
    chat_mode = st.radio("Conversation", ["a2a", "human"], horizontal=True, label_visibility="collapsed",
                         format_func=lambda k: "Agent ↔ agent (across languages)" if k == "a2a"
                         else "Human → agent (ask in any language)")
    if chat_mode == "a2a":
        st.subheader("Agent ↔ agent conversation across languages")
        st.caption("Pick two agents that speak different languages. The sender's own LLM writes your instruction as a "
                   "message in the sender's working language. The receiver's own LLM understands it (translation, "
                   "FIPA-ACL act, intent; numbers checked) and answers in the receiver's language. The sender's LLM "
                   "then understands the answer. Everything travels over the A2A layer and is logged.")
        cards = {e["id"]: e for e in registry() if xent(e)}
        names = {eid: f"{ENTERPRISE_NAMES.get(eid, eid)} ({ml.label(agent_lang(e))})" for eid, e in cards.items()}
        if len(names) < 2:
            st.info("At least two enterprise agents must be online.")
        else:
            ids = list(names)
            c1, c2, c3 = st.columns(3)
            frm = c1.selectbox("From", ids, format_func=names.get,
                               index=ids.index("solargreen") if "solargreen" in ids else 0)
            tos = [e for e in ids if e != frm]
            to = c2.selectbox("To", tos, format_func=names.get,
                              index=tos.index("powergrid") if "powergrid" in tos else 0)
            vlang = c3.selectbox("Viewer language", ["en", "id", "ja", "jv", "ms"], format_func=LANG_OPTIONS.get,
                                 key="a2a_viewer")
            instruction = st.text_area(
                "What should the sender say? (instruction in any language; the sender rewrites it in its own language)",
                "Ask whether PowerGrid can supply 100 kWh of extra non-firm import at 14:00, because our solar output "
                "may drop by 40% this afternoon.", height=90)
            if st.button("Send through the A2A layer", type="primary", icon=":material/send:"):
                with st.spinner("The agents are writing, understanding and answering ..."):
                    res = call("POST", "/agents/converse",
                               json={"from_agent": frm, "to_agent": to, "message": instruction, "language": vlang})
                if res:
                    st.session_state["a2a_last"] = res
            res = st.session_state.get("a2a_last")
            if res:
                cs = res.get("stats") or {}
                st.caption(f"Conversation `{res.get('conversation_id')}` (see the A2A messages in the log): "
                           f"{cs.get('understood', 0)}/{cs.get('interpretations', 0)} foreign messages understood, "
                           f"numbers kept {cs.get('numbers_kept', 0)}/{cs.get('numbers_total', 0)}.")
                for m in res.get("messages", []):
                    with st.container(border=True):
                        render_message(m, res.get("viewer_language", "en"))
                with st.expander("A2A envelopes of this conversation (as logged by the interoperability layer)"):
                    env = call("GET", f"/runs/{res.get('conversation_id')}/messages") or []
                    st.dataframe(pd.DataFrame([{"seq": e["seq"], "sender": e["sender"], "receiver": e["receiver"],
                                                "act": e["message_type"], "mode": (e.get("payload") or {}).get("mode"),
                                                "text": (e.get("payload") or {}).get("text", ""),
                                                "language": (e.get("payload") or {}).get("text_language")}
                                               for e in env]), width="stretch", hide_index=True)
    else:
        st.subheader("Ask the agents (any language)")
        st.caption("Questions travel over the A2A conversational layer. Enterprise agents treat you as an external "
                   "party, so they never reveal private data (try asking IndustriPrima for its reservation price). The "
                   "Explainer Agent answers questions about a run. Ask in English, Bahasa Indonesia or any other "
                   "language.")
        runs_all = call("GET", "/runs") or []
        targets = {"explainer": "Explainer Agent (about a run)"} | {eid: f"{name} agent" for eid, name in
                                                                     ENTERPRISE_NAMES.items()}
        a1, a2, a3 = st.columns(3)
        target = a1.selectbox("Talk to", list(targets), format_func=lambda k: targets[k])
        run_ids = [r["run_id"] for r in runs_all] or ["-"]
        run_for_q = a2.selectbox("Run (for the Explainer Agent)", run_ids, disabled=target != "explainer")
        lang_q = a3.selectbox("Answer language", ["auto", "en", "id", "zh", "jv", "ms"],
                              format_func=lambda k: LANG_OPTIONS.get(k, k))
        history = st.session_state.setdefault("chat", [])
        for msg in history[-24:]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg.get("meta"):
                    st.caption(msg["meta"])
        prompt = st.chat_input("e.g. Kenapa SolarGreen yang membayar biaya penyeimbangan? / Why did PowerGrid refuse?")
        if prompt:
            history.append({"role": "user", "content": f"**To {targets[target]}:** {prompt}"})
            with st.spinner("The agent is reasoning ..."):
                if target == "explainer":
                    res = (call("POST", f"/runs/{run_for_q}/ask", json={"question": prompt, "language": lang_q})
                           if run_for_q != "-" else {"answer": "Run a scenario first.", "provider": "-", "model": ""})
                else:
                    res = call("POST", f"/agents/{target}/ask", json={"question": prompt, "language": lang_q})
            if res:
                history.append({"role": "assistant", "content": res.get("answer", ""),
                                "meta": f"{res.get('agent') or targets[target]} · {res.get('provider')}:"
                                        f"{res.get('model') or 'fallback'} · {res.get('latency_ms')} ms"})
            st.rerun()
        if history and st.button("Clear conversation", icon=":material/delete:"):
            st.session_state["chat"] = []
            st.rerun()

# ------------------------------------------------------------------ glossary tab (English view of the terms)
with tab_gloss:
    g = glossary()
    st.subheader("Glossary: the terms used in Shakti Tantra, in English")
    flt = st.text_input("Filter", "", placeholder="e.g. SOC, headroom, BDI, kebutuhan")
    gdf = pd.DataFrame(g.get("glossary", []))
    if flt and not gdf.empty:
        gdf = gdf[gdf.apply(lambda r: flt.lower() in f"{r['term']} {r['category']} {r['definition']}".lower(), axis=1)]
    st.dataframe(gdf, width="stretch", hide_index=True,
                 column_config={"definition": st.column_config.TextColumn("definition", width="large")})
    st.subheader("Native Bahasa Indonesia terms (IndustriPrima) and their English meaning")
    st.caption("IndustriPrima keeps its native schema in Bahasa Indonesia on purpose (semantic heterogeneity). The "
               "enterprise dashboards show these translations in their English view; the orchestrator maps them into "
               "the canonical English model.")
    tdf = pd.DataFrame([{"native term": k, "English": v} for k, v in (g.get("translations") or {}).items()])
    if flt and not tdf.empty:
        tdf = tdf[tdf.apply(lambda r: flt.lower() in f"{r['native term']} {r['English']}".lower(), axis=1)]
    st.dataframe(tdf, width="stretch", hide_index=True)

# ------------------------------------------------------------------ evaluation tab
with tab_eval:
    runs = call("GET", "/runs?limit=200") or []
    if not runs:
        st.info("No runs yet.")
    else:
        rows = [{"run": r["run_id"], "created": r["created_at"][11:19], "scenario": r["summary"].get("scenario_label"),
                 "mode": r["mode"], "onset": f"{8 + (r['onset_slot'] or 0):02d}:00", "status": r["status"],
                 "cost_rp": r["summary"].get("total_cost_rp"), "J": r["summary"].get("J"),
                 "dr_kwh": r["summary"].get("dr_kwh"), "unserved_kwh": r["summary"].get("unserved_kwh"),
                 "renewable_pct": r["summary"].get("renewable_share_pct"),
                 "decision_changed": r["summary"].get("decision_changed"), "messages": r["summary"].get("messages")}
                for r in runs]
        df = pd.DataFrame(rows)
        st.dataframe(df, width="stretch", hide_index=True,
                     column_config={"cost_rp": st.column_config.NumberColumn("cost (Rp)", format="%,.0f"),
                                    "J": st.column_config.NumberColumn(format="%,.0f")})
        ok = df.dropna(subset=["cost_rp"])
        if not ok.empty:
            e1, e2 = st.columns(2)
            with e1:
                st.markdown("**Total cost per run**")
                st.altair_chart(alt.Chart(ok).mark_bar().encode(
                    x=alt.X("run:N", sort=None), y=alt.Y("cost_rp:Q", title="Rp"), color=alt.Color("scenario:N", legend=None),
                    tooltip=["run", "scenario", "mode", alt.Tooltip("cost_rp:Q", format=",.0f")]).properties(height=260),
                    width="stretch")
            with e2:
                st.markdown("**Flexibility used per run (kWh)**")
                melt = ok.melt(id_vars=["run", "scenario"], value_vars=["dr_kwh", "unserved_kwh"], var_name="kind",
                               value_name="kwh")
                st.altair_chart(alt.Chart(melt).mark_bar().encode(
                    x=alt.X("run:N", sort=None), y=alt.Y("kwh:Q"), color=alt.Color("kind:N", legend=alt.Legend(orient="bottom")),
                    tooltip=["run", "scenario", "kind", "kwh"]).properties(height=260), width="stretch")
        st.markdown("**KPI definitions**: total cost = pay-as-bid firm energy + flexibility (DR, reserve); "
                    "J(x) = α·C + β·T + γ·R − δ·Q (C includes VOLL × unserved); renewable share = solar / physical "
                    "supply; emissions = grid kWh × 0.87 kgCO2/kWh (synthetic assumption); decision changed = any "
                    "resource dispatch differs by ≥ 1 kWh from the latest normal baseline in the same mode.")
