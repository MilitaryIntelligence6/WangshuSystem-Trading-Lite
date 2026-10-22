"""Warnings use persisted request counts without entering the order decision path."""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from wangshu_lite.core import TradingError
from wangshu_lite.models import GatewayPreflightError, OrderIntent
from wangshu_lite.monitor import ActivityMonitor, METRICS, MAX_LIMIT
from wangshu_lite.runtime import Runtime
from wangshu_lite.storage import Journal


DAY = "20261022"
SYMBOL = "rb2610.SHFE"


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Journal(Path(self.tmp.name), "monitor-test")
        self.addCleanup(lambda: self.journal.close())
        self.monitor = ActivityMonitor(self.journal)

    def add_order(self, client_id, day=DAY):
        self.journal.reserve(OrderIntent(client_id, SYMBOL, "BUY", "OPEN", 1, 3500), day)

    def limits(self, **values):
        return dict(dict.fromkeys(METRICS, 0), **values)

    def audit(self):
        return [json.loads(row[0]) for row in self.journal.db.execute(
            "SELECT detail FROM audit WHERE kind='activity_monitor' ORDER BY seq")]

    def reopen(self):
        self.journal.close()
        self.journal = Journal(Path(self.tmp.name), "monitor-test")
        self.monitor = ActivityMonitor(self.journal)

    def assert_unavailable(self, snapshot):
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["counts"], dict.fromkeys(METRICS))
        self.assertEqual(snapshot["alerts"], [])
        self.assertTrue(snapshot["error"])

    def test_default_thresholds_disabled(self):
        self.add_order("one")
        state = self.monitor.snapshot(DAY)
        self.assertTrue(state["available"])
        self.assertEqual(state["limits"], self.limits())
        self.assertEqual(state["counts"], self.limits(orders_last_minute=1, orders_today=1))
        self.assertEqual(state["alerts"], [])

    def test_threshold_crossing_resolution_and_day_switch_do_not_spam(self):
        self.assertEqual(self.monitor.update_limits(self.limits(orders_last_minute=2)), "")
        self.monitor.snapshot(DAY)
        self.add_order("one")
        self.monitor.snapshot(DAY)
        self.assertEqual(len(self.audit()), 1)
        self.add_order("two")
        state = self.monitor.snapshot(DAY)
        self.assertEqual(state["alerts"][0]["metric"], "orders_last_minute")
        self.assertEqual(state["alerts"][0]["count"], 2)
        self.assertEqual(state["alerts"][0]["limit"], 2)
        self.assertEqual(self.audit()[-1]["entered"], ["orders_last_minute"])
        self.add_order("three")
        for _ in range(5):
            self.assertEqual(self.monitor.snapshot(DAY)["counts"]["orders_today"], 3)
        self.assertEqual(len(self.audit()), 2)
        with self.journal.db:
            self.journal.db.execute("UPDATE intents SET created=?", (time.time() - 61,))
        self.assertEqual(self.monitor.snapshot(DAY)["alerts"], [])
        self.assertEqual(self.audit()[-1]["cleared"], ["orders_last_minute"])
        self.assertEqual(len(self.audit()), 3)
        state = self.monitor.snapshot("20261023")
        self.assertTrue(state["available"])
        self.assertEqual(state["counts"], self.limits())
        self.assertEqual(self.audit()[-1]["previous_day"], DAY)
        self.assertTrue(self.audit()[-1]["day_changed"])
        self.assertEqual(len(self.audit()), 4)

    def test_active_state_survives_restart_without_repeat_audit(self):
        self.monitor.update_limits(self.limits(orders_today=1))
        self.add_order("one")
        self.assertTrue(self.monitor.snapshot(DAY)["alerts"])
        self.assertEqual(len(self.audit()), 1)
        self.reopen()
        self.assertTrue(self.monitor.snapshot(DAY)["alerts"])
        self.assertEqual(len(self.audit()), 1)

    def test_threshold_configuration_persists_and_changes_are_audited(self):
        limits = self.limits(orders_today=2, cancels_today=MAX_LIMIT)
        self.assertEqual(self.monitor.update_limits(limits), "")
        self.reopen()
        self.assertEqual(self.monitor.snapshot(DAY)["limits"], limits)
        row = self.journal.db.execute("SELECT detail FROM audit WHERE kind='activity_limits'").fetchone()
        self.assertEqual(json.loads(row[0])["limits"], limits)

    def test_invalid_thresholds_never_replace_configuration(self):
        valid = self.limits(orders_today=2)
        self.monitor.update_limits(valid)
        invalid = [None, [], {}, dict(valid, extra=1)]
        invalid.extend(self.limits(orders_today=value)
                       for value in (True, False, -1, 1.5, "2", None, MAX_LIMIT + 1, float("nan")))
        for limits in invalid:
            with self.subTest(limits=limits):
                self.assertTrue(self.monitor.update_limits(limits))
                self.assertEqual(json.loads(self.journal.get("activity_limits")), valid)
        self.assertEqual(self.monitor.snapshot(DAY)["limits"], valid)

    def test_unknown_day_does_not_claim_zero_counts(self):
        with patch.object(self.journal, "get") as get:
            for day in (None, "", "bad", "2026102", 20261022, "２０２６１０２２"):
                with self.subTest(day=day):
                    self.assert_unavailable(self.monitor.snapshot(day))
            get.assert_not_called()

    def test_unreadable_statistics_and_read_recovery(self):
        with patch.object(self.journal, "activity", side_effect=OSError("test read failure")):
            self.assert_unavailable(self.monitor.snapshot(DAY))
        with patch.object(self.journal, "get", side_effect=OSError("test meta failure")):
            self.assert_unavailable(self.monitor.snapshot(DAY))
        self.assertTrue(self.monitor.snapshot(DAY)["available"])

    def test_alert_audit_failure_is_unavailable_and_retries_once(self):
        with patch.object(self.journal, "set_audited", side_effect=OSError("test audit failure")):
            self.assert_unavailable(self.monitor.snapshot(DAY))
        self.assertEqual(self.audit(), [])
        self.assertTrue(self.monitor.snapshot(DAY)["available"])
        self.assertTrue(self.monitor.snapshot(DAY)["available"])
        self.assertEqual(len(self.audit()), 1)

    def test_configuration_write_failure_retains_previous_limits(self):
        saved = self.limits(orders_today=2)
        self.monitor.update_limits(saved)
        with patch.object(self.journal, "set_audited", side_effect=OSError("test write failure")):
            self.assertTrue(self.monitor.update_limits(self.limits(orders_today=3)))
        self.assertEqual(self.monitor.snapshot(DAY)["limits"], saved)

    def test_bad_persisted_config_and_bad_statistics_are_unavailable(self):
        for raw in ('{"orders_today":1}', '{bad json',
                    '{"orders_last_minute":0,"cancels_last_minute":0,"orders_today":1,"orders_today":2,"cancels_today":0}'):
            with self.subTest(raw=raw):
                self.journal.set("activity_limits", raw)
                self.assert_unavailable(self.monitor.snapshot(DAY))
        self.assertEqual(self.monitor.update_limits(self.limits()), "")
        for counts in ({}, self.limits(orders_today=True), self.limits(orders_today=-1)):
            with patch.object(self.journal, "activity", return_value=counts):
                self.assert_unavailable(self.monitor.snapshot(DAY))
        self.assertTrue(self.monitor.snapshot(DAY)["available"])

    def test_not_sent_is_excluded_and_counts_remain_after_restart(self):
        self.add_order("not-sent")
        self.journal.not_sent("not-sent")
        self.add_order("unknown")
        self.journal.unknown("unknown")
        self.add_order("terminal")
        self.journal.bind("terminal", "order-one")
        self.journal.terminal("order-one")
        self.reopen()
        self.assertEqual(self.monitor.snapshot(DAY)["counts"]["orders_today"], 2)


