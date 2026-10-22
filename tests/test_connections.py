"""Temporary profiles and fake vault only; never access a real credential store."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wangshu_lite.connections import ConnectionStore, ConnectionStoreError, SECRET_FIELDS, SERVICE, _keyring


class FakeVault:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, value):
        self.values[service, account] = value

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class ConnectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.store = ConnectionStore(self.directory)
        self.vault = FakeVault()
        self.settings = {"账户环境": "simulation", "经纪商代码": "test-broker", "产品名称": "test-app",
                         "前置组": "2", "柜台环境": "实盘", "交易服务器": "tcp://one.invalid:1",
                         "行情服务器": "tcp://one.invalid:2", "交易服务器备选": "tcp://two.invalid:3",
                         "行情服务器备选": "tcp://two.invalid:4", "用户名": " test-user ",
                         "密码": " secret with spaces ", "授权编码": " auth with spaces "}

    def test_public_save_never_calls_vault_or_writes_secrets(self):
        with patch("wangshu_lite.connections._keyring", side_effect=AssertionError("no vault")):
            self.store.save(dict(self.settings, arbitrary="must not persist"))
            loaded = self.store.load()
        self.assertEqual(loaded["前置组"], "2")
        self.assertNotIn("arbitrary", loaded)
        content = (self.directory / "connection-simulation.local.json").read_text()
        for key in SECRET_FIELDS:
            self.assertNotIn(key, loaded)
            self.assertNotIn(self.settings[key], content)
            self.assertNotIn(key, content)

    def test_secure_save_and_load_preserves_password_and_auth_whitespace(self):
        with patch("wangshu_lite.connections._keyring", return_value=self.vault):
            self.store.save(self.settings, True)
            loaded = self.store.load()
        self.assertEqual(loaded["用户名"], "test-user")
        self.assertEqual(loaded["密码"], self.settings["密码"])
        self.assertEqual(loaded["授权编码"], self.settings["授权编码"])
        self.assertIn((SERVICE, "ctp-simulation"), self.vault.values)
        self.assertNotIn(self.settings["密码"], (self.directory / "connection-simulation.local.json").read_text())

    def test_public_load_of_saved_profile_never_reads_vault(self):
        with patch("wangshu_lite.connections._keyring", return_value=self.vault):
            self.store.save(self.settings, True)
        with patch("wangshu_lite.connections._keyring", side_effect=AssertionError("no vault")):
            self.assertNotIn("密码", self.store.load(with_credentials=False))

    def test_clear_removes_only_credentials(self):
        with patch("wangshu_lite.connections._keyring", return_value=self.vault):
            self.store.save(self.settings, True)
            self.store.clear_credentials()
            self.assertNotIn("密码", self.store.load())
        self.assertEqual(self.vault.values, {})
        self.assertEqual(self.store.load()["交易服务器备选"], self.settings["交易服务器备选"])

    def test_all_blank_preserves_saved_credentials_but_partial_does_not_save(self):
        with patch("wangshu_lite.connections._keyring", return_value=self.vault):
            self.store.save(self.settings, True)
            blank = dict(self.settings, **{key: "" for key in SECRET_FIELDS})
            self.store.save(blank, True)
            self.assertEqual(self.store.load()["密码"], self.settings["密码"])
            before = (self.directory / "connection-simulation.local.json").read_bytes()
            with self.assertRaises(ConnectionStoreError):
                self.store.save(dict(blank, 用户名="partial-user"), True)
            self.assertEqual(before, (self.directory / "connection-simulation.local.json").read_bytes())

    def test_vault_failure_never_falls_back_to_plaintext_or_echoes_secret(self):
        self.store.save(self.settings)
        before = (self.directory / "connection-simulation.local.json").read_bytes()
        with patch("wangshu_lite.connections._keyring", return_value=self.vault), \
                patch.object(self.vault, "set_password", side_effect=RuntimeError(self.settings["密码"])):
            with self.assertRaises(ConnectionStoreError) as error:
                self.store.save(self.settings, True)
        self.assertNotIn(self.settings["密码"], str(error.exception))
        self.assertEqual(before, (self.directory / "connection-simulation.local.json").read_bytes())

    def test_failed_atomic_replace_keeps_old_profile_and_cleans_temp(self):
        self.store.save(self.settings)
        path = self.directory / "connection-simulation.local.json"
        before = path.read_bytes()
        with patch("wangshu_lite.connections.os.replace", side_effect=OSError("disk problem")), self.assertRaises(ConnectionStoreError):
            self.store.save(dict(self.settings, 前置组="1"))
        self.assertEqual(before, path.read_bytes())
        self.assertEqual([p.name for p in self.directory.iterdir()], [path.name])

    def test_simulation_and_live_profiles_and_vaults_are_separate(self):
        with patch("wangshu_lite.connections._keyring", return_value=self.vault):
            self.store.save(self.settings, True)
            self.store.save(dict(self.settings, 账户环境="live", 用户名="live-user"), True)
            self.assertEqual(self.store.load("simulation")["用户名"], "test-user")
            self.assertEqual(self.store.load("live")["用户名"], "live-user")
        self.assertEqual(len(self.vault.values), 2)

    def test_changed_broker_never_loads_previous_credentials(self):
        with patch("wangshu_lite.connections._keyring", return_value=self.vault):
            self.store.save(self.settings, True)
            self.store.save(dict(self.settings, 经纪商代码="another-broker"))
            self.assertNotIn("密码", self.store.load())
        path = self.directory / "connection-simulation.local.json"
        data = json.loads(path.read_text())
        data["credentials_saved"] = True
        path.write_text(json.dumps(data))
        with patch("wangshu_lite.connections._keyring", return_value=self.vault), self.assertRaises(ConnectionStoreError):
            self.store.load()

    def test_corrupt_or_invalid_profiles_return_generic_errors(self):
        for environment in ("../escape", "unknown"):
            with self.assertRaises(ConnectionStoreError):
                self.store.load(environment)
        path = self.directory / "connection-simulation.local.json"
        path.write_text('{"settings":{"密码":"hidden"},"credentials_saved":true}')
        with patch("wangshu_lite.connections._keyring", return_value=self.vault), self.assertRaises(ConnectionStoreError):
            self.store.load()
        path.write_text("not-json")
        with self.assertRaises(ConnectionStoreError):
            self.store.load()

    def test_only_native_secure_vault_backends_are_accepted(self):
        for module, priority, allowed in (("keyring.backends.macOS", 1, True),
                                         ("keyring.backends.Windows", 1, True),
                                         ("keyring.backends.SecretService", 1, True),
                                         ("keyrings.alt.file", 1, False),
                                         ("keyring.backends.macOSPlain", 1, False),
                                         ("keyring.backends.macOS", 0, False)):
            backend = type("Backend", (), {"__module__": module, "priority": priority})()
            keyring = SimpleNamespace(get_keyring=lambda: backend)
            with patch.dict("sys.modules", {"keyring": keyring}):
                if allowed:
                    self.assertIs(_keyring(), keyring)
                else:
                    with self.assertRaises(ConnectionStoreError):
                        _keyring()

    def test_explicit_save_repairs_corrupt_profile_without_trusting_old_vault_flag(self):
        with patch("wangshu_lite.connections._keyring", return_value=self.vault):
            self.store.save(self.settings, True)
        path = self.directory / "connection-simulation.local.json"
        path.write_text('{"settings":broken,"credentials_saved":true}')
        with patch("wangshu_lite.connections._keyring", side_effect=AssertionError("no implicit vault access")):
            self.store.save(self.settings)
            loaded = self.store.load()
        self.assertEqual(loaded["经纪商代码"], self.settings["经纪商代码"])
        self.assertNotIn("密码", loaded)
        self.assertFalse(json.loads(path.read_text())["credentials_saved"])

    def test_corrupt_profile_does_not_prevent_vault_deletion(self):
        path = self.directory / "connection-simulation.local.json"
        with patch("wangshu_lite.connections._keyring", return_value=self.vault):
            self.store.save(self.settings, True)
            path.write_text("broken original profile")
            original = path.read_bytes()
            message = self.store.clear_credentials()
        self.assertEqual(self.vault.values, {})
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("已清除", message)
        self.assertIn("损坏", message)


if __name__ == "__main__":
    unittest.main()
