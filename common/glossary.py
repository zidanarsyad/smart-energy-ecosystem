"""English view: translations of native (Bahasa Indonesia) terms and a glossary of domain terms.

IndustriPrima deliberately keeps its native schema in Bahasa Indonesia (semantic heterogeneity). The dashboards
offer an English view that applies these deterministic translations; the LLM is only used for free text.
"""
from __future__ import annotations

ID_EN = {
    # IndustriPrima fields / collections
    "kebutuhan_kwh": "demand_kwh", "beban_kritis_kwh": "critical_load_kwh", "beban_fleksibel_kwh": "flexible_load_kwh",
    "jam": "hour", "tanggal": "date", "rencana_produksi": "production_plan", "indeks_produksi": "production_index",
    "suhu_prakiraan_c": "forecast_temperature_c", "pesanan_mendesak": "urgent_order", "keterangan": "note",
    "proses_fleksibel": "flexible_processes", "kode": "code", "nama": "name", "daya_kw": "power_kw",
    "nilai_kerugian_rp_per_kwh": "loss_value_rp_per_kwh", "kebijakan_energi": "energy_policy",
    "batas_kurtailmen_persen": "curtailment_limit_pct", "batas_persetujuan_otomatis_kwh": "auto_approval_limit_kwh",
    "harga_reservasi_dr_rp_per_kwh": "dr_reservation_price_rp_per_kwh",
    "harga_awal_dr_rp_per_kwh": "dr_initial_ask_rp_per_kwh", "taktik_konsesi_e": "concession_tactic_e",
    "maks_putaran": "max_rounds", "target_energi_terbarukan_persen": "renewable_target_pct",
    "nilai_beban_padam_rp_per_kwh": "value_of_lost_load_rp_per_kwh",
    "tarif_acuan_pln_rp_per_kwh": "reference_tariff_rp_per_kwh", "negosiasi": "negotiations", "waktu": "time",
    "id_percakapan": "conversation_id", "jenis_pesan": "message_type", "putaran": "round", "harga_masuk": "price_in",
    "keputusan": "decision", "harga_keluar": "price_out", "alasan": "reason", "event_dr": "dr_events",
    "diterima": "received", "harga_rp_per_kwh": "price_rp_per_kwh", "per_jam_kwh": "per_hour_kwh",
    "rencana_proses": "process_plan", "persetujuan": "approvals", "dibuat": "created", "peran_wajib": "required_role",
    "diputuskan_oleh": "decided_by", "waktu_keputusan": "decision_time", "catatan": "note",
    "jadwal_pasokan": "supply_schedules", "energi_terlayani_kwh": "served_energy_kwh",
    "tidak_terlayani_kwh": "unserved_kwh", "biaya_energi_rp": "energy_cost_rp", "kompensasi_dr_rp": "dr_compensation_rp",
    "biaya_penyeimbangan_rp": "balancing_charge_rp", "pangsa_terbarukan_persen": "renewable_share_pct",
    "utilitas_lokal_rp": "local_utility_rp", "prakiraan": "forecast", "riwayat_beban": "load_history",
    "beban_kwh": "load_kwh", "suhu_c": "temperature_c", "jenis_hari": "day_type",
    "profil_beban_nominal": "nominal_load_profile", "beban_kwh_per_jam": "load_kwh_per_hour", "jejak_bdi": "bdi_trace",
    "opsi": "options", "rencana_terpilih": "selected_plan", "alasan_pemilihan": "selection_reason",
    "model_prakiraan": "forecast_model", "metrik": "metric", "nilai": "value", "rencana": "plan", "tujuan": "goal",
    "prakiraan_window_kwh": "window_forecast_kwh", "fleksibel_total_kwh": "flexible_total_kwh",
    "negosiasi_aktif": "active_negotiations", "dr_disepakati_kwh": "agreed_dr_kwh",
    "harga_dr_disepakati": "agreed_dr_price", "event_terakhir": "last_event", "jenis": "type",
    "satuan_energi": "energy_unit", "zona_waktu": "time_zone", "interval": "intervals", "dasar": "basis",
    "harga_maksimum_rp_per_kwh": "max_price_rp_per_kwh", "perusahaan": "company", "id_pelanggan_pln": "utility_customer_id",
    "kuantitas_kwh": "quantity_kwh", "jam_diizinkan": "allowed_hours", "harga_tawaran_rp_per_kwh": "offered_price_rp_per_kwh",
    "maks_per_jam": "max_per_hour", "maks_kwh": "max_kwh", "status_event": "event_status",
    # status / enum values
    "MENUNGGU": "PENDING", "DISETUJUI": "APPROVED", "DITOLAK": "REJECTED", "USULAN": "PROPOSAL", "MENOLAK": "REFUSED",
    "MENERIMA": "ACCEPTED", "TAWARAN_BALIK": "COUNTER_OFFER", "KONFIRMASI": "CONFIRMED", "DICATAT": "RECORDED",
    "DITERIMA": "ACCEPTED", "MENUNGGU_PERSETUJUAN": "AWAITING_APPROVAL", "TIDAK_ADA_RENCANA": "NO_APPLICABLE_PLAN",
    "TAWARAN": "OFFER", "TERIMA": "ACCEPT", "TOLAK": "REJECT", "deklarasi": "declared",
    "dijalankan": "running", "selesai": "done", "menunggu manusia": "waiting for a human",
    "hari_kerja": "weekday", "sabtu": "saturday", "minggu": "sunday",
    # BDI plan names
    "tolak_cfp_pesanan_mendesak": "refuse_cfp_urgent_order", "usulan_dr": "propose_dr",
    "tolak_tanpa_fleksibilitas": "refuse_no_flexibility", "terima_tawaran": "accept_offer", "tawar_balik": "counter_offer",
    "tolak_batas_waktu": "reject_at_deadline", "konfirmasi_kesepakatan": "confirm_agreement",
    "catat_penolakan": "record_rejection", "opt_out_pesanan_mendesak": "opt_out_urgent_order",
    "opt_out_harga": "opt_out_low_price", "eksekusi_otomatis": "auto_execute",
    "eskalasi_persetujuan_manusia": "escalate_to_human_approval", "catat_jadwal_pasokan": "record_supply_schedule",
    # free-text values that occur in the data
    "Kompresor udara cadangan lini B": "Standby air compressor, line B",
    "Pra-pendinginan HVAC gedung kantor": "Office building HVAC pre-cooling",
    "Pengisian forklift listrik": "Electric forklift charging", "Mixer batch lini 3": "Batch mixer, line 3",
    "dapat dimatikan sementara, tekanan dijaga tangki": "can be switched off briefly; tank keeps the pressure",
    "setpoint dinaikkan 2 derajat C": "set-point raised by 2 degC",
    "digeser ke setelah pukul 16:00": "shifted to after 16:00",
    "batch dijadwal ulang, risiko keterlambatan kecil": "batch rescheduled, small risk of delay",
    "Rencana produksi normal 2 shift (data MES)": "Normal two-shift production plan (MES data)",
    "Penuhi target produksi (beban kritis wajib terlayani)": "Meet the production target (critical load must be served)",
    "Minimalkan biaya energi dan dapatkan kompensasi DR yang layak": "Minimise energy cost and get fair DR compensation",
    "Dukung stabilitas jaringan dan ekosistem energi": "Support grid stability and the energy ecosystem",
    "Capai pangsa energi terbarukan minimal 30%": "Reach at least a 30% renewable share",
}

