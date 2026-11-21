"""交易日历：交易日 ≠ 自然日。

设计口径（沿用生产环境已踩过的坑）：
  1) 周末一律不是交易日；
  2) 法定休市日**不硬编码**（会过期、且不同年份不同），从
     ``data/calendar/holidays.json`` 读取，格式见 examples/trading_calendar.example.json；
  3) 若提供了 extra_sessions（调休开市日），可覆盖周末与非周末规则；
  4) 「行情基准日」另有来源：``snapshot/market.json`` 的 date 字段，代表上游
     实际拿到的最新交易日，用于判断 EOD 是否需要追补，不能拿本地今天代替。

时区固定 Asia/Shanghai（东八区），避免服务器时区把交易日判串。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))

import data_paths
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = data_paths.DATA_DIR
DEFAULT_CALENDAR_PATH = os.path.join(DATA_DIR, "calendar", "holidays.json")
DEFAULT_SNAPSHOT_PATH = os.path.join(DATA_DIR, "snapshot", "market.json")

# 交易所连续竞价时段，仅用于 session_state() 描述，不参与交易日判断
_SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))


def _parse_day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _load_raw(path: str | None = None) -> dict:
    p = path or os.environ.get("ASTOCK_CALENDAR") or DEFAULT_CALENDAR_PATH
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def holidays(path: str | None = None) -> set[str]:
    raw = _load_raw(path)
    out = set()
    for x in raw.get("holidays") or []:
        d = _parse_day(x)
        if d:
            out.add(d.isoformat())
    return out


def extra_sessions(path: str | None = None) -> set[str]:
    raw = _load_raw(path)
    out = set()
    for x in raw.get("extra_sessions") or []:
        d = _parse_day(x)
        if d:
            out.add(d.isoformat())
    return out


def is_trading_day(day, path: str | None = None) -> bool:
    d = _parse_day(day)
    if d is None:
        return False
    key = d.isoformat()
    if key in extra_sessions(path):
        return True
    if key in holidays(path):
        return False
    return d.weekday() < 5              # 0=Mon .. 6=Sun


def prev_trading_day(day=None, path: str | None = None) -> date:
    d = _parse_day(day) or datetime.now(CN_TZ).date()
    d -= timedelta(days=1)
    for _ in range(15):
        if is_trading_day(d, path):
            return d
        d -= timedelta(days=1)
    raise RuntimeError("连续 15 天找不到交易日，检查日历配置是否异常")


def next_trading_day(day=None, path: str | None = None) -> date:
    d = _parse_day(day) or datetime.now(CN_TZ).date()
    d += timedelta(days=1)
    for _ in range(15):
        if is_trading_day(d, path):
            return d
        d += timedelta(days=1)
    raise RuntimeError("连续 15 天找不到交易日，检查日历配置是否异常")


def last_trading_day(day=None, path: str | None = None) -> date:
    """含当日在内的最近一个交易日。"""
    d = _parse_day(day) or datetime.now(CN_TZ).date()
    for _ in range(15):
        if is_trading_day(d, path):
            return d
        d -= timedelta(days=1)
    raise RuntimeError("连续 15 天找不到交易日，检查日历配置是否异常")


def latest_known_trade_date(snapshot_path: str | None = None) -> str | None:
    """上游快照里实际拿到的最新交易日（不是本地今天）。"""
    p = snapshot_path or os.environ.get("ASTOCK_SNAPSHOT") or DEFAULT_SNAPSHOT_PATH
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("date") or None) if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def session_state(now: datetime | None = None) -> str:
    """盘前 / 盘中 / 午休 / 盘后 / 休市。仅用于状态展示。"""
    n = now or datetime.now(CN_TZ)
    if n.tzinfo is None:
        n = n.replace(tzinfo=CN_TZ)
    else:
        n = n.astimezone(CN_TZ)
    if not is_trading_day(n.date()):
        return "休市"
    t = n.timetz().replace(tzinfo=None)
    if t < _SESSIONS[0][0]:
        return "盘前"
    if _SESSIONS[0][0] <= t <= _SESSIONS[0][1]:
        return "盘中"
    if t < _SESSIONS[1][0]:
        return "午休"
    if t <= _SESSIONS[1][1]:
        return "盘中"
    return "盘后"


def describe(day=None, path: str | None = None) -> dict:
    d = _parse_day(day) or datetime.now(CN_TZ).date()
    return {
        "date": d.isoformat(),
        "weekday": d.strftime("%A"),
        "is_trading_day": is_trading_day(d, path),
        "prev_trading_day": prev_trading_day(d, path).isoformat(),
        "next_trading_day": next_trading_day(d, path).isoformat(),
        "last_trading_day": last_trading_day(d, path).isoformat(),
        "snapshot_trade_date": latest_known_trade_date(),
        "session": session_state(),
        "holidays_count": len(holidays(path)),
        "calendar_path": path or os.environ.get("ASTOCK_CALENDAR") or DEFAULT_CALENDAR_PATH,
    }


__all__ = ["CN_TZ", "is_trading_day", "prev_trading_day", "next_trading_day",
           "last_trading_day", "latest_known_trade_date", "holidays",
           "extra_sessions", "session_state", "describe", "DEFAULT_CALENDAR_PATH"]
