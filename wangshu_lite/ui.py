"""Optional native desktop. All trading and strategy work belongs to Runtime."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QCompleter, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget, QAbstractItemView, QHeaderView,
)


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class Worker(QObject):
    finished = Signal(str, object, object)

    @Slot(str, object)
    def execute(self, action, function):
        try:
            result = function()
        except Exception as exc:
            self.finished.emit(action, None, str(exc))
        else:
            self.finished.emit(action, result, None)
        finally:
            if action == "finalize":
                QThread.currentThread().quit()


def _value(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


class TradingWindow(QMainWindow):
    requested = Signal(str, object)

    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime
        self.state = {}
        self._busy = False
        self._polling = False
        self._snapshot_error = True
        self._closing = False
        self._allow_close = False
        self._callbacks = {}
        self._buttons = []
        self._table_values = {}
        self._catalog_symbols = ()
        self.setWindowTitle("望舒 Lite · 人工交易与 vn.py CTA")
        self.resize(1220, 860)
        self.setMinimumSize(760, 600)
        self._thread = QThread(self)
        self._worker = Worker()
        self._worker.moveToThread(self._thread)
        self.requested.connect(self._worker.execute)
        self._worker.finished.connect(self._finished)
        self._thread.finished.connect(self._worker.deleteLater)
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.poll)
        self._thread.start()
        self.timer.start()
        self._apply_enabled()
        QTimer.singleShot(0, self.poll)

    def _button(self, text, callback, layout=None):
        button = QPushButton(text)
        button.clicked.connect(callback)
        self._buttons.append(button)
        if layout is not None:
            layout.addWidget(button)
        return button

    @staticmethod
    def _combo(items):
        widget = NoWheelComboBox()
        for label, value in items:
            widget.addItem(label, value)
        return widget

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.setMinimumHeight(100)
        return table

    def _build(self):
        content = QWidget()
        self.layout = QVBoxLayout(content)
        self.layout.setContentsMargins(12, 10, 12, 10)
        self.layout.setSpacing(8)
        self.connection_panel = QGroupBox("连接")
        grid = QGridLayout(self.connection_panel)
        self.mode = self._combo([("离线演练", "paper"), ("CTP", "ctp")])
        self.sdk = self._combo([("生产 SDK", "实盘"), ("测试 SDK", "测试")])
        self.settings = {}
        fields = [("经纪商代码", "BrokerID"), ("用户名", "账户"), ("密码", "密码"),
                  ("产品名称", "AppID"), ("授权编码", "AuthCode"),
                  ("交易服务器", "交易前置 tcp://…"), ("行情服务器", "行情前置 tcp://…")]
        grid.addWidget(QLabel("环境"), 0, 0)
        grid.addWidget(self.mode, 0, 1)
        grid.addWidget(QLabel("SDK"), 0, 2)
        grid.addWidget(self.sdk, 0, 3)
        for index, (key, placeholder) in enumerate(fields):
            field = QLineEdit()
            field.setPlaceholderText(placeholder)
            field.setAccessibleName(placeholder)
            if key in {"密码", "授权编码"}:
                field.setEchoMode(QLineEdit.EchoMode.Password)
            self.settings[key] = field
            row, column = 1 + index // 4, index % 4
            grid.addWidget(field, row, column)
        self.account_environment = self._combo([("账户环境：仿真", "simulation"), ("账户环境：实盘", "live")])
        self.account_environment.setAccessibleName("账户环境；需人工核对，SDK 模式不代表账户类型")
        grid.addWidget(self.account_environment, 2, 3)
        self.settlement = QCheckBox("已核对柜台环境与结算；允许登录后确认当日结算")
        grid.addWidget(self.settlement, 3, 0, 1, 3)
        connect_row = QHBoxLayout()
        self.connect_button = self._button("连接", self.connect_session, connect_row)
        self.disconnect_button = self._button("断开", self.disconnect_session, connect_row)
        grid.addLayout(connect_row, 3, 3)
        self.mode.currentIndexChanged.connect(self._connection_fields)
        self.layout.addWidget(self.connection_panel)

        toolbar = QHBoxLayout()
        self.enable_button = self._button("允许下单", lambda: self._submit("允许下单", self.runtime.enable), toolbar)
        self.disable_button = self._button("禁止新单", lambda: self._submit("禁止新单", self.runtime.disable), toolbar)
        self.query_button = self._button("刷新柜台", lambda: self._submit("刷新柜台", self.runtime.query), toolbar)
        toolbar.addStretch()
        self.status = QLabel("正在读取交易状态…")
        self.status.setWordWrap(True)
        toolbar.addWidget(self.status, 1)
        self.layout.addLayout(toolbar)
        self.account_label = QLabel("资金：—")
        self.account_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.layout.addWidget(self.account_label)

        self.tabs = QTabWidget()
        self.layout.addWidget(self.tabs, 1)
        self._build_trading()
        self._build_strategies()
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(500)
        self.tabs.addTab(self.logs, "日志")
        self.notice = QLabel("委托、成交及撤单结果以柜台回报为准。密码和 AuthCode 不保存到配置。")
        self.notice.setWordWrap(True)
        self.layout.addWidget(self.notice)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.setCentralWidget(scroll)
        self._connection_fields()

    def _build_trading(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QHBoxLayout(top)
        quotes = QGroupBox("合约与报价")
        qlayout = QVBoxLayout(quotes)
        self.search = QLineEdit()
        self.search.setPlaceholderText("按合约代码、名称或交易所搜索全部柜台合约")
        self.search.textChanged.connect(self._render_catalog)
        qlayout.addWidget(self.search)
        self.contract_table = self._table(["合约", "名称", "最新", "买一", "卖一", "报价时间"])
        self.contract_table.itemDoubleClicked.connect(self._choose_contract)
        qlayout.addWidget(self.contract_table, 1)
        self.catalog_count = QLabel("等待合约目录")
        qlayout.addWidget(self.catalog_count)
        top_layout.addWidget(quotes, 3)
        ticket = QGroupBox("人工下单")
        form = QFormLayout(ticket)
        self.symbol = QLineEdit("rb2610.SHFE")
        self.symbol.setPlaceholderText("准确合约.交易所")
        self.symbol.editingFinished.connect(self._set_price_step)
        form.addRow("合约", self.symbol)
        self.subscribe_button = self._button("订阅选中报价", self.subscribe)
        form.addRow(self.subscribe_button)
        self.side = self._combo([("买", "BUY"), ("卖", "SELL")])
        self.offset = self._combo([("开仓", "OPEN"), ("平仓", "CLOSE"), ("平今", "CLOSE_TODAY"), ("平昨", "CLOSE_YESTERDAY")])
        self.order_type = self._combo([("限价 LIMIT", "LIMIT"), ("FAK", "FAK"), ("FOK", "FOK")])
        self.price = NoWheelDoubleSpinBox()
        self.price.setDecimals(8)
        self.price.setRange(0, 1e12)
        self.price.setValue(3500)
        self.price.setKeyboardTracking(False)
        self.volume = NoWheelSpinBox()
        self.volume.setRange(1, 2_147_483_647)
        self.volume.setValue(1)
        self.volume.setKeyboardTracking(False)
        for label, widget in (("方向", self.side), ("开平", self.offset), ("类型", self.order_type), ("价格", self.price), ("手数", self.volume)):
            form.addRow(label, widget)
        quote_buttons = QHBoxLayout()
        self._button("买一价", lambda: self.use_quote("bid_price"), quote_buttons)
        self._button("卖一价", lambda: self.use_quote("ask_price"), quote_buttons)
        form.addRow(quote_buttons)
        self.send_button = self._button("核对并下单", self.send_order)
        form.addRow(self.send_button)
        form.addRow(QLabel("SHFE / INE 平仓请选择平今或平昨。"))
        top_layout.addWidget(ticket, 2)
        splitter.addWidget(top)
        records = QWidget()
        record_layout = QVBoxLayout(records)
        actions = QHBoxLayout()
        self.cancel_button = self._button("撤选中委托", self.cancel_selected, actions)
        self.cancel_all_button = self._button("撤全部活动委托", self.cancel_all, actions)
        self.reconcile_button = self._button("核对绑定", self.reconcile_selected, actions)
        actions.addStretch()
        self.partial_button = self._button("演练：部分成交", lambda: self.paper_fill(partial=True), actions)
        self.fill_button = self._button("演练：全部成交", self.paper_fill, actions)
        record_layout.addLayout(actions)
        self.records = QTabWidget()
        self.orders = self._table(["委托号", "合约", "买卖", "开平", "价格", "手数", "已成交", "状态", "来源"])
        self.positions = self._table(["合约", "方向", "持仓", "昨仓", "冻结", "成本价", "持仓盈亏"])
        self.trades = self._table(["成交号", "委托号", "合约", "买卖", "开平", "价格", "手数"])
        self.unresolved = self._table(["请求 ID", "交易日", "状态", "委托号"])
        for name, table in (("委托", self.orders), ("持仓", self.positions), ("成交", self.trades), ("待核实", self.unresolved)):
            self.records.addTab(table, name)
        record_layout.addWidget(self.records)
        splitter.addWidget(records)
        splitter.setSizes([330, 260])
        layout.addWidget(splitter)
        self.tabs.addTab(tab, "交易")

    def _build_strategies(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        note = QLabel("加载官方 vn.py CtaTemplate 策略。所选 Python 文件将在本机执行，请只选择你信任的文件；源码不复制进工程。")
        note.setWordWrap(True)
        layout.addWidget(note)
        file_row = QHBoxLayout()
        self.strategy_path = QLineEdit()
        self.strategy_path.setReadOnly(True)
        self.strategy_path.setPlaceholderText("由你选择工程外的策略文件")
        file_row.addWidget(self.strategy_path, 1)
        self.load_strategy_button = self._button("选择并加载 .py", self.load_strategy, file_row)
        layout.addLayout(file_row)
        form = QGridLayout()
        self.strategy_class = NoWheelComboBox()
        self.strategy_class.activated.connect(self.class_parameters)
        self.strategy_name = QLineEdit()
        self.strategy_name.setPlaceholderText("实例名")
        self.strategy_symbol = QLineEdit("rb2610.SHFE")
        for column, (label, widget) in enumerate((("策略类", self.strategy_class), ("实例名", self.strategy_name), ("合约", self.strategy_symbol))):
            form.addWidget(QLabel(label), 0, column)
            form.addWidget(widget, 1, column)
        layout.addLayout(form)
        history_row = QHBoxLayout()
        history_row.addWidget(QLabel("初始化历史 K 线"))
        self.history_interval = self._combo([("1 分钟", "1m"), ("1 小时", "1h"), ("日线", "d")])
        history_row.addWidget(self.history_interval)
        self.history_button = self._button("导入历史 CSV", self.import_history, history_row)
        history_row.addStretch()
        layout.addLayout(history_row)
        layout.addWidget(QLabel("参数 JSON（策略 parameters 中声明的字段）"))
        self.parameters = QPlainTextEdit("{}")
        self.parameters.setMaximumHeight(90)
        layout.addWidget(self.parameters)
        buttons = QHBoxLayout()
        self.add_strategy_button = self._button("添加", self.add_strategy, buttons)
        self.edit_strategy_button = self._button("保存参数", self.edit_strategy, buttons)
        self.init_strategy_button = self._button("初始化", lambda: self.strategy_action("initialize"), buttons)
        self.start_strategy_button = self._button("启动", lambda: self.strategy_action("start"), buttons)
        self.stop_strategy_button = self._button("停止并撤本策略委托", lambda: self.strategy_action("stop"), buttons)
        self.remove_strategy_button = self._button("移除", lambda: self.strategy_action("remove"), buttons)
        buttons.addStretch()
        self.stop_all_button = self._button("停止全部策略", self.stop_all_strategies, buttons)
        layout.addLayout(buttons)
        self.strategy_table = self._table(["实例", "策略类", "合约", "源码已加载", "已初始化", "运行中", "策略净持仓", "错误"])
        self.strategy_table.itemSelectionChanged.connect(self._select_strategy)
        layout.addWidget(self.strategy_table, 1)
        self.strategy_detail = QPlainTextEdit()
        self.strategy_detail.setReadOnly(True)
        self.strategy_detail.setMaximumHeight(125)
        layout.addWidget(self.strategy_detail)
        self.tabs.addTab(tab, "CTA 策略")

    def _connection_fields(self):
        active = self.state.get("mode") in {"paper", "ctp"}
        enabled = self.mode.currentData() == "ctp" and not active and not self._busy and not self._closing
        for field in self.settings.values():
            field.setEnabled(enabled)
        self.sdk.setEnabled(enabled)
        self.account_environment.setEnabled(enabled)
        self.settlement.setEnabled(enabled)

    def confirm(self, title, message):
        return QMessageBox.question(self, title, message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def _submit(self, action, function, callback=None):
        if self._busy or self._closing:
            return
        self._busy = True
        if callback:
            self._callbacks[action] = callback
        self.status.setText(action + "…")
        self._apply_enabled()
        self.requested.emit(action, function)

    @Slot(str, object, object)
    def _finished(self, action, result, error):
        if action == "snapshot":
            self._polling = False
            if error:
                self._snapshot_error = True
                self.status.setText("状态读取失败：" + error)
            else:
                self._snapshot_error = False
                self.state = result
                self._render()
        elif action == "close":
            if error:
                self._closing = False
                self._busy = False
                self.timer.start()
                QMessageBox.critical(self, "退出失败", error + "\n请核对柜台后重试。")
            else:
                self._allow_close = True
                self._thread.finished.connect(self.close)
                self._thread.quit()
        else:
            self._busy = False
            callback = self._callbacks.pop(action, None)
            if error:
                self.statusBar().showMessage(action + "未完成：" + error, 12000)
                QMessageBox.warning(self, "操作未完成", error)
            else:
                if action == "发送委托":
                    message = "委托请求已提交：" + _value(result) + "；最终状态以柜台回报为准"
                else:
                    message = action + "已完成" + ("：" + _value(result) if result is not None else "")
                self.statusBar().showMessage(message, 8000)
                if callback:
                    callback(result)
            if not self._closing:
                QTimer.singleShot(0, self.poll)
        self._apply_enabled()

    def poll(self):
        if self._polling or self._busy or self._closing:
            return
        self._polling = True
        self.requested.emit("snapshot", self.runtime.snapshot)

    def _apply_enabled(self):
        idle = not self._busy and not self._closing
        connection = self.state.get("connection") or {}
        active = self.state.get("mode") in {"paper", "ctp"}
        ready = bool(connection.get("ready")) and not self._snapshot_error
        enabled = bool(self.state.get("enabled")) and ready
        for button in self._buttons:
            button.setEnabled(idle)
        self.connect_button.setEnabled(idle and not active)
        self.disconnect_button.setEnabled(idle and active)
        self.enable_button.setEnabled(idle and ready and not enabled)
        self.disable_button.setEnabled(idle and active)
        self.send_button.setEnabled(idle and enabled)
        self.start_strategy_button.setEnabled(idle and enabled)
        self.init_strategy_button.setEnabled(idle and ready)
        for button in (self.cancel_button, self.cancel_all_button, self.query_button, self.subscribe_button):
            button.setEnabled(idle and active)
        paper = self.state.get("mode", self.mode.currentData()) == "paper"
        self.fill_button.setVisible(paper)
        self.partial_button.setVisible(paper)
        self.mode.setEnabled(idle and not active)
        self._connection_fields()

    def connect_session(self):
        mode = self.mode.currentData()
        settings = {key: field.text() if key in {"密码", "授权编码"} else field.text().strip()
                    for key, field in self.settings.items()}
        settings["柜台环境"] = self.sdk.currentData()
        settings["账户环境"] = self.account_environment.currentData()
        if mode == "ctp" and not self.settlement.isChecked():
            QMessageBox.warning(self, "连接前核对", "请先核对柜台环境与结算，并勾选结算确认。SDK 模式不能判断账户是仿真还是实盘。")
            return
        confirmed = self.settlement.isChecked()
        self._submit("连接", lambda: self.runtime.connect(mode=mode, settings=settings if mode == "ctp" else {},
                                                        settlement_confirmed=confirmed))

    def disconnect_session(self):
        if self.confirm("断开柜台", "将停止策略并禁止新单。断开不等于柜台委托已撤销，请核对活动委托。继续断开？"):
            self._submit("断开", self.runtime.disconnect)

    def subscribe(self):
        symbol = self.symbol.text().strip()
        self._submit("订阅", lambda: self.runtime.subscribe(symbol))

    def use_quote(self, field):
        symbol = self.symbol.text().strip()
        tick = next((row for row in self.state.get("ticks", []) if row.get("vt_symbol") == symbol), None)
        if tick and tick.get(field, 0) > 0:
            self.price.setValue(tick[field])
        else:
            QMessageBox.information(self, "报价", "尚未收到该合约的有效报价，请订阅并核对。")

    def send_order(self):
        if not self.send_button.isEnabled():
            return
        payload = dict(client_id=uuid.uuid4().hex, vt_symbol=self.symbol.text().strip(), side=self.side.currentData(),
                       offset=self.offset.currentData(), order_type=self.order_type.currentData(),
                       price=self.price.value(), volume=self.volume.value())
        if self.confirm("核对委托", f"{payload['vt_symbol']}\n{self.side.currentText()} · {self.offset.currentText()} · {payload['order_type']}\n价格 {payload['price']:g}，数量 {payload['volume']} 手\n确认发送？"):
            self._submit("发送委托", lambda: self.runtime.send_order(payload))

    def _selected_order(self):
        row = self.orders.currentRow()
        if row < 0:
            QMessageBox.information(self, "选择委托", "请先在委托表中选择一笔委托。")
            return None
        return self.orders.item(row, 0).text()

    def cancel_selected(self):
        oid = self._selected_order()
        if oid:
            self._submit("撤单", lambda: self.runtime.cancel(oid))

    def cancel_all(self):
        if self.confirm("撤全部活动委托", "向柜台请求撤销全部已知活动委托；成功与否以回报为准。继续？"):
            self._submit("全部撤单", self.runtime.cancel_all)

    def reconcile_selected(self):
        row = self.unresolved.currentRow()
        oid = self._selected_order()
        if row < 0 or not oid:
            if row < 0:
                QMessageBox.information(self, "核对绑定", "请在待核实表选择请求，并在委托表选择对应的柜台委托。")
            return
        client_id = self.unresolved.item(row, 0).text()
        if self.confirm("核对绑定", f"请求 {client_id}\n柜台委托 {oid}\n请先核对柜台证据。系统仍会校验交易日和委托字段，确认绑定？"):
            self._submit("核对绑定", lambda: self.runtime.reconcile(client_id, oid))

    def paper_fill(self, checked=False, *, partial=False):
        oid = self._selected_order()
        if not oid:
            return
        volume = None
        if partial:
            order = next((o for o in self.state.get("orders", []) if o["order_id"] == oid), {})
            remaining = order.get("volume", 0) - order.get("traded", 0)
            if remaining < 2:
                QMessageBox.information(self, "部分成交", "剩余数量至少为 2 手才能演练部分成交。")
                return
            volume = max(1, remaining // 2)
        self._submit("演练成交", lambda: self.runtime.paper_fill(oid, volume))

    def load_strategy(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择受信任的外部策略文件", "", "Python 策略 (*.py)")
        if path:
            self.strategy_path.setText(path)
            self._submit("加载策略", lambda: self.runtime.strategy("load", path=path), self._loaded_classes)

    def _loaded_classes(self, result):
        names = result if isinstance(result, list) else (result or {}).get("classes", [])
        for name in names:
            if self.strategy_class.findText(name) < 0:
                self.strategy_class.addItem(name)
        self.class_parameters()

    def class_parameters(self, _index=None):
        name = self.strategy_class.currentText()
        if name:
            self._submit("读取默认参数", lambda: self.runtime.strategy("class_parameters", class_name=name),
                         lambda result: self.parameters.setPlainText(json.dumps(result, ensure_ascii=False, indent=2, default=str)))

    def import_history(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入历史 K 线：datetime,open,high,low,close,volume", "", "K 线 CSV (*.csv)")
        if path:
            symbol = self.strategy_symbol.text().strip()
            interval = self.history_interval.currentData()
            self._submit("导入历史 K 线", lambda: self.runtime.strategy("import_history", path=path, symbol=symbol, interval=interval))

    def _parameters(self):
        try:
            value = json.loads(self.parameters.toPlainText())
            if not isinstance(value, dict):
                raise ValueError("参数必须是 JSON 对象")
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "参数格式", str(exc))
            return None
        return value

    def add_strategy(self):
        parameters = self._parameters()
        if parameters is None:
            return
        fields = dict(class_name=self.strategy_class.currentText(), name=self.strategy_name.text().strip(),
                      symbol=self.strategy_symbol.text().strip(), parameters=parameters)
        self._submit("添加策略", lambda: self.runtime.strategy("add", **fields))

    def edit_strategy(self):
        name = self._selected_strategy()
        parameters = self._parameters()
        if name and parameters is not None:
            self._submit("保存参数", lambda: self.runtime.strategy("edit", name=name, parameters=parameters))

    def _selected_strategy(self):
        row = self.strategy_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "选择策略", "请先选择策略实例。")
            return None
        return self.strategy_table.item(row, 0).text()

    def strategy_action(self, action):
        name = self._selected_strategy()
        if not name:
            return
        if action == "start" and not self.confirm("启动策略", f"启动 {name} 后，策略可以向当前柜台账户自动发送委托。确认启动？"):
            return
        if action == "remove" and not self.confirm("移除策略", f"移除实例 {name} 的配置？策略源码文件保持原位置。"):
            return
        self._submit("策略 " + action, lambda: self.runtime.strategy(action, name=name))

    def stop_all_strategies(self):
        self._submit("停止全部策略", lambda: self.runtime.strategy("stop_all"))

    def _select_strategy(self):
        row = self.strategy_table.currentRow()
        if row < 0:
            return
        name = self.strategy_table.item(row, 0).text()
        item = next((s for s in self.state.get("strategies", []) if s.get("name", s.get("strategy_name")) == name), None)
        if item:
            if item.get("source"):
                self.strategy_path.setText(item["source"])
            self.strategy_name.setText(name)
            self.strategy_symbol.setText(item.get("vt_symbol", item.get("symbol", "")))
            classname = item.get("class_name", "")
            if classname and self.strategy_class.findText(classname) < 0:
                self.strategy_class.addItem(classname)
            self.strategy_class.setCurrentText(classname)
            self.parameters.setPlainText(json.dumps(item.get("parameters", {}), ensure_ascii=False, indent=2, default=str))
            self._render_strategy_detail(item)

    def _render_strategy_detail(self, item):
        self.strategy_detail.setPlainText(json.dumps({"变量": item.get("variables", {}), "错误": item.get("error", "")},
                                                   ensure_ascii=False, indent=2, default=str))

    def _fill_table(self, table, rows):
        values = tuple(tuple(_value(value) for value in row) for row in rows)
        if self._table_values.get(table) == values:
            return
        self._table_values[table] = values
        selected = table.item(table.currentRow(), 0).text() if table.currentRow() >= 0 else None
        table.blockSignals(True)
        table.setUpdatesEnabled(False)
        table.setRowCount(len(values))
        selected_row = None
        for row, cells in enumerate(values):
            for column, value in enumerate(cells):
                table.setItem(row, column, QTableWidgetItem(value))
            if cells and cells[0] == selected:
                selected_row = row
        if selected_row is not None:
            table.selectRow(selected_row)
        table.setUpdatesEnabled(True)
        table.blockSignals(False)

    def _render_catalog(self):
        search = self.search.text().strip().casefold()
        ticks = {tick["vt_symbol"]: tick for tick in self.state.get("ticks", [])}
        contracts = self.state.get("contracts", [])
        matches = [c for c in contracts if search in (c["vt_symbol"] + " " + c.get("name", "")).casefold()]
        rows = []
        for contract in matches[:300]:
            symbol = contract["vt_symbol"]
            tick = ticks.get(symbol, {})
            timestamp = tick.get("timestamp")
            stamp = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S") if isinstance(timestamp, (int, float)) and 0 < timestamp < 1e11 else "未订阅 / 等待报价"
            rows.append((symbol, contract.get("name", ""), tick.get("last_price"), tick.get("bid_price"), tick.get("ask_price"), stamp))
        self._fill_table(self.contract_table, rows)
        self.catalog_count.setText(f"匹配 {len(matches)} / {len(contracts)} 个合约；显示前 300 个，双击带入下单区")

    def _choose_contract(self, item):
        symbol = self.contract_table.item(item.row(), 0).text()
        self.symbol.setText(symbol)
        self._set_price_step()

    def _set_price_step(self):
        contract = next((c for c in self.state.get("contracts", []) if c["vt_symbol"] == self.symbol.text().strip()), None)
        if contract and contract.get("pricetick", 0) > 0:
            self.price.setSingleStep(contract["pricetick"])

    def _render(self):
        state = self.state
        connection = state.get("connection") or {}
        if not self._busy and not self._closing:
            label = "已允许下单" if state.get("enabled") else "禁止新单"
            self.status.setText(f"{label} · {connection.get('trading_day', '')} · {connection.get('reason', '未连接')}")
        account = state.get("account") or {}
        self.account_label.setText("   ".join(label + " " + _value(account.get(key)) for label, key in
                                            (("资金", "balance"), ("可用", "available"), ("保证金", "margin"), ("冻结保证金", "frozen_margin"), ("手续费", "commission"))))
        symbols = tuple(c["vt_symbol"] for c in state.get("contracts", []))
        if symbols != self._catalog_symbols:
            self._catalog_symbols = symbols
            completer = QCompleter(list(symbols), self.symbol)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            self.symbol.setCompleter(completer)
        self._render_catalog()
        self._fill_table(self.orders, [tuple(o.get(k) for k in ("order_id", "vt_symbol", "side", "offset", "price", "volume", "traded", "status", "source")) for o in state.get("orders", [])])
        self._fill_table(self.positions, [tuple(p.get(k) for k in ("vt_symbol", "side", "volume", "yesterday", "frozen", "average_price", "position_pnl")) for p in state.get("positions", [])])
        self._fill_table(self.trades, [tuple(t.get(k) for k in ("trade_id", "order_id", "vt_symbol", "side", "offset", "price", "volume")) for t in state.get("trades", [])])
        self._fill_table(self.unresolved, [tuple(r.get(k) for k in ("client_id", "day", "state", "order_id")) for r in state.get("unresolved", [])])
        strategy_rows = state.get("strategies", [])
        self._fill_table(self.strategy_table, [(s.get("name", s.get("strategy_name")), s.get("class_name"), s.get("vt_symbol", s.get("symbol")),
                                                s.get("loaded", True), s.get("inited"), s.get("trading"), s.get("pos", s.get("variables", {}).get("pos")), s.get("error")) for s in strategy_rows])
        current = self.strategy_table.currentRow()
        if current >= 0:
            name = self.strategy_table.item(current, 0).text()
            item = next((s for s in strategy_rows if s.get("name", s.get("strategy_name")) == name), None)
            if item:
                self._render_strategy_detail(item)
        lines = []
        for line in state.get("logs", [])[-500:]:
            if isinstance(line, dict):
                stamp = line.get("time")
                prefix = datetime.fromtimestamp(stamp).strftime("%H:%M:%S") + " " if isinstance(stamp, (int, float)) else ""
                lines.append(prefix + str(line.get("message", line)))
            else:
                lines.append(str(line))
        text = "\n".join(lines)
        if self.logs.toPlainText() != text:
            bottom = self.logs.verticalScrollBar().value() >= self.logs.verticalScrollBar().maximum() - 4
            self.logs.setPlainText(text)
            if bottom:
                self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())

    def closeEvent(self, event: QCloseEvent):
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        if self._closing or not self.confirm("退出望舒 Lite", "退出将停止策略、禁止新单并断开柜台。撤单和成交以柜台回报为准，请核对活动委托。确认退出？"):
            return
        self._closing = True
        self.timer.stop()
        self._apply_enabled()
        self.status.setText("正在停止策略并关闭交易会话…")
        self.requested.emit("close", self.runtime.close)


def main(argv=None):
    from .runtime import Runtime, default_directory
    parser = argparse.ArgumentParser(description="望舒 Lite 桌面交易")
    parser.add_argument("--data-dir", type=Path, default=default_directory())
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("WangshuLite")
    app.setOrganizationName("Wangshu")
    runtime = Runtime(args.data_dir)
    window = TradingWindow(runtime)
    window.show()
    try:
        return app.exec()
    finally:
        if window._thread.isRunning():
            # A direct event-loop exit can bypass window.closeEvent().
            window.timer.stop()
            window.requested.emit("finalize", runtime.close)
            window._thread.wait()
