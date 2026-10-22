"""Everbright CTP simulation wiring, using the original fake native transport."""
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wangshu_lite.core import TradingError
from wangshu_lite.ctp import VnpyGateway
from wangshu_lite.models import OrderIntent
from wangshu_lite.runtime import Runtime
from test_ctp import fake_bindings
from test_connections import FakeVault


SETTINGS = {"用户名": "simulation-user", "密码": " secret-password ", "经纪商代码": "broker",
            "产品名称": "approved-app", "授权编码": " secret-auth ",
            "交易服务器": "tcp://first-trader.invalid:1", "行情服务器": "tcp://first-market.invalid:2",
            "交易服务器备选": "tcp://second-trader.invalid:3", "行情服务器备选": "tcp://second-market.invalid:4",
            "前置组": "1", "柜台环境": "实盘", "账户环境": "simulation"}
OK = {"ErrorID": 0}


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(Path(self.temp.name))
        for mocked in (patch("wangshu_lite.ctp._load_bindings", side_effect=fake_bindings),
                       patch("wangshu_lite.ctp.sys.platform", "win32"),
                       patch("wangshu_lite.runtime.native_directory", side_effect=lambda *_: nullcontext())):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.addCleanup(self.runtime.close)

    def connect(self, **changes):
        settings = dict(SETTINGS)
        settings.update(changes)
        self.runtime.connect("simulation", settings, True)
        return self.runtime.gateway

    def ready(self):
        gateway = self.runtime.gateway
        td, md = gateway._native.td_api, gateway._native.md_api
        td.onRspAuthenticate({}, OK, 1, True)
        td.onRspUserLogin({"FrontID": 1, "SessionID": 2, "TradingDay": "20261022", "MaxOrderRef": "000042"}, OK, 2, True)
        md.onRspUserLogin({}, OK, 1, True)
        td.onRspSettlementInfoConfirm({}, OK, 3, True)
        gateway._last_query = float("-inf")
        gateway._timer()
        reqid = gateway._inflight.reqid
        td.onRspQryInstrument({"InstrumentID": "rb2610", "ExchangeID": "SHFE"}, OK, reqid, False)
        td.onRspQryInstrument({}, OK, reqid, True)
        gateway._engine.drain()

    def test_simulation_is_real_ctp_path_with_independent_sdk_mode(self):
        with patch("wangshu_lite.paper.PaperGateway") as paper:
            gateway = self.connect(账户环境="live")
        paper.assert_not_called()
        self.assertIsInstance(gateway, VnpyGateway)
        self.assertEqual(gateway._settings["账户环境"], "simulation")
        self.assertEqual(gateway._settings["柜台环境"], "实盘")
        self.assertEqual(gateway._settings["密码"], SETTINGS["密码"])
        self.assertEqual(self.runtime.snapshot()["connection_profile"]["environment"], "simulation")
        self.assertFalse(self.runtime.kernel.enabled)

    def test_selected_group_two_is_passed_as_a_complete_pair_without_fallback(self):
        with patch.object(VnpyGateway, "_front_reachable", side_effect=AssertionError("network probe not permitted")):
            gateway = self.connect(前置组="2", 交易服务器="unselected address may remain a draft")
        self.assertEqual(gateway._settings["交易服务器"], SETTINGS["交易服务器备选"])
        self.assertEqual(gateway._settings["行情服务器"], SETTINGS["行情服务器备选"])
        self.assertNotIn("交易服务器备选", gateway._settings)
        self.assertNotIn("行情服务器备选", gateway._settings)
        profile = self.runtime.snapshot()["connection_profile"]
        self.assertEqual(profile["front_pair"], "2")
        self.assertEqual(profile["td_address"], SETTINGS["交易服务器备选"])

    def test_missing_selected_market_or_bad_group_never_constructs_native_api(self):
        for changes in ({"前置组": "2", "行情服务器备选": ""}, {"前置组": "3"}):
            with patch("wangshu_lite.ctp._load_bindings") as native, self.assertRaises(TradingError):
                self.connect(**changes)
            native.assert_not_called()
            self.assertIsNone(self.runtime.kernel)

    def test_front_group_change_preserves_idempotency_evidence(self):
        self.connect()
        directory = self.runtime.journal.directory
        self.runtime.journal.reserve(OrderIntent("unknown-before-front-switch", "rb2610.SHFE", "BUY", "OPEN", 1, 3500), "20261022")
        self.runtime.disconnect()
        self.connect(前置组="2")
        self.assertEqual(self.runtime.journal.directory, directory)
        self.assertEqual(self.runtime.kernel.unresolved()[0]["client_id"], "unknown-before-front-switch")

    def test_login_settlement_and_catalog_stages_are_observed(self):
        self.connect()
        self.assertFalse(any(self.runtime.snapshot()["connection_status"].values()))
        self.ready()
        self.assertTrue(all(self.runtime.snapshot()["connection_status"].values()))
        self.assertTrue(self.runtime.snapshot()["connection"]["ready"])
        self.assertFalse(self.runtime.kernel.enabled)

    def test_native_authentication_error_is_visible_and_redacted(self):
        gateway = self.connect()
        gateway._report_ctp_error("Trader.onRspAuthenticate", error={"ErrorID": 7, "ErrorMsg": "bad auth " + SETTINGS["授权编码"]}, fault="authentication failed")
        gateway._engine.drain()
        state = self.runtime.snapshot()
        self.assertIn("ErrorID", state["connection_error"])
        self.assertIn("7", state["connection_error"])
        self.assertFalse(state["connection"]["ready"])
        serialized = json.dumps(state)
        self.assertNotIn(SETTINGS["密码"], serialized)
        self.assertNotIn(SETTINGS["授权编码"], serialized)

    def test_market_disconnect_is_visible_without_faking_trader_disconnect(self):
        gateway = self.connect()
        self.ready()
        gateway._native.md_api.onFrontDisconnected(1)
        gateway._engine.drain()
        phases = self.runtime.snapshot()["connection_status"]
        self.assertTrue(phases["trader_logged_in"])
        self.assertFalse(phases["market_logged_in"])

    def test_native_initialization_failure_explains_problem_and_releases_session(self):
        with patch("wangshu_lite.ctp._load_bindings", side_effect=RuntimeError("native bindings unavailable " + SETTINGS["授权编码"])):
            with self.assertRaisesRegex(TradingError, "native bindings unavailable") as error:
                self.connect()
        self.assertNotIn(SETTINGS["授权编码"], str(error.exception))
        self.assertIn("native bindings unavailable", self.runtime.snapshot()["connection_error"])
        self.assertIsNone(self.runtime.kernel)
        self.connect()

    def test_unavailable_vault_still_restores_public_connection_parameters(self):
        from wangshu_lite.connections import ConnectionStoreError
        with patch("wangshu_lite.connections._keyring", return_value=FakeVault()):
            self.runtime.save_connection(SETTINGS, remember_credentials=True)
        with patch("wangshu_lite.connections._keyring", side_effect=ConnectionStoreError("系统安全凭据库不可用")):
            loaded = self.runtime.load_connection()
        self.assertEqual(loaded["交易服务器"], SETTINGS["交易服务器"])
        self.assertIn("安全凭据库", loaded["_credential_error"])
        self.assertNotIn("用户名", loaded)
        self.assertNotIn("密码", loaded)
        self.assertNotIn("授权编码", loaded)


if __name__ == "__main__":
    unittest.main()
