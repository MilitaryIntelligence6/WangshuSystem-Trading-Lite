"""Offline contract tests: fake native transport, never a broker connection."""
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from wangshu_lite.models import GatewayPreflightError, Order, OrderIntent, Snapshot
from wangshu_lite.ctp import VnpyGateway, QUERY_TIMEOUT


class Exchange(Enum):
    SHFE = "SHFE"
    DCE = "DCE"
    INE = "INE"
    CFFEX = "CFFEX"


class Direction(Enum):
    LONG = "long"
    SHORT = "short"


class Offset(Enum):
    OPEN = "open"
    CLOSE = "close"
    CLOSETODAY = "today"
    CLOSEYESTERDAY = "yesterday"


class FakeEngine:
    def __init__(self):
        self.handlers = {}
        self.events = []

    def register(self, kind, callback):
        self.handlers[kind] = callback

    def put(self, event):
        self.events.append(event)

    def start(self):
        pass

    def stop(self):
        pass

    def drain(self):
        while self.events:
            event = self.events.pop(0)
            self.handlers[event.type](event)


class FakeBaseGateway:
    def __init__(self, event_engine, gateway_name):
        self.event_engine, self.gateway_name = event_engine, gateway_name


class FakeTd:
    def __init__(self, gateway):
        self.gateway = gateway
        self.reqid = self.order_ref = 0
        self.order_data, self.trade_data = [], []
        self.sysid_orderid_map = {}
        self.calls = []
        self.responses = {}
        self.contract_inited = False
        self.auth_status = self.login_status = False

    def onFrontConnected(self):
        self.calls.append(("authenticate",))

    def onFrontDisconnected(self, reason):
        self.login_status = False

    def onRspAuthenticate(self, data, error, reqid, last):
        self.auth_status = True

    def onRspUserLogin(self, data, error, reqid, last):
        self.frontid, self.sessionid = data["FrontID"], data["SessionID"]
        self.login_status = True
        self.reqid += 1
        self.calls.append(("settlement", self.reqid))

    def onRspSettlementInfoConfirm(self, data, error, reqid, last):
        raise AssertionError("Unsafe upstream settlement callback must never run")

    def onRspQryInstrument(self, data, error, reqid, last):
        contract = NS(symbol=data["InstrumentID"], exchange=Exchange(data["ExchangeID"]),
                      vt_symbol=f"{data['InstrumentID']}.{data['ExchangeID']}", size=10,
                      pricetick=1, min_volume=1, max_volume=100, product=NS(name=data.get("Product", "FUTURES")),
                      option_type=NS(name=data.get("OptionType", "")), option_underlying=data.get("UnderlyingInstrID", ""),
                      option_strike=data.get("StrikePrice"), option_portfolio=data.get("ProductID", ""),
                      option_expiry=datetime.strptime(data["ExpireDate"], "%Y%m%d") if data.get("ExpireDate") else None)
        self.gateway.on_contract(contract)

    def onRtnOrder(self, data):
        self.calls.append(("order-replay", data))

    def onRtnTrade(self, data):
        self.calls.append(("trade-replay", data))

    def onRspError(self, error, reqid, last):
        self.gateway.write_error("native failure", error)

    def _query(self, kind, data, reqid):
        self.calls.append((kind, data, reqid))
        return self.responses.get(kind, 0)

    def reqQryInstrument(self, data, reqid):
        return self._query("contracts", data, reqid)

    def reqQryTradingAccount(self, data, reqid):
        return self._query("account", data, reqid)

    def reqQryInvestorPosition(self, data, reqid):
        return self._query("positions", data, reqid)

    def reqQryInstrumentMarginRate(self, data, reqid):
        return self._query("margin", data, reqid)

    def reqOrderAction(self, data, reqid):
        return self._query("cancel", data, reqid)

    def reqOrderInsert(self, data, reqid):
        return self._query("insert", data, reqid)


class FakeMd:
    def __init__(self, gateway):
        self.gateway = gateway

    def onFrontDisconnected(self, reason):
        self.login_status = False

    def onRspUserLogin(self, data, error, reqid, last):
        self.login_status = True

    def update_date(self):
        pass

    def onRtnDepthMarketData(self, data):
        self.gateway.on_tick(NS(vt_symbol=f"{data['InstrumentID']}.{data['ExchangeID']}", last_price=3000,
            bid_price_1=2999, ask_price_1=3001, limit_up=3300, limit_down=2700,
            datetime=datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)))


class FakeCtpGateway(FakeBaseGateway):
    def connect(self, settings):
        self.td_api.userid = settings["用户名"]
        self.td_api.brokerid = settings["经纪商代码"]
        self.init_query()

    def send_order(self, req):
        self.last_order = req
        self.sends = getattr(self, "sends", 0) + 1
        return getattr(self, "send_result", "WANGSHU.1_2_10")

    def cancel_order(self, req):
        return self.td_api.cancel_order(req)

    def subscribe(self, req):
        self.subscription = req

    def close(self):
        pass


def fake_bindings():
    return NS(
        ctp=NS(CtpGateway=FakeCtpGateway, CtpTdApi=FakeTd, CtpMdApi=FakeMd,
               DIRECTION_CTP2VT={"2": Direction.LONG, "3": Direction.SHORT},
               THOST_FTDC_AF_Delete="0"),
        events=NS(EventEngine=FakeEngine, Event=lambda kind, data: NS(type=kind, data=data)),
        objects=NS(SubscribeRequest=NS, OrderRequest=NS, CancelRequest=NS),
        constants=NS(Exchange=Exchange, Direction=Direction, Offset=Offset,
                     OrderType=NS(LIMIT="LIMIT")),
        BaseGateway=FakeBaseGateway, timer="timer",
    )


SETTINGS = {"用户名": "123456", "密码": "secret-password", "经纪商代码": "broker",
            "授权编码": "secret-auth-code", "产品名称": "approved-app",
            "交易服务器": "tcp://test.invalid:1", "行情服务器": "tcp://test.invalid:2",
            "柜台环境": "测试"}
