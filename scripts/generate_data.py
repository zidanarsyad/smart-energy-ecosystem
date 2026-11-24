"""Generate the synthetic datasets of all five enterprises (each into its own data folder).

Each enterprise owns its data generator (enterprises/<id>/datagen.py) and writes only into
data/<id>/. Running this script resets every enterprise database, audit log and the
orchestrator's run history.

    python scripts/generate_data.py
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.config import DATA_DIR, RANDOM_SEED, data_dir  # noqa: E402
from enterprises.batteryflex import datagen as batteryflex  # noqa: E402
from enterprises.energytrade import datagen as energytrade  # noqa: E402
from enterprises.industriprima import datagen as industriprima  # noqa: E402
from enterprises.powergrid import datagen as powergrid  # noqa: E402
from enterprises.solargreen import datagen as solargreen  # noqa: E402

GENERATORS = [("solargreen", solargreen), ("powergrid", powergrid), ("industriprima", industriprima),
              ("energytrade", energytrade), ("batteryflex", batteryflex)]


def main() -> dict:
    t0 = time.time()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary = {}
    for eid, module in GENERATORS:
        out = data_dir(eid)
        (out / "audit.jsonl").unlink(missing_ok=True)
        print(f"[{eid:13s}] generating synthetic data ...", flush=True)
        summary[eid] = module.generate(out, seed=RANDOM_SEED)
    shutil.rmtree(DATA_DIR / "orchestrator", ignore_errors=True)
    summary["seconds"] = round(time.time() - t0, 1)
    (DATA_DIR / "generation_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    main()
