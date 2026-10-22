"""Explicit CSV bar import for CTA warm-up; CTP itself provides no bar history."""
import csv
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData


TZ = ZoneInfo("Asia/Shanghai")


class BarHistory:
    def __init__(self, directory):
        self.db = sqlite3.connect(Path(directory) / "history.db", check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT, interval TEXT, time REAL, open REAL, high REAL, low REAL,
            close REAL, volume REAL, turnover REAL, interest REAL,
            PRIMARY KEY(symbol, interval, time))""")

    def import_csv(self, path, vt_symbol, interval="1m"):
        symbol, exchange = vt_symbol.rsplit(".", 1)
        Exchange(exchange)
        Interval(interval)
        rows = []
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            for number, row in enumerate(csv.DictReader(stream), 2):
                try:
                    stamp = datetime.fromisoformat(row["datetime"])
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=TZ)
                    values = [float(row[name]) for name in ("open", "high", "low", "close")]
                    values += [float(row.get(name) or 0) for name in ("volume", "turnover", "open_interest")]
                    if not all(math.isfinite(value) for value in values):
                        raise ValueError("non-finite number")
                    if min(values[:4]) <= 0 or values[1] < max(values[0], values[2], values[3]) or values[2] > min(values[:2] + [values[3]]) or min(values[4:]) < 0:
                        raise ValueError("invalid OHLC/volume")
                    rows.append((vt_symbol, interval, stamp.timestamp(), *values))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"CSV 第 {number} 行无效：{exc}") from None
        if not rows:
            raise ValueError("CSV 没有可导入的 K 线")
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def load(self, vt_symbol, days, interval):
        if type(days) is not int or days <= 0:
            raise ValueError("历史天数必须为正整数")
        symbol, exchange = vt_symbol.rsplit(".", 1)
        start = datetime.now(TZ) - timedelta(days=days)
        rows = self.db.execute("SELECT time,open,high,low,close,volume,turnover,interest FROM bars WHERE symbol=? AND interval=? AND time BETWEEN ? AND ? ORDER BY time",
                               (vt_symbol, interval.value, start.timestamp(), datetime.now(TZ).timestamp())).fetchall()
        if not rows:
            raise ValueError(f"{vt_symbol} 最近 {days} 天没有 {interval.value} 历史；请先导入真实 CSV K 线再初始化策略")
        return [BarData(symbol=symbol, exchange=Exchange(exchange), datetime=datetime.fromtimestamp(row[0], TZ),
                        interval=interval, open_price=row[1], high_price=row[2], low_price=row[3], close_price=row[4],
                        volume=row[5], turnover=row[6], open_interest=row[7], gateway_name="CSV") for row in rows]

    def close(self):
        self.db.close()
