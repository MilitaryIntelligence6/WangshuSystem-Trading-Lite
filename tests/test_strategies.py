"""CTA plumbing tests. Probes have no trading signals or user strategy source."""
import csv
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from concurrent.futures import Future


HAS_CTA = importlib.util.find_spec("vnpy_ctastrategy") is not None
if HAS_CTA:
    from wangshu_lite.runtime import native_directory
    _CTA_IMPORT_DIRECTORY = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    with native_directory(Path(_CTA_IMPORT_DIRECTORY.name)):
        from vnpy_ctastrategy import CtaTemplate
        from wangshu_lite.core import TradingKernel
        from wangshu_lite.models import Connection, OrderIntent, Position, Tick
        from wangshu_lite.paper import PaperGateway, SYMBOL
        from wangshu_lite.storage import Journal
        from wangshu_lite.strategies import StrategyManager
        from wangshu_lite.history import TZ

    class CallbackProbe(CtaTemplate):
        """Records callbacks; orders are explicitly submitted by test code."""
        parameters = ["size"]
        variables = ["ticks_seen", "fills_seen"]
        size = 1
        ticks_seen = fills_seen = 0

        def on_init(self):
            self.orders_seen = []

        def on_tick(self, tick):
            if getattr(self, "fail_tick", False):
                raise RuntimeError("callback probe failure")
            self.ticks_seen += 1

        def on_order(self, order):
            self.orders_seen.append(order.vt_orderid)

        def on_trade(self, trade):
            self.fills_seen += 1

    class HistoryProbe(CallbackProbe):
        def on_init(self):
            super().on_init()
            self.bars_seen = []
            self.load_bar(2)

        def on_bar(self, bar):
            self.bars_seen.append(bar)


