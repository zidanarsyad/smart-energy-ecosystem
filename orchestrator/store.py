"""Orchestrator persistence (its own SQLite database): runs, A2A-lite messages, cache."""
from __future__ import annotations

import json
from pathlib import Path

from common.a2a import A2AMessage
from common.stores import SqliteStore
from common.timeutil import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, created_at TEXT, scenario TEXT, mode TEXT,
    onset_slot INTEGER, status TEXT, cost_rp REAL, j_value REAL, summary_json TEXT, result_json TEXT);
CREATE TABLE IF NOT EXISTS messages (message_id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, ts TEXT,
    sender TEXT, receiver TEXT, via TEXT, message_type TEXT, protocol TEXT, interaction TEXT, task TEXT,
    payload_json TEXT, native_json TEXT, in_reply_to TEXT, reply_required INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS cache (enterprise TEXT, kind TEXT, ts TEXT, run_id TEXT, json TEXT,
    PRIMARY KEY (enterprise, kind));
CREATE TABLE IF NOT EXISTS environment_log (ts TEXT, scenario TEXT, onset_slot INTEGER, applied_json TEXT);
"""


class OrchestratorStore:
    def __init__(self, path: Path):
        self.db = SqliteStore(path)
        self.db.script(SCHEMA)

    # ---- runs
    def next_run_id(self) -> str:
        n = self.db.query("SELECT COUNT(*) AS n FROM runs")[0]["n"]
        return f"RUN-{n + 1:04d}"

    def save_run(self, result: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (result["run_id"], result["created_at"], result["scenario"]["id"], result["mode"],
                         result["onset_slot"], result["status"], result.get("kpis", {}).get("total_cost_rp"),
                         result.get("kpis", {}).get("J"), json.dumps(result.get("summary", {}), default=str),
                         json.dumps(result, default=str)))

    def get_run(self, run_id: str) -> dict | None:
        rows = self.db.query("SELECT result_json FROM runs WHERE run_id=?", (run_id,))
        return json.loads(rows[0]["result_json"]) if rows else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT run_id, created_at, scenario, mode, onset_slot, status, cost_rp, j_value, "
                             "summary_json FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?", (limit,))
        for r in rows:
            r["summary"] = json.loads(r.pop("summary_json") or "{}")
        return rows

    def latest_run(self, scenario: str | None = None, mode: str | None = None,
                   ok_only: bool = True) -> dict | None:
        sql = "SELECT result_json FROM runs WHERE 1=1"
        params: list = []
        if scenario:
            sql += " AND scenario=?"
            params.append(scenario)
        if mode:
            sql += " AND mode=?"
            params.append(mode)
        if ok_only:
            sql += " AND status NOT IN ('FAILED', 'BLOCKED')"
        rows = self.db.query(sql + " ORDER BY created_at DESC, run_id DESC LIMIT 1", params)
        return json.loads(rows[0]["result_json"]) if rows else None

    # ---- messages
    def save_message(self, m: A2AMessage) -> None:
        self.db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (m.message_id, m.conversation_id, m.seq, m.timestamp, m.sender, m.receiver, m.via,
                         m.message_type, m.protocol, m.interaction, m.task, json.dumps(m.payload, default=str),
                         json.dumps(m.native, default=str) if m.native else None, m.in_reply_to,
                         int(m.reply_required), m.status))

    def messages(self, run_id: str | None = None, limit: int = 1000) -> list[dict]:
        if run_id:
            rows = self.db.query("SELECT * FROM messages WHERE run_id=? ORDER BY seq LIMIT ?", (run_id, limit))
        else:
            rows = self.db.query("SELECT * FROM messages ORDER BY ts DESC, seq DESC LIMIT ?", (limit,))
        for r in rows:
            r["payload"] = json.loads(r.pop("payload_json") or "{}")
            r["native"] = json.loads(r.pop("native_json")) if r.get("native_json") else None
            r["reply_required"] = bool(r["reply_required"])
        return rows

    # ---- cache of last good canonical perception (fallback when an enterprise is down)
    def cache_put(self, enterprise: str, kind: str, run_id: str, obj: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                        (enterprise, kind, now_iso(), run_id, json.dumps(obj, default=str)))

    def cache_get(self, enterprise: str, kind: str) -> dict | None:
        rows = self.db.query("SELECT * FROM cache WHERE enterprise=? AND kind=?", (enterprise, kind))
        if not rows:
            return None
        return {"ts": rows[0]["ts"], "run_id": rows[0]["run_id"], "data": json.loads(rows[0]["json"])}

    def log_environment(self, scenario: str, onset: int, applied: dict) -> None:
        self.db.execute("INSERT INTO environment_log VALUES (?,?,?,?)",
                        (now_iso(), scenario, onset, json.dumps(applied, default=str)))
