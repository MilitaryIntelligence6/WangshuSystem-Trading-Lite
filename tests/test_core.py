"""Money-path regressions against the real Lite kernel and offline broker."""
from dataclasses import replace
from contextlib import nullcontext
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wangshu_lite.core import TradingError
from wangshu_lite.models import Connection, GatewayPreflightError, OrderIntent
from wangshu_lite.runtime import Runtime


SYMBOL = "rb2610.SHFE"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Runtime(Path(self.tmp.name))
        self.addCleanup(self.runtime.close)
        self.runtime.connect()
        self.runtime.enable()

    def order(self, **kwargs):
        return self.runtime.send_order(dict(vt_symbol=SYMBOL, side="BUY", offset="OPEN", price=3500, volume=4, **kwargs))

    def test_manual_partial_cancel_close_and_reconnect(self):
        oid = self.order()
        self.runtime.paper_fill(oid, 2)
        self.runtime.cancel(oid)
        close = self.runtime.send_order(dict(vt_symbol=SYMBOL, side="SELL", offset="CLOSE_TODAY", price=3501, volume=2))
        self.runtime.paper_fill(close)
        state = self.runtime.snapshot()
        self.assertEqual(state["positions"], [])
        self.assertEqual([row["status"] for row in state["orders"]], ["CANCELLED", "FILLED"])
        self.runtime.disconnect()
        self.runtime.connect()
        self.assertFalse(self.runtime.snapshot()["enabled"])
        self.assertEqual(len(self.runtime.snapshot()["orders"]), 2)
        self.runtime.enable()

    def test_manual_limit_needs_no_quote_account_or_valuation_gate(self):
        kernel = self.runtime.kernel
        kernel.account, kernel.positions, kernel.ticks = None, [], {}
        self.assertTrue(self.order())

    def test_no_local_budget_frequency_or_loss_cap(self):
        self.runtime.kernel.account = replace(self.runtime.kernel.account, available=-1, daily_pnl=-999999)
        for _ in range(4):
            self.order()
        self.assertEqual(len(self.runtime.kernel.orders), 4)

    def test_duplicate_request_survives_restart(self):
        self.order(client_id="one-request")
        with self.assertRaises(TradingError):
            self.order(client_id="one-request")
        self.runtime.disconnect()
        self.runtime.connect()
        self.runtime.enable()
        with self.assertRaises(TradingError):
            self.order(client_id="one-request")

    def test_disk_failure_never_calls_gateway(self):
        with patch.object(self.runtime.journal, "reserve", side_effect=OSError("disk full")), patch.object(self.runtime.gateway, "send_order") as send:
            with self.assertRaises(TradingError):
                self.order()
        send.assert_not_called()
        self.assertFalse(self.runtime.kernel.enabled)

    def test_uncertain_send_never_retries_and_matching_reconcile(self):
        original = self.runtime.gateway.send_order
        observed = []
        def accepted_but_timeout(intent, revision):
            observed.append(original(intent, revision))
            raise TimeoutError("lost acknowledgement")
        with patch.object(self.runtime.gateway, "send_order", side_effect=accepted_but_timeout) as send:
            with self.assertRaises(TradingError):
                self.order(client_id="timeout-request")
            self.assertEqual(send.call_count, 1)
        self.assertFalse(self.runtime.kernel.enabled)
        with self.assertRaises(TradingError):
            self.runtime.enable()
        self.runtime.reconcile("timeout-request", observed[0])
        self.runtime.enable()
        self.assertEqual(len(self.runtime.kernel.orders), 1)

    def test_preflight_rejection_is_proven_not_sent(self):
        with patch.object(self.runtime.gateway, "send_order", side_effect=GatewayPreflightError("not ready")):
            with self.assertRaises(TradingError):
                self.order()
        self.assertEqual(self.runtime.kernel.unresolved(), [])

    def test_stop_new_orders_keeps_cancellation_available(self):
        oid = self.order()
        self.runtime.disable()
        with self.assertRaises(TradingError):
            self.order()
        self.runtime.cancel(oid)
        self.assertEqual(self.runtime.kernel.orders[oid].status, "CANCELLED")

    def test_bad_inputs_never_reach_gateway(self):
        for changes in ({"volume": True}, {"volume": 0}, {"price": float("nan")}, {"price": 3500.5}, {"offset": "CLOSE"}, {"order_type": "MARKET"}):
            payload = dict(vt_symbol=SYMBOL, side="BUY", offset="OPEN", price=3500, volume=1)
            payload.update(changes)
            with patch.object(self.runtime.gateway, "send_order") as send, self.assertRaises(TradingError):
                self.runtime.send_order(payload)
            send.assert_not_called()

    def test_terminal_callback_before_send_returns_stays_terminal(self):
        oid = self.runtime.send_order(dict(vt_symbol=SYMBOL, side="BUY", offset="OPEN", price=3501, volume=1, order_type="FOK"))
        self.assertEqual(self.runtime.kernel.orders[oid].status, "FILLED")
        self.assertEqual(self.runtime.journal.intents()[0]["state"], "TERMINAL")

    def test_duplicate_trade_is_not_forwarded_twice(self):
        oid = self.order()
        self.runtime.paper_fill(oid, 1)
        trade = next(iter(self.runtime.kernel.trades.values()))
        listener = []
        self.runtime.kernel.add_listener(lambda kind, data: listener.append(kind))
        self.runtime.kernel.on_event("trade", trade)
        self.assertNotIn("trade", listener)

    def test_account_lock_and_invalid_ctp_never_initialize_native(self):
        other = Runtime(Path(self.tmp.name))
        with self.assertRaises(TradingError):
            other.connect()
        other.close()
        self.runtime.disconnect()
        with self.assertRaises(TradingError):
            self.runtime.connect("ctp", settings={"密码": "secret-do-not-echo"})

    def test_gateway_day_change_before_callback_cannot_send_under_old_day(self):
        gateway = self.runtime.gateway
        gateway.state = replace(gateway.state, trading_day="20990101")
        with patch.object(gateway, "send_order") as send, self.assertRaises(TradingError):
            self.order()
        send.assert_not_called()
        self.assertFalse(self.runtime.kernel.enabled)

    def test_failed_close_retains_account_lock_and_objects(self):
        gateway = self.runtime.gateway
        with patch.object(gateway, "close", side_effect=RuntimeError("native exit failed")):
            with self.assertRaises(TradingError):
                self.runtime.disconnect()
        self.assertIs(self.runtime.gateway, gateway)
        self.assertIsNotNone(self.runtime.journal)
        self.assertFalse(self.runtime.kernel.enabled)
        with self.assertRaises(TradingError):
            self.runtime.connect()
        with self.assertRaises(TradingError):
            self.runtime.disconnect()
        # This test has no native threads. Release the injected latch for cleanup.
        self.runtime._close_failed = False

    def test_switching_ctp_front_preserves_account_journal(self):
        runtime = self.runtime
        runtime.disconnect()
        class FakeCtp:
            def __init__(self, settings, sink):
                self.sink = sink
            def connect(self):
                self.sink("connection", Connection(True, True, "20261022", trader_connected=True))
            def query(self):
                pass
            def close(self):
                pass
        settings = {"用户名": "test-user", "密码": "test-password", "经纪商代码": "test-broker",
                    "产品名称": "test-app", "授权编码": "test-auth", "交易服务器": "tcp://primary.invalid:1",
                    "行情服务器": "tcp://market.invalid:2", "账户环境": "simulation"}
        with patch.dict("sys.modules", {"wangshu_lite.ctp": SimpleNamespace(VnpyGateway=FakeCtp)}), patch("wangshu_lite.runtime.native_directory", side_effect=lambda *_: nullcontext()):
            runtime.connect("ctp", settings, True)
            directory = runtime.journal.directory
            runtime.journal.reserve(OrderIntent("still-unknown", SYMBOL, "BUY", "OPEN", 1, 3500), "20261022")
            runtime.disconnect()
            runtime.connect("ctp", dict(settings, 交易服务器="tcp://backup.invalid:3"), True)
            self.assertEqual(runtime.journal.directory, directory)
            self.assertTrue(runtime.journal.exists("still-unknown"))
            self.assertEqual(runtime.kernel.unresolved()[0]["state"], "RESERVED")
            runtime.disconnect()


if __name__ == "__main__":
    unittest.main()