GLOSSARY = [
    {"term": "Shakti Tantra", "category": "Project", "definition": "Name of this ecosystem: Sanskrit shakti (energy, power) + tantra (system, framework) = 'sistem energi' (energy system)."},
    {"term": "Heterogeneous ecosystem", "category": "Concept", "definition": "Enterprises that differ in platform, database, protocol, data semantics, AI/agent paradigm and governance, yet must cooperate (BAB 10, 10.2)."},
    {"term": "Agentic AI", "category": "Concept", "definition": "AI agents that perceive, reason, plan, negotiate, decide, act and adapt toward goals, here across company boundaries."},
    {"term": "A2A (Agent2Agent)", "category": "Protocol", "definition": "Open agent-to-agent protocol concept: agents publish an agent card for discovery and exchange task messages. Here every enterprise exposes an A2A-style card and a JSON-RPC message/send endpoint."},
    {"term": "Agent card", "category": "Protocol", "definition": "JSON document at /.well-known/agent-card.json describing an agent's skills, transport and (here) its enterprise profile E_i."},
    {"term": "MCP (Model Context Protocol)", "category": "Protocol", "definition": "Protocol for connecting an AI model to tools and data. Not used in the MVP; agents use APIs."},
    {"term": "SOAP / XML", "category": "Protocol", "definition": "Legacy web-service style used by PowerGrid's on-premises EMS (XML envelopes, one endpoint, operations in the body)."},
    {"term": "JSON-RPC 2.0", "category": "Protocol", "definition": "Remote-procedure-call style used by BatteryFlex (method + params, numeric error codes)."},
    {"term": "OpenADR", "category": "Protocol", "definition": "Open Automated Demand Response standard. A VTN (server) sends demand-response events to VENs (clients). IndustriPrima exposes an OpenADR 3.0-style VEN endpoint."},
    {"term": "VTN / VEN", "category": "Protocol", "definition": "Virtual Top Node (event issuer, here the orchestrator on behalf of PowerGrid) / Virtual End Node (event receiver, here IndustriPrima)."},
    {"term": "Canonical model D*", "category": "Interoperability", "definition": "Shared data model all native data is mapped to: kWh per hourly slot, Rp/kWh, ISO 8601 time with +07:00."},
    {"term": "Semantic mapping phi_i", "category": "Interoperability", "definition": "Function translating enterprise i's native data (units, clocks, field names, language) into D* and back."},
    {"term": "kW vs kWh", "category": "Energy", "definition": "kW is power (rate); kWh is energy (amount). 100 kW for 15 minutes = 25 kWh."},
    {"term": "WIB", "category": "Energy", "definition": "Waktu Indonesia Barat, Western Indonesia Time, UTC+7."},
    {"term": "Headroom", "category": "Energy", "definition": "How much more power PowerGrid can deliver to the estate in an hour without breaching the contract or feeder limits."},
    {"term": "Feeder / derating", "category": "Energy", "definition": "A feeder is a medium-voltage line serving an area. Derating means its usable capacity is reduced (e.g. transformer maintenance)."},
    {"term": "N-1 security margin", "category": "Energy", "definition": "Spare capacity kept so the network survives the loss of one element."},
    {"term": "Demand response (DR)", "category": "Energy", "definition": "A consumer reduces or shifts flexible load when asked, in exchange for compensation."},
    {"term": "Flexible vs critical load", "category": "Energy", "definition": "Flexible load can be curtailed or shifted (compressors, HVAC pre-cooling, forklift charging); critical load must be served."},
    {"term": "SOC (state of charge)", "category": "Energy", "definition": "Battery energy level in % of capacity. BatteryFlex keeps SOC >= 20% (10-20% is emergency reserve)."},
    {"term": "BESS", "category": "Energy", "definition": "Battery energy storage system (BatteryFlex: 1,250 kWh / 250 kW, LFP chemistry)."},
    {"term": "Nowcast", "category": "Energy", "definition": "Very short-term forecast correction from live measurements (SCADA): the observed actual/forecast ratio is carried forward."},
    {"term": "P10 / P50 / P90", "category": "AI model", "definition": "Forecast quantiles: 10% / 50% / 90% probability that the actual value is below them. P50 is the median used for the offer."},
    {"term": "Gradient boosting (quantile)", "category": "AI model", "definition": "Tree-ensemble ML model (XGBoost family) trained to predict quantiles, used by SolarGreen."},
    {"term": "Ridge ARX", "category": "AI model", "definition": "Linear time-series regression with exogenous inputs (production plan, temperature) and lags, used by IndustriPrima."},
    {"term": "BDI agent", "category": "Agent paradigm", "definition": "Belief-Desire-Intention agent: revises beliefs, picks plans that serve its highest-priority desire, commits to intentions (IndustriPrima)."},
    {"term": "Reactive agent", "category": "Agent paradigm", "definition": "Condition-action rules without planning (PowerGrid's safety agent with veto)."},
    {"term": "MPC (model predictive control)", "category": "Agent paradigm", "definition": "Optimise over a horizon, apply the first step, measure, re-optimise (BatteryFlex)."},
    {"term": "LP (linear programming)", "category": "Algorithm", "definition": "Optimisation with a linear objective and constraints. EnergyTrade Hub clears the market with an LP (HiGHS solver)."},
    {"term": "Marginal / clearing price", "category": "Market", "definition": "Dual value of the energy balance: cost of one more kWh in that hour (uniform-price auction)."},
    {"term": "Pay-as-bid", "category": "Market", "definition": "Each seller is paid its own offer price for the energy dispatched."},
    {"term": "Lot size", "category": "Market", "definition": "Quantities are normalised to 10 kWh multiples by the hub."},
    {"term": "Contract Net Protocol (CNP)", "category": "Negotiation", "definition": "Call for proposals, then proposals or refusals, then award and reject (FIPA)."},
    {"term": "Alternating offers / Boulware", "category": "Negotiation", "definition": "Bilateral bargaining with offers and counter-offers. A Boulware tactic concedes late, a conceder concedes early."},
    {"term": "Reservation price", "category": "Negotiation", "definition": "Private worst acceptable price of a negotiator (never shared)."},
    {"term": "J(x)", "category": "Objective", "definition": "Global objective alpha*C + beta*T + gamma*R - delta*Q: cost, service disruption, risk, renewable energy."},
    {"term": "VOLL", "category": "Objective", "definition": "Value of lost load: penalty per kWh of unserved demand (Rp5,000/kWh, synthetic)."},
    {"term": "Local utility U_i", "category": "Objective", "definition": "Revenue - cost - risk penalty of enterprise i, computed privately and reported."},
    {"term": "Causer pays", "category": "Governance", "definition": "Balancing (flexibility) cost is charged to the parties whose deviation caused the deficit."},
    {"term": "Human-in-the-loop", "category": "Governance", "definition": "High-impact actions (large DR, load shedding, deep discharge) wait for an authorised human."},
    {"term": "Deliberation", "category": "Collaboration", "definition": "LLM-assisted round table: every enterprise agent assesses the situation from its own private data and proposes contributions; a facilitator synthesises a joint view. It is advisory; binding decisions stay with LP/BDI/MPC/rules."},
]


def translate(value):
    """Translate a single Bahasa term (exact match); unknown values are returned unchanged."""
    if isinstance(value, str):
        return ID_EN.get(value, value)
    return value
