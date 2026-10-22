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
    from PySide6.QtCore import QPoint, QPointF, Qt, QItemSelectionModel
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
        self.window.mode.setCurrentIndex(self.window.mode.findData("paper"))
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
        for widget in (self.window.volume, self.window.price, self.window.side, self.window.offset, self.window.order_type,
                       *self.window.activity_inputs.values()):
            widget.setFocus()
            before = widget.value() if hasattr(widget, "value") else widget.currentIndex()
            for pixel, angle in ((QPoint(), QPoint(0, 120)), (QPoint(0, -24), QPoint())):
                event = QWheelEvent(QPointF(5, 5), QPointF(widget.mapToGlobal(QPoint(5, 5))), pixel, angle,
                                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                    Qt.ScrollPhase.ScrollUpdate, False)
                self.app.sendEvent(widget, event)
            after = widget.value() if hasattr(widget, "value") else widget.currentIndex()
            self.assertEqual(before, after)

    def test_activity_warning_is_visible_persisted_and_never_blocks_manual_trading(self):
        self.connect_paper()
        self.window.timer.stop()
        self.window.activity_inputs["orders_today"].setValue(2)
        self.window.save_activity_button.click()
        self.refreshed(lambda state: state["activity_monitor"]["limits"]["orders_today"] == 2)
        for count in range(1, 4):
            self.window.send_button.click()
            self.refreshed(lambda state: len(state["orders"]) == count)
        self.assertIn("当日报单3次", self.window.activity_warning.text())
        self.assertTrue(self.window.send_button.isEnabled())
        self.assertTrue(self.window.cancel_all_button.isEnabled())
        self.assertTrue(self.window.state["enabled"])
        self.window.activity_inputs["orders_today"].setValue(9)
        self.window.poll()
        self.wait(lambda: not self.window._polling)
        self.assertEqual(self.window.activity_inputs["orders_today"].value(), 9)
        self.window.disconnect_button.click()
        self.refreshed(lambda state: state["mode"] == "disconnected")
        self.connect_paper()
        self.assertEqual(self.window.state["activity_monitor"]["counts"]["orders_today"], 3)
        self.assertEqual(self.window.activity_inputs["orders_today"].value(), 2)
        self.window.tabs.setCurrentIndex(3)
        self.screenshot("lite-activity-monitor.png")
        self.assertFalse(self.errors)

    def test_multi_cancel_keeps_selection_during_reports_and_continues_after_one_failure(self):
        self.connect_paper()
        self.window.timer.stop()
        self.window.volume.setValue(4)
        for count in range(1, 4):
            self.window.send_button.click()
            self.refreshed(lambda state: len(state["orders"]) == count)
        self.window.orders.selectRow(0)
        self.window.partial_button.click()
        self.refreshed(lambda state: state["orders"][0]["traded"] == 2)
        selected_ids = [self.window.orders.item(row, 0).text() for row in (0, 2)]
        for row in (0, 2):
            self.window.orders.selectionModel().select(self.window.orders.model().index(row, 0),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        # A changed report rebuilds the table without replacing the user's selection.
        self.window.state["orders"][0]["status"] = "PARTIAL"
        self.window._render()
        self.assertEqual({self.window.orders.item(index.row(), 0).text()
                          for index in self.window.orders.selectionModel().selectedRows()}, set(selected_ids))
        original = self.runtime.gateway.cancel_order
        attempted = []
        def cancel(order):
            attempted.append(order.order_id)
            if order.order_id == selected_ids[0]:
                raise RuntimeError("测试柜台撤单暂不可用")
            return original(order)
        with patch.object(self.runtime.gateway, "cancel_order", side_effect=cancel):
            self.window.cancel_button.click()
            self.refreshed(lambda state: any(order["order_id"] == selected_ids[1] and order["status"] == "CANCELLED"
                                             for order in state["orders"]))
        self.assertEqual(attempted, selected_ids)
        self.assertIn("测试柜台撤单暂不可用", self.window.cancel_results.toPlainText())
        self.assertIn("已请求，等待柜台回报", self.window.cancel_results.toPlainText())
        self.assertEqual(self.window.state["activity_monitor"]["counts"]["cancels_today"], 2)
        self.assertEqual(self.window.state["orders"][0]["traded"], 2)
        self.assertNotEqual(self.window.state["orders"][1]["status"], "CANCELLED")
        self.window.centralWidget().verticalScrollBar().setValue(self.window.centralWidget().verticalScrollBar().maximum())
        self.screenshot("lite-multi-cancel.png")
        self.assertFalse(self.errors)

    def test_full_audit_export_uses_worker_and_logs_survive_reconnection(self):
        self.connect_paper()
        self.window.timer.stop()
        self.runtime.log("界面导出与恢复记录")
        self.window.disconnect_button.click()
        self.refreshed(lambda state: state["mode"] == "disconnected")
        self.connect_paper()
        self.assertIn("界面导出与恢复记录", self.window.logs.toPlainText())
        for index in range(320):
            self.runtime.journal.audit("ui_export_test", {"index": index})
        destination = Path(self.temp.name) / "complete-audit.csv"
        threads = []
        export = self.runtime.export_logs
        def record_export(path):
            threads.append(threading.current_thread())
            return export(path)
        with patch("wangshu_lite.ui.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
             patch.object(self.runtime, "export_logs", side_effect=record_export):
            self.window.export_logs_button.click()
            self.wait(lambda: self.window._export_thread is None)
        import csv
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(sum(row["kind"] == "ui_export_test" for row in rows), 320)
        self.assertTrue(all(thread is not threading.main_thread() for thread in threads))
        self.assertIn("已导出", self.window.audit_note.text())
        self.window.tabs.setCurrentIndex(2)
        self.window.poll()
        self.wait(lambda: not self.window._polling)
        self.screenshot("lite-persistent-logs.png")
        self.assertFalse(self.errors)

    def test_slow_export_leaves_polling_and_manual_cancel_available(self):
        self.connect_paper()
        self.window.timer.stop()
        self.window.send_button.click()
        self.refreshed(lambda state: len(state["orders"]) == 1)
        entered, release = threading.Event(), threading.Event()
        destination = Path(self.temp.name) / "slow-audit.csv"
        original = self.runtime.export_logs
        def slow_export(path):
            entered.set()
            release.wait(5)
            return original(path)
        try:
            with patch("wangshu_lite.ui.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
                 patch.object(self.runtime, "export_logs", side_effect=slow_export):
                self.window.export_logs_button.click()
                self.wait(entered.is_set)
                self.assertFalse(self.window._busy)
                self.assertTrue(self.window.send_button.isEnabled())
                self.assertFalse(self.window.export_logs_button.isEnabled())
                self.window.orders.selectRow(0)
                self.window.cancel_button.click()
                self.refreshed(lambda state: state["orders"][0]["status"] == "CANCELLED")
                self.assertFalse(destination.exists())
                release.set()
                self.wait(lambda: self.window._export_thread is None)
        finally:
            release.set()
            self.wait(lambda: self.window._export_thread is None)
        self.assertTrue(destination.exists())
        self.assertFalse(self.errors)

    def test_unsaved_runtime_logs_remain_visible_alongside_existing_disk_history(self):
        self.connect_paper()
        self.window.timer.stop()
        self.runtime.log("已落盘界面日志")
        self.window.poll()
        self.wait(lambda: not self.window._polling)
        self.assertEqual(self.window.logs.toPlainText().count("已落盘界面日志"), 1)
        with patch.object(self.runtime.journal, "audit", side_effect=OSError("disk full")):
            self.runtime.log("本次写入失败的实时日志")
            self.runtime.kernel.on_event("error", "CTP ErrorID=999：柜台原始错误仍需展示")
            self.window.poll()
            self.wait(lambda: not self.window._polling)
        text = self.window.logs.toPlainText()
        self.assertIn("已落盘界面日志", text)
        self.assertIn("[未落盘] 本次写入失败的实时日志", text)
        self.assertIn("[未落盘] 运行日志写入失败", text)
        self.assertIn("CTP ErrorID=999：柜台原始错误仍需展示", text)
        self.assertFalse(self.window.send_button.isEnabled())
        self.assertTrue(self.window.cancel_button.isEnabled())
        self.assertTrue(self.window.state["audit"])
        self.window.tabs.setCurrentIndex(2)
        self.screenshot("lite-unsaved-logs.png")
        self.assertFalse(self.errors)

    def test_close_waits_for_log_export_without_destroying_a_running_thread(self):
        self.connect_paper()
        self.window.timer.stop()
        entered, release = threading.Event(), threading.Event()
        destination = Path(self.temp.name) / "close-audit.csv"
        journal = self.runtime.journal
        def slow_export(path):
            entered.set()
            release.wait(5)
            rows = journal.export_audit(Path(path))
            return {"rows": rows, "path": path}
        try:
            with patch("wangshu_lite.ui.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
                 patch.object(self.runtime, "export_logs", side_effect=slow_export):
                self.window.export_logs_button.click()
                self.wait(entered.is_set)
                self.window.close()
                self.wait(lambda: not self.window._thread.isRunning())
                self.assertTrue(self.window._export_thread.isRunning())
                self.assertTrue(self.window.isVisible())
                self.window.close()  # A repeated close cannot destroy the export thread.
                self.assertTrue(self.window.isVisible())
                release.set()
                self.wait(lambda: self.window._export_thread is None and not self.window.isVisible())
        finally:
            release.set()
            self.wait(lambda: self.window._export_thread is None)
        self.assertTrue(destination.exists())
        self.assertFalse(self.errors)

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
        self.window.settings["密码"].setText(" password with spaces ")
        self.window.settlement.setChecked(True)
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

    def test_simulation_entry_uses_real_ctp_transport_and_selected_pair(self):
        self.assertEqual(self.window.mode.currentData(), "simulation")
        self.assertEqual(self.window.connect_button.text(), "连接光大仿真")
        self.assertFalse(self.window.account_environment.isEnabled())
        self.window.sdk.setCurrentIndex(self.window.sdk.findData("实盘"))
        self.window.front_pair.setCurrentIndex(self.window.front_pair.findData("2"))
        for key, value in {"交易服务器": "tcp://group1.invalid:1", "行情服务器": "tcp://group1.invalid:2",
                           "交易服务器备选": "tcp://group2.invalid:3", "行情服务器备选": "tcp://group2.invalid:4"}.items():
            self.window.settings[key].setText(value)
        self.window.settlement.setChecked(True)
        with patch.object(self.runtime, "connect") as connect:
            self.window.connect_button.click()
            self.wait(lambda: not self.window._busy)
            kwargs = connect.call_args.kwargs
            self.assertEqual(kwargs["mode"], "ctp")
            self.assertEqual(kwargs["settings"]["账户环境"], "simulation")
            self.assertEqual(kwargs["settings"]["柜台环境"], "实盘")
            self.assertEqual(kwargs["settings"]["前置组"], "2")
            self.assertEqual(kwargs["settings"]["交易服务器备选"], "tcp://group2.invalid:3")
            self.assertEqual(kwargs["settings"]["行情服务器备选"], "tcp://group2.invalid:4")
        self.window.mode.setCurrentIndex(self.window.mode.findData("paper"))
        self.assertEqual(self.window.connect_button.text(), "启动离线演练")
        self.window.mode.setCurrentIndex(self.window.mode.findData("simulation"))
        self.assertEqual(self.window.settings["交易服务器备选"].text(), "tcp://group2.invalid:3")
        self.assertFalse(self.errors)
        self.screenshot("lite-simulation-connection.png")

    def test_connection_save_load_clear_stay_off_ui_thread_without_secret_status(self):
        self.window.timer.stop()
        self.window.settings["经纪商代码"].setText("test-broker")
        self.window.settings["用户名"].setText("test-user")
        self.window.settings["密码"].setText(" secret with spaces ")
        self.window.settings["授权编码"].setText(" secret-auth ")
        self.window.remember_credentials.setChecked(True)
        calls = []
        saved = {}
        def save(settings, remember_credentials=False):
            calls.append(("save", threading.current_thread(), remember_credentials))
            saved.update(settings)
            return "参数已保存在本机"
        def load(environment="simulation", with_credentials=True):
            calls.append(("load", threading.current_thread(), with_credentials))
            return dict(saved)
        def clear(environment="simulation"):
            calls.append(("clear", threading.current_thread(), environment))
            return "本机凭据已清除"
        with patch.object(self.runtime, "save_connection", side_effect=save, create=True), \
             patch.object(self.runtime, "load_connection", side_effect=load, create=True), \
             patch.object(self.runtime, "clear_connection_credentials", side_effect=clear, create=True):
            self.window.save_connection_button.click()
            self.wait(lambda: not self.window._busy)
            self.assertEqual(saved["密码"], " secret with spaces ")
            self.assertEqual(saved["授权编码"], " secret-auth ")
            self.window.settings["密码"].clear()
            self.window.settlement.setChecked(True)
            self.window.load_connection_button.click()
            self.wait(lambda: not self.window._busy)
            self.assertEqual(self.window.settings["密码"].text(), " secret with spaces ")
            self.assertFalse(self.window.settlement.isChecked())
            self.assertNotIn("secret", self.window.statusBar().currentMessage())
            self.window.clear_credentials_button.click()
            self.wait(lambda: not self.window._busy)
            for key in ("用户名", "密码", "授权编码"):
                self.assertEqual(self.window.settings[key].text(), "")
            self.assertEqual(self.window.settings["经纪商代码"].text(), "test-broker")
            self.assertFalse(self.window.remember_credentials.isChecked())
        self.assertEqual([item[0] for item in calls], ["save", "load", "clear"])
        self.assertTrue(all(item[1] is not threading.main_thread() for item in calls))
        self.assertFalse(self.errors)

    def test_live_and_simulation_drafts_are_isolated_and_confirmation_expires(self):
        window = self.window
        window.settings["用户名"].setText("simulation-user")
        window.settings["交易服务器"].setText("tcp://simulation.invalid:1")
        window.settings["密码"].setText("simulation-password")
        window.settlement.setChecked(True)
        window.mode.setCurrentIndex(window.mode.findData("ctp"))
        self.assertFalse(window.settlement.isChecked())
        window.account_environment.setCurrentIndex(window.account_environment.findData("live"))
        for key in ("用户名", "密码", "交易服务器"):
            self.assertEqual(window.settings[key].text(), "")
        window.settings["用户名"].setText("live-user")
        window.settings["密码"].setText("live-password")
        window.settings["交易服务器"].setText("tcp://live.invalid:1")
        window.sdk.setCurrentIndex(window.sdk.findData("测试"))
        window.front_pair.setCurrentIndex(window.front_pair.findData("2"))
        window.settlement.setChecked(True)
        window.mode.setCurrentIndex(window.mode.findData("simulation"))
        self.assertEqual(window.settings["用户名"].text(), "simulation-user")
        self.assertEqual(window.settings["密码"].text(), "simulation-password")
        self.assertEqual(window.settings["交易服务器"].text(), "tcp://simulation.invalid:1")
        self.assertEqual(window.sdk.currentData(), "实盘")
        self.assertEqual(window.front_pair.currentData(), "1")
        self.assertFalse(window.settlement.isChecked())
        for change in (lambda: window.settings["交易服务器"].setText("tcp://simulation.invalid:2"),
                       lambda: window.sdk.setCurrentIndex(window.sdk.findData("测试")),
                       lambda: window.front_pair.setCurrentIndex(window.front_pair.findData("2"))):
            window.settlement.setChecked(True)
            change()
            self.assertFalse(window.settlement.isChecked())
        window.mode.setCurrentIndex(window.mode.findData("paper"))
        window.mode.setCurrentIndex(window.mode.findData("simulation"))
        self.assertEqual(window.settings["用户名"].text(), "simulation-user")
        self.assertEqual(window.settings["交易服务器"].text(), "tcp://simulation.invalid:2")
        # A frequent enable-state refresh must not clear the chosen draft.
        window._connection_fields()
        window._connection_fields()
        self.assertEqual(window.settings["密码"].text(), "simulation-password")
        window.mode.setCurrentIndex(window.mode.findData("ctp"))
        window.account_environment.setCurrentIndex(window.account_environment.findData("live"))
        self.assertEqual(window.settings["用户名"].text(), "live-user")
        self.assertEqual(window.settings["密码"].text(), "live-password")
        self.assertEqual(window.settings["交易服务器"].text(), "tcp://live.invalid:1")
        self.assertEqual(window.sdk.currentData(), "测试")
        self.assertEqual(window.front_pair.currentData(), "2")
        self.assertFalse(window.settlement.isChecked())

    def test_vault_failure_keeps_public_parameters_and_shows_actual_error(self):
        self.window.settings["用户名"].setText("stale-user")
        self.window.settings["密码"].setText("stale-password")
        self.window.settings["授权编码"].setText("stale-auth")
        saved = {"经纪商代码": "saved-broker", "产品名称": "saved-app",
                 "交易服务器": "tcp://saved.invalid:1", "行情服务器": "tcp://saved.invalid:2",
                 "_credential_error": "系统安全凭据库不可用"}
        with patch.object(self.runtime, "load_connection", return_value=saved, create=True):
            self.window.load_connection_button.click()
            self.wait(lambda: not self.window._busy)
        self.assertEqual(self.window.settings["经纪商代码"].text(), "saved-broker")
        self.assertEqual(self.window.settings["交易服务器"].text(), "tcp://saved.invalid:1")
        for key in ("用户名", "密码", "授权编码"):
            self.assertEqual(self.window.settings[key].text(), "")
        self.assertIn("系统安全凭据库不可用", self.window.connection_note.text())
        self.assertIn("凭据读取失败", self.window.connection_note.text())
        self.assertNotIn("stale-password", self.window.statusBar().currentMessage())
        self.assertFalse(self.window.settlement.isChecked())

    def test_simulation_connection_phases_and_failure_are_visible(self):
        self.window.state.update(mode="ctp", connection={"ready": False, "connected": False},
            connection_profile={"environment": "simulation", "front_pair": "2"},
            connection_status={"authenticated": True, "trader_logged_in": True, "market_logged_in": False,
                               "settlement_confirmed": False, "contracts_ready": False},
            connection_error="CTP ErrorID=7：认证被拒绝")
        self.window._render()
        self.window._apply_enabled()
        self.assertIn("光大 CTP 仿真", self.window.connection_progress.text())
        self.assertIn("交易登录：完成", self.window.connection_progress.text())
        self.assertIn("行情登录：等待", self.window.connection_progress.text())
        self.assertIn("结算确认：等待", self.window.connection_progress.text())
        self.assertIn("前置组 2", self.window.connection_progress.text())
        self.assertIn("ErrorID=7", self.window.connection_error.text())
        self.assertTrue(self.window.disconnect_button.isEnabled())
        self.assertFalse(self.window.send_button.isEnabled())
        self.screenshot("lite-simulation-phases.png")

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
