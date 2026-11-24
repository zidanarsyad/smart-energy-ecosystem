"""IndustriPrima Energy Agent: a BDI (Belief-Desire-Intention) agent.

Interpreter cycle per incoming event (Rao 1996, AgentSpeak(L); Wooldridge 2009, ch. 4):
    1. belief revision      - update beliefs from the event
    2. option generation    - plans whose trigger matches and whose context condition holds
    3. deliberation         - choose the option serving the highest-priority desire
    4. intention & action   - commit to the plan and execute its body
    5. trace                - beliefs/options/choice are recorded for explainability
The agent's private information (reservation price, loss values) never leaves the enterprise;
only its *decisions* are communicated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from common.timeutil import now_iso


@dataclass
class Desire:
    id: str
    description: str
    priority: int


@dataclass
class Plan:
    name: str
    trigger: str
    serves: str
    description: str
    context: Callable[[dict, dict], bool]
    body: Callable[["BDIAgent", dict], dict]


class BDIAgent:
    def __init__(self, agent_id: str, beliefs: dict, desires: list[Desire], plans: list[Plan]):
        self.agent_id = agent_id
        self.beliefs = beliefs
        self.desires = {d.id: d for d in desires}
        self.plans = plans
        self.intentions: list[dict] = []
        self.traces: list[dict] = []

    def revise_beliefs(self, event: dict) -> None:
        self.beliefs["event_terakhir"] = {"jenis": event["type"], "waktu": now_iso()}
        for key, value in event.get("beliefs", {}).items():
            self.beliefs[key] = value

    def options(self, event: dict) -> list[Plan]:
        return [p for p in self.plans if p.trigger == event["type"] and p.context(self.beliefs, event)]

    def deliberate(self, options: list[Plan]) -> Plan:
        return max(options, key=lambda p: self.desires[p.serves].priority)

    def handle(self, event: dict) -> dict:
        self.revise_beliefs(event)
        opts = self.options(event)
        if not opts:
            result = {"keputusan": "TIDAK_ADA_RENCANA", "alasan": f"tidak ada rencana yang berlaku untuk {event['type']}",
                      "alasan_en": f"no applicable plan for {event['type']}"}
            chosen = None
        else:
            chosen = self.deliberate(opts)
            intention = {"rencana": chosen.name, "tujuan": chosen.serves, "event": event["type"],
                         "mulai": now_iso(), "status": "dijalankan"}
            self.intentions.append(intention)
            result = chosen.body(self, event)
            intention["status"] = result.get("status_intensi", "selesai")
        trace = {
            "waktu": now_iso(), "event": event["type"],
            "keyakinan": self.snapshot(),
            "keinginan": [{"id": d.id, "deskripsi": d.description, "prioritas": d.priority}
                          for d in sorted(self.desires.values(), key=lambda d: -d.priority)],
            "opsi": [{"rencana": p.name, "melayani": p.serves} for p in opts],
            "rencana_terpilih": chosen.name if chosen else None,
            "alasan_pemilihan": (f"melayani keinginan {chosen.serves} "
                                 f"(prioritas {self.desires[chosen.serves].priority})") if chosen else None,
            "tindakan": {k: v for k, v in result.items() if k != "status_intensi"},
        }
        self.traces = (self.traces + [trace])[-50:]
        self.intentions = self.intentions[-50:]
        return {**result, "jejak_bdi": trace}

    def snapshot(self) -> dict:
        keys = ["prakiraan_window_kwh", "fleksibel_total_kwh", "pesanan_mendesak", "negosiasi_aktif",
                "dr_disepakati_kwh", "harga_dr_disepakati", "event_terakhir"]
        return {k: self.beliefs.get(k) for k in keys}


# ---------------------------------------------------------------- IndustriPrima plan library
def concession_ask(t: int, a0: float, reservation: float, deadline: int, e: float) -> float:
    """Time-dependent concession tactic (Faratin, Sierra & Jennings 1998); e<1 = Boulware."""
    frac = min(1.0, max(0.0, t / deadline))
    return float(round(a0 - (a0 - reservation) * frac ** (1.0 / e)))  # whole rupiah


def allocate_to_processes(per_hour_kwh: list[float], processes: list[dict]) -> tuple[list[list[dict]], float]:
    plan, loss = [], 0.0
    ordered = sorted(processes, key=lambda p: p["nilai_kerugian_rp_per_kwh"])
    for need in per_hour_kwh:
        rest, hour_plan = need, []
        for p in ordered:
            if rest <= 1e-9:
                break
            take = min(rest, float(p["daya_kw"]))
            hour_plan.append({"kode": p["kode"], "nama": p["nama"], "kwh": round(take, 2)})
            loss += take * p["nilai_kerugian_rp_per_kwh"]
            rest -= take
        plan.append(hour_plan)
    return plan, round(loss, 2)


def build_energy_agent(policy: dict, processes: list[dict]) -> BDIAgent:
    desires = [
        Desire("D1", "Penuhi target produksi (beban kritis wajib terlayani)", 3),
        Desire("D2", "Minimalkan biaya energi dan dapatkan kompensasi DR yang layak", 2),
        Desire("D3", "Dukung stabilitas jaringan dan ekosistem energi", 2),
        Desire("D4", "Capai pangsa energi terbarukan minimal 30%", 1),
    ]
    beliefs = {"kebijakan": policy, "proses_fleksibel": processes, "pesanan_mendesak": False,
               "negosiasi": {}, "negosiasi_aktif": 0, "dr_disepakati_kwh": 0.0, "harga_dr_disepakati": None,
               "fleksibel_per_jam": [], "fleksibel_total_kwh": 0.0, "prakiraan_window_kwh": None}
    pol = policy
    T, e = int(pol["maks_putaran"]), float(pol["taktik_konsesi_e"])
    a0, r = float(pol["harga_awal_dr_rp_per_kwh"]), float(pol["harga_reservasi_dr_rp_per_kwh"])

    def flex_available(b: dict, ev: dict) -> float:
        allowed = ev.get("slot_diizinkan") or list(range(len(b["fleksibel_per_jam"])))
        return sum(b["fleksibel_per_jam"][i] for i in allowed if i < len(b["fleksibel_per_jam"]))

    # ---- CFP (Contract Net participant)
    def refuse_urgent(agent, ev):
        return {"keputusan": "MENOLAK", "alasan": "Ada pesanan mendesak; beban fleksibel tidak dapat dikurangi (D1).",
                "alasan_en": "Urgent order in progress; flexible load cannot be reduced (desire D1)."}

    def propose(agent, ev):
        avail = flex_available(agent.beliefs, ev)
        qty = round(min(float(ev["kebutuhan_kwh"]), avail), 2)
        conv = ev["conv"]
        agent.beliefs["negosiasi"][conv] = {"putaran": 0, "ask": a0, "kuantitas": qty, "status": "USULAN"}
        agent.beliefs["negosiasi_aktif"] = sum(1 for n in agent.beliefs["negosiasi"].values() if n["status"] != "SELESAI")
        allowed = ev.get("slot_diizinkan") or list(range(len(agent.beliefs["fleksibel_per_jam"])))
        per_jam = [round(agent.beliefs["fleksibel_per_jam"][i], 2) if i in allowed else 0.0
                   for i in range(len(agent.beliefs["fleksibel_per_jam"]))]
        partial = qty < float(ev["kebutuhan_kwh"]) - 1e-6
        return {"keputusan": "USULAN", "kuantitas_kwh": qty, "harga_rp_per_kwh": a0, "maks_per_jam_kwh": per_jam,
                "alasan": ("Usulan parsial: fleksibilitas tersedia hanya " if partial else "Usulan penuh: ")
                          + f"{avail:.0f} kWh dari proses fleksibel (maks {pol['batas_kurtailmen_persen']}% beban).",
                "alasan_en": ("Partial proposal: only " if partial else "Full proposal: ")
                             + f"{avail:.0f} kWh available from flexible processes "
                               f"(max {pol['batas_kurtailmen_persen']}% of load)."}

    def refuse_no_flex(agent, ev):
        return {"keputusan": "MENOLAK", "alasan": "Tidak ada beban fleksibel pada slot yang diminta.",
                "alasan_en": "No flexible load in the requested hours."}

    # ---- bilateral bargaining (alternating offers)
    def accept_offer(agent, ev):
        conv, price = ev["conv"], float(ev["harga"])
        n = agent.beliefs["negosiasi"].setdefault(conv, {"kuantitas": ev.get("kuantitas", 0)})
        n.update({"putaran": ev["putaran"], "status": "SELESAI", "harga": price})
        agent.beliefs["harga_dr_disepakati"] = price
        agent.beliefs["dr_disepakati_kwh"] = n.get("kuantitas", 0)
        nxt = concession_ask(ev['putaran'] + 1, a0, r, T, e)
        return {"keputusan": "MENERIMA", "harga_rp_per_kwh": price, "kuantitas_kwh": n.get("kuantitas"),
                "alasan": f"Tawaran Rp{price:,.0f} >= ask berikutnya Rp{nxt:,.0f} (taktik Boulware e={e}).",
                "alasan_en": f"Offer Rp{price:,.0f} >= my next ask Rp{nxt:,.0f} (Boulware tactic e={e}): accepted."}

    def counter(agent, ev):
        conv, t = ev["conv"], int(ev["putaran"])
        ask = concession_ask(t + 1, a0, r, T, e)
        n = agent.beliefs["negosiasi"].setdefault(conv, {"kuantitas": ev.get("kuantitas", 0)})
        n.update({"putaran": t, "ask": ask, "status": "TAWAR_MENAWAR"})
        return {"keputusan": "TAWARAN_BALIK", "harga_rp_per_kwh": ask, "kuantitas_kwh": n.get("kuantitas"),
                "alasan": f"Tawaran Rp{float(ev['harga']):,.0f} di bawah ask putaran {t + 1} (Rp{ask:,.0f}).",
                "alasan_en": f"Offer Rp{float(ev['harga']):,.0f} is below my round-{t + 1} ask (Rp{ask:,.0f}): "
                             "counter-offer."}

    def reject_deadline(agent, ev):
        n = agent.beliefs["negosiasi"].setdefault(ev["conv"], {})
        n.update({"status": "SELESAI", "hasil": "GAGAL"})
        return {"keputusan": "MENOLAK", "alasan": "Batas putaran tercapai dan tawaran di bawah harga reservasi.",
                "alasan_en": "Round limit reached and the offer is below my reservation price."}

    def confirm(agent, ev):
        n = agent.beliefs["negosiasi"].setdefault(ev["conv"], {})
        n.update({"status": "SELESAI", "harga": float(ev["harga"])})
        agent.beliefs["harga_dr_disepakati"] = float(ev["harga"])
        agent.beliefs["dr_disepakati_kwh"] = n.get("kuantitas", 0)
        return {"keputusan": "KONFIRMASI", "harga_rp_per_kwh": float(ev["harga"]),
                "kuantitas_kwh": n.get("kuantitas"), "alasan": "Kesepakatan DR dicatat.",
                "alasan_en": "DR agreement recorded."}

    def note_rejection(agent, ev):
        agent.beliefs["negosiasi"].setdefault(ev["conv"], {}).update({"status": "SELESAI", "hasil": "DITOLAK_PEMBELI"})
        return {"keputusan": "DICATAT", "alasan": "Usulan tidak dipilih oleh koordinator ekosistem.",
                "alasan_en": "Proposal not selected by the ecosystem coordinator."}

    # ---- OpenADR-style DR dispatch event
    def opt_out(agent, ev):
        return {"keputusan": "OPT_OUT", "status_event": "OPT_OUT",
                "alasan": "Pesanan mendesak: event DR ditolak untuk melindungi produksi (D1).",
                "alasan_en": "Urgent order: DR event declined to protect production (desire D1)."}

    def opt_out_price(agent, ev):
        return {"keputusan": "OPT_OUT", "status_event": "OPT_OUT",
                "alasan": f"Harga event Rp{ev['harga']:,.0f} di bawah reservasi.",
                "alasan_en": f"Event price Rp{ev['harga']:,.0f} is below my reservation price."}

    def auto_accept(agent, ev):
        plan, loss = allocate_to_processes(ev["per_jam_kwh"], agent.beliefs["proses_fleksibel"])
        return {"keputusan": "DITERIMA", "status_event": "ACCEPTED", "rencana_proses": plan,
                "nilai_kerugian_rp": loss,
                "alasan": f"Total {sum(ev['per_jam_kwh']):.0f} kWh <= batas persetujuan otomatis "
                          f"{pol['batas_persetujuan_otomatis_kwh']} kWh.",
                "alasan_en": f"Total {sum(ev['per_jam_kwh']):.0f} kWh <= auto-approval limit "
                             f"{pol['batas_persetujuan_otomatis_kwh']} kWh: accepted automatically."}

    def escalate(agent, ev):
        plan, loss = allocate_to_processes(ev["per_jam_kwh"], agent.beliefs["proses_fleksibel"])
        return {"keputusan": "MENUNGGU_PERSETUJUAN", "status_event": "PENDING_APPROVAL", "rencana_proses": plan,
                "nilai_kerugian_rp": loss, "status_intensi": "menunggu manusia",
                "alasan": f"Total {sum(ev['per_jam_kwh']):.0f} kWh > batas otomatis "
                          f"{pol['batas_persetujuan_otomatis_kwh']} kWh: perlu persetujuan Energy Manager.",
                "alasan_en": f"Total {sum(ev['per_jam_kwh']):.0f} kWh > auto-approval limit "
                             f"{pol['batas_persetujuan_otomatis_kwh']} kWh: Energy Manager approval required."}

    # ---- supply schedule notification
    def record_schedule(agent, ev):
        share = ev.get("pangsa_terbarukan_persen", 0.0)
        target = pol["target_energi_terbarukan_persen"]
        return {"keputusan": "DICATAT", "pangsa_terbarukan_persen": share,
                "alasan": ("Target energi terbarukan tercapai" if share >= target
                           else f"Pangsa terbarukan {share:.1f}% di bawah target {target}% (D4 belum tercapai)"),
                "alasan_en": ("Renewable target reached" if share >= target
                              else f"Renewable share {share:.1f}% is below the {target}% target (desire D4 not met)")}

    urgent = lambda b, ev: bool(b.get("pesanan_mendesak"))  # noqa: E731
    plans = [
        Plan("tolak_cfp_pesanan_mendesak", "cfp_dr", "D1", "Tolak CFP bila ada pesanan mendesak", urgent, refuse_urgent),
        Plan("usulan_dr", "cfp_dr", "D3", "Ajukan usulan DR dari beban fleksibel",
             lambda b, ev: not urgent(b, ev) and flex_available(b, ev) > 0, propose),
        Plan("tolak_tanpa_fleksibilitas", "cfp_dr", "D1", "Tolak bila tak ada beban fleksibel",
             lambda b, ev: not urgent(b, ev) and flex_available(b, ev) <= 0, refuse_no_flex),
        Plan("terima_tawaran", "tawaran_dr", "D2", "Terima tawaran >= ask berikutnya",
             lambda b, ev: float(ev["harga"]) >= concession_ask(int(ev["putaran"]) + 1, a0, r, T, e), accept_offer),
        Plan("tawar_balik", "tawaran_dr", "D2", "Ajukan tawaran balik (konsesi Boulware)",
             lambda b, ev: float(ev["harga"]) < concession_ask(int(ev["putaran"]) + 1, a0, r, T, e)
             and int(ev["putaran"]) + 1 < T, counter),
        Plan("tolak_batas_waktu", "tawaran_dr", "D1", "Tolak di batas putaran",
             lambda b, ev: float(ev["harga"]) < r and int(ev["putaran"]) + 1 >= T, reject_deadline),
        Plan("konfirmasi_kesepakatan", "terima_dr", "D3", "Konfirmasi kesepakatan DR", lambda b, ev: True, confirm),
        Plan("catat_penolakan", "tolak_dr", "D2", "Catat usulan yang ditolak", lambda b, ev: True, note_rejection),
        Plan("opt_out_pesanan_mendesak", "event_dr", "D1", "Opt-out event DR saat pesanan mendesak", urgent, opt_out),
        Plan("opt_out_harga", "event_dr", "D2", "Opt-out bila harga di bawah reservasi",
             lambda b, ev: not urgent(b, ev) and float(ev["harga"]) < r, opt_out_price),
        Plan("eksekusi_otomatis", "event_dr", "D3", "Terima event DR dalam batas otomatis",
             lambda b, ev: not urgent(b, ev) and float(ev["harga"]) >= r
             and sum(ev["per_jam_kwh"]) <= pol["batas_persetujuan_otomatis_kwh"], auto_accept),
        Plan("eskalasi_persetujuan_manusia", "event_dr", "D1", "Eskalasi ke Energy Manager",
             lambda b, ev: not urgent(b, ev) and float(ev["harga"]) >= r
             and sum(ev["per_jam_kwh"]) > pol["batas_persetujuan_otomatis_kwh"], escalate),
        Plan("catat_jadwal_pasokan", "jadwal_pasokan", "D4", "Catat jadwal pasokan, cek target terbarukan",
             lambda b, ev: True, record_schedule),
    ]
    return BDIAgent("industriprima.bdi_energy_agent", beliefs, desires, plans)
