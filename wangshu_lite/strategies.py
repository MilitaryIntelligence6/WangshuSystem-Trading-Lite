"""Official vn.py CTA engine with a single Lite execution path.

Only a user-selected external module is imported. No strategy folder discovery,
default example strategies, source copying, or automatic restart is performed.
External Python is trusted local code, not a security sandbox.
"""
from collections import defaultdict
from concurrent.futures import Future, TimeoutError
from copy import deepcopy
from datetime import datetime
import importlib.util
import importlib
import inspect
import json
from pathlib import Path
import sys
from threading import current_thread, Lock
import traceback
from uuid import uuid4

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Product, Status
from vnpy.trader.converter import OffsetConverter
from vnpy.trader.engine import BaseEngine
from vnpy.trader.object import ContractData, OrderData, PositionData, TickData, TradeData
from vnpy_ctastrategy import CtaEngine, CtaTemplate, TargetPosTemplate
from vnpy_ctastrategy.base import APP_NAME

from .history import BarHistory, TZ
from .models import OrderIntent


COMMAND = "eLiteCtaCommand"
DOMAIN = "eLiteCtaDomain"
OFFSETS = {"OPEN": Offset.OPEN, "CLOSE": Offset.CLOSE,
           "CLOSE_TODAY": Offset.CLOSETODAY, "CLOSE_YESTERDAY": Offset.CLOSEYESTERDAY}
STATUSES = {"SUBMITTING": Status.SUBMITTING, "ACCEPTED": Status.NOTTRADED,
            "WORKING": Status.NOTTRADED, "PARTIAL": Status.PARTTRADED,
            "FILLED": Status.ALLTRADED, "CANCELLED": Status.CANCELLED, "REJECTED": Status.REJECTED}


def parts(symbol):
    name, exchange = symbol.rsplit(".", 1)
    return name, Exchange(exchange)


def native_order(order):
    gateway, orderid = order.order_id.split(".", 1)
    symbol, exchange = parts(order.vt_symbol)
    return OrderData(gateway_name=gateway, symbol=symbol, exchange=exchange, orderid=orderid,
                     type=OrderType[order.order_type], direction=Direction.LONG if order.side == "BUY" else Direction.SHORT,
                     offset=OFFSETS[order.offset], price=order.price, volume=order.volume,
                     traded=order.traded, status=STATUSES[order.status])


def native_trade(trade):
    gateway, orderid = trade.order_id.split(".", 1)
    symbol, exchange = parts(trade.vt_symbol)
    return TradeData(gateway_name=gateway, symbol=symbol, exchange=exchange, orderid=orderid,
                     tradeid=trade.trade_id, direction=Direction.LONG if trade.side == "BUY" else Direction.SHORT,
                     offset=OFFSETS[trade.offset], price=trade.price, volume=trade.volume, datetime=datetime.now(TZ))


class _Bridge:
    """The small MainEngine surface used by official CtaEngine/OffsetConverter."""
    def __init__(self, manager):
        self.manager, self.kernel = manager, manager.kernel
        self.converter = OffsetConverter(self)

    def get_contract(self, vt_symbol):
        value = self.kernel.contracts.get(vt_symbol)
        if value is None:
            return None
        symbol, exchange = parts(vt_symbol)
        return ContractData(gateway_name="LITE", symbol=symbol, exchange=exchange, name=value.name,
                            product=Product[value.product], size=value.size, pricetick=value.pricetick,
                            min_volume=value.min_volume, stop_supported=False)

    def get_order(self, orderid):
        value = self.kernel.orders.get(orderid)
        return native_order(value) if value else None

    def send_order(self, req, gateway):
        source = req.reference.removeprefix(APP_NAME + "_")
        if not float(req.volume).is_integer():
            raise ValueError("CTP 手数必须为整数")
        intent = OrderIntent(uuid4().hex, req.vt_symbol,
                             "BUY" if req.direction == Direction.LONG else "SELL",
                             {value: key for key, value in OFFSETS.items()}[req.offset],
                             int(req.volume), req.price, req.type.name)
        return self.kernel.send_order(intent, source="strategy:" + source)

    def cancel_order(self, req, gateway):
        self.kernel.cancel(gateway + "." + req.orderid)

    def subscribe(self, req, gateway):
        self.kernel.subscribe(req.vt_symbol)

    def convert_order_request(self, req, gateway, lock, net):
        converted = self.converter.convert_order_request(req, lock, net)
        if not converted:
            raise ValueError("没有可转换的平仓数量，请刷新柜台持仓并核对平今/平昨")
        return converted

    def update_order_request(self, req, orderid, gateway):
        self.converter.update_order_request(req, orderid)


