"""Persistent activity and audit regressions, using only temporary accounts."""
import csv
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from wangshu_lite.core import TradingError, TradingKernel
from wangshu_lite.models import Connection, Contract, GatewayPreflightError, OrderIntent
from wangshu_lite.storage import Journal


DAY = "20990102"  # Intentionally unrelated to the machine's calendar date.
SYMBOL = "rb2610.SHFE"


def intent(client_id, **changes):
    return replace(OrderIntent(client_id, SYMBOL, "BUY", "OPEN", 1, 3500), **changes)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "account"
        self.journal = Journal(self.directory, "test-binding")
        self.addCleanup(lambda: self.journal.close())

    def test_activity_uses_intents_and_attempts_and_survives_restart(self):
        journal = self.journal
        with patch("wangshu_lite.storage.time.time", return_value=1900):
            journal.reserve(intent("old"), DAY)
            journal.record_cancel("old-order", DAY)
        with patch("wangshu_lite.storage.time.time", return_value=2000):
            journal.reserve(intent("sent"), DAY)
            journal.bind("sent", "PAPER.1")
            journal.terminal("PAPER.1")
            journal.terminal("PAPER.1")
            journal.reserve(intent("unknown"), DAY)
            journal.unknown("unknown")
            journal.reserve(intent("reserved"), DAY)
            journal.reserve(intent("not-sent"), DAY)
            journal.not_sent("not-sent")
            journal.reserve(intent("another-day"), "20990103")
            journal.record_cancel("PAPER.1", DAY)
            journal.record_cancel("another-day", "20990103")
            expected = {"orders_last_minute": 3, "cancels_last_minute": 1,
                        "orders_today": 4, "cancels_today": 2}
            self.assertEqual(journal.activity(DAY), expected)
            journal.close()
            self.journal = Journal(self.directory, "test-binding")
            self.assertEqual(self.journal.activity(DAY), expected)
            for day in ("", "2026-10-22", None):
                with self.assertRaises(ValueError):
                    self.journal.activity(day)

    def test_minute_queries_have_day_time_indexes(self):
        orders = self.journal.db.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM intents WHERE day=? AND created>=? AND state!='NOT_SENT'", (DAY, 100)).fetchall()
        cancels = self.journal.db.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM cancel_requests WHERE day=? AND time>=?", (DAY, 100)).fetchall()
        self.assertIn("intents_activity", str([tuple(row) for row in orders]))
        self.assertIn("cancel_requests_activity", str([tuple(row) for row in cancels]))

    def test_cancel_and_config_audits_are_atomic(self):
        self.journal.db.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT, 'disk fault'); END")
        self.journal.db.commit()
        with self.assertRaises(Exception):
            self.journal.record_cancel("PAPER.1", DAY)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM cancel_requests").fetchone()[0], 0)
        with self.assertRaisesRegex(RuntimeError, "统计存在缺口"):
            self.journal.activity(DAY)
        with self.assertRaises(Exception):
            self.journal.set_audited("limits", "{}", "activity_limits", {})
        self.assertEqual(self.journal.get("limits"), "")
        self.journal.db.execute("DROP TRIGGER reject_audit")
        self.journal.db.commit()
        self.journal.set_audited("limits", "{}", "activity_limits", {"enabled": False})
        self.assertEqual(self.journal.list_audit()[0]["detail"], {"enabled": False})

    def test_activity_gap_remains_unknown_and_persists_after_disk_recovers(self):
        with patch.object(self.journal, "set", side_effect=OSError("disk full")):
            self.journal.mark_activity_gap("撤单请求计数保存失败", DAY)
            with self.assertRaisesRegex(RuntimeError, "统计存在缺口"):
                self.journal.activity(DAY)
        with self.assertRaisesRegex(RuntimeError, "统计存在缺口"):
            self.journal.activity(DAY)
        self.assertTrue(self.journal.get("activity_gap"))
        self.journal.close()
        self.journal = Journal(self.directory, "test-binding")
        with self.assertRaisesRegex(RuntimeError, "统计存在缺口"):
            self.journal.activity(DAY)
        self.assertEqual(self.journal.activity("20990103")["cancels_today"], 0)

    def test_recent_log_pagination_and_full_safe_csv(self):
        for number in range(1005):
            self.journal.audit("event", {"number": number, "中文": "保留"})
        self.journal.audit(" =HYPERLINK(\"bad\")", {"text": "=1+1"})
        first = self.journal.list_audit(limit=300)
        second = self.journal.list_audit(limit=300, before_seq=first[-1]["seq"])
        self.assertEqual(len(first), 300)
        self.assertLess(second[0]["seq"], first[-1]["seq"])
        self.assertIsInstance(first[0]["detail"], dict)
        for limit in (0, 1001, True):
            with self.assertRaises(ValueError):
                self.journal.list_audit(limit)
        target = Path(self.tmp.name) / "日志.csv"
        self.assertEqual(self.journal.export_audit(target), 1006)
        self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))
        with target.open(encoding="utf-8-sig", newline="") as exported:
            rows = list(csv.DictReader(exported))
        self.assertEqual(len(rows), 1006)
        self.assertEqual(json.loads(rows[0]["detail"])["中文"], "保留")
        self.assertTrue(rows[-1]["kind"].startswith("' ="))
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            self.journal.export_audit(target)
        self.assertEqual(target.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.journal.export_audit(self.directory / "journal.db")

    def test_export_snapshot_does_not_hold_trading_lock(self):
        self.journal.audit("before", {})
        ready, release = threading.Event(), threading.Event()
        original_writer = csv.writer

        def slow_writer(output):
            writer = original_writer(output)
            class SlowWriter:
                def writerow(self, row):
                    if row[0] == "seq":
                        ready.set()
                        if not release.wait(5):
                            raise TimeoutError("test export did not resume")
                    writer.writerow(row)
            return SlowWriter()

        result = []
        target = Path(self.tmp.name) / "snapshot.csv"
        with patch("wangshu_lite.storage.csv.writer", side_effect=slow_writer):
            worker = threading.Thread(target=lambda: result.append(self.journal.export_audit(target)))
            worker.start()
            self.assertTrue(ready.wait(5))
            try:
                self.journal.audit("during-export", {})
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [1])
        self.assertEqual(len(self.journal.list_audit()), 2)


class KernelAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name), "test-binding")
        self.addCleanup(self.journal.close)
        self.output = []
        self.kernel = TradingKernel(self.journal, self.output.append, secrets=("test-password",))
        self.gateway = Mock()
        self.connection = Connection(True, True, DAY, trader_connected=True)
        self.gateway.health.return_value = self.connection
        self.gateway.send_order.side_effect = lambda req, _: "PAPER." + req.client_id
        self.kernel.attach(self.gateway)
        self.kernel.on_event("connection", self.connection)
        self.kernel.on_event("contract", Contract(SYMBOL, 10, 1, max_volume=10))
        self.kernel.enable()

    def test_rejections_and_source_are_audited_without_counting_local_refusal(self):
        for changes, reason in (({"volume": 11}, "最大单笔数量 10"), ({"price": 3500.5}, "最小变动价位 1"), ({"vt_symbol": "missing.SHFE"}, "missing.SHFE")):
            with self.assertRaisesRegex(TradingError, reason):
                self.kernel.send_order(intent("rejected", **changes), source="cta:test")
        self.gateway.send_order.assert_not_called()
        self.assertEqual(self.journal.activity(DAY)["orders_today"], 0)
        oid = self.kernel.send_order(intent("accepted"), source="cta:test")
        with self.assertRaisesRegex(TradingError, "请求编号已使用"):
            self.kernel.send_order(intent("accepted"), source="cta:test")
        self.kernel.on_event("order", self.kernel.orders[oid])
        self.kernel.on_event("order", self.kernel.orders[oid])
        self.assertEqual(self.journal.activity(DAY)["orders_today"], 1)
        events = self.journal.list_audit()
        self.assertEqual(sum(row["kind"] == "order_rejected" for row in events), 4)
        sent = next(row for row in events if row["kind"] == "order_sent")
        self.assertEqual(sent["detail"], {"source": "cta:test", "client_id": "accepted", "order_id": oid})

    def test_preflight_and_unknown_have_distinct_persistent_counts_and_reasons(self):
        self.gateway.send_order.side_effect = GatewayPreflightError("test-password not ready")
        with self.assertRaises(TradingError):
            self.kernel.send_order(intent("not-sent"))
        self.assertEqual(self.journal.intents()[0]["state"], "NOT_SENT")
        self.assertEqual(self.journal.activity(DAY)["orders_today"], 0)
        self.gateway.send_order.side_effect = TimeoutError("test-password lost acknowledgement")
        with self.assertRaises(TradingError):
            self.kernel.send_order(intent("unknown"))
        self.assertEqual(self.journal.intents()[0]["state"], "UNKNOWN")
        self.assertEqual(self.journal.activity(DAY)["orders_today"], 1)
        logs = self.journal.list_audit()
        self.assertIn("order_unknown", [row["kind"] for row in logs])
        self.assertNotIn("test-password", json.dumps(logs))
        self.assertIn("NOT_SENT", json.dumps(logs))

    def test_connection_log_and_error_are_persistent_and_redacted(self):
        self.kernel.on_event("connection", replace(self.connection, reason="test-password connected"))
        self.kernel.on_event("log", "test-password normal log")
        self.kernel.on_event("error", "test-password broker error")
        rows = self.journal.list_audit()
        self.assertTrue({"connection", "log", "error"}.issubset({row["kind"] for row in rows}))
        self.assertNotIn("test-password", json.dumps(rows) + str(self.output))
        self.assertIn("[已隐藏]", str(self.output))

    def test_cancel_attempts_include_failures_without_faking_success(self):
        one = self.kernel.send_order(intent("one"))
        two = self.kernel.send_order(intent("two"))
        self.gateway.cancel_order.side_effect = [RuntimeError("test-password failure"), None]
        result = self.kernel.cancel_orders([one, two, two])
        self.assertEqual(result[one], "[已隐藏] failure")
        self.assertEqual(result[two], "已请求，等待柜台回报")
        self.assertEqual(self.gateway.cancel_order.call_count, 2)
        self.assertTrue(all(order.active for order in self.kernel.orders.values()))
        self.assertEqual(self.journal.activity(DAY)["cancels_today"], 2)
        self.assertIn("cancel_failed", [row["kind"] for row in self.journal.list_audit()])
        with self.assertRaises(TradingError):
            self.kernel.cancel("missing")
        self.assertEqual(self.journal.activity(DAY)["cancels_today"], 2)

    def test_disk_failure_disables_new_orders_but_still_calls_broker_cancel(self):
        oid = self.kernel.send_order(intent("one"))
        with patch.object(self.journal, "record_cancel", side_effect=OSError("disk full")):
            self.kernel.cancel(oid)
        self.gateway.cancel_order.assert_called_once()
        self.assertFalse(self.kernel.enabled)
        self.assertTrue(self.kernel.fatal)
        self.assertTrue(self.kernel.orders[oid].active)
        with self.assertRaisesRegex(RuntimeError, "统计存在缺口"):
            self.journal.activity(DAY)

    def test_cancel_uses_gateway_day_and_marks_stale_or_missing_day_unknown(self):
        oid = self.kernel.send_order(intent("one"))
        self.gateway.health.return_value = replace(self.connection, trading_day="20990103")
        self.kernel.cancel(oid)
        self.assertEqual(self.journal.db.execute("SELECT day FROM cancel_requests").fetchone()[0], "20990103")
        with self.assertRaisesRegex(RuntimeError, "统计存在缺口"):
            self.journal.activity(DAY)
        self.gateway.health.side_effect = RuntimeError("health unavailable")
        self.kernel.cancel(oid)
        self.assertEqual(self.gateway.cancel_order.call_count, 2)
        self.assertEqual(self.journal.db.execute("SELECT day FROM cancel_requests ORDER BY seq DESC").fetchone()[0], "")

    def test_activity_is_observational_and_never_limits_orders_or_cancels(self):
        for number in range(12):
            self.kernel.send_order(intent("order-" + str(number)))
        results = self.kernel.cancel_all()
        self.assertEqual(len(results), 12)
        self.assertTrue(self.kernel.enabled)
        self.assertEqual(self.journal.activity(DAY), {"orders_last_minute": 12, "cancels_last_minute": 12,
                                                    "orders_today": 12, "cancels_today": 12})


if __name__ == "__main__":
    unittest.main()
