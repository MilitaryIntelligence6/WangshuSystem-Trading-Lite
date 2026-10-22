"""Application assembly. Clients may use Runtime without importing Qt or CTA."""
from collections import deque
from contextlib import contextmanager
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit

from .core import TradingError, TradingKernel
from .models import Connection, OrderIntent
from .storage import Journal


_native_owner = threading.Lock()


def default_directory():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "WangshuLite"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/WangshuLite"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "wangshu-lite"


@contextmanager
def native_directory(directory):
    """Keep vn.py files in this account's runtime directory, including re-imports."""
    directory = Path(directory).resolve()
    (directory / ".vntrader").mkdir(parents=True, exist_ok=True)
    prior = Path.cwd()
    os.chdir(directory)
    try:
        # Import only on explicit CTP/CTA use; the standard-library core is standalone.
        from vnpy.trader import utility
        utility.TRADER_DIR, utility.TEMP_DIR = directory, directory / ".vntrader"
        yield
    finally:
        os.chdir(prior)


class Runtime:
    def __init__(self, directory=None):
        self.directory = Path(directory or default_directory()).resolve()
        self.kernel = self.journal = self.gateway = self.strategies = None
        self.mode = "disconnected"
        self._logs = deque(maxlen=300)
        self._log_lock = threading.Lock()
        self._lifecycle = threading.RLock()
        self._pulse_stop = threading.Event()
        self._pulse_thread = None
        self._owns_native = False
        self._close_failed = False
        self._secrets = ()

    def log(self, message):
        message = str(message)
        for secret in self._secrets:
            message = message.replace(secret, "[已隐藏]")
        with self._log_lock:
            self._logs.append({"time": time.time(), "message": message})

    def connect(self, mode="paper", settings=None, settlement_confirmed=False):
        with self._lifecycle:
            if self.kernel is not None:
                raise TradingError("已有交易会话，请先断开")
            if mode not in {"paper", "ctp"}:
                raise TradingError("连接模式只能是 paper 或 ctp")
            settings = dict(settings or {})
            if mode == "ctp":
                if settlement_confirmed is not True:
                    raise TradingError("请先核对结算，并确认登录将确认当日结算单")
                for key in ("用户名", "密码", "经纪商代码", "产品名称", "授权编码", "交易服务器", "行情服务器"):
                    if not isinstance(settings.get(key), str) or not settings[key].strip():
                        raise TradingError(f"请填写 CTP {key}")
                for key in ("交易服务器", "行情服务器"):
                    try:
                        url = urlsplit(settings[key])
                        valid = url.scheme == "tcp" and url.hostname and url.port and not url.username and not url.password and not url.path and not url.query and not url.fragment
                    except ValueError:
                        valid = False
                    if not valid:
                        raise TradingError(f"{key}需为 tcp://主机:端口")
                if settings.get("柜台环境", "实盘") not in {"实盘", "测试"}:
                    raise TradingError("SDK 模式需按券商要求选择生产或测试")
                if not _native_owner.acquire(blocking=False):
                    raise TradingError("同一进程只能打开一个原生 CTP 会话")
                self._owns_native = True
            self._secrets = tuple(sorted((settings[k] for k in ("用户名", "密码", "授权编码") if settings.get(k)), key=len, reverse=True))
            environment = settings.get("账户环境", "simulation")
            if environment not in {"simulation", "live"}:
                if self._owns_native:
                    self._owns_native = False
                    _native_owner.release()
                raise TradingError("请选择实际账户环境：仿真或实盘")
            identity = "|".join((mode, settings.get("经纪商代码", ""), settings.get("用户名", "PAPER"), environment))
            binding = hashlib.sha256(identity.encode()).hexdigest()
            account_directory = self.directory / ("paper" if mode == "paper" else "ctp-" + binding[:20])
            try:
                self.journal = Journal(account_directory, binding)
                self.kernel = TradingKernel(self.journal, self.log, settings.get("用户名", "PAPER"), self._secrets)
                if mode == "paper":
                    from .paper import PaperGateway
                    self.gateway = PaperGateway(self.kernel.on_event, self.journal)
                else:
                    with native_directory(account_directory):
                        from .ctp import VnpyGateway
                        self.gateway = VnpyGateway(settings, self.kernel.on_event)
                self.kernel.attach(self.gateway)
                self.mode = mode
                self.gateway.connect()
                self.gateway.query()
                self._pulse_stop.clear()
                if mode == "paper":
                    self._pulse_thread = threading.Thread(target=self._pulse, name="lite-paper", daemon=True)
                    self._pulse_thread.start()
                self.log("已启动会话，默认禁止下单；请核对柜台状态后允许下单")
                return "连接已启动，最终状态以柜台回报为准"
            except Exception as exc:
                self.log(f"连接失败：{exc}")
                self.disconnect()
                raise TradingError("连接失败，请检查安装环境与本地日志；敏感连接参数不会保存") from None

    def _pulse(self):
        while not self._pulse_stop.wait(1):
            try:
                self.gateway.pulse()
            except Exception as exc:
                self.log(f"离线演练异常：{exc}")
                if self.kernel:
                    self.kernel.on_event("connection", Connection(False, False, reason="演练异常"))
                return

    def _kernel(self):
        if self.kernel is None:
            raise TradingError("请先连接柜台或离线演练")
        return self.kernel

    def enable(self):
        self._kernel().enable()
        return "已允许下单；策略仍须单独启动"

    def disable(self):
        self._kernel().disable()
        return "已禁止新单，已发委托状态仍以柜台回报为准"

    def subscribe(self, symbol):
        self._kernel().subscribe(symbol)
        return "已提交行情订阅"

    def query(self):
        self._kernel().query()
        return "已请求资金、持仓与合约刷新，等待柜台回报"

    def send_order(self, payload):
        allowed = {f.name for f in fields(OrderIntent)}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise TradingError("委托包含不支持的字段")
        payload = dict(payload)
        payload.setdefault("client_id", secrets.token_hex(16))
        try:
            intent = OrderIntent(**payload)
        except TypeError:
            raise TradingError("委托字段不完整") from None
        oid = self._kernel().send_order(intent, source="manual")
        self.query()
        return oid

    def cancel(self, order_id):
        self._kernel().cancel(order_id)
        self.query()
        return "已请求撤单，等待柜台最终确认"

    def cancel_all(self):
        return self._kernel().cancel_all()

    def reconcile(self, client_id, order_id):
        self._kernel().reconcile(client_id, order_id)
        return "已绑定匹配柜台委托；核对后仍须手动允许下单"

    def paper_fill(self, order_id, volume=None):
        if self.mode != "paper":
            raise TradingError("合成成交只允许离线演练")
        return self.gateway.fill(order_id, volume)

    def strategy(self, action, **kwargs):
        with self._lifecycle:
            kernel = self._kernel()
            if action == "stop_all" and self.strategies is None:
                return None
            if self.strategies is None:
                try:
                    with native_directory(self.journal.directory):
                        from .strategies import StrategyManager
                        self.strategies = StrategyManager(kernel, self.journal.directory)
                except ImportError:
                    raise TradingError("请安装策略依赖：pip install -e '.[strategy]'；人工交易仍可使用") from None
            methods = {"load": "load_file", "load_module": "load_module", "add": "add", "edit": "edit", "initialize": "initialize",
                       "start": "start", "stop": "stop", "stop_all": "stop_all", "remove": "remove",
                       "class_parameters": "class_parameters", "import_history": "import_history"}
            if action not in methods:
                raise TradingError("不支持的策略操作")
            manager = self.strategies
        return getattr(manager, methods[action])(**kwargs)

    def snapshot(self):
        with self._lifecycle:
            if self.kernel:
                state = self.kernel.snapshot()
            else:
                state = {"enabled": False, "fatal": False, "connection": {"connected": False, "ready": False, "trading_day": "", "reason": "尚未连接"},
                         "account": None, "positions": [], "orders": [], "trades": [], "ticks": [], "contracts": [], "unresolved": []}
            state["mode"] = self.mode
            state["strategies"] = self.strategies.snapshot() if self.strategies else []
            with self._log_lock:
                state["logs"] = list(self._logs)
            return state

    def disconnect(self):
        with self._lifecycle:
            if self._close_failed:
                raise TradingError("上次会话关闭失败，已保留账户锁并禁止新会话；请核对柜台后退出重启应用")
            self._pulse_stop.set()
            if self._pulse_thread:
                self._pulse_thread.join(3)
                if self._pulse_thread.is_alive():
                    self._close_failed = True
                    if self.kernel:
                        self.kernel.enabled = False
                    raise TradingError("演练线程尚未退出，已保留会话锁；请退出并重新打开应用")
                self._pulse_thread = None
            try:
                if self.kernel:
                    self.kernel.enabled = False
                if self.strategies:
                    self.strategies.close()
                if self.kernel:
                    self.kernel.close()
                elif self.gateway:
                    self.gateway.close()
                if self.journal:
                    self.journal.close()
            except Exception as exc:
                self._close_failed = True
                if self.kernel:
                    self.kernel.enabled, self.kernel.fatal = False, True
                self.log(f"会话关闭失败，已保留账户锁：{exc}")
                raise TradingError("关闭失败，旧线程状态须核查；请退出重启，不允许启动第二会话") from None
            self.strategies = self.kernel = self.gateway = self.journal = None
            self.mode = "disconnected"
            if self._owns_native:
                self._owns_native = False
                _native_owner.release()
            self.log("会话已断开；断开不等于柜台活动委托已撤销，请核对最终回报")
            return "会话已断开"

    def close(self):
        return self.disconnect()
