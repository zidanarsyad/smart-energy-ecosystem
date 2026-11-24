"""Synthetic data for PowerGrid's legacy EMS database (SQLite, upper-case legacy schema)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from common.config import SIM_DATE
from common.stores import SqliteStore

SCHEMA = """
DROP TABLE IF EXISTS FEEDER;
DROP TABLE IF EXISTS CONN_AGREEMENT;
DROP TABLE IF EXISTS GRID_RULE;
DROP TABLE IF EXISTS BG_LOAD_HIST;
DROP TABLE IF EXISTS GRID_SCHEDULE;
DROP TABLE IF EXISTS RULE_EVENT;
CREATE TABLE FEEDER (FEEDER_ID TEXT PRIMARY KEY, NAME TEXT, SUBSTATION TEXT,
                     THERMAL_LIMIT_KW REAL, SECURITY_MARGIN REAL);
CREATE TABLE CONN_AGREEMENT (CUST_ID TEXT PRIMARY KEY, CUST_NAME TEXT, FEEDER_ID TEXT, CONTRACT_KW REAL,
                     TARIFF_IDR_MWH REAL, EMERG_TARIFF_IDR_MWH REAL, WHOLESALE_IDR_MWH REAL, EF_KG_MWH REAL);
CREATE TABLE GRID_RULE (RULE_ID TEXT PRIMARY KEY, NAME TEXT, DESCRIPTION TEXT, SEVERITY TEXT, PARAM REAL);
CREATE TABLE BG_LOAD_HIST (TS_LOCAL TEXT, FEEDER_ID TEXT, LOAD_MWH REAL);
CREATE TABLE GRID_SCHEDULE (SCHED_ID TEXT, RECEIVED_TS TEXT, CUST_ID TEXT, TS_LOCAL TEXT, IMPORT_MWH REAL,
                     VERDICT TEXT, CONVERSATION_ID TEXT);
CREATE TABLE RULE_EVENT (EVT_TS TEXT, RULE_ID TEXT, SEVERITY TEXT, TS_LOCAL TEXT, MESSAGE TEXT, CONTEXT TEXT);
"""

# Background load of other customers on feeder FDR-07 (kW, weekday, hour 0..23).
BG_WEEKDAY_KW = [300, 290, 285, 285, 295, 320, 350, 365, 380, 420, 450, 470,
                 440, 460, 480, 450, 470, 520, 560, 550, 500, 430, 370, 330]

RULES = [
    ("R1", "CONTRACT_HEADROOM", "Import must not exceed the available contract headroom", "VIOLATION", 0.5),
    ("R2", "STRESS_ZONE", "Import above 90% of headroom raises operational risk", "WARNING", 0.90),
    ("R3", "RAMP_RATE", "Hour-to-hour import change above 150 kW", "WARNING", 150.0),
    ("R4", "FEEDER_THERMAL", "Feeder loading above 95% of usable thermal capacity", "WARNING", 0.95),
    ("R5", "EMERGENCY_IMPORT", "No non-firm emergency import if feeder utilisation forecast > 70%", "REFUSE", 0.70),
    ("R6", "NO_EXPORT", "Reverse power flow to the feeder is not permitted for this connection", "VIOLATION", -0.5),
]


def generate(out: Path, seed: int = 42) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed + 1)
    db = SqliteStore(out / "powergrid_ems.db")
    db.script(SCHEMA)
    db.execute("INSERT INTO FEEDER VALUES (?,?,?,?,?)",
               ("FDR-07", "Feeder 07 - Kawasan Industri Prima", "GI Kalasan 150/20 kV", 1000.0, 0.10))
    db.execute("INSERT INTO CONN_AGREEMENT VALUES (?,?,?,?,?,?,?,?)",
               ("IND-PRIMA-01", "IndustriPrima Estate", "FDR-07", 250.0, 1_300_000.0, 2_200_000.0,
                1_050_000.0, 870.0))
    db.executemany("INSERT INTO GRID_RULE VALUES (?,?,?,?,?)", RULES)

    rows = []
    start = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day) - timedelta(days=90)
    for d in range(90):
        day = start + timedelta(days=d)
        scale = 0.8 if day.weekday() >= 5 else 1.0
        level = 1.0 + rng.normal(0.0, 0.03)
        for h in range(24):
            kw = BG_WEEKDAY_KW[h] * scale * level * (1.0 + rng.normal(0.0, 0.035))
            ts = (day + timedelta(hours=h)).strftime("%d/%m/%Y %H:%M")
            rows.append((ts, "FDR-07", round(kw / 1000.0, 4)))
    db.executemany("INSERT INTO BG_LOAD_HIST VALUES (?,?,?)", rows)
    db.close()
    return {"bg_load_rows": len(rows)}
