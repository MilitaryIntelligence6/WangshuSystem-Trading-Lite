"""Local public connection profiles and opt-in OS-vault CTP credentials."""
import json
import os
from pathlib import Path
import tempfile


SERVICE = "Wangshu Trading Lite"
PUBLIC_FIELDS = ("账户环境", "经纪商代码", "产品名称", "交易服务器", "行情服务器",
                 "交易服务器备选", "行情服务器备选", "前置组", "柜台环境")
SECRET_FIELDS = ("用户名", "密码", "授权编码")


class ConnectionStoreError(RuntimeError):
    pass


def _keyring():
    try:
        import keyring
        backend = keyring.get_keyring()
        secure = ("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService")
        module = type(backend).__module__
        if backend.priority <= 0 or not any(module == name or module.startswith(name + ".") for name in secure):
            raise RuntimeError
        return keyring
    except Exception:
        raise ConnectionStoreError("系统安全凭据库不可用；凭证未保存，不会改用明文文件") from None


class ConnectionStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, environment):
        if environment not in {"simulation", "live"}:
            raise ConnectionStoreError("账户环境必须为仿真或实盘")
        return self.directory / f"connection-{environment}.local.json"

    @staticmethod
    def _public(settings, environment):
        if not isinstance(settings, dict) or any(not isinstance(settings.get(key, ""), str) for key in PUBLIC_FIELDS):
            raise ConnectionStoreError("连接参数格式无效")
        result = {key: settings[key].strip() for key in PUBLIC_FIELDS if key in settings}
        result["账户环境"] = environment
        if result.get("前置组", "1") not in {"1", "2"} or result.get("柜台环境", "实盘") not in {"实盘", "测试"}:
            raise ConnectionStoreError("前置组或 SDK 模式无效")
        return result

    @staticmethod
    def _identity(settings):
        return [settings.get(key, "") for key in ("经纪商代码", "产品名称")]

    def _read(self, environment):
        path = self._path(environment)
        if not path.exists():
            return {"settings": {"账户环境": environment}, "credentials_saved": False}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or type(value.get("credentials_saved", False)) is not bool:
                raise ValueError
            return {"settings": self._public(value["settings"], environment),
                    "credentials_saved": value.get("credentials_saved", False)}
        except (OSError, ValueError, TypeError, KeyError):
            raise ConnectionStoreError("本机连接参数无法读取；请检查文件权限或重新保存参数") from None

    def _write(self, environment, value):
        path = self._path(environment)
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".connection-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            raise ConnectionStoreError("本机连接参数保存失败，请检查数据目录权限") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(self, settings, remember_credentials=False):
        if not isinstance(settings, dict) or type(remember_credentials) is not bool:
            raise ConnectionStoreError("连接参数或保存选项无效")
        environment = settings.get("账户环境", "simulation")
        self._path(environment)
        public = self._public(settings, environment)
        try:
            prior = self._read(environment)
        except ConnectionStoreError:
            # Explicitly saving new public parameters can repair a broken file.
            # Corrupt metadata never authorizes loading previously saved secrets.
            prior = {"settings": {}, "credentials_saved": False}
        saved = prior["credentials_saved"] and self._identity(prior["settings"]) == self._identity(public)
        if remember_credentials:
            credentials = {key: settings.get(key, "") for key in SECRET_FIELDS}
            if any(not isinstance(value, str) for value in credentials.values()):
                raise ConnectionStoreError("账号、密码和 AuthCode 格式无效")
            credentials["用户名"] = credentials["用户名"].strip()
            supplied = [bool(value) for value in credentials.values()]
            if any(supplied) and not all(supplied):
                raise ConnectionStoreError("账号、密码和 AuthCode 必须一起填写；或全部留空以保留已有凭据")
            if all(supplied):
                vault = _keyring()
                try:
                    vault.set_password(SERVICE, "ctp-" + environment,
                                       json.dumps({"identity": self._identity(public), "credentials": credentials}, ensure_ascii=False))
                except Exception:
                    raise ConnectionStoreError("系统安全凭据库保存失败；凭证未写入普通配置") from None
                saved = True
        self._write(environment, {"settings": public, "credentials_saved": saved})
        return "连接参数已保存在本机；凭证保存在系统安全凭据库" if saved else "连接参数已保存在本机；凭证未保存到普通配置"

    def load(self, environment="simulation", with_credentials=True):
        value = self._read(environment)
        settings = value["settings"]
        if with_credentials and value["credentials_saved"]:
            vault = _keyring()
            try:
                raw = vault.get_password(SERVICE, "ctp-" + environment)
                if raw is None:
                    raise ValueError
                payload = json.loads(raw)
                credentials = payload["credentials"]
                if payload["identity"] != self._identity(settings) or not isinstance(credentials, dict):
                    raise ValueError
                if any(not isinstance(credentials.get(key), str) or not credentials[key] for key in SECRET_FIELDS):
                    raise ValueError
                settings.update({key: credentials[key] for key in SECRET_FIELDS})
            except Exception:
                raise ConnectionStoreError("无法读取匹配的系统安全凭据；普通参数仍可单独加载，请重新输入账号、密码和 AuthCode") from None
        return settings

    def clear_credentials(self, environment="simulation"):
        self._path(environment)
        vault = _keyring()
        try:
            if vault.get_password(SERVICE, "ctp-" + environment) is not None:
                vault.delete_password(SERVICE, "ctp-" + environment)
        except Exception:
            raise ConnectionStoreError("无法清除系统安全凭据，请检查本机凭据库") from None
        try:
            value = self._read(environment)
        except ConnectionStoreError:
            return "本机已保存的账号、密码和 AuthCode 已清除；普通参数文件损坏，请重新保存连接参数"
        value["credentials_saved"] = False
        self._write(environment, value)
        return "本机已保存的账号、密码和 AuthCode 已清除；普通连接参数保留"
