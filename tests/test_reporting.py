"""Runtime reporting remains durable without turning advisory reads into gates."""
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wangshu_lite.core import TradingError
from wangshu_lite.runtime import Runtime


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(Path(self.temp.name))
        self.addCleanup(self.runtime.close)
        self.runtime.connect()
        self.runtime.enable()

    def order(self):
        return self.runtime.send_order(dict(vt_symbol="rb2610.SHFE", side="BUY", offset="OPEN", price=3500, volume=1))

    def test_runtime_and_connection_logs_are_redacted_durable_and_exported(self):
        self.runtime._secrets = ("test-private-password",)
        self.runtime.log("认证 test-private-password 的运行记录")
        self.runtime.disconnect()
        self.runtime.connect()
        audit = self.runtime.snapshot()["audit"]
        text = str(audit)
        self.assertIn("[已隐藏]", text)
        self.assertNotIn("test-private-password", text)
        self.assertTrue(any(row["kind"] == "connection" for row in audit))
        self.assertTrue(any(row["kind"] == "runtime" and "会话已断开" in str(row["detail"]) for row in audit))
        destination = Path(self.temp.name) / "audit.csv"
        result = self.runtime.export_logs(destination)
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(result["rows"], len(rows))
        self.assertNotIn("test-private-password", destination.read_text(encoding="utf-8-sig"))

    def test_reporting_read_failures_remain_visible_without_disabling_trading(self):
        with patch.object(self.runtime.journal, "list_audit", side_effect=OSError("read unavailable")), \
             patch.object(self.runtime.journal, "activity", side_effect=OSError("read unavailable")):
            state = self.runtime.snapshot()
        self.assertFalse(state["activity_monitor"]["available"])
        self.assertIsNone(state["activity_monitor"]["counts"]["orders_today"])
        self.assertIn("读取失败", state["audit_error"])
        self.assertTrue(self.runtime.kernel.enabled)
        self.assertTrue(self.order())

    def test_runtime_log_disk_failure_stops_new_orders_but_keeps_cancellation(self):
        order_id = self.order()
        with patch.object(self.runtime.journal, "audit", side_effect=OSError("disk full")):
            self.runtime.log("须持久化的运行记录")
            self.assertFalse(self.runtime.kernel.enabled)
            self.assertTrue(self.runtime.kernel.fatal)
            self.runtime.cancel(order_id)
        self.assertEqual(self.runtime.kernel.orders[order_id].status, "CANCELLED")
        self.assertTrue(any("日志写入失败" in row["message"] for row in self.runtime.snapshot()["logs"]))

    def test_bad_limit_save_does_not_disable_trading_and_export_requires_a_session(self):
        with self.assertRaises(TradingError):
            self.runtime.update_activity_limits({"orders_today": -1})
        self.assertTrue(self.runtime.kernel.enabled)
        self.runtime.disconnect()
        with self.assertRaises(TradingError):
            self.runtime.export_logs(Path(self.temp.name) / "no-session.csv")