@unittest.skipUnless(HAS_CTA, "install the cta extra for official vn.py integration tests")
class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.journal = Journal(self.directory, "PAPER")
        self.kernel = TradingKernel(self.journal, output=lambda *_: None, expected_account="PAPER")
        self.paper = PaperGateway(self.kernel.on_event, self.journal)
        self.kernel.attach(self.paper)
        self.paper.connect()
        self.kernel.enable()
        self.manager = StrategyManager(self.kernel, self.directory)
        self.register(CallbackProbe)

    def tearDown(self):
        self.manager.close()
        self.kernel.close()
        self.journal.close()
        self.tmp.cleanup()

    def register(self, cls):
        def register():
            self.manager.engine.classes[cls.__name__] = cls
            self.manager.class_sources[cls.__name__] = "test-probe"
        self.manager._call(register)

    def started(self, name="probe", cls="CallbackProbe"):
        self.manager.add(cls, name, SYMBOL, {"size": 2})
        self.manager.initialize(name)
        self.manager.start(name)
        return self.manager.engine.strategies[name]

    def flush(self):
        self.manager._call(lambda: None)

    def test_real_cta_template_orders_use_kernel_and_fill_updates_pos_once(self):
        strategy = self.started()
        ids = self.manager._call(lambda: strategy.buy(3500, 2))
        self.flush()
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.journal.order_sources()[ids[0]], "strategy:probe")
        self.assertIn(ids[0], strategy.orders_seen)
        self.paper.fill(ids[0], 1)
        self.flush()
        self.assertEqual((strategy.pos, strategy.fills_seen), (1, 1))
        trade = next(iter(self.kernel.trades.values()))
        self.kernel.on_event("trade", trade)
        self.flush()
        self.assertEqual((strategy.pos, strategy.fills_seen), (1, 1))
        self.paper.fill(ids[0])
        self.flush()
        self.assertEqual((strategy.pos, strategy.fills_seen), (2, 2))
        saved = json.loads(self.journal.get("cta_state"))
        self.assertEqual(saved["data"]["probe"]["pos"], 2)

    def test_manual_trade_does_not_change_strategy_pos_and_stop_only_cancels_own(self):
        strategy = self.started()
        owned = self.manager._call(lambda: strategy.buy(3490, 1))[0]
        manual = self.kernel.send_order(OrderIntent("manual", SYMBOL, "BUY", "OPEN", 1, 3490))
        self.paper.fill(manual)
        self.flush()
        self.assertEqual(strategy.pos, 0)
        manual2 = self.kernel.send_order(OrderIntent("manual2", SYMBOL, "BUY", "OPEN", 1, 3490))
        self.manager.stop("probe")
        self.flush()
        self.assertFalse(strategy.trading)
        self.assertEqual(self.kernel.orders[owned].status, "CANCELLED")
        self.assertTrue(self.kernel.orders[manual2].active)

    def test_shfe_sell_uses_official_today_yesterday_conversion(self):
        strategy = self.started()
        self.paper.positions = [Position(SYMBOL, "LONG", 3, 2, average_price=3500)]
        self.paper.query()
        self.flush()
        ids = self.manager._call(lambda: strategy.sell(3500, 3))
        self.assertEqual([(self.kernel.orders[oid].offset, self.kernel.orders[oid].volume) for oid in ids],
                         [("CLOSE_TODAY", 1), ("CLOSE_YESTERDAY", 2)])

    def test_callback_failure_cancels_orders_and_does_not_restart(self):
        strategy = self.started()
        oid = self.manager._call(lambda: strategy.buy(3490, 1))[0]
        strategy.fail_tick = True
        self.kernel.on_event("tick", Tick(SYMBOL, 3500, 3499, 3501, 3850, 3150, datetime.now(TZ).timestamp()))
        self.flush()
        self.assertFalse(strategy.trading)
        self.assertFalse(strategy.inited)
        self.assertEqual(self.kernel.orders[oid].status, "CANCELLED")
        self.assertIn("callback probe failure", self.manager.snapshot()[0]["error"])

    def test_disconnect_and_disable_stop_strategies_without_auto_restart(self):
        strategy = self.started()
        self.kernel.disable()
        self.flush()
        self.assertFalse(strategy.trading)
        self.kernel.enable()
        self.flush()
        self.assertFalse(strategy.trading)
        self.manager.start("probe")
        self.kernel.on_event("connection", Connection(False, False))
        self.flush()
        self.assertFalse(strategy.trading)

    def test_failed_init_and_start_never_report_success(self):
        self.manager.add("CallbackProbe", "probe", SYMBOL, {})
        strategy = self.manager.engine.strategies["probe"]
        def fail():
            raise RuntimeError("intentional test failure")
        strategy.on_init = fail
        with self.assertRaisesRegex(ValueError, "初始化失败"):
            self.manager.initialize("probe")
        self.assertFalse(strategy.inited)
        strategy.on_init = lambda: None
        self.manager.initialize("probe")
        strategy.on_start = fail
        with self.assertRaisesRegex(ValueError, "启动失败"):
            self.manager.start("probe")
        self.assertFalse(strategy.trading)

    def test_history_warmup_is_real_and_missing_history_fails_init(self):
        self.register(HistoryProbe)
        self.manager.add("HistoryProbe", "probe", SYMBOL, {})
        with self.assertRaisesRegex(ValueError, "没有.*历史"):
            self.manager.initialize("probe")
        path = self.directory / "bars.csv"
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
            writer.writerow([(datetime.now(TZ) - timedelta(minutes=1)).isoformat(), 3499, 3501, 3498, 3500, 12])
        self.assertEqual(self.manager.import_history(path, SYMBOL), 1)
        self.manager.initialize("probe")
        strategy = self.manager.engine.strategies["probe"]
        self.assertEqual(len(strategy.bars_seen), 1)
        self.assertEqual(strategy.bars_seen[0].volume, 12)

    def test_parameters_survive_partial_edit_and_require_reinit(self):
        self.started()
        self.manager.stop("probe")
        self.manager.edit("probe", {"size": 5})
        self.assertFalse(self.manager.engine.strategies["probe"].inited)
        self.assertEqual(self.manager.snapshot()[0]["parameters"]["size"], 5)

    def test_explicit_file_load_and_restart_restore_without_auto_import(self):
        source = self.directory / "callback_probe.py"
        source.write_text("from vnpy_ctastrategy import CtaTemplate\nclass FileProbe(CtaTemplate):\n    def on_init(self):\n        pass\n", encoding="utf-8")
        self.assertEqual(self.manager.load_file(source), ["FileProbe"])
        self.manager.add("FileProbe", "file", SYMBOL, {})
        self.manager.initialize("file")
        self.manager.start("file")
        strategy = self.manager.engine.strategies["file"]
        oid = self.manager._call(lambda: strategy.buy(3500, 1))[0]
        self.paper.fill(oid)
        self.flush()
        self.manager.close()
        self.manager = StrategyManager(self.kernel, self.directory)
        row = self.manager.snapshot()[0]
        self.assertFalse(row["loaded"])
        self.assertFalse(row["trading"])
        self.manager.load_file(source)
        strategy = self.manager.engine.strategies["file"]
        self.assertEqual(strategy.pos, 1)
        self.assertFalse(strategy.inited)
        self.manager.initialize("file")
        self.assertEqual(strategy.pos, 1)
        self.assertFalse(strategy.trading)

    def test_official_local_stop_order_fires_via_same_kernel_then_cancels_on_stop(self):
        strategy = self.started()
        ids = self.manager._call(lambda: strategy.buy(3510, 1, stop=True))
        self.assertTrue(ids[0].startswith("STOP"))
        self.kernel.on_event("tick", Tick(SYMBOL, 3510, 3509, 3511, 3850, 3150, datetime.now(TZ).timestamp()))
        self.flush()
        owned = [oid for oid, source in self.journal.order_sources().items() if source == "strategy:probe"]
        self.assertEqual(len(owned), 1)
        self.manager.stop("probe")
        self.assertFalse(self.kernel.orders[owned[0]].active)

    def test_removed_strategy_stays_removed_after_manager_restart(self):
        self.started()
        self.manager.stop("probe")
        self.manager.remove("probe")
        self.assertEqual(self.manager.snapshot(), [])
        self.manager.close()
        self.manager = StrategyManager(self.kernel, self.directory)
        self.assertEqual(self.manager.snapshot(), [])

    def test_trade_while_source_unloaded_is_replayed_once_when_explicitly_loaded(self):
        source = self.directory / "callback_probe.py"
        source.write_text("from vnpy_ctastrategy import CtaTemplate\nclass FileProbe(CtaTemplate):\n    def on_init(self):\n        pass\n", encoding="utf-8")
        self.manager.load_file(source)
        self.manager.add("FileProbe", "file", SYMBOL, {})
        self.manager.close()
        self.manager = StrategyManager(self.kernel, self.directory)
        oid = self.kernel.send_order(OrderIntent("unloaded", SYMBOL, "BUY", "OPEN", 1, 3500), source="strategy:file")
        self.paper.fill(oid)
        self.flush()
        self.manager.load_file(source)
        strategy = self.manager.engine.strategies["file"]
        self.assertEqual(strategy.pos, 1)
        self.manager.close()
        self.manager = StrategyManager(self.kernel, self.directory)
        self.manager.load_file(source)
        self.assertEqual(self.manager.engine.strategies["file"].pos, 1)

    def test_snapshot_reads_cached_state_while_user_callback_is_slow(self):
        strategy = self.started()
        entered, release = threading.Event(), threading.Event()
        def slow_tick(tick):
            entered.set()
            release.wait(3)
        strategy.on_tick = slow_tick
        self.kernel.on_event("tick", Tick(SYMBOL, 3500, 3499, 3501, 3850, 3150, datetime.now(TZ).timestamp()))
        self.assertTrue(entered.wait(1))
        try:
            start = time.monotonic()
            self.manager.snapshot()
            self.assertLess(time.monotonic() - start, 0.1)
        finally:
            release.set()
        self.flush()

    def test_expired_queued_start_never_runs_later(self):
        self.manager.add("CallbackProbe", "probe", SYMBOL, {})
        self.manager.initialize("probe")
        strategy = self.manager.engine.strategies["probe"]
        entered, release = threading.Event(), threading.Event()
        def slow_tick(tick):
            entered.set()
            release.wait(3)
        strategy.on_tick = slow_tick
        self.kernel.on_event("tick", Tick(SYMBOL, 3500, 3499, 3501, 3850, 3150, datetime.now(TZ).timestamp()))
        self.assertTrue(entered.wait(1))
        class ShortFuture(Future):
            def result(self, timeout=None):
                return super().result(timeout=0.01)
        try:
            with patch("wangshu_lite.strategies.Future", ShortFuture), self.assertRaises(TimeoutError):
                self.manager.start("probe")
        finally:
            release.set()
        self.flush()
        self.assertFalse(strategy.trading)

    def test_errors_are_redacted_before_audit_and_snapshot(self):
        strategy = self.started()
        self.kernel._secrets = ("private-auth-code",)
        def fail_tick(tick):
            raise ValueError("private-auth-code")
        strategy.on_tick = fail_tick
        self.kernel.on_event("tick", Tick(SYMBOL, 3500, 3499, 3501, 3850, 3150, datetime.now(TZ).timestamp()))
        self.flush()
        self.assertNotIn("private-auth-code", self.manager.snapshot()[0]["error"])
        audit = self.journal.db.execute("SELECT detail FROM audit WHERE kind='strategy_log'").fetchall()
        self.assertTrue(audit)
        self.assertNotIn("private-auth-code", str([row[0] for row in audit]))


if __name__ == "__main__":
    unittest.main()
