"""SQLite intent journal, adapted from Wangshu's tested single-account journal."""
from dataclasses import asdict
import csv
import json
import os
from pathlib import Path
import sqlite3
import threading
import time


class ActivityGapError(RuntimeError):
    """Some broker attempts cannot be represented by complete local counts."""


class Journal:
    def __init__(self, directory: Path, binding: str):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._activity_gap = {}
        self._activity_gap_saved = False
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
                CREATE TABLE IF NOT EXISTS cancel_requests (
                    seq INTEGER PRIMARY KEY, time REAL NOT NULL,
                    day TEXT NOT NULL, order_id TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS intents_order ON intents(order_id);
                CREATE INDEX IF NOT EXISTS intents_state ON intents(state);
                CREATE INDEX IF NOT EXISTS intents_activity ON intents(day,created) WHERE state!='NOT_SENT';
                CREATE INDEX IF NOT EXISTS cancel_requests_activity ON cancel_requests(day,time);
            """)
            prior = self.get("binding")
            if prior and prior != binding:
                raise RuntimeError("数据目录属于另一个账户或环境")
            self.set("binding", binding)
            raw_gap = self.get("activity_gap")
            try:
                self._activity_gap = json.loads(raw_gap) if raw_gap else {}
                if not isinstance(self._activity_gap, dict) or any(
                        not isinstance(key, str) or (key != "*" and (len(key) != 8 or not key.isascii() or not key.isdigit()))
                        or not isinstance(reason, str) or not reason for key, reason in self._activity_gap.items()):
                    raise ValueError("invalid activity gap")
            except (TypeError, ValueError):
                self._activity_gap = {"*": "申撤单统计缺口标记无效"}
            self._activity_gap_saved = bool(self._activity_gap)
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

    def set_audited(self, key, value, kind, detail):
        encoded = json.dumps(detail, ensure_ascii=False, allow_nan=False)
        with self._lock, self.db:
            self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.db.execute("INSERT INTO audit(time,kind,detail) VALUES (?,?,?)", (time.time(), kind, encoded))

    def record_cancel(self, order_id, day):
        now = time.time()
        try:
            with self._lock, self.db:
                self.db.execute("INSERT INTO cancel_requests(time,day,order_id) VALUES (?,?,?)", (now, day, order_id))
                self.db.execute("INSERT INTO audit(time,kind,detail) VALUES (?,?,?)",
                                (now, "cancel_requested", json.dumps({"order_id": order_id, "day": day}, ensure_ascii=False)))
        except Exception:
            self.mark_activity_gap("撤单请求计数保存失败", day)
            raise

    def mark_activity_gap(self, reason, day=""):
        # Cancellation still goes to the broker; missing local counts stay unknown.
        day = day if isinstance(day, str) and len(day) == 8 and day.isascii() and day.isdigit() else "*"
        with self._lock:
            if day not in self._activity_gap:
                self._activity_gap[day] = reason
                self._activity_gap_saved = False
            if not self._activity_gap_saved:
                try:
                    self.set("activity_gap", json.dumps(self._activity_gap, ensure_ascii=False))
                    self._activity_gap_saved = True
                except Exception:
                    pass  # Keep the session flag and retry if the disk recovers.

    def activity(self, day):
        if not isinstance(day, str) or len(day) != 8 or not day.isascii() or not day.isdigit():
            raise ValueError("柜台交易日未知，无法统计当日申撤单")
        cutoff = time.time() - 60
        with self._lock:
            if self._activity_gap:
                gap_day = "*" if "*" in self._activity_gap else next(iter(self._activity_gap))
                self.mark_activity_gap(self._activity_gap[gap_day], gap_day)
                reason = self._activity_gap.get("*") or self._activity_gap.get(day)
                if reason:
                    raise ActivityGapError("申撤单统计存在缺口：" + reason)
            orders_today = self.db.execute("SELECT COUNT(*) FROM intents WHERE day=? AND state!='NOT_SENT'", (day,)).fetchone()[0]
            orders_minute = self.db.execute("SELECT COUNT(*) FROM intents WHERE day=? AND created>=? AND state!='NOT_SENT'", (day, cutoff)).fetchone()[0]
            cancels_today = self.db.execute("SELECT COUNT(*) FROM cancel_requests WHERE day=?", (day,)).fetchone()[0]
            cancels_minute = self.db.execute("SELECT COUNT(*) FROM cancel_requests WHERE day=? AND time>=?", (day, cutoff)).fetchone()[0]
        return {"orders_last_minute": orders_minute, "cancels_last_minute": cancels_minute,
                "orders_today": orders_today, "cancels_today": cancels_today}

    def list_audit(self, limit=300, before_seq=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("日志每页条数必须为1至1000")
        if before_seq is not None and (type(before_seq) is not int or before_seq < 1):
            raise ValueError("日志游标无效")
        with self._lock:
            rows = self.db.execute("SELECT * FROM audit WHERE seq<? ORDER BY seq DESC LIMIT ?", (before_seq, limit)) if before_seq is not None else self.db.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
            return [dict(row, detail=json.loads(row["detail"])) for row in rows]

    def export_audit(self, destination: Path):
        destination = Path(destination).resolve()
        database = self.directory / "journal.db"
        if destination in {database, Path(str(database) + "-wal"), Path(str(database) + "-shm"), self.directory / "kernel.lock"}:
            raise ValueError("不能覆盖交易记录数据库或锁文件")

        def cell(value):
            value = str(value)
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value

        # A separate read-only WAL snapshot does not hold the trading connection's lock.
        reader = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
        created = False
        try:
            reader.execute("PRAGMA query_only=ON")
            reader.execute("BEGIN")
            rows = reader.execute("SELECT seq,time,kind,detail FROM audit ORDER BY seq")
            with destination.open("x", encoding="utf-8-sig", newline="") as output:
                created = True
                writer = csv.writer(output)
                writer.writerow(("seq", "time", "kind", "detail"))
                count = 0
                for row in rows:
                    writer.writerow([cell(value) for value in row])
                    count += 1
            return count
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise
        finally:
            reader.close()

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
