"""Environment scenarios (simulation harness). Each one changes the *world* inside one or more
enterprises; the agents are not told the scenario and must perceive its effects."""
from __future__ import annotations

SCENARIOS: dict[str, dict] = {
    "normal": {
        "label": "Normal operation (Case Study 04 baseline)",
        "description": "Partly cloudy day; 4,000 kWh estate demand; solar 1,500 / grid 2,000 / battery 1,000 kWh.",
        "env": {}},
    "solar_drop_40": {
        "label": "Solar production drops 40% (cloud cover)",
        "description": "Case Study 04 adaptation scenario: a cloud bank cuts solar production 40% (SolarGreen's "
                       "nowcast); redispatch + flexible load reduction.",
        "env": {"solargreen": {"nowcast_factor": 0.6}}},
    "grid_derating": {
        "label": "Feeder FDR-07 derated to 65% (transformer maintenance)",
        "description": "PowerGrid's usable feeder capacity falls; headroom for the estate shrinks.",
        "env": {"powergrid": {"feeder_derate_factor": 0.65}}},
    "battery_fault": {
        "label": "BatteryFlex rack fault (50% capacity offline)",
        "description": "Half of the battery racks trip: energy and power are halved.",
        "env": {"batteryflex": {"availability": 0.5}}},
    "demand_surge_20": {
        "label": "Industrial demand +20% (rush order)",
        "description": "IndustriPrima's production plan rises 20% (BAB 10 lab disruption 'demand increases 20%').",
        "env": {"industriprima": {"demand_factor": 1.2}}},
    "solar_drop_urgent_order": {
        "label": "Solar -40% while IndustriPrima has an urgent order",
        "description": "Same shortfall, but IndustriPrima's BDI agent refuses DR to protect production.",
        "env": {"solargreen": {"nowcast_factor": 0.6}, "industriprima": {"urgent_order": True}}},
    "solar_cloud_outage": {
        "label": "SolarGreen cloud service unavailable",
        "description": "BAB 10 lab disruption 'cloud service unavailable': SolarGreen's API returns 503.",
        "env": {"solargreen": {"outage": True}}},
    "compound": {
        "label": "Solar -40% and feeder derated to 80%",
        "description": "Two simultaneous disruptions across two different enterprises.",
        "env": {"solargreen": {"nowcast_factor": 0.6}, "powergrid": {"feeder_derate_factor": 0.8}}},
}
