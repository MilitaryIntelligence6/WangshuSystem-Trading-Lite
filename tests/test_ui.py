"""Opt-in native UI checks. All broker operations use temporary PAPER state.

WANGSHU_LITE_QT_TESTS=1 QT_QPA_PLATFORM=offscreen python -m unittest tests.test_ui
"""
import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
RUN_QT = os.environ.get("WANGSHU_LITE_QT_TESTS") == "1" and importlib.util.find_spec("PySide6") is not None
if RUN_QT:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication, QMessageBox
    from wangshu_lite.runtime import Runtime
    from wangshu_lite.ui import TradingWindow


@unittest.skipUnless(RUN_QT, "Native desktop checks require WANGSHU_LITE_QT_TESTS=1 and PySide6")
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Path(self.temp.name))
        self.window = TradingWindow(self.runtime)
        self.window.confirm = lambda *_: True
        self.errors = []
        self.warning = patch.object(QMessageBox, "warning", side_effect=lambda *args: self.errors.append(str(args[-1])))
        self.info = patch.object(QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok)
        self.warning.start()
        self.info.start()
        self.window.show()
        self.wait(lambda: not self.window._polling)

    def tearDown(self):
        self.window.confirm = lambda *_: True
        self.window.close()
        self.wait(lambda: not self.window._thread.isRunning())
        self.app.processEvents()
        self.runtime.close()
        self.warning.stop()
        self.info.stop()
        self.temp.cleanup()

    def wait(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("UI operation timed out; errors=" + repr(self.errors))

    def refreshed(self, predicate):
        def check():
            if not self.window._busy and not self.window._polling and predicate(self.window.state):
                return True
            self.window.poll()
            return False
        self.wait(check)

    def connect_paper(self):
        self.window.connect_button.click()
        self.refreshed(lambda state: state.get("connection", {}).get("ready", False))
        self.window.enable_button.click()
        self.refreshed(lambda state: state.get("enabled"))
        self.assertFalse(self.errors)

    def screenshot(self, filename):
        directory = os.environ.get("WANGSHU_LITE_SCREENSHOTS")
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            self.app.processEvents()
            self.assertTrue(self.window.grab().save(str(target / filename)))

    def test_real_paper_open_partial_cancel_remainder_and_close_through_ui(self):
        self.connect_paper()
        self.window.subscribe_button.click()
        self.refreshed(lambda state: bool(state.get("ticks")))
        self.window.volume.setValue(4)
        self.window.send_button.click()
        self.refreshed(lambda state: len(state["orders"]) == 1)
        self.window.orders.selectRow(0)
        self.window.partial_button.click()
        self.refreshed(lambda state: state["orders"][0]["traded"] == 2)
        self.assertEqual(self.window.state["positions"][0]["volume"], 2)
        self.window.orders.selectRow(0)
        self.window.cancel_button.click()
        self.refreshed(lambda state: state["orders"][0]["status"] == "CANCELLED")
        self.assertEqual(self.window.state["orders"][0]["traded"], 2)
        self.window.side.setCurrentIndex(self.window.side.findData("SELL"))
        self.window.offset.setCurrentIndex(self.window.offset.findData("CLOSE_TODAY"))
        self.window.volume.setValue(2)
        self.window.send_button.click()
        self.refreshed(lambda state: len(state["orders"]) == 2)
        close_id = next(o["order_id"] for o in self.window.state["orders"] if o["offset"] == "CLOSE_TODAY")
        for row in range(self.window.orders.rowCount()):
            if self.window.orders.item(row, 0).text() == close_id:
                self.window.orders.selectRow(row)
                break
        self.window.fill_button.click()
        self.refreshed(lambda state: not state["positions"] and len(state["trades"]) == 2)
        self.assertEqual(self.window.orders.rowCount(), 2)
        self.assertEqual(self.window.trades.rowCount(), 2)
        rendered = {self.window.orders.item(row, 0).text(): self.window.orders.item(row, 7).text()
                    for row in range(self.window.orders.rowCount())}
        self.assertEqual(rendered, {o["order_id"]: o["status"] for o in self.window.state["orders"]})
        self.assertFalse(self.errors)
        self.screenshot("lite-trading.png")

    def test_mouse_wheel_and_trackpad_do_not_modify_numeric_or_choice_inputs(self):
        self.window.timer.stop()
        for widget in (self.window.volume, self.window.price, self.window.side, self.window.offset, self.window.order_type):
            widget.setFocus()
            before = widget.value() if hasattr(widget, "value") else widget.currentIndex()
            for pixel, angle in ((QPoint(), QPoint(0, 120)), (QPoint(0, -24), QPoint())):
                event = QWheelEvent(QPointF(5, 5), QPointF(widget.mapToGlobal(QPoint(5, 5))), pixel, angle,
                                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                    Qt.ScrollPhase.ScrollUpdate, False)
                self.app.sendEvent(widget, event)
            after = widget.value() if hasattr(widget, "value") else widget.currentIndex()
            self.assertEqual(before, after)

    def test_calls_are_off_ui_thread_and_snapshot_polling_does_not_accumulate(self):
        self.connect_paper()
        self.wait(lambda: not self.window._busy and not self.window._polling)
        self.window.timer.stop()
        entered, release = threading.Event(), threading.Event()
        call_threads = []

        def slow_query():
            call_threads.append(threading.current_thread())
            entered.set()
            release.wait(3)
            self.runtime.query()

        self.window._submit("测试慢查询", slow_query)
        self.wait(entered.is_set)
        for _ in range(20):
            self.window.poll()
            self.app.processEvents()
        self.assertTrue(self.window._busy)
        self.assertFalse(self.window._polling)
        self.assertFalse(self.window.send_button.isEnabled())
        self.assertIsNot(call_threads[0], threading.main_thread())
        release.set()
        self.wait(lambda: not self.window._busy and not self.window._polling)

    def test_strategy_controls_dispatch_parameters_lifecycle_and_history(self):
        self.connect_paper()
        self.window.timer.stop()
        calls = []
        rows = []

        def strategy(action, **kwargs):
            calls.append((action, kwargs, threading.current_thread()))
            if action == "load":
                return ["CallbackProbe"]
            if action == "class_parameters":
                return {"size": 1}
            if action == "add":
                rows.append(dict(name=kwargs["name"], class_name=kwargs["class_name"], vt_symbol=kwargs["symbol"],
                                 parameters=kwargs["parameters"], variables={"pos": 0}, inited=False, trading=False, error=""))
            if action in {"initialize", "start", "stop"}:
                row = rows[0]
                row["inited"] = True
                row["trading"] = action == "start"
            if action == "remove":
                rows.clear()

        snapshot = self.runtime.snapshot

        def with_strategies():
            state = snapshot()
            state["strategies"] = copy.deepcopy(rows)
            return state

        with patch.object(self.runtime, "strategy", side_effect=strategy), patch.object(self.runtime, "snapshot", side_effect=with_strategies), \
             patch("wangshu_lite.ui.QFileDialog.getOpenFileName", return_value=("/private/tmp/explicit-user-selected.py", "")):
            self.window.load_strategy_button.click()
            self.wait(lambda: not self.window._busy and self.window.strategy_class.count() == 1)
            self.assertEqual(json_load(self.window.parameters.toPlainText()), {"size": 1})
            self.window.strategy_name.setText("probe")
            self.window.add_strategy_button.click()
            self.refreshed(lambda state: len(state["strategies"]) == 1)
            self.window.strategy_table.selectRow(0)
            self.window.init_strategy_button.click()
            self.refreshed(lambda state: state["strategies"][0]["inited"])
            self.window.start_strategy_button.click()
            self.refreshed(lambda state: state["strategies"][0]["trading"])
            self.window.stop_strategy_button.click()
            self.refreshed(lambda state: not state["strategies"][0]["trading"])
            self.window.history_button.click()
            self.wait(lambda: not self.window._busy)
            self.assertTrue(any(action == "import_history" for action, _, _ in calls))
            self.assertTrue(all(thread is not threading.main_thread() for _, _, thread in calls))
            self.window.tabs.setCurrentIndex(1)
            self.screenshot("lite-strategies.png")
        self.assertFalse(self.errors)

    def test_invalid_parameter_json_never_reaches_runtime(self):
        self.window.parameters.setPlainText("[]")
        with patch.object(self.runtime, "strategy") as strategy:
            self.window.add_strategy()
            strategy.assert_not_called()
        self.assertTrue(self.errors)

    def test_ctp_passes_explicit_account_environment_separately_from_sdk(self):
        self.window.mode.setCurrentIndex(self.window.mode.findData("ctp"))
        self.window.account_environment.setCurrentIndex(self.window.account_environment.findData("live"))
        self.window.sdk.setCurrentIndex(self.window.sdk.findData("测试"))
        self.window.settlement.setChecked(True)
        self.window.settings["密码"].setText(" password with spaces ")
        with patch.object(self.runtime, "connect") as connect:
            self.window.connect_button.click()
            self.wait(lambda: not self.window._busy)
            kwargs = connect.call_args.kwargs
            self.assertEqual(kwargs["settings"]["账户环境"], "live")
            self.assertEqual(kwargs["settings"]["柜台环境"], "测试")
            self.assertEqual(kwargs["settings"]["密码"], " password with spaces ")
            self.assertTrue(kwargs["settlement_confirmed"])
        self.window.state["mode"] = "ctp"
        self.window.state["connection"] = {"connected": False, "ready": False}
        self.window._apply_enabled()
        self.assertTrue(self.window.disconnect_button.isEnabled())
        self.assertFalse(self.window.connect_button.isEnabled())
        self.assertTrue(self.window.cancel_button.isEnabled())

    @unittest.skipUnless(importlib.util.find_spec("vnpy_ctastrategy") is not None, "official CTA dependency required")
    def test_real_cta_file_loading_initialization_and_callbacks_through_ui(self):
        self.connect_paper()
        self.window.timer.stop()
        path = Path(self.temp.name) / "ui_callback_probe.py"
        # A callback recorder, with no signal generation or order algorithm.
        path.write_text("from vnpy_ctastrategy import CtaTemplate\n"
                        "class UiCallbackProbe(CtaTemplate):\n"
                        "    parameters = ['size']\n"
                        "    variables = ['ticks_seen']\n"
                        "    size = 1\n"
                        "    ticks_seen = 0\n"
                        "    def on_init(self):\n"
                        "        pass\n"
                        "    def on_tick(self, tick):\n"
                        "        self.ticks_seen += 1\n", encoding="utf-8")
        with patch("wangshu_lite.ui.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            self.window.load_strategy_button.click()
            self.wait(lambda: not self.window._busy and self.window.strategy_class.count() == 1)
        self.window.strategy_name.setText("ui-probe")
        self.window.add_strategy_button.click()
        self.refreshed(lambda state: len(state["strategies"]) == 1)
        self.window.strategy_table.selectRow(0)
        self.window.init_strategy_button.click()
        self.refreshed(lambda state: state["strategies"][0]["inited"])
        self.window.start_strategy_button.click()
        self.refreshed(lambda state: state["strategies"][0]["trading"])
        self.refreshed(lambda state: state["strategies"][0]["variables"]["ticks_seen"] > 0)
        self.window.tabs.setCurrentIndex(1)
        self.screenshot("lite-strategies-real.png")
        self.window.stop_strategy_button.click()
        self.refreshed(lambda state: not state["strategies"][0]["trading"])
        self.assertFalse(self.errors)


def json_load(text):
    import json
    return json.loads(text)
