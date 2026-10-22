"""Single-account CTP bridge, using the official vnpy_ctp native bindings.

No native library is imported by the offline simulator.  The small overrides
below are tied to Windows vnpy_ctp 6.7.11.4 and the official Mac 6.7.7.2
source revision pinned in pyproject.toml: upstream silently accepts a
failed settlement confirmation, blocks in an unbounded query retry, and omits
empty position snapshots.  Never infer transport state from translated logs.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from functools import wraps
from importlib import import_module, metadata
import inspect
import json
import logging
import math
import re
import socket
import sys
from threading import RLock
from time import monotonic
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from .models import Account, Connection, Contract, GatewayPreflightError, Order, OrderIntent, Position, Sink, Snapshot, Tick, Trade


GATEWAY_NAME = "WANGSHU"
EVENT_WANGSHU = "eWangshu"
QUERY_INTERVAL = 1.2
QUERY_TIMEOUT = 60.0
MAX_QUERY_ATTEMPTS = 3
LOG = logging.getLogger(__name__)


def is_dce_spread_position(value: str) -> bool:
    """Keep broker spread holdings visible without making them order symbols."""
    return isinstance(value, str) and len(value) <= 80 and bool(
        re.fullmatch(r"SP [A-Za-z0-9]+&[A-Za-z0-9]+\.DCE", value))


def _load_bindings() -> SimpleNamespace:
    """Check the audited interface before constructing any native API object."""
    try:
        ctp_version = metadata.version("vnpy_ctp")
        vnpy_version = metadata.version("vnpy")
        expected_ctp = "6.7.7.2" if sys.platform == "darwin" else "6.7.11.4"
        if ctp_version != expected_ctp or vnpy_version != "4.4.0":
            raise RuntimeError(f"CTP adapter requires vnpy 4.4.0 and vnpy_ctp {expected_ctp} on this platform")
        ctp = import_module("vnpy_ctp.gateway.ctp_gateway")
        events = import_module("vnpy.event")
        objects = import_module("vnpy.trader.object")
        constants = import_module("vnpy.trader.constant")
        base = import_module("vnpy.trader.gateway")
        timer = import_module("vnpy.trader.event").EVENT_TIMER
    except (ImportError, OSError, metadata.PackageNotFoundError) as exc:
        raise RuntimeError("CTP native dependencies are unavailable; install the documented CTP environment") from exc
    required = {
        "onRspSettlementInfoConfirm": ("self", "data", "error", "reqid", "last"),
        "onRspQryInvestorPosition": ("self", "data", "error", "reqid", "last"),
        "connect": ("self", "address", "userid", "password", "brokerid", "auth_code", "appid")
        + (() if expected_ctp == "6.7.7.2" else ("production_mode",)),
    }
    for name, parameters in required.items():
        if tuple(inspect.signature(getattr(ctp.CtpTdApi, name)).parameters) != parameters:
            raise RuntimeError(f"Unsupported vnpy_ctp interface: CtpTdApi.{name}")
    return SimpleNamespace(ctp=ctp, events=events, objects=objects, constants=constants,
                           BaseGateway=base.BaseGateway, timer=timer)


def check_dependencies() -> None:
    """Import and validate native bindings without initializing or connecting CTP."""
    _load_bindings()


@dataclass
class _Query:
    kind: str
    attempts: int = 0
    reqid: int = 0
    sent_at: float = 0.0
    revision: int = 0


class VnpyGateway:
    """One official gateway and one broker account, with fail-closed health.

    Connection.ready means the native connection is usable; it never enables
    trading in the core.  The core must latch its own manual arm state off on
    every disconnect/error, including a reconnect that later becomes ready.
    """

    def __init__(self, settings: dict[str, str], sink: Sink, *,
                 native_market_symbols: tuple[str, ...] = (),
                 server_condition_symbols: tuple[str, ...] = ()) -> None:
        self._settings = dict(settings)
        self._selected_endpoints: dict[str, str] = {}
        self.account_id = self._settings["用户名"]
        if not self.account_id or not self._settings.get("授权编码") or not self._settings.get("产品名称"):
            raise ValueError("CTP requires an account, broker-issued AppID and AuthCode")
        if sys.platform == "darwin" and self._settings.get("柜台环境") == "测试":
            raise ValueError("Mac CTP 6.7.7.2 cannot select the native test SDK mode; confirm the supported SDK mode with the broker")
        self._sink = sink
        self._native_market_symbols = frozenset(native_market_symbols)
        self._server_condition_symbols = frozenset(server_condition_symbols)
        self._lock = RLock()
        self._bindings = _load_bindings()
        self._engine = self._bindings.events.EventEngine()
        self._engine.register(EVENT_WANGSHU, self._dispatch)
        self._engine.register(self._bindings.timer, self._timer)
        self._started = False
        self._closed = False
        self._faulted = False
        self._fault_reason = ""
        self._td_login = self._md_login = self._authenticated = False
        self._settled = self._contracts_ready = False
        self._trading_day = ""
        self._health = Connection(False, False, reason="not connected")
        self._queue: deque[_Query] = deque()
        self._inflight: _Query | None = None
        self._last_query = float("-inf")
        self._last_refresh = monotonic()
        self._positions: dict[tuple[str, str], Position] = {}
        self._state_revision = 0
        self._order_reports: dict[str, Order] = {}
        self._trade_reports: set[str] = set()
        self._native_contracts: dict[str, Any] = {}
        self._contracts: dict[str, Contract] = {}
        self._subscriptions: set[str] = set()
        self._margin_rows: list[dict] = []
        self._pre_settlement_prices: dict[str, float | None] = {}
        self._market_volume_limits: dict[str, tuple[int, int]] = {}
        self._exchange_order_ids: dict[str, tuple[str, str]] = {}
        native_type = _gateway_type(self._bindings)
        self._native = native_type(self, self._engine)

    def _redact(self, message: str) -> str:
        for key in ("用户名", "密码", "授权编码"):
            secret = self._settings.get(key)
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message

    def _emit(self, kind: str, value: object) -> None:
        if kind in {"account", "positions"}:
            value = Snapshot(value, self._state_revision)
        if isinstance(value, str):
            value = self._redact(value)
        if not self._closed:
            self._engine.put(self._bindings.events.Event(EVENT_WANGSHU, (kind, value)))

    def _dispatch(self, event: Any) -> None:
        try:
            kind, value = event.data
            if isinstance(value, Snapshot):
                with self._lock:
                    if value.revision != self._state_revision:
                        self._enqueue(kind)
                        return
            # Never hold the adapter lock while calling the core's sink. The
            # core's send path takes its own lock before the adapter's lock.
            self._sink(*event.data)
        except Exception:
            LOG.exception("CTP event consumer failed")
            # Do not recursively publish into a broken sink or terminate the
            # vn.py event thread; health() remains synchronously fail closed.
            with self._lock:
                self._faulted = True
                self._fault_reason = self._fault_reason or "event consumer failed"
                self._health = Connection(self._td_login and self._md_login, False,
                                          self._trading_day, self._fault_reason, self._md_login, self._td_login)

    def _publish_health(self, reason: str) -> None:
        connected = self._td_login and self._md_login
        ready = bool(self._td_login and self._authenticated and self._settled and
                     self._trading_day and not self._faulted and not self._closed)
        if self._faulted:
            reason = self._fault_reason
        elif not ready and not self._closed:
            pending = [label for complete, label in (
                (self._authenticated, "客户端认证"), (self._td_login, "交易登录"),
                (self._settled, "结算确认"),
                (bool(self._trading_day), "柜台交易日"),
            ) if not complete]
            reason = "等待" + "、".join(pending) + ("；" + reason if reason else "")
        health = Connection(connected, ready, self._trading_day, reason, self._md_login, self._td_login)
        if health != self._health:
            self._health = health
            self._emit("connection", health)

    def _fault(self, message: str, *, detail: str | None = None) -> None:
        self._faulted = True
        self._fault_reason = self._fault_reason or message
        self._publish_health(message)
        self._emit("error", detail or message)

    @staticmethod
    def _ctp_record(callback: str, **fields: object) -> str:
        """Keep complete native callback fields in the local diagnostic log."""
        payload = {"callback": callback, **fields}
        return f"CTP raw callback: {json.dumps(payload, ensure_ascii=False, sort_keys=True, default=repr)}"

    def _report_ctp_error(self, callback: str, *, data: object = None, error: object = None,
                          reqid: object = None, last: object = None,
                          fault: str = "", note: str = "") -> None:
        fields = {"data": data, "error": error, "reqid": reqid, "last": last}
        record = self._ctp_record(callback, **fields)
        code = error.get("ErrorID", "unknown") if isinstance(error, dict) else "unknown"
        if fault:
            self._fault(f"{fault} (ErrorID={code})", detail=record)
        else:
            self._emit("error", f"{note}; {record}" if note else record)

    def health(self) -> Connection:
        with self._lock:
            return self._health

    def snapshot_revision(self) -> int:
        with self._lock:
            return self._state_revision

    def connect(self) -> None:
        with self._lock:
            if self._closed or self._started:
                raise RuntimeError("Gateway connection lifecycle cannot be started twice")
            self._started = True
            self._engine.start()
            try:
                settings = self._select_fronts(self._settings)
                self._native.connect(settings)
            except Exception:
                self._fault("CTP initialization failed; check installation and broker settings")
                raise RuntimeError("CTP initialization failed; check installation and broker settings") from None

    @staticmethod
    def _front_reachable(address: str, timeout: float = 1.5) -> bool:
        """Perform a read-only TCP probe before choosing a configured front.

        CTP's native ``connect`` starts asynchronously and normally does not
        raise when a front is down.  A short probe lets a configured backup be
        selected during startup without reconnecting an already active session.
        If both probes fail, retain the primary and let CTP return its native
        connection error instead of hiding a broker-side diagnostic.
        """
        try:
            parsed = urlsplit(address)
            if parsed.scheme != "tcp" or not parsed.hostname or not parsed.port:
                return False
            with socket.create_connection((parsed.hostname, parsed.port), timeout=timeout):
                return True
        except (OSError, ValueError):
            return False

    def _select_fronts(self, settings: dict[str, str]) -> dict[str, str]:
        selected = dict(settings)
        pair = settings.get("前置组", "")
        pair_label = f"第{pair}组" if pair in {"1", "2"} else "主"
        for service, primary_key, backup_key in (
            ("交易", "交易服务器", "交易服务器备选"),
            ("行情", "行情服务器", "行情服务器备选"),
        ):
            primary = settings[primary_key]
            backup = settings.get(backup_key, "").strip()
            address, label = primary, pair_label
            if backup and not self._front_reachable(primary):
                if self._front_reachable(backup):
                    address, label = backup, "备"
                else:
                    self._emit("log", f"CTP {service}主备前置均未通过网络探测，继续尝试主前置")
            selected[primary_key] = address
            self._selected_endpoints[service] = label
            self._emit("log", f"CTP {service}前置使用{label}地址")
        return selected

    def watch(self, vt_symbol: str) -> None:
        """Observe quotes; vn.py retains market subscriptions across reconnects."""
        with self._lock:
            symbol, exchange = self._symbol(vt_symbol)
            self._native.subscribe(self._bindings.objects.SubscribeRequest(symbol=symbol, exchange=exchange))

    def subscribe(self, vt_symbol: str) -> None:
        """Subscribe to quotes; optional margin queries are explicit diagnostics."""
        with self._lock:
            self.watch(vt_symbol)
            self._subscriptions.add(vt_symbol)

    def _symbol(self, vt_symbol: str) -> tuple[str, Any]:
        symbol, exchange = vt_symbol.rsplit(".", 1)
        if not symbol:
            raise ValueError("Contract symbol is empty")
        return symbol, self._bindings.constants.Exchange(exchange)

    def send_order(self, intent: OrderIntent, expected_revision: int | None) -> str:
        with self._lock:
            if not self._health.ready:
                raise GatewayPreflightError("CTP is not ready")
            if expected_revision is not None and expected_revision != self._state_revision:
                raise GatewayPreflightError("CTP account state changed; fresh account and position snapshots are required")
            self._validate_order(intent)
            # Never retry a mutating request, even when its acknowledgement is
            # absent: only reconciliation can determine its final result.
            self._changed()
            return self._native.td_api.send_intent(intent)

    def _validate_order(self, intent: OrderIntent) -> None:
        """Validate the wire capabilities again, independently of GUI/core."""
        if intent.order_type not in {"LIMIT", "MARKET", "FAK", "FOK"}:
            raise GatewayPreflightError("Unsupported CTP order type")
        if intent.condition_mode not in {"IMMEDIATE", "SERVER"}:
            raise GatewayPreflightError("Local conditions must be evaluated by the trading kernel")
        if intent.vt_symbol not in self._contracts:
            raise GatewayPreflightError("CTP contract has not been synchronized")
        if intent.side not in {"BUY", "SELL"} or intent.offset not in {"OPEN", "CLOSE", "CLOSE_TODAY", "CLOSE_YESTERDAY"}:
            raise GatewayPreflightError("Unsupported CTP order direction or offset")
        if type(intent.volume) is not int or intent.volume <= 0 or not math.isfinite(intent.price):
            raise GatewayPreflightError("Invalid CTP order quantity or price")
        if intent.order_type == "MARKET":
            if intent.vt_symbol not in self._native_market_symbols or intent.price != 0:
                raise GatewayPreflightError("Native market order is not approved for this contract or price is not zero")
            minimum, maximum = self._market_volume_limits.get(intent.vt_symbol, (0, 0))
            if minimum <= 0 or maximum <= 0 or not minimum <= intent.volume <= maximum:
                raise GatewayPreflightError("Native market quantity limits are missing or exceeded")
        elif intent.price <= 0:
            raise GatewayPreflightError("A positive limit price is required")
        if intent.condition_mode == "SERVER":
            if intent.vt_symbol not in self._server_condition_symbols or intent.order_type != "LIMIT":
                raise GatewayPreflightError("Server condition requires an approved contract and LIMIT order")
            if intent.comparator not in {"GE", "LE"} or not math.isfinite(intent.trigger_price) or intent.trigger_price <= 0:
                raise GatewayPreflightError("Invalid server trigger condition")
        elif intent.trigger_price != 0 or intent.comparator:
            raise GatewayPreflightError("Immediate orders cannot contain a trigger")

    def cancel_order(self, order: Order) -> None:
        with self._lock:
            if not self._td_login or self._closed:
                raise RuntimeError("CTP trader is disconnected")
            gateway, native_orderid = order.order_id.split(".", 1)
            if gateway != GATEWAY_NAME:
                raise ValueError("Order belongs to a different gateway")
            symbol, exchange = self._symbol(order.vt_symbol)
            # The broker-reported exchange id pair also addresses server-created
            # child orders whose FrontID/SessionID are zero. Do not accept a GUI
            # supplied sysid: only cache entries received through the native SPI.
            observed = self._exchange_order_ids.get(native_orderid)
            order_sys_id = observed[1] if observed and observed[0] == exchange.value else ""
            parts = native_orderid.split("_")
            if not order_sys_id and (len(parts) != 3 or not all(part.isdigit() for part in parts)
                                     or int(parts[0]) == int(parts[1]) == 0):
                raise ValueError("Invalid native order identifier")
            request = self._bindings.objects.CancelRequest(
                orderid=native_orderid, symbol=symbol, exchange=exchange)
            self._changed()
            self._native.td_api.cancel_order(request, order_sys_id=order_sys_id)

    def query(self) -> None:
        with self._lock:
            if not self._contracts_ready:
                self._enqueue("contracts")
            self._enqueue("account")
            self._enqueue("positions")

    def refresh_margin_rates(self) -> None:
        """Explicit retry/refresh through the same bounded serialized queue."""
        with self._lock:
            for symbol in sorted(self._subscriptions):
                if self._contracts.get(symbol) and self._contracts[symbol].product == "FUTURES":
                    self._enqueue("margin:" + symbol)

    def _enqueue(self, kind: str) -> None:
        if self._inflight and self._inflight.kind == kind:
            return
        if not any(query.kind == kind for query in self._queue):
            if kind in {"account", "positions"}:
                index = next((i for i, row in enumerate(self._queue) if row.kind.startswith("margin:")), len(self._queue))
                self._queue.insert(index, _Query(kind))
            else:
                self._queue.append(_Query(kind))

    def _revoke_margin(self, vt_symbol: str) -> None:
        contract = self._contracts.get(vt_symbol)
        if contract and contract.margin_source:
            contract = replace(contract, long_margin_by_money=None, long_margin_by_volume=None,
                               short_margin_by_money=None, short_margin_by_volume=None,
                               margin_source="", margin_trading_day="")
            self._contracts[vt_symbol] = contract
            self._changed()
            self._emit("contract", contract)

    def _current(self, kind: str, reqid: int) -> bool:
        return bool(self._td_login and self._inflight and self._inflight.kind == kind
                    and self._inflight.reqid == reqid)

    def _changed(self) -> None:
        self._state_revision += 1
        self.query()

    def _publish_order(self, order: Order) -> None:
        with self._lock:
            prior = self._order_reports.get(order.order_id)
            if order == prior:
                return
            self._order_reports[order.order_id] = order
            self._changed()
            self._emit("order", order)

    def _stale_snapshot(self, kind: str, reqid: int, last: bool) -> bool:
        if not self._current(kind, reqid):
            return True
        if self._inflight.revision != self._state_revision:
            self._positions.clear()
            # Drain the entire native response before issuing another query;
            # CTP permits only one outstanding query on this connection.
            if last:
                self._complete(kind, reqid)
                self._enqueue(kind)
            return True
        return False

    def _complete(self, kind: str, reqid: int) -> None:
        if self._current(kind, reqid):
            self._inflight = None

    def _query_failed(self, kind: str, message: str, *, detail: str | None = None) -> None:
        if kind.startswith("margin:"):
            self._margin_rows.clear()
            self._revoke_margin(kind.split(":", 1)[1])
            self._emit("log", message + "; optional margin data unavailable; funds approval remains with broker"
                       + ("; " + detail if detail else ""))
        else:
            self._emit("error", message + "; background query unavailable" + ("; " + detail if detail else ""))

    def _timer(self, event: Any = None) -> None:
        query_kind = ""
        try:
            with self._lock:
                if self._closed or self._faulted or not self._td_login or not self._settled:
                    return
                now = monotonic()
                self._native.md_api.update_date()
                if self._inflight:
                    if now - self._inflight.sent_at > QUERY_TIMEOUT:
                        kind = self._inflight.kind
                        self._inflight = None
                        self._positions.clear()
                        self._query_failed(kind, f"CTP {kind} query timed out")
                    elif self._inflight.kind == "contracts" and not self._contracts_ready:
                        self._publish_health("正在查询柜台合约")
                    return
                if now - self._last_refresh >= 10 and self._contracts_ready:
                    self.query()
                    self._last_refresh = now
                if not self._queue or now - self._last_query < QUERY_INTERVAL:
                    return
                query = self._queue.popleft()
                query_kind = query.kind
                if query.kind != "contracts" and not self._contracts_ready:
                    self._queue.append(query)
                    return
                td = self._native.td_api
                td.reqid += 1
                query.reqid = td.reqid
                query.attempts += 1
                query.sent_at = now
                query.revision = self._state_revision
                self._inflight = query
                self._last_query = now
                if query.kind == "contracts":
                    result = td.reqQryInstrument({}, query.reqid)
                elif query.kind == "account":
                    result = td.reqQryTradingAccount({}, query.reqid)
                elif query.kind.startswith("margin:"):
                    self._margin_rows.clear()
                    vt_symbol = query.kind.split(":", 1)[1]
                    symbol, exchange = self._symbol(vt_symbol)
                    result = td.reqQryInstrumentMarginRate({"BrokerID": td.brokerid, "InvestorID": td.userid,
                                "InstrumentID": symbol, "ExchangeID": exchange.value, "HedgeFlag": "1"}, query.reqid)
                else:
                    self._positions.clear()
                    result = td.reqQryInvestorPosition(
                        {"BrokerID": td.brokerid, "InvestorID": td.userid}, query.reqid)
                if result:
                    self._inflight = None
                    if result in {-2, -3} and query.attempts < MAX_QUERY_ATTEMPTS:
                        self._queue.appendleft(query)
                    else:
                        self._query_failed(query.kind, f"CTP {query.kind} request rejected: code {int(result)}")
        except Exception as exc:
            with self._lock:
                self._inflight = None
                self._positions.clear()
                self._query_failed(query_kind, f"CTP query callback failed ({type(exc).__name__})")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._td_login = self._md_login = False
            self._health = Connection(False, False, self._trading_day, "closed", False, False)
            self._queue.clear()
            self._inflight = None
        # Native exit joins its callback thread: never hold the callback lock.
        try:
            self._native.close()
        finally:
            if self._started:
                self._engine.stop()


def _guard_callbacks(cls: type) -> type:
    """Isolate every native SPI callback, including inherited vn.py callbacks."""
    for name in dir(cls):
        method = getattr(cls, name)
        if not name.startswith("on") or not callable(method):
            continue

        def wrap(callback: Any) -> Any:
            @wraps(callback)
            def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
                owner = self.gateway.owner
                with owner._lock:
                    if owner._closed:
                        return None
                    query_kind = owner._inflight.kind if owner._inflight else ""
                    try:
                        return callback(self, *args, **kwargs)
                    except Exception as exc:
                        LOG.exception("CTP callback failed; callback=%s", callback.__name__)
                        record = owner._ctp_record(
                            callback.__name__, args=args, kwargs=kwargs,
                            exception={"type": type(exc).__name__, "message": str(exc)},
                        )
                        message = f"CTP {callback.__name__} callback failed ({type(exc).__name__})"
                        if callback.__name__ in {"onRspQryInstrument", "onRspQryInstrumentMarginRate",
                                                 "onRspQryTradingAccount", "onRspQryInvestorPosition"} and query_kind:
                            owner._inflight = None
                            owner._positions.clear()
                            owner._query_failed(query_kind, message, detail=record)
                        else:
                            owner._fault(message, detail=record)
                        return None
            return guarded

        setattr(cls, name, wrap(method))
    return cls


def _gateway_type(bindings: SimpleNamespace) -> type:
    ctp = bindings.ctp

    @_guard_callbacks
    class Trader(ctp.CtpTdApi):
        def __init__(self, gateway: Any) -> None:
            super().__init__(gateway)
            self._reported_order_ids: set[str] = set()

        def send_intent(self, intent: OrderIntent) -> str:
            """One native call, with audited CTP wire values from its headers.

            AnyPrice/GFD in upstream is a different exchange instruction:
            this adapter's MARKET explicitly uses AnyPrice/IOC/AV.  A server
            condition is an actual CTP limit order, never a local fallback.
            """
            owner = self.gateway.owner
            owner._validate_order(intent)
            symbol, exchange = owner._symbol(intent.vt_symbol)
            self.order_ref += 1
            native_id = f"{self.frontid}_{self.sessionid}_{self.order_ref}"
            order_id = f"{GATEWAY_NAME}.{native_id}"
            order = Order(order_id, intent.vt_symbol, intent.side, intent.offset, intent.volume,
                          intent.price, order_type=intent.order_type, condition_mode=intent.condition_mode,
                          trigger_price=intent.trigger_price, comparator=intent.comparator)
            request = {
                "InstrumentID": symbol, "ExchangeID": exchange.value,
                "LimitPrice": intent.price, "VolumeTotalOriginal": intent.volume,
                "OrderPriceType": "1" if intent.order_type == "MARKET" else "2",
                "TimeCondition": "1" if intent.order_type in {"MARKET", "FAK", "FOK"} else "3",
                "VolumeCondition": "3" if intent.order_type == "FOK" else "1", "MinVolume": 1,
                "Direction": {"BUY": "0", "SELL": "1"}[intent.side],
                "CombOffsetFlag": {"OPEN": "0", "CLOSE": "1", "CLOSE_TODAY": "3", "CLOSE_YESTERDAY": "4"}[intent.offset],
                "OrderRef": str(self.order_ref), "InvestorID": self.userid, "UserID": self.userid,
                "BrokerID": self.brokerid, "CombHedgeFlag": "1", "ForceCloseReason": "0", "IsAutoSuspend": 0,
                "ContingentCondition": {"GE": "6", "LE": "8"}[intent.comparator] if intent.condition_mode == "SERVER" else "1",
                "StopPrice": intent.trigger_price if intent.condition_mode == "SERVER" else 0,
            }
            self.reqid += 1
            result = self.reqOrderInsert(request, self.reqid)
            if result:
                # A nonzero SDK result does not authorize any resend. Preserve
                # the existing kernel's conservative empty-id reconciliation.
                owner._emit("error", f"CTP order request rejected locally: code {int(result)}")
                return ""
            if order_id not in self._reported_order_ids:
                owner._emit("order", order)
            return order_id

        def _publish_raw_order(self, data: dict, status_override: str = "") -> None:
            owner = self.gateway.owner
            contract = owner._native_contracts[data["InstrumentID"]]
            front, session = int(data["FrontID"]), int(data["SessionID"])
            sysid = str(data.get("OrderSysID", ""))
            native_id = f"{front}_{session}_{data['OrderRef']}"
            if front == session == 0:
                if not sysid.strip():
                    raise ValueError("Server-generated order has no usable broker identity")
                native_id = f"SYS_{owner._trading_day}_{contract.exchange.value}_{sysid.encode('ascii').hex()}"
            order_id = f"{GATEWAY_NAME}.{native_id}"
            status = status_override or {
                "0": "FILLED", "1": "PARTIAL", "2": "CANCELLED", "3": "ACCEPTED",
                "4": "CANCELLED", "5": "CANCELLED", "a": "SUBMITTING",
                "b": "WAITING_TRIGGER", "c": "TRIGGERED",
            }[str(data["OrderStatus"])]
            if str(data.get("OrderSubmitStatus", "")) == "4":
                status = "REJECTED"
            wire_type = (str(data["OrderPriceType"]), str(data["TimeCondition"]), str(data["VolumeCondition"]))
            order_type = {("2", "3", "1"): "LIMIT", ("2", "1", "1"): "FAK",
                          ("2", "1", "3"): "FOK", ("1", "1", "1"): "MARKET",
                          # Observe pre-existing/upstream market-to-limit
                          # orders too; never silently drop actual exposure.
                          ("1", "3", "1"): "MARKET_TO_LIMIT"}.get(wire_type, "UNKNOWN")
            contingent = str(data.get("ContingentCondition", "1"))
            condition_mode = "IMMEDIATE" if contingent == "1" else "SERVER"
            comparator = {"6": "GE", "8": "LE"}.get(contingent, "" if contingent == "1" else "UNKNOWN")
            trigger = _optional_float(data, "StopPrice") or 0
            volume, traded = int(data["VolumeTotalOriginal"]), int(data.get("VolumeTraded", 0))
            if volume <= 0 or traded < 0 or traded > volume:
                raise ValueError("Invalid native order quantities")
            order = Order(order_id, contract.vt_symbol, {"0": "BUY", "1": "SELL"}[data["Direction"]],
                          {"0": "OPEN", "1": "CLOSE", "3": "CLOSE_TODAY", "4": "CLOSE_YESTERDAY"}[data["CombOffsetFlag"]],
                          volume, float(data["LimitPrice"]), traded, status, order_type, condition_mode, trigger, comparator,
                          exchange_order_id=sysid, parent_exchange_order_id=str(data.get("RelativeOrderSysID", "")))
            self._reported_order_ids.add(order_id)
            if sysid.strip():
                owner._exchange_order_ids[native_id] = (contract.exchange.value, sysid)
                self.sysid_orderid_map[sysid] = native_id
            owner._publish_order(order)
            if order_type in {"UNKNOWN", "MARKET_TO_LIMIT"} or comparator == "UNKNOWN":
                owner._fault("CTP returned an order instruction outside supported semantics; review and reconcile")
            if status == "TRIGGERED":
                # A Touched report is not evidence that its resulting ordinary
                # order has finished. Retain the parent's active record
                # until a broker-verified parent/child reconciliation exists.
                owner._emit("error", "柜台条件已触发，父子委托关联尚待核验；保留父单记录并禁止新单，请核对柜台实际委托")

        def onRtnOrder(self, data: dict) -> None:
            if data.get("InstrumentID") not in self.gateway.owner._native_contracts:
                self.order_data.append(dict(data))
                return
            self._publish_raw_order(data)
            self._flush_trades()

        def onRtnTrade(self, data: dict) -> None:
            owner = self.gateway.owner
            contract = owner._native_contracts.get(data.get("InstrumentID"))
            order_id = self.sysid_orderid_map.get(data.get("OrderSysID"))
            if contract is None or not order_id:
                self.trade_data.append(dict(data))
                return
            volume, price = int(data["Volume"]), float(data["Price"])
            if volume <= 0 or not math.isfinite(price) or price <= 0:
                raise ValueError("Invalid native trade quantities or price")
            trade_id = f"{GATEWAY_NAME}.{owner._trading_day}.{contract.exchange.value}.{data['TradeID']}"
            if trade_id in owner._trade_reports:
                return
            owner._trade_reports.add(trade_id)
            owner._changed()
            owner._emit("trade", Trade(trade_id, f"{GATEWAY_NAME}.{order_id}", contract.vt_symbol,
                {"0": "BUY", "1": "SELL"}[data["Direction"]],
                {"0": "OPEN", "1": "CLOSE", "3": "CLOSE_TODAY", "4": "CLOSE_YESTERDAY"}[data["OffsetFlag"]],
                volume, price))

        def _flush_trades(self) -> None:
            trades, self.trade_data = self.trade_data, []
            for row in trades:
                self.onRtnTrade(row)

        def onRspQryOrder(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            if error.get("ErrorID", 0):
                self.gateway.owner._report_ctp_error("Trader.onRspQryOrder", data=data, error=error,
                                                     reqid=reqid, last=last, fault="CTP order query failed")
            elif data and data.get("InstrumentID"):
                self.onRtnOrder(data)

        def _has_rejected_order_fields(self, data: dict) -> bool:
            required = {"InstrumentID", "OrderRef", "Direction", "CombOffsetFlag", "VolumeTotalOriginal",
                        "LimitPrice", "OrderPriceType", "TimeCondition", "VolumeCondition"}
            return isinstance(data, dict) and required.issubset(data)

        def _handle_order_insert_error(self, callback: str, data: dict, error: dict,
                                       reqid: int, last: bool) -> None:
            owner = self.gateway.owner
            owner._report_ctp_error(callback, data=data, error=error, reqid=reqid, last=last,
                                    note="CTP order rejected")
            if not self._has_rejected_order_fields(data) or data["InstrumentID"] not in owner._native_contracts:
                return
            self._publish_raw_order(dict(data, FrontID=self.frontid, SessionID=self.sessionid), "REJECTED")

        def onRspOrderInsert(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            if not error.get("ErrorID", 0):
                return
            self._handle_order_insert_error("Trader.onRspOrderInsert", data, error, reqid, last)

        def onErrRtnOrderInsert(self, data: dict, error: dict) -> None:
            if error.get("ErrorID", 0):
                self._handle_order_insert_error("Trader.onErrRtnOrderInsert", data, error, 0, True)

        def onRtnErrorConditionalOrder(self, data: dict) -> None:
            owner = self.gateway.owner
            owner._emit("error", owner._ctp_record("Trader.onRtnErrorConditionalOrder", data=data))
            if data.get("InstrumentID") not in owner._native_contracts:
                # Preserve the actual failure through contract initialization,
                # using the same ordered replay as private order reports.
                if self._has_rejected_order_fields(data):
                    self.order_data.append(dict(data, OrderSubmitStatus="4", OrderStatus="5"))
            elif self._has_rejected_order_fields(data) and data.get("InstrumentID") in owner._native_contracts:
                self._publish_raw_order(data, "REJECTED")

        def onFrontConnected(self) -> None:
            self.gateway.owner._publish_health("trader connected; authenticating")
            super().onFrontConnected()

        def onFrontDisconnected(self, reason: int) -> None:
            owner = self.gateway.owner
            owner._td_login = owner._authenticated = owner._settled = owner._contracts_ready = False
            owner._inflight = None
            owner._queue.clear()
            owner._positions.clear()
            owner._native_contracts.clear()
            owner._contracts.clear()
            owner._margin_rows.clear()
            owner._pre_settlement_prices.clear()
            owner._market_volume_limits.clear()
            owner._exchange_order_ids.clear()
            self.contract_inited = False
            self.auth_status = False
            owner._publish_health(f"trader disconnected: {reason}; manual re-arm required")
            super().onFrontDisconnected(reason)

        def onRspAuthenticate(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            owner = self.gateway.owner
            if error.get("ErrorID", 0):
                owner._authenticated = False
                owner._report_ctp_error("Trader.onRspAuthenticate", data=data, error=error,
                                        reqid=reqid, last=last, fault="CTP authentication failed")
                return
            owner._authenticated = True
            super().onRspAuthenticate(data, {"ErrorID": 0}, reqid, last)
            owner._publish_health("authenticated; awaiting login")

        def onRspUserLogin(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            owner = self.gateway.owner
            if error.get("ErrorID", 0):
                owner._td_login = False
                owner._report_ctp_error("Trader.onRspUserLogin", data=data, error=error,
                                        reqid=reqid, last=last, fault="CTP trader login failed")
                return
            day = str(data["TradingDay"])
            if len(day) != 8:
                raise ValueError("Invalid trading day")
            datetime.strptime(day, "%Y%m%d")
            owner._trading_day = day
            self._reported_order_ids.clear()
            owner._order_reports.clear()
            owner._trade_reports.clear()
            owner._td_login = True
            owner._settled = owner._contracts_ready = False
            self.contract_inited = False
            self.order_ref = max(self.order_ref, int(data.get("MaxOrderRef", "0").strip() or 0))
            super().onRspUserLogin(data, {"ErrorID": 0}, reqid, last)
            owner._publish_health("trader logged in; confirming settlement")

        def onRspSettlementInfoConfirm(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            owner = self.gateway.owner
            if error.get("ErrorID", 0):
                owner._settled = False
                owner._report_ctp_error("Trader.onRspSettlementInfoConfirm", data=data, error=error,
                                        reqid=reqid, last=last, fault="CTP settlement confirmation failed")
                return
            if not owner._td_login or not last:
                return
            # Do not call upstream: it ignores error and retries forever in a
            # native callback. Query scheduling belongs to the event timer.
            owner._settled = True
            owner._enqueue("contracts")
            owner._publish_health("settlement confirmed; querying contracts")

        def onRspQryInstrument(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            owner = self.gateway.owner
            if not owner._current("contracts", reqid):
                return
            if error.get("ErrorID", 0):
                owner._complete("contracts", reqid)
                owner._query_failed("contracts", "CTP contract query failed",
                                    detail=owner._ctp_record("Trader.onRspQryInstrument", data=data, error=error, reqid=reqid, last=last))
                return
            if data and data.get("InstrumentID"):
                vt_symbol = f"{data['InstrumentID']}.{data['ExchangeID']}"
                owner._market_volume_limits[vt_symbol] = (int(data.get("MinMarketOrderVolume", 0)),
                                                          int(data.get("MaxMarketOrderVolume", 0)))
                super().onRspQryInstrument(data, {"ErrorID": 0}, reqid, False)
                # A large catalogue can take longer than QUERY_TIMEOUT in total.
                # Only silence from the current query counts as a timeout.
                owner._inflight.sent_at = monotonic()
            if last:
                owner._complete("contracts", reqid)
                if not owner._native_contracts:
                    owner._query_failed("contracts", "CTP returned no contracts")
                    return
                self.contract_inited = True
                # Private/PublicTopic(0) in upstream connect replays the current
                # trading day's broker orders/trades, preserving native ids.
                orders, self.order_data = self.order_data, []
                trades, self.trade_data = self.trade_data, []
                for row in orders:
                    self.onRtnOrder(row)
                for row in trades:
                    self.onRtnTrade(row)
                owner._contracts_ready = True
                owner.query()
                owner._publish_health("CTP ready; account reconciliation and manual arm required")

        def onRspQryInstrumentMarginRate(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            owner = self.gateway.owner
            query = owner._inflight
            if not query or not query.kind.startswith("margin:") or not owner._current(query.kind, reqid):
                return
            kind = query.kind
            vt_symbol = kind.split(":", 1)[1]
            if error.get("ErrorID", 0):
                owner._complete(kind, reqid)
                owner._query_failed(kind, "CTP investor margin query failed for " + vt_symbol,
                                    detail=owner._ctp_record("Trader.onRspQryInstrumentMarginRate",
                                        data=data, error=error, reqid=reqid, last=last))
                return
            if data and data.get("InstrumentID"):
                owner._margin_rows.append(dict(data))
            if not last:
                return
            rows, owner._margin_rows = owner._margin_rows, []
            owner._complete(kind, reqid)
            contract = owner._contracts.get(vt_symbol)
            symbol = vt_symbol.split(".")[0]
            fields = ("LongMarginRatioByMoney", "LongMarginRatioByVolume", "ShortMarginRatioByMoney", "ShortMarginRatioByVolume")
            # Relative broker additions cannot safely substitute for an effective
            # investor rate. Neither Instrument.LongMarginRatio nor guessed
            # exchange defaults are used as a fallback.
            valid = [row for row in rows if row.get("InstrumentID") == symbol and str(row.get("HedgeFlag")) == "1"
                     and row.get("IsRelative") in (0, False) and "IsRelative" in row
                     and (row.get("InvestorID") == owner.account_id
                          or not row.get("InvestorID") and str(row.get("InvestorRange")) == "1")
                     and row.get("BrokerID", self.brokerid) == self.brokerid
                     and all(key in row for key in fields)]
            if contract is None or len(valid) != 1:
                owner._revoke_margin(vt_symbol)
                owner._emit("log", f"CTP effective investor margin rate unavailable or ambiguous for {vt_symbol}; automatic opens blocked; manual funds approval remains with broker")
                return
            rates = tuple(float(valid[0][field]) for field in fields)
            if (any(not math.isfinite(value) or not 0 <= value < 1e100 for value in rates)
                    or rates[0] > 1 or rates[2] > 1 or rates[0] + rates[1] <= 0 or rates[2] + rates[3] <= 0):
                owner._revoke_margin(vt_symbol)
                owner._emit("log", f"CTP investor margin rate invalid for {vt_symbol}; automatic opens blocked; manual funds approval remains with broker")
                return
            contract = replace(contract, long_margin_by_money=rates[0], long_margin_by_volume=rates[1],
                               short_margin_by_money=rates[2], short_margin_by_volume=rates[3],
                               margin_source="ctp_investor", margin_trading_day=owner._trading_day)
            if contract != owner._contracts[vt_symbol]:
                owner._contracts[vt_symbol] = contract
                owner._changed()
                owner._emit("contract", contract)

        def onRspQryTradingAccount(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            owner = self.gateway.owner
            if owner._stale_snapshot("account", reqid, last):
                return
            if error.get("ErrorID", 0):
                owner._complete("account", reqid)
                owner._query_failed("account", "CTP account query failed",
                                    detail=owner._ctp_record("Trader.onRspQryTradingAccount", data=data, error=error, reqid=reqid, last=last))
                return
            if data and data.get("AccountID"):
                if str(data["AccountID"]) != owner.account_id:
                    owner._fault("CTP account identity does not match configured account")
                    return
                trading_day = str(data.get("TradingDay", "")).strip()
                if trading_day and trading_day != owner._trading_day:
                    owner._complete("account", reqid)
                    owner._fault("CTP account trading day changed; restart, log in and reconcile before trading")
                    return
                if data.get("CurrencyID", "CNY") == "CNY":
                    balance = float(data["Balance"])
                    owner._emit("account", Account(owner.account_id, balance, float(data["Available"]),
                                balance - float(data["PreBalance"]) - float(data["Deposit"]) + float(data["Withdraw"]),
                                owner._trading_day, margin=_optional_float(data, "CurrMargin"),
                                frozen_margin=_optional_float(data, "FrozenMargin"), frozen_cash=_optional_float(data, "FrozenCash"),
                                frozen_commission=_optional_float(data, "FrozenCommission"), commission=_optional_float(data, "Commission"),
                                position_pnl=_optional_float(data, "PositionProfit"), close_pnl=_optional_float(data, "CloseProfit"),
                                pre_balance=_optional_float(data, "PreBalance"), deposit=_optional_float(data, "Deposit"),
                                withdraw=_optional_float(data, "Withdraw"), cash_in=_optional_float(data, "CashIn")))
            if last:
                owner._complete("account", reqid)

        def onRspQryInvestorPosition(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            owner = self.gateway.owner
            if owner._stale_snapshot("positions", reqid, last):
                return
            if error.get("ErrorID", 0):
                owner._positions.clear()
                owner._complete("positions", reqid)
                owner._query_failed("positions", "CTP position query failed",
                                    detail=owner._ctp_record("Trader.onRspQryInvestorPosition", data=data, error=error, reqid=reqid, last=last))
                return
            if data and data.get("InstrumentID"):
                symbol = data["InstrumentID"]
                contract = owner._native_contracts.get(symbol)
                vt_symbol = contract.vt_symbol if contract else f"{symbol}.{data.get('ExchangeID', '')}"
                if contract is None and not is_dce_spread_position(vt_symbol):
                    raise KeyError(symbol)
                side = ctp.DIRECTION_CTP2VT[data["PosiDirection"]].name
                if side not in {"LONG", "SHORT"}:
                    raise ValueError("Unsupported position direction")
                volume = int(data["Position"])
                if (contract.exchange.value if contract else "DCE") in {"SHFE", "INE"}:
                    # CTP provides separate PositionDate=1(today)/2(history)
                    # records; YdPosition alone is not today's remaining yd.
                    position_date = str(data["PositionDate"])
                    if position_date not in {"1", "2"}:
                        raise ValueError("Unknown SHFE position date")
                    yesterday = volume if position_date == "2" else 0
                else:
                    yesterday = volume - int(data["TodayPosition"])
                frozen = int(data["ShortFrozen" if side == "LONG" else "LongFrozen"])
                if contract and getattr(contract.product, "name", "") == "OPTION" and side == "LONG":
                    # External exercise orders also encumber long options.
                    # YdStrikeFrozen is a historical subset, not another lot.
                    frozen += max(int(data.get("StrikeFrozen", 0)), int(data.get("YdStrikeFrozen", 0)))
                if min(volume, yesterday, frozen) < 0 or yesterday > volume:
                    raise ValueError("Invalid native position quantities")
                key = (vt_symbol, side)
                prior = owner._positions.get(key)
                cost = _optional_float(data, "PositionCost")
                pnl = _optional_float(data, "PositionProfit")
                margin = _optional_float(data, "UseMargin")
                if prior is not None:
                    cost = None if cost is None or prior.average_price is None or contract is None else cost + prior.average_price * prior.volume * contract.size
                    pnl = None if pnl is None or prior.position_pnl is None else pnl + prior.position_pnl
                    margin = None if margin is None or prior.margin is None else margin + prior.margin
                prior = prior or Position(*key, 0, 0)
                total_volume = prior.volume + volume
                average_price = cost / (total_volume * contract.size) if contract and cost is not None and total_volume and contract.size else None
                owner._positions[key] = Position(*key, prior.volume + volume,
                                                 prior.yesterday + yesterday, prior.frozen + frozen,
                                                 average_price, pnl, margin,
                                                 "ctp_spread" if is_dce_spread_position(vt_symbol) else "ctp_position_cost")
            if last:
                snapshot = [position for position in owner._positions.values() if position.volume]
                owner._positions.clear()
                owner._complete("positions", reqid)
                owner._emit("positions", snapshot)  # [] explicitly clears a previous holding.

        def cancel_order(self, req: Any, *, order_sys_id: str = "") -> None:
            request = {"InstrumentID": req.symbol, "ExchangeID": req.exchange.value,
                       "ActionFlag": ctp.THOST_FTDC_AF_Delete, "BrokerID": self.brokerid, "InvestorID": self.userid}
            if order_sys_id:
                request["OrderSysID"] = order_sys_id
            else:
                front, session, reference = req.orderid.split("_")
                request.update(OrderRef=reference, FrontID=int(front), SessionID=int(session))
            self.reqid += 1
            result = self.reqOrderAction(request, self.reqid)
            if result:
                raise RuntimeError(f"CTP cancellation request rejected: code {int(result)}")

        def onRspError(self, error: dict, reqid: int, last: bool) -> None:
            self.gateway.owner._report_ctp_error("Trader.onRspError", error=error, reqid=reqid, last=last,
                                                 fault="CTP trader error")

    @_guard_callbacks
    class Market(ctp.CtpMdApi):
        def onRtnDepthMarketData(self, data: dict) -> None:
            owner = self.gateway.owner
            contract = owner._native_contracts.get(data.get("InstrumentID", ""))
            if contract:
                raw = data.get("PreSettlementPrice")
                value = float(raw) if raw is not None else None
                owner._pre_settlement_prices[contract.vt_symbol] = (
                    value if value is not None and math.isfinite(value) and 0 <= value < 1e100 else None)
            super().onRtnDepthMarketData(data)

        def onFrontDisconnected(self, reason: int) -> None:
            owner = self.gateway.owner
            owner._md_login = False
            owner._pre_settlement_prices.clear()
            owner._publish_health(f"market disconnected: {reason}; manual limits remain available through trader")
            super().onFrontDisconnected(reason)

        def onRspUserLogin(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            owner = self.gateway.owner
            if error.get("ErrorID", 0):
                owner._md_login = False
                owner._report_ctp_error("Market.onRspUserLogin", data=data, error=error,
                                        reqid=reqid, last=last, note="CTP market login failed; trader remains independent")
                return
            super().onRspUserLogin(data, {"ErrorID": 0}, reqid, last)
            owner._md_login = True
            if owner._contracts_ready:
                owner._changed()
            owner._publish_health("market logged in")

    class NativeGateway(ctp.CtpGateway):
        def __init__(self, owner: VnpyGateway, engine: Any) -> None:
            bindings.BaseGateway.__init__(self, engine, GATEWAY_NAME)
            self.owner = owner
            self.td_api = Trader(self)
            self.md_api = Market(self)

        def init_query(self) -> None:
            pass  # VnpyGateway owns a single serialized query queue.

        def write_log(self, msg: str) -> None:
            self.owner._emit("log", msg)

        def write_error(self, msg: str, error: dict) -> None:
            self.owner._emit("error", self.owner._ctp_record("Gateway.write_error", message=msg, error=error))

        def on_contract(self, value: Any) -> None:
            self.owner._native_contracts[value.symbol] = value
            option_type = getattr(getattr(value, "option_type", None), "name", "")
            expiry = getattr(value, "option_expiry", None)
            contract = Contract(value.vt_symbol, value.size, value.pricetick,
                                int(value.min_volume), int(value.max_volume or 0), value.product.name, getattr(value, "name", ""),
                                option_type=option_type, option_underlying=getattr(value, "option_underlying", ""),
                                option_strike=getattr(value, "option_strike", None) if value.product.name == "OPTION" else None,
                                option_expiry=expiry.strftime("%Y%m%d") if isinstance(expiry, datetime) else "",
                                option_portfolio=getattr(value, "option_portfolio", ""))
            self.owner._contracts[value.vt_symbol] = contract
            self.owner._emit("contract", contract)
            orders, self.td_api.order_data = self.td_api.order_data, []
            for row in orders:
                self.td_api.onRtnOrder(row)
            self.td_api._flush_trades()

        def on_tick(self, value: Any) -> None:
            def depth_price(name):
                price = getattr(value, name, None)
                return price if isinstance(price, (int, float)) and math.isfinite(price) and 0 < price < 1e100 else None
            self.owner._emit("tick", Tick(value.vt_symbol, value.last_price, value.bid_price_1,
                                          value.ask_price_1, value.limit_up, value.limit_down,
                                          value.datetime.timestamp(), self.owner._pre_settlement_prices.get(value.vt_symbol),
                                          depth_price("bid_price_2"), depth_price("ask_price_2"),
                                          volume=float(getattr(value, "volume", 0)),
                                          turnover=float(getattr(value, "turnover", 0)),
                                          open_interest=float(getattr(value, "open_interest", 0))))

        def on_order(self, value: Any) -> None:
            statuses = {"ALLTRADED": "FILLED", "NOTTRADED": "ACCEPTED", "PARTTRADED": "PARTIAL",
                        "SUBMITTING": "SUBMITTING", "CANCELLED": "CANCELLED", "REJECTED": "REJECTED"}
            self.owner._publish_order(Order(value.vt_orderid, value.vt_symbol,
                              _side(value.direction), _offset(value.offset), int(value.volume),
                              value.price, int(value.traded), statuses[value.status.name],
                              getattr(getattr(value, "type", None), "name", "LIMIT")))

        def on_trade(self, value: Any) -> None:
            # Exchange+trading day disambiguate trade ids reused across venues
            # or days; core persistence can safely deduplicate replay events.
            trade_id = f"{value.gateway_name}.{self.owner._trading_day}.{value.exchange.value}.{value.tradeid}"
            if trade_id in self.owner._trade_reports:
                return
            self.owner._trade_reports.add(trade_id)
            self.owner._changed()
            self.owner._emit("trade", Trade(trade_id, value.vt_orderid, value.vt_symbol,
                              _side(value.direction), _offset(value.offset), int(value.volume), value.price))

    return NativeGateway


def _optional_float(data: dict, key: str) -> float | None:
    """Absent native fields remain unknown; missing never means zero."""
    if key not in data:
        return None
    value = float(data[key])
    if not math.isfinite(value) or abs(value) >= 1e100:
        raise ValueError("Invalid native money field")
    return value


def _side(direction: Any) -> str:
    return {"LONG": "BUY", "SHORT": "SELL"}[direction.name]


def _offset(offset: Any) -> str:
    return {"OPEN": "OPEN", "CLOSE": "CLOSE", "CLOSETODAY": "CLOSE_TODAY",
            "CLOSEYESTERDAY": "CLOSE_YESTERDAY"}[offset.name]