class _LiteCtaEngine(CtaEngine):
    """CtaEngine 1.4.1 with explicit local storage and no plugin discovery.

    Initialize the upstream runtime fields explicitly to avoid get_database()
    loading a global database plugin or files from the user's home directory.
    The order, stop-order and trade-event implementations are the official ones.
    """
    def __init__(self, manager, event_engine):
        self.manager = manager
        bridge = _Bridge(manager)
        BaseEngine.__init__(self, bridge, event_engine, APP_NAME)
        self.strategy_setting, self.strategy_data = {}, {}
        self.classes, self.strategies = {}, {}
        self.symbol_strategy_map = defaultdict(list)
        self.orderid_strategy_map = {}
        self.strategy_orderid_map = defaultdict(set)
        self.stop_order_count, self.stop_orders = 0, {}
        self.vt_tradeids = set(manager.saved.get("trades", []))

    def write_log(self, msg, strategy=None):
        name = strategy.strategy_name if strategy else "CTA"
        self.manager.log(f"[{name}] {msg}")

    def update_strategy_setting(self, name, setting):
        strategy = self.strategies[name]
        self.strategy_setting[name] = {"class_name": type(strategy).__name__, "vt_symbol": strategy.vt_symbol,
                                       "setting": strategy.get_parameters(), "source": self.manager.class_sources.get(type(strategy).__name__, "")}
        self.manager.persist()

    def remove_strategy_setting(self, name):
        self.strategy_setting.pop(name, None)
        self.strategy_data.pop(name, None)
        self.manager.persist()

    def sync_strategy_data(self, strategy):
        self.strategy_data[strategy.strategy_name] = {key: value for key, value in strategy.get_variables().items()
                                                       if key not in {"inited", "trading"}}
        self.manager.persist()

    def put_strategy_event(self, strategy):
        pass  # UI reads manager.snapshot(); no second UI event bus.

    def call_strategy_func(self, strategy, func, params=None):
        try:
            func() if params is None else func(params)
        except Exception:
            strategy.trading = strategy.inited = False
            self.manager.errors[strategy.strategy_name] = self.manager.kernel.redact(traceback.format_exc())
            self.write_log("回调异常，策略已停止：\n" + self.manager.errors[strategy.strategy_name], strategy)
            self.manager.cancel_strategy(strategy)
            self.sync_strategy_data(strategy)

    def send_order(self, strategy, *args, **kwargs):
        if not strategy.trading or not self.manager.kernel.enabled:
            return []
        try:
            ids = super().send_order(strategy, *args, **kwargs)
            self.manager.persist()
            return ids
        except Exception as exc:
            self.write_log(f"委托未完成：{exc}", strategy)
            raise

    def load_bar(self, vt_symbol, days, interval, callback, use_database):
        bars = self.manager.history.load(vt_symbol, days, interval)
        self.write_log(f"加载 {vt_symbol} {len(bars)} 根真实本地历史 K 线")
        return bars

    def load_tick(self, vt_symbol, days, callback):
        raise ValueError("Lite 当前提供 CSV K 线历史；不提供历史 Tick，使用 load_tick 的策略需要接入真实 Tick 历史后才能初始化")

    def send_email(self, msg, strategy=None):
        raise ValueError("Lite 未配置邮件发送服务")


