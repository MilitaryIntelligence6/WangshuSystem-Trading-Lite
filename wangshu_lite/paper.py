"""Explicit offline broker exercise: synthetic quotes, 2-lot IOC depth and fees.

No broker connection, alpha, historical backtest or real exchange matching.
Committed broker records survive restart; callbacks follow the durable write.
"""
from __future__ import annotations

import copy
import json
import math
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from functools import wraps

from .models import Account, Connection, Contract, GatewayPreflightError, Order, Position, Snapshot, Tick, Trade

SYMBOL = "rb2610.SHFE"
SIZE = 10
FEE = 2.0
MARGIN = 0.1


def _mutation(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            previous = {key: copy.deepcopy(getattr(self, key)) for key in self._saved_fields}
            try:
                result, events = method(self, *args, **kwargs)
                self._save()
            except Exception:
                for key, value in previous.items():
                    setattr(self, key, value)
                raise
        # Never acquire the kernel lock while holding a gateway lock.
        for event in events:
            self.sink(*event)
        return result
    return call


class PaperGateway:
    _saved_fields = ("sequence", "trade_sequence", "realized", "commission", "orders", "trades", "positions", "revision")

    def __init__(self, sink, journal):
        self.sink, self.journal = sink, journal
        self._lock = threading.RLock()
        self.sequence = self.trade_sequence = self.revision = self.quotes = 0
        self.realized = self.commission = 0.0
        self.orders: dict[str, Order] = {}
        self.trades: dict[str, Trade] = {}
        self.positions: list[Position] = []
        self.subscriptions: set[str] = set()
        self.last_price = 3500.0
        self.day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
        saved = journal.get("paper_broker_state")
        if saved:
            value = json.loads(saved)
            if value.get("version") != 1:
                raise ValueError("离线账本版本不受支持，请保留记录并核查")
            self.day = value["day"]
            for name in ("sequence", "trade_sequence", "realized", "commission"):
                setattr(self, name, value[name])
            self.orders = {row["order_id"]: Order(**row) for row in value["orders"]}
            self.trades = {row["trade_id"]: Trade(**row) for row in value["trades"]}
            self.positions = [Position(**row) for row in value["positions"]]
        self.state = Connection(False, False, self.day, "PAPER 未连接", False, False)
        self.contracts = {SYMBOL: Contract(SYMBOL, SIZE, 1, max_volume=100, name="螺纹钢 · 合成演练样例")}

    def _save(self):
        value = {"version": 1, "day": self.day, "sequence": self.sequence,
                 "trade_sequence": self.trade_sequence, "realized": self.realized, "commission": self.commission,
                 "orders": [asdict(x) for x in self.orders.values()],
                 "trades": [asdict(x) for x in self.trades.values()],
                 "positions": [asdict(x) for x in self.positions]}
        self.journal.set("paper_broker_state", json.dumps(value, ensure_ascii=False, allow_nan=False))

    def health(self):
        with self._lock:
            return self.state

    def snapshot_revision(self):
        with self._lock:
            return self.revision

    def connect(self):
        with self._lock:
            self.state = Connection(True, True, self.day,
                "离线 PAPER：合成报价、2 手 IOC 深度、10% 模拟保证金及 2 元/手模拟费用", True, True)
            events = [("connection", self.state)] + [("contract", x) for x in self.contracts.values()]
            events += [("order", x) for x in self.orders.values()] + [("trade", x) for x in self.trades.values()]
        for event in events:
            self.sink(*event)
        self.subscribe(SYMBOL)
        self.query()

    def quote(self, symbol):
        if symbol not in self.contracts:
            raise ValueError("离线演练仅提供 rb2610.SHFE 合成样例")
        return Tick(symbol, self.last_price, self.last_price - 1, self.last_price + 1,
                    3850, 3150, time.time(), 3500, volume=self.quotes * 2,
                    turnover=self.quotes * 2 * self.last_price * SIZE, open_interest=10000)

    def subscribe(self, vt_symbol):
        with self._lock:
            tick = self.quote(vt_symbol)
            self.subscriptions.add(vt_symbol)
            connected = self.state.connected
        if connected:
            self.sink("tick", tick)

    def pulse(self):
        with self._lock:
            if not self.state.ready:
                return
            self.quotes += 1
            self.last_price = 3500 + round(6 * math.sin(self.quotes / 9))
            ticks = [self.quote(symbol) for symbol in sorted(self.subscriptions)]
        for tick in ticks:
            self.sink("tick", tick)
        self.query()

    @_mutation
    def _insert(self, intent, expected_revision):
        if not self.state.ready or expected_revision is not None and expected_revision != self.revision:
            raise GatewayPreflightError("离线柜台未连接或状态已变化")
        if intent.vt_symbol not in self.contracts or intent.order_type not in {"LIMIT", "FAK", "FOK"} or intent.condition_mode != "IMMEDIATE":
            raise GatewayPreflightError("离线柜台仅支持样例合约的 LIMIT / FAK / FOK 立即委托")
        if type(intent.volume) is not int or intent.volume <= 0 or not math.isfinite(intent.price) or intent.price <= 0:
            raise GatewayPreflightError("离线委托价格或手数无效")
        self.sequence += 1
        self.revision += 1
        oid = f"PAPER.1_1_{self.sequence}"
        order = Order(oid, intent.vt_symbol, intent.side, intent.offset, intent.volume, intent.price,
                      status="ACCEPTED", order_type=intent.order_type)
        self.orders[oid] = order
        return oid, [("order", order)]

    def send_order(self, intent, expected_revision=None):
        oid = self._insert(intent, expected_revision)
        if intent.order_type in {"FAK", "FOK"}:
            quote = self.quote(intent.vt_symbol)
            price = quote.ask_price if intent.side == "BUY" else quote.bid_price
            crosses = intent.price >= price if intent.side == "BUY" else intent.price <= price
            if crosses and (intent.order_type == "FAK" or intent.volume <= 2):
                self.fill(oid, min(intent.volume, 2), execution_price=price)
            if self.orders[oid].active:
                self.cancel_order(self.orders[oid])
        return oid

    @_mutation
    def _fill(self, order_id, volume=None, execution_price=None):
        if not self.state.ready:
            raise ValueError("离线柜台未连接")
        order = self.orders.get(order_id)
        if not order or not order.active:
            raise ValueError("请选择活动委托")
        remaining = order.volume - order.traded
        volume = remaining if volume is None else volume
        if type(volume) is not int or not 0 < volume <= remaining:
            raise ValueError("模拟成交数量无效")
        price = order.price if execution_price is None else execution_price
        if not math.isfinite(price) or price <= 0:
            raise ValueError("模拟成交价格无效")
        side = ("LONG" if order.side == "BUY" else "SHORT") if order.offset == "OPEN" else ("SHORT" if order.side == "BUY" else "LONG")
        prior = next((p for p in self.positions if p.vt_symbol == order.vt_symbol and p.side == side), None)
        if order.offset == "OPEN":
            total = (prior.volume if prior else 0) + volume
            average = ((prior.average_price or price) * prior.volume + price * volume) / total if prior else price
            position = Position(order.vt_symbol, side, total, prior.yesterday if prior else 0,
                                average_price=average, price_basis="paper_average")
        else:
            if not prior or volume > prior.volume:
                raise ValueError("模拟可平持仓不足")
            if order.offset == "CLOSE_TODAY" and volume > prior.volume - prior.yesterday:
                raise ValueError("模拟今仓不足")
            if order.offset == "CLOSE_YESTERDAY" and volume > prior.yesterday:
                raise ValueError("模拟昨仓不足")
            self.realized += (price - prior.average_price) * SIZE * volume * (1 if side == "LONG" else -1)
            yesterday = prior.yesterday - volume if order.offset == "CLOSE_YESTERDAY" else min(prior.yesterday, prior.volume - volume)
            position = replace(prior, volume=prior.volume - volume, yesterday=yesterday)
        self.positions = [p for p in self.positions if (p.vt_symbol, p.side) != (order.vt_symbol, side)]
        if position.volume:
            self.positions.append(position)
        self.commission += FEE * volume
        self.trade_sequence += 1
        self.revision += 1
        updated = replace(order, traded=order.traded + volume, status="FILLED" if volume == remaining else "PARTIAL")
        trade = Trade(f"PAPER.{self.day}.T{self.trade_sequence}", order_id, order.vt_symbol,
                      order.side, order.offset, volume, price)
        self.orders[order_id] = updated
        self.trades[trade.trade_id] = trade
        return None, [("order", updated), ("trade", trade)]

    def fill(self, order_id, volume=None, execution_price=None):
        self._fill(order_id, volume, execution_price)
        self.query()

    @_mutation
    def cancel_order(self, order):
        if not self.state.ready:
            raise ValueError("离线柜台未连接，撤单未发送")
        current = self.orders[order.order_id]
        if not current.active:
            raise ValueError("委托已经结束")
        self.revision += 1
        updated = replace(current, status="CANCELLED")
        self.orders[current.order_id] = updated
        return None, [("order", updated)]

    def query(self):
        with self._lock:
            if not self.state.ready:
                return
            positions = []
            for p in self.positions:
                pnl = (self.last_price - p.average_price) * SIZE * p.volume * (1 if p.side == "LONG" else -1)
                frozen = sum(o.volume - o.traded for o in self.orders.values() if o.active and o.offset != "OPEN"
                             and o.vt_symbol == p.vt_symbol and (o.side == "SELL") == (p.side == "LONG"))
                positions.append(replace(p, frozen=min(p.volume, frozen), position_pnl=pnl,
                                         margin=self.last_price * SIZE * MARGIN * p.volume))
            unrealized = sum(p.position_pnl for p in positions)
            margin = sum(p.margin for p in positions)
            frozen = sum((o.volume - o.traded) * o.price * SIZE * MARGIN for o in self.orders.values()
                         if o.active and o.offset == "OPEN")
            balance = 100000 + self.realized + unrealized - self.commission
            account = Account("PAPER", balance, balance - margin - frozen, balance - 100000, self.day,
                              margin=margin, frozen_margin=frozen, frozen_cash=0, frozen_commission=0,
                              commission=self.commission, position_pnl=unrealized, close_pnl=self.realized,
                              pre_balance=100000, deposit=0, withdraw=0, cash_in=0)
            events = [("account", Snapshot(account, self.revision)), ("positions", Snapshot(positions, self.revision))]
        for event in events:
            self.sink(*event)

    def close(self):
        with self._lock:
            self.state = Connection(False, False, self.day, "PAPER 已断开；未自动撤单", False, False)
            state = self.state
        self.sink("connection", state)