OK = {"ErrorID": 0}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        with patch("wangshu_lite.ctp._load_bindings", fake_bindings), patch("wangshu_lite.ctp.sys.platform", "win32"):
            self.adapter = VnpyGateway(SETTINGS, lambda kind, value: self.events.append((kind, value)))
        self.adapter.connect()
        self.td, self.md = self.adapter._native.td_api, self.adapter._native.md_api

    def tearDown(self):
        self.adapter.close()

    def test_startup_selects_backup_when_primary_front_is_unreachable(self):
        gateway = object.__new__(VnpyGateway)
        gateway._selected_endpoints = {}
        gateway._emit = lambda *_args: None
        settings = dict(SETTINGS, **{"交易服务器备选": "tcp://backup.invalid:11", "行情服务器备选": "tcp://backup.invalid:12"})
        with patch.object(VnpyGateway, "_front_reachable", side_effect=[False, True, True]):
            selected = gateway._select_fronts(settings)
        self.assertEqual(selected["交易服务器"], "tcp://backup.invalid:11")
        self.assertEqual(selected["行情服务器"], "tcp://test.invalid:2")
        self.assertEqual(gateway._selected_endpoints, {"交易": "备", "行情": "主"})

    def login(self):
        self.td.onRspAuthenticate({}, OK, 1, True)
        self.td.onRspUserLogin({"FrontID": 1, "SessionID": 2, "TradingDay": "20260917",
                               "MaxOrderRef": "000042"}, OK, 2, True)
        self.md.onRspUserLogin({}, OK, 1, True)

    def ready(self):
        self.login()
        self.td.onRspSettlementInfoConfirm({}, OK, 3, True)
        self.adapter._last_query = float("-inf")
        self.adapter._timer()
        query_id = self.adapter._inflight.reqid
        self.td.onRspQryInstrument({"InstrumentID": "rb2610", "ExchangeID": "SHFE"}, OK, query_id, False)
        self.td.onRspQryInstrument({"InstrumentID": "m2701", "ExchangeID": "DCE"}, OK, query_id, False)
        self.td.onRspQryInstrument({}, OK, query_id, True)
        self.adapter._engine.drain()

    def issue(self, kind):
        self.adapter._queue.clear()
        self.adapter._enqueue(kind)
        self.adapter._last_query = float("-inf")
        self.adapter._timer()
        self.assertIsNotNone(self.adapter._inflight)
        return self.adapter._inflight.reqid

    def values(self, kind):
        self.adapter._engine.drain()
        return [value.data if isinstance(value, Snapshot) else value for event, value in self.events if event == kind]

    def margin_ready(self):
        reqid = self.issue("margin:rb2610.SHFE")
        self.td.onRspQryInstrumentMarginRate({"InstrumentID": "rb2610", "HedgeFlag": "1", "IsRelative": 0,
            "InvestorID": SETTINGS["用户名"], "BrokerID": SETTINGS["经纪商代码"],
            "LongMarginRatioByMoney": 0.1, "LongMarginRatioByVolume": 0,
            "ShortMarginRatioByMoney": 0.12, "ShortMarginRatioByVolume": 0}, OK, reqid, True)
        self.adapter._engine.drain()

    def raw_order(self, **changes):
        row = {"InstrumentID": "rb2610", "ExchangeID": "SHFE", "FrontID": 1, "SessionID": 2,
               "OrderRef": "43", "OrderSysID": "exchange-43", "OrderStatus": "3", "OrderSubmitStatus": "3",
               "OrderPriceType": "2", "TimeCondition": "3", "VolumeCondition": "1",
               "ContingentCondition": "1", "StopPrice": 0, "Direction": "0", "CombOffsetFlag": "0",
               "VolumeTotalOriginal": 2, "VolumeTraded": 0, "LimitPrice": 3500}
        row.update(changes)
        return row

    def approve_capabilities(self):
        self.adapter.close()
        with patch("wangshu_lite.ctp._load_bindings", fake_bindings), patch("wangshu_lite.ctp.sys.platform", "win32"):
            self.adapter = VnpyGateway(SETTINGS, lambda kind, value: self.events.append((kind, value)),
                native_market_symbols=("rb2610.SHFE",), server_condition_symbols=("rb2610.SHFE",))
        self.adapter.connect()
        self.td, self.md = self.adapter._native.td_api, self.adapter._native.md_api
        self.ready()

    def test_trader_ready_needs_auth_login_settlement_but_not_market_or_full_catalogue(self):
        self.assertFalse(self.adapter.health().ready)
        self.login()
        self.assertFalse(self.adapter.health().ready)
        self.assertIn("等待结算确认", self.adapter.health().reason)
        self.assertEqual(self.td.order_ref, 42)
        self.td.onRspSettlementInfoConfirm({}, OK, 3, True)
        self.md.onFrontDisconnected(4097)
        self.assertTrue(self.adapter.health().ready)
        self.adapter._timer()
        query_id = self.adapter._inflight.reqid
        with self.assertRaises(GatewayPreflightError):
            self.adapter.send_order(OrderIntent("unknown", "rb2610.SHFE", "BUY", "OPEN", 1, 3500), None)
        self.td.onRspQryInstrument({"InstrumentID": "rb2610", "ExchangeID": "SHFE"}, OK, query_id, False)
        self.assertTrue(self.adapter.health().ready)
        oid = self.adapter.send_order(OrderIntent("known", "rb2610.SHFE", "BUY", "OPEN", 1, 3500), None)
        self.assertTrue(oid)
        self.assertEqual(sum(c[0] == "insert" for c in self.td.calls), 1)
        self.td.onRspQryInstrument({}, OK, query_id, True)
        self.assertEqual(self.adapter.health().trading_day, "20260917")

    def test_settlement_error_never_enters_upstream_loop_or_queries(self):
        self.login()
        self.td.onRspSettlementInfoConfirm({}, {"ErrorID": 99}, 3, True)
        self.adapter._timer()
        self.assertFalse(self.adapter.health().ready)
        self.assertFalse(any(call[0] == "contracts" for call in self.td.calls))
        self.assertIn("99", self.values("error")[-1])
        failure_reason = self.adapter.health().reason
        self.md.onRspUserLogin({}, OK, 4, True)
        self.assertFalse(self.adapter.health().ready)
        self.assertEqual(self.adapter.health().reason, failure_reason)

    def test_streaming_contract_query_times_out_on_silence_instead_of_total_duration(self):
        self.login()
        self.td.onRspSettlementInfoConfirm({}, OK, 3, True)
        with patch("wangshu_lite.ctp.monotonic", return_value=100.0) as now:
            self.adapter._timer()
            query_id = self.adapter._inflight.reqid
            now.return_value = 100.0 + QUERY_TIMEOUT - 1
            self.td.onRspQryInstrument({"InstrumentID": "rb2610", "ExchangeID": "SHFE"}, OK, query_id, False)
            now.return_value = 100.0 + QUERY_TIMEOUT + 1
            self.adapter._timer()
            self.assertFalse(self.adapter._faulted)
            self.assertTrue(self.adapter.health().ready)
            self.assertEqual(len(self.adapter._native_contracts), 1)
            # A stale response cannot keep the current query alive.
            now.return_value = 100.0 + QUERY_TIMEOUT * 2 + 1
            self.td.onRspQryInstrument({"InstrumentID": "m2701", "ExchangeID": "DCE"}, OK, query_id - 1, False)
            self.adapter._timer()
            self.assertFalse(self.adapter._faulted)
            self.assertIn("contracts query timed out", self.values("error")[-1])

    def test_query_flow_control_retries_are_bounded_and_do_not_block(self):
        self.login()
        self.td.responses["contracts"] = -3
        self.td.onRspSettlementInfoConfirm({}, OK, 3, True)
        for _ in range(5):
            self.adapter._last_query = float("-inf")
            self.adapter._timer()
        self.assertEqual(sum(call[0] == "contracts" for call in self.td.calls), 3)
        self.assertTrue(self.adapter.health().ready)
        self.assertIn("rejected", self.values("error")[-1])

    def test_account_pnl_uses_native_trading_day_and_cash_adjustments(self):
        self.ready()
        reqid = self.issue("account")
        self.td.onRspQryTradingAccount({"AccountID": "123456", "Balance": 1050,
            "Available": 700, "PreBalance": 1000, "Deposit": 100, "Withdraw": 20}, OK, reqid, True)
        account = self.values("account")[-1]
        self.assertEqual(account.daily_pnl, -30)
        self.assertEqual(account.available, 700)
        self.assertEqual(account.trading_day, "20260917")
        self.assertIsNone(account.margin)
        self.assertIsNone(account.frozen_margin)

    def test_account_broker_margin_and_fees_are_not_synthesized(self):
        self.ready()
        reqid = self.issue("account")
        self.td.onRspQryTradingAccount({"AccountID": "123456", "Balance": 100000,
            "Available": 75000, "PreBalance": 99000, "Deposit": 0, "Withdraw": 0,
            "CurrMargin": 20000, "FrozenMargin": 4500, "FrozenCash": 100, "FrozenCommission": 400,
            "Commission": 25, "PositionProfit": 1250, "CloseProfit": -225}, OK, reqid, True)
        account = self.values("account")[-1]
        self.assertEqual((account.margin, account.frozen_margin, account.frozen_cash, account.frozen_commission),
                         (20000, 4500, 100, 400))
        self.assertEqual((account.commission, account.position_pnl, account.close_pnl), (25, 1250, -225))

    def test_account_preserves_raw_cash_fields_without_premium_pnl_guess(self):
        self.ready()
        reqid = self.issue("account")
        self.td.onRspQryTradingAccount({"AccountID": "123456", "Balance": 900,
            "Available": 850, "PreBalance": 1000, "Deposit": 0, "Withdraw": 0,
            "CashIn": -100}, OK, reqid, True)
        account = self.values("account")[-1]
        self.assertEqual((account.pre_balance, account.deposit, account.withdraw, account.cash_in), (1000, 0, 0, -100))
        self.assertEqual(account.daily_pnl, -100)  # Cash delta; core performs explicitly configured option equity valuation.

    def test_option_metadata_survives_and_never_requests_futures_margin(self):
        self.ready()
        reqid = self.issue("contracts")
        self.td.onRspQryInstrument({"InstrumentID": "IO2612-C-4000", "ExchangeID": "CFFEX", "Product": "OPTION",
            "OptionType": "CALL", "UnderlyingInstrID": "IF2612", "StrikePrice": 4000, "ProductID": "IO",
            "ExpireDate": "20261218"}, OK, reqid, True)
        option = self.adapter._contracts["IO2612-C-4000.CFFEX"]
        self.assertEqual((option.product, option.option_type, option.option_underlying, option.option_strike,
                          option.option_expiry, option.option_portfolio), ("OPTION", "CALL", "IF2612", 4000, "20261218", "IO"))
        self.adapter._queue.clear()
        self.adapter.subscribe(option.vt_symbol)
        self.adapter.refresh_margin_rates()
        self.assertEqual(list(self.adapter._queue), [])

    def test_quote_watch_does_not_refresh_margin_or_invalidate_account_state(self):
        self.ready()
        self.adapter._queue.clear()
        revision = self.adapter.snapshot_revision()
        health = self.adapter.health()
        self.adapter.watch("rb2610.SHFE")
        self.assertEqual(self.adapter._native.subscription.symbol, "rb2610")
        self.adapter.refresh_margin_rates()
        self.assertEqual(list(self.adapter._queue), [])
        self.assertEqual(self.adapter.snapshot_revision(), revision)
        self.assertEqual(self.adapter.health(), health)
        # Subscribing still only requests quotes; margin diagnostics are explicit.
        self.adapter.subscribe("rb2610.SHFE")
        self.assertEqual(list(self.adapter._queue), [])
        self.adapter._queue.clear()
        self.adapter.watch("m2701.DCE")
        self.adapter.refresh_margin_rates()
        self.assertEqual([query.kind for query in self.adapter._queue], ["margin:rb2610.SHFE"])

    def test_option_long_exercise_freeze_is_reserved_for_close_availability(self):
        self.ready()
        self.adapter._native_contracts["rb2610"].product = NS(name="OPTION")
        reqid = self.issue("positions")
        self.td.onRspQryInvestorPosition({"InstrumentID": "rb2610", "PosiDirection": "2", "PositionDate": "2",
            "Position": 5, "TodayPosition": 0, "ShortFrozen": 1, "StrikeFrozen": 2, "YdStrikeFrozen": 1}, OK, reqid, True)
        self.assertEqual(self.values("positions")[-1][0].frozen, 3)

    def test_investor_margin_query_is_account_scoped_and_publishes_effective_rates(self):
        self.ready()
        self.adapter.subscribe("rb2610.SHFE")
        self.adapter.refresh_margin_rates()
        self.assertIn("margin:rb2610.SHFE", [q.kind for q in self.adapter._queue])
        self.margin_ready()
        query = next(call for call in self.td.calls if call[0] == "margin")
        self.assertEqual(query[1], {"BrokerID": "broker", "InvestorID": "123456", "InstrumentID": "rb2610",
                                    "ExchangeID": "SHFE", "HedgeFlag": "1"})
        contract = self.values("contract")[-1]
        self.assertEqual((contract.margin_source, contract.margin_trading_day), ("ctp_investor", "20260917"))
        self.assertEqual((contract.long_margin_by_money, contract.short_margin_by_money), (0.1, 0.12))

    def test_unchanged_margin_refresh_preserves_snapshot_revision(self):
        self.ready()
        self.margin_ready()
        before = self.adapter.snapshot_revision()
        reqid = self.issue("margin:rb2610.SHFE")
        self.assertEqual(self.adapter.snapshot_revision(), before)
        self.td.onRspQryInstrumentMarginRate({"InstrumentID": "rb2610", "HedgeFlag": "1", "IsRelative": 0,
            "InvestorID": SETTINGS["用户名"], "BrokerID": SETTINGS["经纪商代码"],
            "LongMarginRatioByMoney": 0.1, "LongMarginRatioByVolume": 0,
            "ShortMarginRatioByMoney": 0.12, "ShortMarginRatioByVolume": 0}, OK, reqid, True)
        self.assertEqual(self.adapter.snapshot_revision(), before)

    def test_account_and_positions_queries_precede_margin_refresh(self):
        self.ready()
        self.adapter._queue.clear()
        self.adapter._enqueue("margin:rb2610.SHFE")
        self.adapter.query()
        self.assertEqual([row.kind for row in self.adapter._queue],
                         ["account", "positions", "margin:rb2610.SHFE"])

    def test_failed_or_relative_margin_refresh_clears_prior_verified_rate(self):
        self.ready()
        self.margin_ready()
        reqid = self.issue("margin:rb2610.SHFE")
        self.td.onRspQryInstrumentMarginRate({"InstrumentID": "rb2610", "HedgeFlag": "1", "IsRelative": 1,
            "InvestorID": "123456", "BrokerID": "broker", "LongMarginRatioByMoney": 0.01,
            "LongMarginRatioByVolume": 0, "ShortMarginRatioByMoney": 0.01,
            "ShortMarginRatioByVolume": 0}, OK, reqid, True)
        self.assertEqual(self.values("contract")[-1].margin_source, "")
        self.assertIsNone(self.adapter._contracts["rb2610.SHFE"].long_margin_by_money)
        self.assertIn("unavailable", self.values("log")[-1])

    def test_margin_query_never_accepts_another_investor(self):
        self.ready()
        reqid = self.issue("margin:rb2610.SHFE")
        self.td.onRspQryInstrumentMarginRate({"InstrumentID": "rb2610", "HedgeFlag": "1", "IsRelative": 0,
            "InvestorID": "other", "BrokerID": "broker", "LongMarginRatioByMoney": 0.01,
            "LongMarginRatioByVolume": 0, "ShortMarginRatioByMoney": 0.01,
            "ShortMarginRatioByVolume": 0}, OK, reqid, True)
        self.assertEqual(self.adapter._contracts["rb2610.SHFE"].margin_source, "")

    def test_margin_timeout_does_not_fault_trading_connection(self):
        self.ready()
        self.margin_ready()
        self.issue("margin:rb2610.SHFE")
        self.adapter._inflight.sent_at -= QUERY_TIMEOUT + 1
        self.adapter._timer()
        self.assertTrue(self.adapter.health().ready)
        self.assertIsNone(self.adapter._inflight)
        self.assertEqual(self.adapter._contracts["rb2610.SHFE"].margin_source, "")
        self.assertIn("timed out", self.values("log")[-1])

    def test_margin_request_failure_keeps_connection_ready_after_bounded_retries(self):
        self.ready()
        self.margin_ready()
        self.td.responses["margin"] = -3
        self.adapter._queue.clear()
        self.adapter._enqueue("margin:rb2610.SHFE")
        for _ in range(3):
            self.adapter._last_query = float("-inf")
            self.adapter._timer()
        self.assertEqual(sum(call[0] == "margin" for call in self.td.calls), 4)
        self.assertTrue(self.adapter.health().ready)
        self.assertEqual(self.adapter._contracts["rb2610.SHFE"].margin_source, "")
        self.assertIn("request rejected", self.values("log")[-1])

    def test_malformed_margin_callback_only_invalidates_estimate(self):
        self.ready()
        self.margin_ready()
        reqid = self.issue("margin:rb2610.SHFE")
        self.td.onRspQryInstrumentMarginRate({"InstrumentID": "rb2610", "HedgeFlag": "1", "IsRelative": 0,
            "InvestorID": SETTINGS["用户名"], "BrokerID": SETTINGS["经纪商代码"],
            "LongMarginRatioByMoney": "invalid", "LongMarginRatioByVolume": 0,
            "ShortMarginRatioByMoney": 0.12, "ShortMarginRatioByVolume": 0}, OK, reqid, True)
        self.assertTrue(self.adapter.health().ready)
        self.assertIsNone(self.adapter._inflight)
        self.assertEqual(self.adapter._contracts["rb2610.SHFE"].margin_source, "")
        self.assertIn("ValueError", self.values("log")[-1])

    def test_position_cost_price_is_weighted_settlement_basis_with_raw_pnl_margin(self):
        self.ready()
        reqid = self.issue("positions")
        row = {"InstrumentID": "rb2610", "PosiDirection": "2", "PositionDate": "1",
               "Position": 2, "YdPosition": 0, "TodayPosition": 2, "ShortFrozen": 0,
               "PositionCost": 70000, "PositionProfit": 200, "UseMargin": 7000}
        self.td.onRspQryInvestorPosition(row, OK, reqid, False)
        self.td.onRspQryInvestorPosition(dict(row, PositionDate="2", Position=1, TodayPosition=0,
            PositionCost=36000, PositionProfit=-50, UseMargin=3600), OK, reqid, True)
        position = self.values("positions")[-1][0]
        self.assertAlmostEqual(position.average_price, 106000 / 30)
        self.assertEqual((position.position_pnl, position.margin, position.price_basis), (150, 10600, "ctp_position_cost"))

    def test_account_trading_day_rollover_without_relogin_fails_closed(self):
        self.ready()
        reqid = self.issue("account")
        self.td.onRspQryTradingAccount({"AccountID": "123456", "Balance": 1050,
            "Available": 700, "PreBalance": 1000, "Deposit": 100, "Withdraw": 20,
            "TradingDay": "20260918"}, OK, reqid, True)
        self.assertFalse(self.adapter.health().ready)
        self.assertEqual(self.values("account"), [])
        self.assertIn("trading day changed", self.values("error")[-1])

    def test_shfe_positions_accumulate_raw_dates_then_empty_snapshot_clears(self):
        self.ready()
        reqid = self.issue("positions")
        row = {"InstrumentID": "rb2610", "PosiDirection": "2", "PositionDate": "1",
               "Position": 2, "YdPosition": 9, "TodayPosition": 2, "ShortFrozen": 1}
        self.td.onRspQryInvestorPosition(row, OK, reqid, False)
        yesterday = dict(row, PositionDate="2", Position=3, TodayPosition=0, ShortFrozen=2)
        self.td.onRspQryInvestorPosition(yesterday, OK, reqid, False)
        self.assertEqual(self.values("positions"), [])
        self.td.onRspQryInvestorPosition({}, OK, reqid, True)
        position = self.values("positions")[-1][0]
        self.assertEqual((position.volume, position.yesterday, position.frozen), (5, 3, 3))
        reqid = self.issue("positions")
        self.td.onRspQryInvestorPosition({}, OK, reqid, True)
        self.assertEqual(self.values("positions")[-1], [])

    def test_other_exchange_remaining_yesterday_comes_from_position_minus_today(self):
        self.ready()
        reqid = self.issue("positions")
        self.td.onRspQryInvestorPosition({"InstrumentID": "m2701", "PosiDirection": "3",
              "Position": 7, "TodayPosition": 2, "YdPosition": 9, "LongFrozen": 1}, OK, reqid, True)
        position = self.values("positions")[-1][0]
        self.assertEqual((position.side, position.yesterday), ("SHORT", 5))

    def test_dce_spread_position_is_preserved_without_a_tradable_contract(self):
        self.ready()
        reqid = self.issue("positions")
        self.td.onRspQryInvestorPosition({"InstrumentID": "SP jm2701&jm2701", "ExchangeID": "DCE",
            "PosiDirection": "2", "Position": 1, "TodayPosition": 0, "ShortFrozen": 0,
            "PositionCost": 0.0, "PositionProfit": 0.0, "UseMargin": 10450.8}, OK, reqid, True)
        position = self.values("positions")[-1][0]
        self.assertEqual((position.vt_symbol, position.side, position.volume, position.yesterday),
                         ("SP jm2701&jm2701.DCE", "LONG", 1, 1))
        self.assertEqual((position.average_price, position.margin, position.price_basis),
                         (None, 10450.8, "ctp_spread"))
        self.assertTrue(self.adapter.health().ready)
        self.assertNotIn(position.vt_symbol, self.adapter._contracts)

    def test_snapshot_error_does_not_publish_partial_position_replacement(self):
        self.ready()
        reqid = self.issue("positions")
        self.td.onRspQryInvestorPosition({"InstrumentID": "m2701", "PosiDirection": "3",
              "Position": 7, "TodayPosition": 2, "LongFrozen": 1}, OK, reqid, False)
        self.td.onRspQryInvestorPosition({}, {"ErrorID": 8}, reqid, True)
        self.assertEqual(self.values("positions"), [])
        self.assertTrue(self.adapter.health().ready)

    def test_callbacks_are_contained_and_secrets_are_not_logged(self):
        self.ready()
        reqid = self.issue("positions")
        self.td.onRspQryInvestorPosition({"InstrumentID": "missing"}, OK, reqid, True)
        self.assertTrue(self.adapter.health().ready)
        diagnostic = self.values("error")[-1]
        self.assertIn("InstrumentID", diagnostic)
        self.assertIn("missing", diagnostic)
        self.assertIn("KeyError", diagnostic)
        self.adapter._native.write_log("secret-password secret-auth-code account=123456")
        self.td.onRspError({"ErrorID": 2, "ErrorMsg": "secret-password"}, 1, True)
        self.adapter._engine.drain()
        messages = [value for kind, value in self.events if kind in {"log", "error"}]
        self.assertNotIn("secret-password", repr(messages))
        self.assertNotIn("secret-auth-code", repr(messages))
        self.assertNotIn("123456", repr(messages))

    def test_native_error_diagnostics_keep_full_error_and_callback_fields(self):
        self.td.onRspAuthenticate({"FrontLabel": "simulation"},
                                  {"ErrorID": 4040, "ErrorMsg": "front handshake rejected", "VendorCode": "X7"},
                                  17, True)
        message = self.values("error")[-1]
        for value in ("Trader.onRspAuthenticate", "4040", "front handshake rejected", "VendorCode", "X7",
                      "FrontLabel", "simulation", "17"):
            self.assertIn(value, message)
        self.assertFalse(self.adapter.health().ready)

    def test_incomplete_order_rejection_is_logged_without_keyerror(self):
        self.ready()
        self.td.onRspOrderInsert({"InstrumentID": "rb2610", "OrderRef": "77", "BrokerExtra": "raw-value"},
                                 {"ErrorID": 31, "ErrorMsg": "insufficient margin", "BrokerCode": "M9"},
                                 23, True)
        message = self.values("error")[-1]
        for value in ("Trader.onRspOrderInsert", "insufficient margin", "BrokerCode", "M9",
                      "BrokerExtra", "raw-value", "23"):
            self.assertIn(value, message)
        self.assertEqual(self.values("order"), [])
        self.assertTrue(self.adapter.health().ready)

    def test_vnpy_write_error_keeps_entire_error_dictionary(self):
        self.adapter._native.write_error("native failure", {
            "ErrorID": 4040, "ErrorMsg": "native handshake details", "ExtraField": "complete"})
        message = self.values("error")[-1]
        for value in ("Gateway.write_error", "native failure", "4040", "native handshake details",
                      "ExtraField", "complete"):
            self.assertIn(value, message)

    def test_disconnect_health_changes_before_event_dispatch_and_stale_queries_ignored(self):
        self.ready()
        reqid = self.issue("positions")
        self.td.onFrontDisconnected(4097)
        self.assertFalse(self.adapter.health().ready)
        self.assertFalse(self.adapter._contracts_ready)
        self.td.onRspQryInvestorPosition({}, OK, reqid, True)
        self.assertEqual(self.values("positions"), [])

    def test_market_disconnect_keeps_trader_orders_and_native_cancel_available(self):
        self.ready()
        self.md.onFrontDisconnected(4097)
        self.assertFalse(self.adapter.health().market_connected)
        self.assertTrue(self.adapter.health().trader_connected)
        self.assertTrue(self.adapter.health().ready)
        self.adapter.cancel_order(Order("WANGSHU.90_81_72", "rb2610.SHFE", "BUY", "OPEN", 1, 3000))
        call = self.td.calls[-1]
        self.assertEqual(call[0], "cancel")
        self.assertEqual((call[1]["FrontID"], call[1]["SessionID"], call[1]["OrderRef"]), (90, 81, "72"))

        self.adapter.send_order(OrderIntent("md-offline", "rb2610.SHFE", "BUY", "OPEN", 1, 3500), None)
        self.assertEqual(sum(c[0] == "insert" for c in self.td.calls), 1)

    def test_send_maps_explicit_close_today_without_retry(self):
        self.ready()
        self.td.responses["insert"] = -3
        order_id = self.adapter.send_order(OrderIntent("client-1", "rb2610.SHFE", "SELL", "CLOSE_TODAY", 1, 3500),
                                           self.adapter.snapshot_revision())
        self.assertEqual(order_id, "")
        inserts = [call for call in self.td.calls if call[0] == "insert"]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1]["CombOffsetFlag"], "3")
        self.assertEqual(inserts[0][1]["OrderPriceType"], "2")

    def test_fak_fok_use_limit_price_ioc_and_correct_volume_condition(self):
        self.ready()
        for order_type, volume_condition in (("FAK", "1"), ("FOK", "3")):
            self.adapter.send_order(OrderIntent("test", "rb2610.SHFE", "BUY", "OPEN", 2, 3500,
                order_type=order_type), self.adapter.snapshot_revision())
            request = [call[1] for call in self.td.calls if call[0] == "insert"][-1]
            self.assertEqual((request["OrderPriceType"], request["TimeCondition"], request["VolumeCondition"],
                              request["LimitPrice"], request["ContingentCondition"]), ("2", "1", volume_condition, 3500, "1"))
            self.assertEqual(self.values("order")[-1].order_type, order_type)

    def test_market_and_server_paths_fail_closed_without_capability_whitelist(self):
        self.ready()
        for kwargs, price in (({"order_type": "MARKET"}, 0),
                              ({"condition_mode": "SERVER", "trigger_price": 3400, "comparator": "GE"}, 3500)):
            with self.assertRaises(GatewayPreflightError):
                self.adapter.send_order(OrderIntent("test", "rb2610.SHFE", "BUY", "OPEN", 1, price, **kwargs),
                                        self.adapter.snapshot_revision())
        self.assertFalse(any(call[0] == "insert" for call in self.td.calls))

    def test_native_market_uses_anyprice_ioc_and_its_own_broker_volume_limits(self):
        self.approve_capabilities()
        intent = OrderIntent("market", "rb2610.SHFE", "BUY", "OPEN", 1, 0, order_type="MARKET")
        with self.assertRaisesRegex(GatewayPreflightError, "quantity limits"):
            self.adapter.send_order(intent, self.adapter.snapshot_revision())
        reqid = self.issue("contracts")
        self.td.onRspQryInstrument({"InstrumentID": "rb2610", "ExchangeID": "SHFE",
            "MinMarketOrderVolume": 1, "MaxMarketOrderVolume": 1}, OK, reqid, True)
        order_id = self.adapter.send_order(intent, self.adapter.snapshot_revision())
        request = [call[1] for call in self.td.calls if call[0] == "insert"][-1]
        self.assertEqual((request["OrderPriceType"], request["TimeCondition"], request["VolumeCondition"], request["LimitPrice"]),
                         ("1", "1", "1", 0))
        self.td.onRtnOrder(self.raw_order(OrderPriceType="1", TimeCondition="1", LimitPrice=0))
        self.assertEqual((self.values("order")[-1].order_id, self.values("order")[-1].order_type), (order_id, "MARKET"))
        with self.assertRaisesRegex(GatewayPreflightError, "quantity limits"):
            self.adapter.send_order(OrderIntent("large", "rb2610.SHFE", "BUY", "OPEN", 2, 0, order_type="MARKET"),
                                    self.adapter.snapshot_revision())
        self.assertEqual(sum(call[0] == "insert" for call in self.td.calls), 1)

    def test_server_condition_wire_callback_and_cancel_keep_trigger_semantics(self):
        self.approve_capabilities()
        for comparator, native_condition in (("GE", "6"), ("LE", "8")):
            self.adapter.send_order(OrderIntent("condition", "rb2610.SHFE", "BUY", "OPEN", 2, 3500,
                condition_mode="SERVER", trigger_price=3400, comparator=comparator), self.adapter.snapshot_revision())
            request = [call[1] for call in self.td.calls if call[0] == "insert"][-1]
            self.assertEqual((request["ContingentCondition"], request["StopPrice"], request["LimitPrice"], request["TimeCondition"]),
                             (native_condition, 3400, 3500, "3"))
            row = self.raw_order(OrderRef=request["OrderRef"], OrderStatus="b", ContingentCondition=native_condition, StopPrice=3400)
            self.td.onRtnOrder(row)
            order = self.values("order")[-1]
            self.assertEqual((order.status, order.active, order.condition_mode, order.trigger_price, order.comparator),
                             ("WAITING_TRIGGER", True, "SERVER", 3400, comparator))
            self.adapter.cancel_order(order)
            self.assertEqual(self.td.calls[-1][1]["OrderSysID"], "exchange-43")
            self.assertNotIn("FrontID", self.td.calls[-1][1])
            self.td.onRtnOrder(dict(row, OrderStatus="c"))
            self.assertEqual(self.values("order")[-1].status, "TRIGGERED")
            self.assertTrue(self.values("order")[-1].active)
            self.assertIn("父子委托", self.values("error")[-1])
        self.assertTrue(self.adapter.health().ready)

    def test_server_nonlimit_and_local_route_are_never_silently_downgraded(self):
        self.approve_capabilities()
        for order_type, mode in (("FAK", "SERVER"), ("LIMIT", "LOCAL")):
            with self.assertRaises(GatewayPreflightError):
                self.adapter.send_order(OrderIntent("bad", "rb2610.SHFE", "BUY", "OPEN", 1, 3500,
                    order_type=order_type, condition_mode=mode, trigger_price=3400, comparator="GE"), self.adapter.snapshot_revision())
        self.assertFalse(any(call[0] == "insert" for call in self.td.calls))

    def test_server_condition_error_and_insert_rejection_preserve_original_order(self):
        self.approve_capabilities()
        row = self.raw_order(OrderStatus="b", ContingentCondition="6", StopPrice=3400, ErrorID=31,
                             ErrorMsg="secret-password")
        self.td.onRtnErrorConditionalOrder(row)
        order = self.values("order")[-1]
        self.assertEqual((order.status, order.condition_mode, order.trigger_price), ("REJECTED", "SERVER", 3400))
        self.td.onRspOrderInsert(row, {"ErrorID": 31}, 1, True)
        self.assertEqual(self.values("order")[-1].comparator, "GE")
        self.assertNotIn("secret-password", repr(self.values("error")))

    def test_known_contract_order_and_trade_callbacks_do_not_wait_for_full_catalogue(self):
        self.login()
        self.td.onRspSettlementInfoConfirm({}, OK, 3, True)
        self.adapter._timer()
        reqid = self.adapter._inflight.reqid
        trade = dict(InstrumentID="rb2610", OrderSysID="exchange-43", TradeID="trade-1",
                     Direction="0", OffsetFlag="0", Volume=1, Price=3500)
        self.td.onRtnTrade(trade)
        self.td.onRtnOrder(self.raw_order())
        self.assertEqual(self.values("trade"), [])
        self.td.onRspQryInstrument({"InstrumentID": "rb2610", "ExchangeID": "SHFE"}, OK, reqid, False)
        self.assertFalse(self.td.contract_inited)
        self.assertEqual(self.values("order")[-1].order_id, "WANGSHU.1_2_43")
        self.assertEqual(self.values("trade")[-1].order_id, "WANGSHU.1_2_43")
        self.assertEqual(self.values("trade")[-1].trade_id, "WANGSHU.20260917.SHFE.trade-1")
        self.td.onRtnTrade(trade)
        self.td.onRspQryInstrument({}, OK, reqid, True)
        self.assertEqual(len(self.values("trade")), 1)
        self.assertEqual(self.td.trade_data, [])

    def test_private_replay_keeps_pending_server_condition_and_exchange_trade_link(self):
        self.login()
        self.td.onRtnOrder(self.raw_order(OrderStatus="b", ContingentCondition="8", StopPrice=3600))
        self.assertEqual(len(self.td.order_data), 1)
        self.td.onRspSettlementInfoConfirm({}, OK, 3, True)
        self.adapter._timer()
        self.td.onRspQryInstrument({"InstrumentID": "rb2610", "ExchangeID": "SHFE"}, OK,
                                  self.adapter._inflight.reqid, True)
        self.assertEqual(self.values("order")[-1].status, "WAITING_TRIGGER")
        self.assertEqual(self.td.sysid_orderid_map["exchange-43"], "1_2_43")
        self.assertFalse(any(call[0] == "insert" for call in self.td.calls))

    def test_server_children_have_stable_unique_ids_and_cancel_by_observed_sysid(self):
        self.ready()
        for sysid in ("child-001", "child-002"):
            self.td.onRtnOrder(self.raw_order(FrontID=0, SessionID=0, OrderRef="", OrderSysID=sysid,
                                           RelativeOrderSysID="TJBD_001", OrderType="0"))
        orders = self.values("order")[-2:]
        self.assertNotEqual(orders[0].order_id, orders[1].order_id)
        self.assertTrue(all(order.order_id.startswith("WANGSHU.SYS_20260917_SHFE_") for order in orders))
        self.assertEqual(orders[1].exchange_order_id, "child-002")
        self.assertEqual(orders[1].parent_exchange_order_id, "TJBD_001")
        self.assertEqual(self.td.sysid_orderid_map["child-002"], orders[1].order_id.removeprefix("WANGSHU."))
        self.adapter.cancel_order(orders[1])
        request = self.td.calls[-1][1]
        self.assertEqual((request["ExchangeID"], request["OrderSysID"]), ("SHFE", "child-002"))
        self.assertFalse(any(field in request for field in ("OrderRef", "FrontID", "SessionID")))

    def test_quote_derived_order_never_releases_condition_parent_resources(self):
        self.ready()
        self.td.onRtnOrder(self.raw_order(OrderStatus="c", ContingentCondition="6", StopPrice=3400,
                                        OrderSysID="condition-parent", OrderType="4"))
        self.td.onRtnOrder(self.raw_order(OrderType="1", RelativeOrderSysID="quoted-order"))
        parents = [order for order in self.values("order") if order.exchange_order_id == "condition-parent"]
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0].status, "TRIGGERED")
        self.assertTrue(parents[0].active)

    def test_unknown_server_identity_and_unobserved_cancel_sysid_are_not_guessed(self):
        self.ready()
        self.td.onRtnOrder(self.raw_order(FrontID=0, SessionID=0, OrderRef="", OrderSysID=""))
        self.assertFalse(self.adapter.health().ready)
        self.assertEqual(self.values("order"), [])
        with self.assertRaisesRegex(ValueError, "Invalid native"):
            self.adapter.cancel_order(Order("WANGSHU.SYS_fake", "rb2610.SHFE", "BUY", "OPEN", 1, 3500,
                                            exchange_order_id="unobserved-sysid"))
        self.assertFalse(any(call[0] == "cancel" for call in self.td.calls))

    def test_query_callbacks_retain_nonqueueing_remainders_and_unknown_market_exposure(self):
        self.ready()
        self.td.onRspQryOrder(self.raw_order(OrderStatus="2", VolumeTraded=1), OK, 3, True)
        self.assertEqual((self.values("order")[-1].status, self.values("order")[-1].traded), ("CANCELLED", 1))
        self.td.onRtnOrder(self.raw_order(OrderPriceType="1", TimeCondition="3", LimitPrice=0))
        self.assertEqual(self.values("order")[-1].order_type, "MARKET_TO_LIMIT")
        self.assertFalse(self.adapter.health().ready)

    def test_query_timeout_keeps_trader_ready_without_inventing_empty_positions(self):
        self.ready()
        self.issue("positions")
        self.adapter._inflight.sent_at -= QUERY_TIMEOUT + 1
        self.adapter._timer()
        self.assertTrue(self.adapter.health().ready)
        self.assertEqual(self.values("positions"), [])

    def test_broken_sink_does_not_terminate_event_dispatch_or_recurse(self):
        self.ready()
        self.adapter._sink = lambda *args: 1 / 0
        self.adapter._emit("log", "test")
        self.adapter._engine.drain()
        self.assertFalse(self.adapter.health().ready)
        self.assertEqual(self.adapter.health().reason, "event consumer failed")

    def test_trade_identity_is_qualified_with_exchange_and_trading_day(self):
        self.ready()
        self.adapter._native.on_trade(NS(gateway_name="WANGSHU", exchange=Exchange.SHFE, tradeid="100",
            vt_orderid="WANGSHU.1_2_3", vt_symbol="rb2610.SHFE", direction=Direction.LONG,
            offset=Offset.OPEN, volume=1, price=3000))
        self.assertEqual(self.values("trade")[-1].trade_id, "WANGSHU.20260917.SHFE.100")

    def test_old_account_response_after_send_is_suppressed_and_requeried(self):
        self.ready()
        reqid = self.issue("account")
        self.adapter.send_order(OrderIntent("client-1", "rb2610.SHFE", "BUY", "OPEN", 1, 3000),
                                self.adapter.snapshot_revision())
        self.td.onRspQryTradingAccount({"AccountID": "123456", "Balance": 1050,
            "Available": 700, "PreBalance": 1000, "Deposit": 100, "Withdraw": 20}, OK, reqid, True)
        self.assertEqual(self.values("account"), [])
        self.assertIn("account", [query.kind for query in self.adapter._queue])

    def test_duplicate_order_callback_does_not_restart_account_sync(self):
        self.ready()
        row = self.raw_order()
        self.td.onRtnOrder(row)
        revision = self.adapter.snapshot_revision()
        self.adapter._queue.clear()
        self.td.onRtnOrder(row)
        self.assertEqual(self.adapter.snapshot_revision(), revision)
        self.assertEqual(list(self.adapter._queue), [])
        self.assertEqual(len(self.values("order")), 1)
        self.td.onRtnOrder(self.raw_order(OrderStatus="1", VolumeTraded=1))
        self.assertEqual(self.adapter.snapshot_revision(), revision + 1)
        self.assertEqual(self.values("order")[-1].traded, 1)

    def test_duplicate_trade_callback_does_not_restart_account_sync(self):
        self.ready()
        trade = NS(gateway_name="WANGSHU", exchange=Exchange.SHFE, tradeid="100",
            vt_orderid="WANGSHU.1_2_3", vt_symbol="rb2610.SHFE", direction=Direction.LONG,
            offset=Offset.OPEN, volume=1, price=3000)
        self.adapter._native.on_trade(trade)
        revision = self.adapter.snapshot_revision()
        self.adapter._queue.clear()
        self.adapter._native.on_trade(trade)
        self.assertEqual(self.adapter.snapshot_revision(), revision)
        self.assertEqual(list(self.adapter._queue), [])
        self.assertEqual(len(self.values("trade")), 1)

    def test_old_position_batch_after_trade_is_discarded_entirely(self):
        self.ready()
        reqid = self.issue("positions")
        self.td.onRspQryInvestorPosition({"InstrumentID": "m2701", "PosiDirection": "3",
              "Position": 7, "TodayPosition": 2, "LongFrozen": 1}, OK, reqid, False)
        self.adapter._native.on_trade(NS(gateway_name="WANGSHU", exchange=Exchange.DCE, tradeid="100",
            vt_orderid="WANGSHU.1_2_3", vt_symbol="m2701.DCE", direction=Direction.LONG,
            offset=Offset.CLOSE, volume=1, price=3000))
        self.td.onRspQryInvestorPosition({}, OK, reqid, True)
        self.assertEqual(self.values("positions"), [])
        self.assertEqual(self.adapter._positions, {})
        self.assertIn("positions", [query.kind for query in self.adapter._queue])

    def test_market_reconnect_requests_new_account_and_positions(self):
        self.ready()
        self.adapter._queue.clear()
        self.md.onFrontDisconnected(4097)
        self.md.onRspUserLogin({}, OK, 3, True)
        self.assertTrue(self.adapter.health().ready)
        self.assertEqual([query.kind for query in self.adapter._queue], ["account", "positions"])

    def test_queued_snapshot_is_rechecked_at_dispatch(self):
        self.ready()
        reqid = self.issue("account")
        revision = self.adapter.snapshot_revision()
        self.td.onRspQryTradingAccount({"AccountID": "123456", "Balance": 1050,
            "Available": 700, "PreBalance": 1000, "Deposit": 100, "Withdraw": 20}, OK, reqid, True)
        self.adapter.send_order(OrderIntent("client-1", "rb2610.SHFE", "BUY", "OPEN", 1, 3000), revision)
        self.assertEqual(self.values("account"), [])

    def test_atomic_send_check_rejects_revision_changed_after_core_validation(self):
        self.ready()
        revision = self.adapter.snapshot_revision()
        self.adapter._native.on_trade(NS(gateway_name="WANGSHU", exchange=Exchange.SHFE, tradeid="100",
            vt_orderid="WANGSHU.1_2_3", vt_symbol="rb2610.SHFE", direction=Direction.LONG,
            offset=Offset.OPEN, volume=1, price=3000))
        with self.assertRaisesRegex(RuntimeError, "state changed"):
            self.adapter.send_order(OrderIntent("client-1", "rb2610.SHFE", "BUY", "OPEN", 1, 3000), revision)
        self.assertFalse(any(call[0] == "insert" for call in self.td.calls))

    def test_tick_preserves_vnpy_bar_generator_inputs(self):
        self.ready()
        self.adapter._native.on_tick(NS(vt_symbol="rb2610.SHFE", last_price=3500,
            bid_price_1=3499, ask_price_1=3501, limit_up=3850, limit_down=3150,
            datetime=datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc),
            volume=123, turnover=4305000, open_interest=12000))
        tick = self.values("tick")[-1]
        self.assertEqual((tick.volume, tick.turnover, tick.open_interest), (123, 4305000, 12000))

    def test_tick_keeps_exchange_timestamp(self):
        self.ready()
        timestamp = datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc)
        self.adapter._native.on_tick(NS(vt_symbol="rb2610.SHFE", last_price=3000, bid_price_1=2999,
            ask_price_1=3001, bid_price_2=2998, ask_price_2=3002,
            limit_up=3300, limit_down=2700, datetime=timestamp))
        tick = self.values("tick")[-1]
        self.assertEqual(tick.timestamp, timestamp.timestamp())
        self.assertEqual((tick.bid_price_2, tick.ask_price_2), (2998, 3002))

    def test_tick_previous_settlement_comes_from_raw_md_and_missing_is_not_zero(self):
        self.ready()
        self.md.onRtnDepthMarketData({"InstrumentID": "rb2610", "ExchangeID": "SHFE", "PreSettlementPrice": 3456})
        self.assertEqual(self.values("tick")[-1].pre_settlement_price, 3456)
        self.md.onRtnDepthMarketData({"InstrumentID": "rb2610", "ExchangeID": "SHFE", "PreSettlementPrice": 1.797e308})
        self.assertIsNone(self.values("tick")[-1].pre_settlement_price)


if __name__ == "__main__":
    unittest.main()
