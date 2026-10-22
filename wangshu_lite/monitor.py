"""Persisted activity counts and advisory warnings, independent of order handling."""
import json
import threading

from .storage import ActivityGapError


METRICS = {
    "orders_last_minute": "近60秒报单",
    "cancels_last_minute": "近60秒撤单请求",
    "orders_today": "当日报单",
    "cancels_today": "当日撤单请求",
}
MAX_LIMIT = 2_147_483_647


def _limits(value):
    if not isinstance(value, dict) or set(value) != set(METRICS):
        raise ValueError("监控阈值必须包含且仅包含四项计数")
    if any(type(value[key]) is not int or not 0 <= value[key] <= MAX_LIMIT for key in METRICS):
        raise ValueError(f"监控阈值须为0至{MAX_LIMIT}的整数，0表示关闭预警")
    return {key: value[key] for key in METRICS}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("监控配置包含重复字段")
        result[key] = value
    return result


class ActivityMonitor:
    def __init__(self, journal):
        self.journal = journal
        self._limits = dict.fromkeys(METRICS, 0)
        self._lock = threading.RLock()

    def update_limits(self, limits):
        """Return an empty error string on success; never affect trading state."""
        with self._lock:
            try:
                limits = _limits(limits)
            except ValueError as exc:
                return str(exc)
            try:
                self.journal.set_audited(
                    "activity_limits", json.dumps(limits), "activity_limits", {"limits": limits})
            except Exception:
                return "监控阈值保存失败，请检查本地日志目录"
            self._limits = limits
            return ""

    def snapshot(self, trading_day):
        """Report counts and warnings only. Unavailable data is never shown as zero."""
        with self._lock:
            result = {
                "available": False, "day": trading_day or "",
                "counts": dict.fromkeys(METRICS), "limits": dict(self._limits),
                "alerts": [], "error": "",
            }
            if not isinstance(trading_day, str) or len(trading_day) != 8 or not trading_day.isascii() or not trading_day.isdigit():
                result["error"] = "尚未获得柜台交易日，计数暂不可用"
                return result
            try:
                raw = self.journal.get("activity_limits")
                limits = _limits(json.loads(raw, object_pairs_hook=_unique_object)) if raw else dict.fromkeys(METRICS, 0)
                self._limits = limits
                result["limits"] = dict(limits)
                counts = self.journal.activity(trading_day)
                if (not isinstance(counts, dict) or set(counts) != set(METRICS)
                        or any(type(counts[key]) is not int or counts[key] < 0 for key in METRICS)):
                    raise ValueError("监控统计无效")
                alerts = [
                    {"metric": key, "label": label, "count": counts[key], "limit": limits[key],
                     "message": f"{label}{counts[key]}次，已达到预警阈值{limits[key]}次"}
                    for key, label in METRICS.items() if limits[key] and counts[key] >= limits[key]
                ]
                self._record_changes(trading_day, counts, limits, alerts)
                result.update(available=True, counts=dict(counts), alerts=alerts)
            except (ValueError, TypeError):
                result["error"] = "监控配置或统计无效，请重新保存阈值并检查本地日志目录"
            except ActivityGapError:
                result["error"] = "申撤单计数不完整，请核对柜台与持久日志；仍可尝试撤单"
            except Exception:
                result["error"] = "监控读取或审计失败，请检查本地日志目录"
            return result

    def _record_changes(self, day, counts, limits, alerts):
        active = [alert["metric"] for alert in alerts]
        state = {"day": day, "active": active}
        raw = self.journal.get("activity_monitor_state")
        prior = json.loads(raw) if raw else {"day": "", "active": []}
        if (not isinstance(prior, dict) or set(prior) != {"day", "active"}
                or not isinstance(prior["day"], str) or not isinstance(prior["active"], list)
                or any(metric not in METRICS for metric in prior["active"])):
            raise ValueError("监控状态无效")
        if prior == state:
            return
        day_changed = prior["day"] != day
        detail = {
            "day": day, "previous_day": prior["day"], "day_changed": day_changed,
            "entered": [key for key in active if day_changed or key not in prior["active"]],
            "cleared": [key for key in prior["active"] if day_changed or key not in active],
            "counts": counts, "limits": limits,
        }
        self.journal.set_audited(
            "activity_monitor_state", json.dumps(state), "activity_monitor", detail)
