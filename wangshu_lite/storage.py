"""SQLite intent journal, adapted from Wangshu's tested single-account journal."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import threading
import time


class Journal:
    def __init__(self, directory: Path, binding: str):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._file = (self.directory / "kernel.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._file.write(b"0")
                self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            raise RuntimeError("该账户的数据目录已有交易内核运行") from None
        try:
            self.db = sqlite3.connect(self.directory / "journal.db", check_same_thread=False, timeout=1)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents (
                    client_id TEXT PRIMARY KEY, day TEXT NOT NULL, created REAL NOT NULL,
                    request TEXT NOT NULL, state TEXT NOT NULL, order_id TEXT,
                    source TEXT NOT NULL DEFAULT 'manual');
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY, time REAL NOT NULL,
                    kind TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS intents_order ON intents(order_id);
                CREATE INDEX IF NOT EXISTS intents_state ON intents(state);
            """)
            prior = self.get("binding")
            if prior and prior != binding:
                raise RuntimeError("数据目录属于另一个账户或环境")
            self.set("binding", binding)
        except Exception:
            self.close()
            raise

    def get(self, key):
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else ""

    def set(self, key, value):
        with self._lock, self.db:
            self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def audit(self, kind, detail):
        with self._lock, self.db:
            self.db.execute("INSERT INTO audit(time,kind,detail) VALUES (?,?,?)",
                            (time.time(), kind, json.dumps(detail, ensure_ascii=False, allow_nan=False)))

    def reserve(self, intent, day, source="manual"):
        encoded = json.dumps(asdict(intent), ensure_ascii=False, allow_nan=False)
        with self._lock, self.db:
            self.db.execute("INSERT INTO intents VALUES (?,?,?,?,?,NULL,?)",
                            (intent.client_id, day, time.time(), encoded, "RESERVED", source))
            self.db.execute("INSERT INTO audit(time,kind,detail) VALUES (?,?,?)",
                            (time.time(), "send_intent", json.dumps({"source": source, "request": asdict(intent)}, allow_nan=False)))

    def bind(self, client_id, order_id):
        with self._lock, self.db:
            existing = self.db.execute("SELECT client_id FROM intents WHERE order_id=?", (order_id,)).fetchone()
            if existing and existing[0] != client_id:
                raise ValueError("柜台委托已绑定其他请求")
            self.db.execute("UPDATE intents SET order_id=?,state='SENT' WHERE client_id=?", (order_id, client_id))

    def unknown(self, client_id):
        self._state(client_id, "UNKNOWN")

    def not_sent(self, client_id):
        self._state(client_id, "NOT_SENT")

    def _state(self, client_id, state):
        with self._lock, self.db:
            self.db.execute("UPDATE intents SET state=? WHERE client_id=?", (state, client_id))

    def terminal(self, order_id):
        with self._lock, self.db:
            self.db.execute("UPDATE intents SET state='TERMINAL' WHERE order_id=?", (order_id,))

    def exists(self, client_id):
        with self._lock:
            return self.db.execute("SELECT 1 FROM intents WHERE client_id=?", (client_id,)).fetchone() is not None

    def intents(self, limit=500, *, pending=False):
        with self._lock:
            if pending:
                rows = self.db.execute("SELECT * FROM intents WHERE state IN ('RESERVED','UNKNOWN','SENT') ORDER BY created")
            else:
                rows = self.db.execute("SELECT * FROM intents ORDER BY created DESC LIMIT ?", (limit,))
            return [dict(row) for row in rows]

    def order_sources(self):
        with self._lock:
            return {row[0]: row[1] for row in self.db.execute("SELECT order_id,source FROM intents WHERE order_id IS NOT NULL")}

    def close(self):
        if self._closed:
            return
        self._closed = True
        if hasattr(self, "db"):
            self.db.close()
        if not self._file.closed:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            self._file.close()
