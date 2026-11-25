"""Start the whole ecosystem: 5 enterprise servers + orchestrator (+ Streamlit control center).

    python scripts/run_all.py              # everything, incl. the control center on :8501
    python scripts/run_all.py --no-ui      # only the API servers
    python scripts/run_all.py --regenerate # regenerate synthetic data first

Each enterprise runs in its own process on its own port (independent failure domain).
Stop everything with Ctrl+C.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.config import (DATA_DIR, ECOSYSTEM_NAME, ECOSYSTEM_TAGLINE, ENTERPRISE_NAMES,  # noqa: E402
                           ENTERPRISE_PORTS, HOST, ORCHESTRATOR_PORT, UI_PORT, browser_url)

SERVICES = [
    ("energytrade", "enterprises.energytrade.app:app"),
    ("solargreen", "enterprises.solargreen.app:app"),
    ("powergrid", "enterprises.powergrid.app:app"),
    ("industriprima", "enterprises.industriprima.app:app"),
    ("batteryflex", "enterprises.batteryflex.app:app"),
]


def _port_in_use(port: int) -> bool:
    """Bind HOST:port the way uvicorn and Streamlit will; if that fails, another process holds the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":  # as uvicorn/tornado do on Unix: ignores TIME_WAIT, still fails on a live server
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
        except OSError:
            return True
    return False


def _wait_healthy(name: str, port: int, proc: subprocess.Popen, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited with code {proc.returncode} (is port {port} already in use?)")
        try:
            if httpx.get(f"http://{HOST}:{port}/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"{name} did not become healthy on port {port}")


def _start(target: str, port: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "uvicorn", target, "--host", HOST, "--port", str(port),
                             "--log-level", "warning"], cwd=ROOT, env=env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ui", action="store_true", help="do not start the Streamlit control center")
    parser.add_argument("--regenerate", action="store_true", help="regenerate all synthetic data first")
    args = parser.parse_args()

    # Check the ports first. A second copy would otherwise regenerate the data under the running one, and its
    # health checks would pass against the old servers before its own servers fail to bind.
    wanted = [(ENTERPRISE_NAMES[name], ENTERPRISE_PORTS[name]) for name, _ in SERVICES]
    wanted.append(("Ecosystem Orchestrator", ORCHESTRATOR_PORT))
    if not args.no_ui:
        wanted.append(("Global Control Center", UI_PORT))
    busy = [f"  {port}  {label}" for label, port in sorted(wanted, key=lambda w: w[1]) if _port_in_use(port)]
    if busy:
        sys.exit(f"Cannot start {ECOSYSTEM_NAME}: these ports are already in use:\n" + "\n".join(busy) + "\n"
                 "An earlier run_all is probably still running. Stop it with Ctrl+C in its terminal,\n"
                 "or end the leftover python processes, then run this again.")

    if args.regenerate or not (DATA_DIR / "generation_summary.json").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_data.py")], check=True, cwd=ROOT)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    procs: list[tuple[str, int, subprocess.Popen]] = []
    print(f"\n{ECOSYSTEM_NAME} - {ECOSYSTEM_TAGLINE}\nStarting enterprise servers ...", flush=True)
    try:
        for name, target in SERVICES:
            procs.append((name, ENTERPRISE_PORTS[name], _start(target, ENTERPRISE_PORTS[name], env)))
        for name, port, proc in procs:
            _wait_healthy(name, port, proc)
            print(f"  [ok] {ENTERPRISE_NAMES[name]:24s} {browser_url(port)}", flush=True)
        orch = _start("orchestrator.app:app", ORCHESTRATOR_PORT, env)
        procs.append(("orchestrator", ORCHESTRATOR_PORT, orch))
        _wait_healthy("orchestrator", ORCHESTRATOR_PORT, orch)
        print(f"  [ok] {'Ecosystem Orchestrator':24s} {browser_url(ORCHESTRATOR_PORT)}/docs", flush=True)
        if not args.no_ui:
            ui = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(ROOT / "frontend" / "control_center.py"),
                                   "--server.port", str(UI_PORT), "--server.address", HOST,
                                   "--server.headless", "true", "--browser.gatherUsageStats", "false"],
                                  cwd=ROOT, env=env)
            procs.append(("control_center", UI_PORT, ui))
            print(f"  [..] {'Global Control Center':24s} {browser_url(UI_PORT)}", flush=True)
        print(f"\n{ECOSYSTEM_NAME} ecosystem running. Press Ctrl+C to stop all servers.", flush=True)
        while True:
            for name, port, proc in procs:
                if proc.poll() is not None:
                    raise RuntimeError(f"{name} (port {port}) stopped with code {proc.returncode}")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping ...")
    finally:
        for _, _, proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for _, _, proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