class StrategyManager:
    def __init__(self, kernel, directory):
        self.kernel = kernel
        self.saved = json.loads(kernel.journal.get("cta_state") or "{}")
        self.class_sources, self.errors = {}, {}
        self._converted_trades = set()
        self._view_lock, self._published = Lock(), []
        self._request = None
        self._closed = False
        self.history = BarHistory(directory)
        self.events = EventEngine()
        self.engine = _LiteCtaEngine(self, self.events)
        self.engine.strategy_data = dict(self.saved.get("data", {}))
        self.events.register(COMMAND, self._command)
        self.events.register(DOMAIN, self._domain)
        self._publish()
        self.events.start()
        future = Future()
        with self.kernel._lock:
            initial_positions = list(kernel.positions)
            initial_orders = list(kernel.orders.values())
            initial_trades = list(kernel.trades.values())
            def seed():
                self._seed_positions(initial_positions, initial_orders)
                self._converted_trades.update(native_trade(trade).vt_tradeid for trade in initial_trades)
            self.events.put(Event(COMMAND, (seed, future)))
            self.kernel.add_listener(self._enqueue)
        future.result(timeout=10)

    def log(self, message):
        message = self.kernel.redact(message)
        try:
            self.kernel.journal.audit("strategy_log", message)
        except Exception:
            self._storage_failed()
        output = getattr(self.kernel, "output", None)
        if output:
            output(message)

    def _enqueue(self, kind, data):
        if not self._closed:
            self.events.put(Event(DOMAIN, (kind, data)))

    def _command(self, event):
        function, future = event.data
        if not future.set_running_or_notify_cancel():
            return
        self._request = future
        try:
            result = function()
            self._publish()
            future.set_result(result)
        except Exception as exc:
            self._publish()
            future.set_exception(exc)
        finally:
            self._request = None

    def _call(self, function):
        if self._closed:
            raise RuntimeError("策略引擎已经关闭")
        if current_thread() is self.events._thread:
            return function()
        future = Future()
        self.events.put(Event(COMMAND, (function, future)))
        try:
            return future.result(timeout=60)
        except TimeoutError:
            if future.done():
                raise
            future.lite_expired = True
            future.cancel()  # An expired queued start must never execute later.
            raise TimeoutError("策略回调超过 60 秒；请检查本地策略，未执行的排队操作已经取消") from None

    def _domain(self, event):
        kind, value = event.data
        try:
            converter = self.engine.main_engine.converter
            if kind == "tick":
                symbol, exchange = parts(value.vt_symbol)
                tick = TickData(gateway_name="LITE", symbol=symbol, exchange=exchange,
                                datetime=datetime.fromtimestamp(value.timestamp, TZ), last_price=value.last_price,
                                bid_price_1=value.bid_price, ask_price_1=value.ask_price,
                                limit_up=value.limit_up, limit_down=value.limit_down,
                                volume=value.volume, turnover=value.turnover, open_interest=value.open_interest)
                self.engine.process_tick_event(Event("tick", tick))
            elif kind == "order":
                order = native_order(value)
                converter.update_order(order)
                self.engine.process_order_event(Event("order", order))
            elif kind == "trade":
                trade = native_trade(value)
                if trade.vt_tradeid not in self._converted_trades:
                    converter.update_trade(trade)
                    self._converted_trades.add(trade.vt_tradeid)
                if trade.vt_orderid in self.engine.orderid_strategy_map and trade.vt_tradeid not in self.engine.vt_tradeids:
                    self.engine.process_trade_event(Event("trade", trade))
                    self.persist()
            elif kind == "positions":
                self._seed_positions(value)
            elif (kind == "connection" and not value.ready) or (kind == "enabled" and value is False):
                for name in list(self.engine.strategies):
                    self._stop(name)
        except Exception:
            self.log("CTA 事件处理异常，已停止所有策略：\n" + traceback.format_exc())
            for name in list(self.engine.strategies):
                try:
                    self._stop(name)
                except Exception:
                    self._storage_failed()
        finally:
            self._publish()

    def _storage_failed(self):
        with self.kernel._lock:
            self.kernel.enabled, self.kernel.fatal = False, True
        for strategy in self.engine.strategies.values():
            strategy.trading = False
            self.errors[strategy.strategy_name] = "策略运行记录写入失败，已停止新单；请核对柜台"

    def _seed_positions(self, positions, orders=None):
        converter = self.engine.main_engine.converter
        converter.holdings.clear()
        for position in positions:
            try:
                symbol, exchange = parts(position.vt_symbol)
            except ValueError:
                continue
            converter.update_position(PositionData(gateway_name="LITE", symbol=symbol, exchange=exchange,
                direction=Direction.LONG if position.side == "LONG" else Direction.SHORT,
                volume=position.volume, yd_volume=position.yesterday, frozen=position.frozen))
        if orders is None:
            with self.kernel._lock:
                orders = list(self.kernel.orders.values())
        for order in orders:
            converter.update_order(native_order(order))

    def persist(self):
        settings = dict(self.saved.get("settings", {}))
        settings.update(self.engine.strategy_setting)
        data = dict(self.saved.get("data", {}))
        data.update(self.engine.strategy_data)
        orders = dict(self.saved.get("orders", {}))
        orders.update({oid: strategy.strategy_name for oid, strategy in self.engine.orderid_strategy_map.items()})
        state = {"settings": settings, "data": data, "orders": orders, "trades": sorted(self.engine.vt_tradeids)}
        try:
            self.kernel.journal.set("cta_state", json.dumps(state, ensure_ascii=False, allow_nan=False))
        except Exception:
            self._storage_failed()
            raise
        self.saved = state

    def load_file(self, path):
        def load():
            source = str(Path(path).expanduser().resolve())
            if any(strategy.trading for strategy in self.engine.strategies.values()):
                raise ValueError("请先停止策略再加载模块")
            if Path(source).suffix.lower() != ".py":
                raise ValueError("请选择外部 Python 策略模块")
            if Path(source).is_relative_to(Path(__file__).resolve().parents[1]):
                raise ValueError("策略源码必须保存在工程目录外")
            module_name = "wangshu_user_" + uuid4().hex
            spec = importlib.util.spec_from_file_location(module_name, source)
            if spec is None or spec.loader is None:
                raise ValueError("无法加载选定模块")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            return self._register_module(module, source)
        return self._call(load)

    def load_module(self, module_name):
        def load():
            if any(strategy.trading for strategy in self.engine.strategies.values()):
                raise ValueError("请先停止策略再加载模块")
            module = importlib.import_module(module_name)
            return self._register_module(module, "module:" + module_name)
        return self._call(load)

    def _register_module(self, module, source):
        classes = {value.__name__: value for value in vars(module).values()
                   if inspect.isclass(value) and issubclass(value, CtaTemplate)
                   and value not in {CtaTemplate, TargetPosTemplate} and value.__module__ == module.__name__}
        if not classes:
            raise ValueError("选定模块没有 CtaTemplate 策略类")
        for name in classes:
            prior = self.class_sources.get(name)
            if prior and prior != source:
                raise ValueError(f"策略类 {name} 与已加载模块重名")
        self.engine.classes.update(classes)
        self.class_sources.update({name: source for name in classes})
        for name, config in list(self.saved.get("settings", {}).items()):
            if name not in self.engine.strategies and config.get("source") == source and config["class_name"] in classes:
                self._add(config["class_name"], name, config["vt_symbol"], config["setting"])
        return sorted(classes)

    def class_parameters(self, class_name):
        return self._call(lambda: self.engine.classes[class_name].get_class_parameters())

    def _add(self, class_name, name, symbol, parameters):
        if not name or name in self.engine.strategies:
            raise ValueError("策略名称为空或已存在")
        cls = self.engine.classes[class_name]
        unknown = set(parameters) - set(cls.parameters)
        if unknown:
            raise ValueError("未知策略参数：" + ", ".join(sorted(unknown)))
        parts(symbol)
        self.engine.add_strategy(class_name, name, symbol, parameters)
        strategy = self.engine.strategies[name]
        for key, value in self.engine.strategy_data.get(name, {}).items():
            if key in strategy.variables and key not in {"inited", "trading"}:
                setattr(strategy, key, value)
        sources = self.kernel.journal.order_sources()
        orders = dict(self.saved.get("orders", {}))
        orders.update({oid: source.removeprefix("strategy:") for oid, source in sources.items() if source.startswith("strategy:")})
        for orderid, strategy_name in orders.items():
            if strategy_name == name:
                self.engine.orderid_strategy_map[orderid] = strategy
                order = self.kernel.orders.get(orderid)
                if order and order.active:
                    self.engine.strategy_orderid_map[name].add(orderid)
        with self.kernel._lock:
            known_trades = list(self.kernel.trades.values())
        for value in known_trades:
            trade = native_trade(value)
            if self.engine.orderid_strategy_map.get(trade.vt_orderid) is strategy and trade.vt_tradeid not in self.engine.vt_tradeids:
                self.engine.process_trade_event(Event("trade", trade))
        self.persist()

    def add(self, class_name, name, symbol, parameters):
        return self._call(lambda: self._add(class_name, name, symbol, parameters))

    def initialize(self, name):
        def initialize():
            strategy = self.engine.strategies[name]
            if strategy.trading or strategy.inited:
                raise ValueError("策略已初始化或正在运行")
            if strategy.vt_symbol not in self.kernel.contracts:
                raise ValueError("尚未收到策略合约信息，请先连接柜台并查询")
            self.errors.pop(name, None)
            self.engine.call_strategy_func(strategy, strategy.on_init)
            if name in self.errors:
                raise ValueError("策略初始化失败：" + self.errors[name])
            for key, value in self.engine.strategy_data.get(name, {}).items():
                if key in strategy.variables and key not in {"inited", "trading"}:
                    setattr(strategy, key, value)
            self.kernel.subscribe(strategy.vt_symbol)
            strategy.inited = True
            self.engine.sync_strategy_data(strategy)
        return self._call(initialize)

    def start(self, name):
        def start():
            strategy = self.engine.strategies[name]
            if not strategy.inited or strategy.trading:
                raise ValueError("请先初始化，且不要重复启动")
            if not self.kernel.enabled or not self.kernel.connection.ready:
                raise ValueError("请先连接柜台并允许交易")
            self.errors.pop(name, None)
            self.engine.call_strategy_func(strategy, strategy.on_start)
            if name in self.errors:
                raise ValueError("策略启动失败：" + self.errors[name])
            if self._request is not None and getattr(self._request, "lite_expired", False):
                self.cancel_strategy(strategy)
                raise TimeoutError("启动操作已超时，策略保持停止")
            strategy.trading = True
            self.engine.sync_strategy_data(strategy)
        return self._call(start)

    def cancel_strategy(self, strategy):
        ids = set(self.engine.strategy_orderid_map[strategy.strategy_name])
        ids.update(oid for oid, owner in self.engine.orderid_strategy_map.items() if owner is strategy and self.kernel.orders.get(oid) and self.kernel.orders[oid].active)
        for orderid in ids:
            try:
                self.engine.cancel_order(strategy, orderid)
            except Exception as exc:
                self.engine.write_log(f"撤单未确认 {orderid}：{exc}", strategy)

    def _stop(self, name):
        strategy = self.engine.strategies[name]
        was_trading, strategy.trading = strategy.trading, False
        if was_trading:
            self.engine.call_strategy_func(strategy, strategy.on_stop)
        self.cancel_strategy(strategy)
        self.engine.sync_strategy_data(strategy)

    def stop(self, name):
        return self._call(lambda: self._stop(name))

    def stop_all(self):
        return self._call(lambda: [self._stop(name) for name in list(self.engine.strategies)])

    def edit(self, name, parameters):
        def edit():
            strategy = self.engine.strategies[name]
            if strategy.trading:
                raise ValueError("请先停止策略再修改参数")
            if set(parameters) - set(strategy.parameters):
                raise ValueError("包含未知策略参数")
            self.engine.edit_strategy(name, parameters)
            strategy.inited = False
        return self._call(edit)

    def remove(self, name):
        def remove():
            strategy = self.engine.strategies.get(name)
            if strategy:
                if strategy.trading:
                    raise ValueError("请先停止策略")
                if any(owner is strategy and self.kernel.orders.get(oid) and self.kernel.orders[oid].active for oid, owner in self.engine.orderid_strategy_map.items()):
                    raise ValueError("仍有活动委托，请等待柜台撤单回报")
                self.engine.remove_strategy(name)
            self.saved.get("settings", {}).pop(name, None)
            self.saved.get("data", {}).pop(name, None)
            self.persist()
        return self._call(remove)

    def import_history(self, path, symbol, interval="1m"):
        return self._call(lambda: self.history.import_csv(path, symbol, interval))

    def _publish(self):
        rows = []
        for name, config in self.saved.get("settings", {}).items():
            strategy = self.engine.strategies.get(name)
            rows.append({"name": name, "class_name": config["class_name"], "vt_symbol": config["vt_symbol"],
                         "source": config.get("source", ""), "parameters": strategy.get_parameters() if strategy else config["setting"],
                         "variables": strategy.get_variables() if strategy else self.saved.get("data", {}).get(name, {}),
                         "loaded": strategy is not None, "inited": bool(strategy and strategy.inited),
                         "trading": bool(strategy and strategy.trading), "error": self.errors.get(name, ""),
                         "active_orders": len(self.engine.strategy_orderid_map[name]) if strategy else 0})
        with self._view_lock:
            self._published = deepcopy(rows)

    def snapshot(self):
        # A slow user callback cannot block UI state reads.
        with self._view_lock:
            return deepcopy(self._published)

    def close(self):
        if self._closed:
            return
        def close():
            for name in list(self.engine.strategies):
                self._stop(name)
        self._call(close)
        self._closed = True
        remove = getattr(self.kernel, "remove_listener", None)
        if remove:
            remove(self._enqueue)
        self.events.stop()
        self.history.close()
