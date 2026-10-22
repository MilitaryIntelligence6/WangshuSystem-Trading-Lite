"""Small execution path extracted from Wangshu: validate, record, send, reconcile.

No capital budget, loss cap, activity quota, valuation gate or UI dependency.
"""
from dataclasses import asdict
from decimal import Decimal
import json
import math
import re
import threading

from .models import Account, Connection, Contract, GatewayPreflightError, Order, OrderIntent, Position, Snapshot, Tick, Trade


class TradingError(RuntimeError):
    pass


class TradingKernel:
    def __init__(self, journal, output=print, expected_account="", secrets=()):
        self.journal, self.output = journal, output
        self.expected_account = expected_account
        self._secrets = tuple(sorted(filter(None, secrets), key=len, reverse=True))
        self._lock = threading.RLock()
        self._listeners = []
        self.gateway = None
        self.enabled = self.fatal = False
        self.connection = Connection(False, False)
        self.account = None
        self.positions = []
        self.contracts, self.ticks, self.orders, self.trades = {}, {}, {}, {}
        self.order_sources = journal.order_sources()

    def redact(self, text):
        for secret in self._secrets:
            text = str(text).replace(secret, "[已隐藏]")
        return str(text)

    def add_listener(self, callback):
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback):
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _notify(self, kind, data):
        # Listeners enqueue immutable events; strategy callbacks never run here.
        for callback in tuple(self._listeners):
            callback(kind, data)

    def attach(self, gateway):
        if self.gateway is not None:
            raise TradingError("只允许一个柜台连接")
        self.gateway = gateway

    def _audit(self, kind, detail):
        try:
            self.journal.audit(kind, detail)
        except Exception:
            self.enabled, self.fatal = False, True
            self._notify("enabled", False)
            raise TradingError("交易记录写入失败，已停止新单；仍可尝试撤单") from None

    def on_event(self, kind, data):
        with self._lock:
            try:
                if isinstance(data, Snapshot):
                    if self.gateway and data.revision != self.gateway.snapshot_revision():
                        return
                    data = data.data
                if kind == "connection":
                    changed_day = self.connection.trading_day and self.connection.trading_day != data.trading_day
                    if not data.ready or changed_day:
                        self.enabled = False
                        self._notify("enabled", False)
                    if data.trader_connected is False or changed_day:
                        self.contracts.clear()
                        self.ticks.clear()
                    self.connection = data
                elif kind == "contract":
                    self.contracts[data.vt_symbol] = data
                elif kind == "tick":
                    self.ticks[data.vt_symbol] = data
                elif kind == "account":
                    if self.expected_account and data.account_id != self.expected_account:
                        raise TradingError("柜台账户与连接账户不匹配")
                    if not all(math.isfinite(v) for v in (data.balance, data.available, data.daily_pnl)):
                        raise TradingError("柜台资金字段无效")
                    self.account = data
                elif kind == "positions":
                    if not isinstance(data, list) or not all(isinstance(p, Position) and type(p.volume) is int and p.volume >= 0 for p in data):
                        raise TradingError("柜台持仓回报无效")
                    self.positions = list(data)
                elif kind == "order":
                    prior = self.orders.get(data.order_id)
                    if prior and (data.traded < prior.traded or not prior.active and data.active):
                        return
                    self.orders[data.order_id] = data
                    if not data.active:
                        self.journal.terminal(data.order_id)
                    self._audit(kind, asdict(data))
                elif kind == "trade":
                    key = self.connection.trading_day + ":" + data.trade_id
                    if key in self.trades:
                        return
                    self.trades[key] = data
                    self._audit(kind, asdict(data))
                elif kind in {"log", "error"}:
                    data = self.redact(data)
                    self.output(data)
                self._notify(kind, data)
            except Exception as exc:
                self.enabled, self.fatal = False, True
                self.output(self.redact(f"回报处理异常，已停止新单：{exc}"))
                try:
                    self._notify("enabled", False)
                except Exception:
                    pass

    def _ready(self):
        health = self.gateway.health() if self.gateway else None
        if not health or not self.connection.ready or not health.ready:
            raise TradingError("柜台未完成交易登录与结算确认")
        if health.trading_day != self.connection.trading_day or not re.fullmatch(r"[0-9]{8}", health.trading_day):
            self.enabled = False
            self._notify("enabled", False)
            raise TradingError("柜台交易日已变化，请等待回报并核对后重新允许下单")

    def enable(self):
        with self._lock:
            self._ready()
            if self.fatal:
                raise TradingError("交易记录或回报故障，请核对并重新连接")
            if self.unresolved():
                raise TradingError("存在结果未核实的发送记录，请先与柜台核对")
            self._audit("enabled", True)
            self.enabled = True
            self._notify("enabled", True)

    def disable(self):
        with self._lock:
            self.enabled = False
            self._notify("enabled", False)
            self._audit("enabled", False)

    def subscribe(self, symbol):
        if symbol not in self.contracts:
            raise TradingError("请选择柜台已返回的准确合约代码")
        self.gateway.subscribe(symbol)

    def query(self):
        if self.gateway:
            self.gateway.query()

    def _validate(self, req):
        self._ready()
        if not self.enabled or self.fatal:
            raise TradingError("请先允许下单")
        if not isinstance(req, OrderIntent) or not isinstance(req.client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", req.client_id):
            raise TradingError("委托请求编号无效")
        if self.journal.exists(req.client_id):
            raise TradingError("请求编号已使用，不重复发送")
        if self.unresolved():
            raise TradingError("上次发送结果未核实，禁止自动重发或继续报单")
        c = self.contracts.get(req.vt_symbol)
        if not c or c.product not in {"FUTURES", "OPTION"}:
            raise TradingError("未收到可交易的期货或期权合约信息")
        if req.side not in {"BUY", "SELL"} or req.offset not in {"OPEN", "CLOSE", "CLOSE_TODAY", "CLOSE_YESTERDAY"}:
            raise TradingError("买卖或开平字段无效")
        if c.product == "OPTION" and ((req.offset == "OPEN" and req.side != "BUY") or (req.offset != "OPEN" and req.side != "SELL")):
            raise TradingError("Lite 当前仅保留期权买方的买开、卖平")
        if type(req.volume) is not int or not max(1, c.min_volume) <= req.volume <= 2_147_483_647 or c.max_volume and req.volume > c.max_volume:
            raise TradingError("手数必须为正整数，并符合柜台合约数量范围")
        if req.order_type not in {"LIMIT", "FAK", "FOK"} or req.condition_mode != "IMMEDIATE" or req.trigger_price or req.comparator:
            raise TradingError("Lite 支持立即执行的 LIMIT、FAK、FOK 委托")
        if type(req.price) not in (int, float) or not math.isfinite(req.price) or not 0 < req.price < 1e100:
            raise TradingError("价格必须为有限正数")
        if not math.isfinite(c.pricetick) or c.pricetick <= 0 or Decimal(str(req.price)) % Decimal(str(c.pricetick)):
            raise TradingError("价格不符合合约最小变动价位")
        exchange = req.vt_symbol.rsplit(".", 1)[1]
        if req.offset != "OPEN":
            if exchange in {"SHFE", "INE"} and req.offset == "CLOSE":
                raise TradingError("该交易所需明确选择平今或平昨")
            if exchange not in {"SHFE", "INE"} and req.offset != "CLOSE":
                raise TradingError("该交易所使用普通平仓，由柜台处理顺序")

    def send_order(self, req, source="manual"):
        with self._lock:
            self._validate(req)
            try:
                self.journal.reserve(req, self.connection.trading_day, source)
            except Exception:
                self.enabled, self.fatal = False, True
                self._notify("enabled", False)
                raise TradingError("发送记录保存失败，本次未发单") from None
            try:
                oid = self.gateway.send_order(req, None)
                if not oid:
                    raise RuntimeError("柜台未返回本地委托编号")
                self.journal.bind(req.client_id, oid)
                self.order_sources[oid] = source
                if oid not in self.orders:
                    self.orders[oid] = Order(oid, req.vt_symbol, req.side, req.offset, req.volume, req.price, order_type=req.order_type)
                elif not self.orders[oid].active:
                    self.journal.terminal(oid)
                return oid
            except GatewayPreflightError as exc:
                try:
                    self.journal.not_sent(req.client_id)
                except Exception:
                    self.enabled, self.fatal = False, True
                    self._notify("enabled", False)
                raise TradingError(self.redact(f"发送前被拒绝，本次未发单：{exc}")) from None
            except Exception:
                self.enabled = False
                try:
                    self.journal.unknown(req.client_id)
                except Exception:
                    self.fatal = True
                self._notify("enabled", False)
                raise TradingError("发送结果不确定，请查询核对；本程序不会自动重发") from None

    def cancel(self, order_id):
        with self._lock:
            order = self.orders.get(order_id)
            if not order or not order.active:
                raise TradingError("请选择仍然活动的柜台委托")
            try:
                self._audit("cancel_requested", {"order_id": order_id})
            except TradingError:
                pass  # A failed local disk must not prevent a broker cancellation.
            self.gateway.cancel_order(order)

    def cancel_all(self):
        results = {}
        for order in list(self.orders.values()):
            if order.active:
                try:
                    self.cancel(order.order_id)
                    results[order.order_id] = "已请求，等待柜台回报"
                except Exception as exc:
                    results[order.order_id] = self.redact(str(exc))
        return results

    def unresolved(self):
        return [r for r in self.journal.intents(pending=True) if r["state"] in {"RESERVED", "UNKNOWN"} or r["order_id"] not in self.orders]

    def reconcile(self, client_id, order_id):
        with self._lock:
            row = next((r for r in self.unresolved() if r["client_id"] == client_id), None)
            order = self.orders.get(order_id)
            if not row or not order or row["day"] != self.connection.trading_day:
                raise TradingError("需要同交易日未核实记录与已收到的柜台委托")
            req = json.loads(row["request"])
            if any(req[k] != getattr(order, k) for k in ("vt_symbol", "side", "offset", "volume", "price", "order_type")):
                raise TradingError("请求与柜台委托字段不一致")
            self.journal.bind(client_id, order_id)
            self.order_sources[order_id] = row["source"]
            if not order.active:
                self.journal.terminal(order_id)
            self._audit("reconcile", {"client_id": client_id, "order_id": order_id})

    def snapshot(self):
        with self._lock:
            facts = {"enabled": self.enabled, "fatal": self.fatal, "connection": self.connection,
                     "account": self.account, "positions": list(self.positions),
                     "contracts": list(self.contracts.values()), "ticks": list(self.ticks.values()),
                     "orders": list(self.orders.values()), "trades": list(self.trades.values())[-500:]}
            sources = dict(self.order_sources)
            unresolved = self.unresolved()
        # Frozen domain objects are copied under the lock; presentation runs outside.
        result = {key: ([asdict(v) for v in value] if isinstance(value, list) else asdict(value) if hasattr(value, "__dataclass_fields__") else value)
                  for key, value in facts.items()}
        for row in result["orders"]:
            row["source"] = sources.get(row["order_id"], "external")
        result["unresolved"] = [dict(r, request=json.loads(r["request"])) for r in unresolved]
        return result

    def close(self):
        self.enabled = False
        self._notify("enabled", False)
        if self.gateway:
            self.gateway.close()
