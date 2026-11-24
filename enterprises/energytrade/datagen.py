"""Synthetic data for EnergyTrade Hub's order-book database (SQLite, snake_case schema)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from common.config import SIM_DATE
from common.stores import SqliteStore

SCHEMA = """
DROP TABLE IF EXISTS participants;
DROP TABLE IF EXISTS market_rules;
DROP TABLE IF EXISTS price_history;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS clearing_results;
DROP TABLE IF EXISTS trades;
DROP TABLE IF EXISTS settlements;
CREATE TABLE participants (participant_id TEXT PRIMARY KEY, name TEXT, role TEXT, platform TEXT, joined_at TEXT);
CREATE TABLE market_rules (rule_id TEXT PRIMARY KEY, description TEXT, value REAL, unit TEXT);
CREATE TABLE price_history (interval_start TEXT PRIMARY KEY, clearing_price_idr_kwh REAL, volume_kwh REAL);
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, created_at TEXT, window_start TEXT, hours INTEGER,
                       status TEXT, weights_json TEXT);
CREATE TABLE orders (order_id TEXT, session_id TEXT, participant_id TEXT, side TEXT, resource TEXT,
                     price_idr_kwh REAL, quantity_json TEXT, total_kwh REAL, energy_limit_kwh REAL, status TEXT);
CREATE TABLE clearing_results (session_id TEXT PRIMARY KEY, created_at TEXT, status TEXT, cost_idr REAL,
                     objective_json TEXT, allocations_json TEXT, prices_json TEXT, unserved_json TEXT);
CREATE TABLE trades (trade_id TEXT PRIMARY KEY, session_id TEXT, seller TEXT, buyer TEXT, resource TEXT,
                     interval_start TEXT, quantity_kwh REAL, price_idr_kwh REAL, amount_idr REAL);
CREATE TABLE settlements (settlement_id TEXT PRIMARY KEY, session_id TEXT, participant_id TEXT, receivable_idr REAL,
                     payable_idr REAL, fee_idr REAL, net_idr REAL, status TEXT, created_at TEXT);
"""

PARTICIPANTS = [
    ("solargreen", "SolarGreen Energy", "seller (solar)", "Google Cloud"),
    ("powergrid", "PowerGrid Distribution", "seller (grid) / balancing account", "On-premises"),
    ("batteryflex", "BatteryFlex Storage", "seller (storage)", "Private cloud / edge"),
    ("industriprima", "IndustriPrima", "buyer + DR provider", "Microsoft Azure"),
]

RULES = [
    ("PRICE_CAP", "Maximum offer price accepted by the hub", 2000.0, "IDR/kWh"),
    ("LOT_SIZE", "Quantities normalised to lots (largest-remainder, total preserving)", 10.0, "kWh"),
    ("HUB_FEE", "Hub fee charged to sellers per traded kWh", 10.0, "IDR/kWh"),
    ("GATE_CLOSURE", "Orders must arrive before gate closure (informational in the MVP)", 60.0, "minutes"),
    ("SESSION_COST", "Hub operating cost per clearing session", 5000.0, "IDR"),
]

PRICE_SHAPE = [950, 940, 930, 930, 940, 980, 1050, 1120, 1180, 1220, 1260, 1280,
               1250, 1270, 1290, 1270, 1330, 1420, 1480, 1470, 1400, 1250, 1100, 1000]


def generate(out: Path, seed: int = 42) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed + 3)
    db = SqliteStore(out / "energytrade_market.db")
    db.script(SCHEMA)
    joined = datetime(2026, 1, 15).isoformat()
    db.executemany("INSERT INTO participants VALUES (?,?,?,?,?)", [(*p, joined) for p in PARTICIPANTS])
    db.executemany("INSERT INTO market_rules VALUES (?,?,?,?)", RULES)
    rows = []
    start = datetime(SIM_DATE.year, SIM_DATE.month, SIM_DATE.day) - timedelta(days=90)
    for d in range(90):
        day = start + timedelta(days=d)
        level = rng.normal(0.0, 25.0)
        for h in range(24):
            ts = (day + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00+07:00")
            price = PRICE_SHAPE[h] + level + rng.normal(0.0, 30.0)
            rows.append((ts, round(price, 1), round(float(rng.uniform(1500, 4200)) / 8, 1)))
    db.executemany("INSERT INTO price_history VALUES (?,?,?)", rows)
    db.close()
    return {"price_history_rows": len(rows)}
