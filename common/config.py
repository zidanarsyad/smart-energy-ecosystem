"""Global configuration of Shakti Tantra, the heterogeneous multi-enterprise energy ecosystem (MVP).

Every enterprise runs as its own server process with its own data folder. The only
thing they share is this bootstrap configuration (ports + simulation window), which plays
the role of the consortium's endpoint directory.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

# Ecosystem identity. Sanskrit: shakti (energy, power) + tantra (system, framework) = "sistem energi".
ECOSYSTEM_NAME = "Shakti Tantra"
ECOSYSTEM_TAGLINE = "Sistem Energi: Smart Energy Trading & Distribution Ecosystem (Case Study 04)"
ECOSYSTEM_MEANING = "Sanskrit shakti (energy) + tantra (system) = sistem energi"

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("SEE_DATA_DIR", str(ROOT / "data")))
FRONTEND_DIR = ROOT / "frontend"
BRAND_DIR = FRONTEND_DIR / "brand"  # logo "the woven current": mark, lockups, favicon (SVG)

HOST = os.environ.get("SEE_HOST", "127.0.0.1")
PORT_BASE = int(os.environ.get("SEE_PORT_BASE", "8200"))
UI_PORT = int(os.environ.get("SEE_UI_PORT", "8501"))

# Case Study 04 simulation period: one 8-hour operating window of the industrial estate.
SIM_DATE = date(2026, 11, 18)
WINDOW_START_HOUR = 8
WINDOW_HOURS = 8
LOT_KWH = 10.0  # EnergyTrade Hub market rule: quantities are normalised to 10 kWh lots
RANDOM_SEED = 42

ORCHESTRATOR_PORT = PORT_BASE
ENTERPRISE_PORTS = {
    "solargreen": PORT_BASE + 1,
    "powergrid": PORT_BASE + 2,
    "industriprima": PORT_BASE + 3,
    "energytrade": PORT_BASE + 4,
    "batteryflex": PORT_BASE + 5,
}
ENTERPRISE_ORDER = list(ENTERPRISE_PORTS)

ENTERPRISE_NAMES = {
    "solargreen": "SolarGreen Energy",
    "powergrid": "PowerGrid Distribution",
    "industriprima": "IndustriPrima",
    "energytrade": "EnergyTrade Hub",
    "batteryflex": "BatteryFlex Storage",
}


def enterprise_url(eid: str) -> str:
    return f"http://{HOST}:{ENTERPRISE_PORTS[eid]}"


def orchestrator_url() -> str:
    return f"http://{HOST}:{ORCHESTRATOR_PORT}"


def browser_url(port: int) -> str:
    return f"http://localhost:{port}"


def data_dir(eid: str) -> Path:
    path = DATA_DIR / eid
    path.mkdir(parents=True, exist_ok=True)
    return path
