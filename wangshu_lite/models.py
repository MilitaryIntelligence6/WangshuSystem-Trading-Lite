"""Single brokerage account domain objects; no allocation or virtual ledgers."""
from dataclasses import dataclass
from typing import Callable, Protocol


class GatewayPreflightError(RuntimeError):
    """The gateway rejected locally before making any mutating native call."""


@dataclass(frozen=True)
class Snapshot:
    data: object
    revision: int


@dataclass(frozen=True)
class Connection:
    connected: bool
    ready: bool
    trading_day: str = ""
    reason: str = ""
    market_connected: bool | None = None
    trader_connected: bool | None = None


@dataclass(frozen=True)
class Contract:
    vt_symbol: str
    size: float
    pricetick: float
    min_volume: int = 1
    max_volume: int = 0
    product: str = "FUTURES"
    name: str = ""
    # Effective investor/speculation rates, never exchange reference rates.
    long_margin_by_money: float | None = None
    long_margin_by_volume: float | None = None
    short_margin_by_money: float | None = None
    short_margin_by_volume: float | None = None
    margin_source: str = ""
    margin_trading_day: str = ""
    option_type: str = ""
    option_underlying: str = ""
    option_strike: float | None = None
    option_expiry: str = ""  # YYYYMMDD from the broker, never calculated from a code
    option_portfolio: str = ""


@dataclass(frozen=True)
class Tick:
    vt_symbol: str
    last_price: float
    bid_price: float
    ask_price: float
    limit_up: float
    limit_down: float
    timestamp: float  # UTC epoch, exchange timestamp
    pre_settlement_price: float | None = None
    bid_price_2: float | None = None
    ask_price_2: float | None = None
    volume: float = 0
    turnover: float = 0
    open_interest: float = 0


@dataclass(frozen=True)
class Account:
    account_id: str
    balance: float
    available: float
    daily_pnl: float  # Balance - PreBalance - Deposit + Withdraw, CTP trading day
    trading_day: str
    margin: float | None = None
    frozen_margin: float | None = None
    frozen_cash: float | None = None
    frozen_commission: float | None = None
    commission: float | None = None
    position_pnl: float | None = None
    close_pnl: float | None = None
    pre_balance: float | None = None
    deposit: float | None = None
    withdraw: float | None = None
    cash_in: float | None = None


@dataclass(frozen=True)
class Position:
    vt_symbol: str
    side: str  # LONG / SHORT
    volume: int
    yesterday: int
    frozen: int = 0
    # CTP PositionCost / (volume * multiplier), a settlement cost basis;
    # this is not a reconstructed historical opening average.
    average_price: float | None = None
    position_pnl: float | None = None
    margin: float | None = None
    price_basis: str = ""


@dataclass(frozen=True)
class OrderIntent:
    client_id: str
    vt_symbol: str
    side: str  # BUY / SELL
    offset: str  # OPEN / CLOSE / CLOSE_TODAY / CLOSE_YESTERDAY
    volume: int
    price: float
    order_type: str = "LIMIT"  # LIMIT / MARKET (price=0) / FAK / FOK
    condition_mode: str = "IMMEDIATE"  # IMMEDIATE / SERVER
    trigger_price: float = 0
    comparator: str = ""


@dataclass(frozen=True)
class Order:
    order_id: str  # vn.py vt_orderid, includes gateway
    vt_symbol: str
    side: str
    offset: str
    volume: int
    price: float
    traded: int = 0
    status: str = "SUBMITTING"
    order_type: str = "LIMIT"
    condition_mode: str = "IMMEDIATE"
    trigger_price: float = 0
    comparator: str = ""
    exchange_order_id: str = ""
    parent_exchange_order_id: str = ""

    @property
    def active(self) -> bool:
        return self.status not in {"FILLED", "CANCELLED", "REJECTED"}


@dataclass(frozen=True)
class Trade:
    trade_id: str
    order_id: str
    vt_symbol: str
    side: str
    offset: str
    volume: int
    price: float


Sink = Callable[[str, object], None]


class Gateway(Protocol):
    """Events: connection, contract, tick, account, positions (full list), order,
    trade, log (string), error (string). account/positions use Snapshot wrappers.
    Sink must be safe on callback threads.
    connect may auto-confirm settlement as implemented by upstream CTP gateway.
    """
    def connect(self) -> None: ...
    def health(self) -> Connection: ...
    def snapshot_revision(self) -> int: ...
    def subscribe(self, vt_symbol: str) -> None: ...
    # None permits a manual precheck against the last received account data.
    def send_order(self, intent: OrderIntent, expected_revision: int | None) -> str: ...
    def cancel_order(self, order: Order) -> None: ...
    def query(self) -> None: ...
    def close(self) -> None: ...
