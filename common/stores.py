"""Small storage engines. Each enterprise uses a *different* one (database heterogeneity):

* SolarGreen     -> CSV time-series data lake (pandas, used directly in its package)
* PowerGrid      -> SQLite relational EMS database (legacy upper-case schema)
* IndustriPrima  -> JSON document store (NoSQL-style collections, Bahasa Indonesia fields)
* EnergyTrade    -> SQLite order book / ledger (modern snake_case schema)
* BatteryFlex    -> JSON Lines append-only telemetry store
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from common.timeutil import now_iso


class AuditLog:
    """Append-only JSONL audit trail kept inside each enterprise's own data folder."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, actor: str, event: str, detail: Any = None, level: str = "INFO") -> dict:
        rec = {"ts": now_iso(), "actor": actor, "event": event, "level": level, "detail": detail}
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def tail(self, n: int = 100) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in reversed(lines[-n:]):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


class JsonDocStore:
    """Tiny MongoDB-like document store persisted as one JSON file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, list[dict]] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def collections(self) -> list[str]:
        return list(self._data)

    def all(self, name: str) -> list[dict]:
        with self._lock:
            return list(self._data.get(name, []))

    def find(self, name: str, **filters: Any) -> list[dict]:
        return [d for d in self.all(name) if all(d.get(k) == v for k, v in filters.items())]

    def find_one(self, name: str, **filters: Any) -> dict | None:
        found = self.find(name, **filters)
        return found[0] if found else None

    def insert(self, name: str, doc: dict, persist: bool = True) -> dict:
        with self._lock:
            doc = dict(doc)
            doc.setdefault("_id", uuid.uuid4().hex[:12])
            self._data.setdefault(name, []).append(doc)
            if persist:
                self.save()
            return doc

    def insert_many(self, name: str, docs: list[dict]) -> None:
        with self._lock:
            for d in docs:
                self.insert(name, d, persist=False)
            self.save()

    def update(self, name: str, _id: str, changes: dict) -> dict | None:
        with self._lock:
            for d in self._data.get(name, []):
                if d.get("_id") == _id:
                    d.update(changes)
                    self.save()
                    return d
        return None

    def replace_collection(self, name: str, docs: list[dict]) -> None:
        with self._lock:
            self._data[name] = []
            self.insert_many(name, docs)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)


class JsonlStore:
    """Append-only JSON Lines store (time-series telemetry)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, rec: dict) -> dict:
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock:
            return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def tail(self, n: int = 50) -> list[dict]:
        return list(reversed(self.read()[-n:]))


class SqliteStore:
    """Thread-safe sqlite3 wrapper returning rows as dicts."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def script(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)
            self._conn.commit()

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid

    def executemany(self, sql: str, rows: list) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def tables(self) -> list[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [r["name"] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