class AdvisoryIntegrationTests(unittest.TestCase):
    def test_failed_cancel_journal_still_cancels_and_marks_counts_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory))
            try:
                runtime.connect()
                runtime.enable()
                order_id = runtime.send_order(dict(
                    vt_symbol=SYMBOL, side="BUY", offset="OPEN", price=3500, volume=1))
                day = runtime.kernel.connection.trading_day
                self.assertTrue(runtime.monitor.snapshot(day)["available"])
                # Reject only cancellation auditing. The offline broker can still
                # persist its own successful cancellation, as a real broker would.
                runtime.journal.db.execute("""
                    CREATE TRIGGER fail_cancel_audit BEFORE INSERT ON audit
                    WHEN NEW.kind='cancel_requested'
                    BEGIN SELECT RAISE(ABORT, 'cancel journal disk fault'); END
                """)
                runtime.journal.db.commit()
                runtime.cancel(order_id)
                self.assertEqual(runtime.kernel.orders[order_id].status, "CANCELLED")
                self.assertFalse(runtime.kernel.enabled)
                self.assertTrue(runtime.kernel.fatal)
                state = runtime.monitor.snapshot(day)
                self.assertFalse(state["available"])
                self.assertEqual(state["counts"], dict.fromkeys(METRICS))
                self.assertEqual(state["alerts"], [])
                self.assertIn("不完整", state["error"])
                # Recovery of ordinary disk writes cannot repair a missing attempt.
                runtime.journal.db.execute("DROP TRIGGER fail_cancel_audit")
                runtime.journal.db.commit()
                self.assertFalse(runtime.monitor.snapshot(day)["available"])
                runtime.disconnect()
                runtime.connect()
                self.assertFalse(runtime.monitor.snapshot(day)["available"])
                self.assertTrue(runtime.monitor.snapshot("20990103")["available"])
            finally:
                runtime.close()

    def test_cancel_uses_current_broker_day_and_marks_day_mismatch_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory))
            try:
                runtime.connect()
                runtime.enable()
                order_id = runtime.send_order(dict(
                    vt_symbol=SYMBOL, side="BUY", offset="OPEN", price=3500, volume=1))
                prior_day = runtime.kernel.connection.trading_day
                new_day = "20990102"
                runtime.gateway.state = replace(runtime.gateway.state, trading_day=new_day)
                runtime.kernel.cancel(order_id)
                self.assertEqual(runtime.kernel.connection.trading_day, prior_day)
                self.assertEqual(runtime.kernel.orders[order_id].status, "CANCELLED")
                row = runtime.journal.db.execute("SELECT day FROM cancel_requests").fetchone()
                self.assertEqual(row[0], new_day)
                prior = runtime.journal.db.execute(
                    "SELECT COUNT(*) FROM cancel_requests WHERE day=?", (prior_day,)).fetchone()
                self.assertEqual(prior[0], 0)
                state = runtime.monitor.snapshot(new_day)
                self.assertFalse(state["available"])
                self.assertEqual(state["counts"], dict.fromkeys(METRICS))
                self.assertIn("不完整", state["error"])
                self.assertFalse(runtime.monitor.snapshot(prior_day)["available"])
                self.assertTrue(runtime.monitor.snapshot("20990103")["available"])
            finally:
                runtime.close()

    def test_cancel_without_current_broker_day_still_cancels_without_guessing_day(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory))
            try:
                runtime.connect()
                runtime.enable()
                order_id = runtime.send_order(dict(
                    vt_symbol=SYMBOL, side="BUY", offset="OPEN", price=3500, volume=1))
                day = runtime.kernel.connection.trading_day
                runtime.gateway.state = replace(runtime.gateway.state, trading_day="")
                runtime.kernel.cancel(order_id)
                self.assertEqual(runtime.kernel.orders[order_id].status, "CANCELLED")
                row = runtime.journal.db.execute("SELECT day FROM cancel_requests").fetchone()
                self.assertEqual(row[0], "")
                state = runtime.monitor.snapshot(day)
                self.assertFalse(state["available"])
                self.assertEqual(state["counts"], dict.fromkeys(METRICS))
                self.assertIn("不完整", state["error"])
            finally:
                runtime.close()

    def test_warning_never_blocks_orders_or_cancels_or_counts_callbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory))
            try:
                runtime.connect()
                runtime.enable()
                monitor = ActivityMonitor(runtime.journal)
                limits = dict.fromkeys(METRICS, 2)
                self.assertEqual(monitor.update_limits(limits), "")
                payload = dict(vt_symbol=SYMBOL, side="BUY", offset="OPEN", price=3500, volume=1)
                with patch.object(runtime.gateway, "send_order", side_effect=GatewayPreflightError("not ready")):
                    with self.assertRaises(TradingError):
                        runtime.send_order(payload)
                day = runtime.kernel.connection.trading_day
                self.assertEqual(monitor.snapshot(day)["counts"]["orders_today"], 0)
                runtime.enable()
                orders = [runtime.send_order(payload) for _ in range(2)]
                self.assertEqual(monitor.snapshot(day)["alerts"][0]["count"], 2)
                orders.append(runtime.send_order(payload))
                self.assertTrue(runtime.kernel.enabled)
                for order_id in orders:
                    runtime.kernel.on_event("order", runtime.kernel.orders[order_id])
                    runtime.kernel.on_event("order", runtime.kernel.orders[order_id])
                    runtime.cancel(order_id)
                state = monitor.snapshot(day)
                self.assertEqual(state["counts"], dict.fromkeys(METRICS, 3))
                self.assertEqual(len(state["alerts"]), 4)
                self.assertTrue(all(runtime.kernel.orders[oid].status == "CANCELLED" for oid in orders))
                with patch.object(runtime.journal, "activity", side_effect=OSError("test monitor failure")):
                    self.assertFalse(monitor.snapshot(day)["available"])
                    self.assertTrue(runtime.send_order(payload))
                self.assertTrue(runtime.kernel.enabled)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
