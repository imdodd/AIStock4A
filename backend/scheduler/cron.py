"""五字段 cron 解析与下一次触发时间计算（标准库，零依赖）。

支持 ``*`` / ``*/n`` / ``a-b`` / ``a,b,c`` / 单值，字段顺序为
``分 时 日 月 周``（周 0 与 7 都表示周日，与 crontab 一致）。

APScheduler 后端不需要本模块算触发（它自己有 CronTrigger），但 ``next_fire``
仍用于 ``/api/jobs`` 的状态展示与内置后端的轮询，两者口径必须一致。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .calendar import CN_TZ

FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
FIELD_NAMES = ("minute", "hour", "day_of_month", "month", "day_of_week")
_PART = re.compile(r"^(\*|\d+|\d+-\d+|\*/\d+)$")


@dataclass(frozen=True)
class CronSpec:
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool
    raw: str

    def matches_day(self, d) -> bool:
        if d.month not in self.month:
            return False
        # 标准 crontab 语义：dom 与 dow 同时受限时取 OR
        dom_ok = d.day in self.dom
        dow_ok = ((d.weekday() + 1) % 7) in self.dow
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True

    def matches_time(self, hour: int, minute: int) -> bool:
        return hour in self.hour and minute in self.minute


def _field(text: str, idx: int) -> tuple[frozenset[int], bool]:
    lo, hi = FIELD_RANGES[idx]
    restricted = text != "*"
    values: set[int] = set()
    for part in text.split(","):
        if not _PART.match(part):
            raise ValueError(f"cron 第 {idx + 1} 段 {text!r} 含非法片段 {part!r}")
        if part == "*":
            values.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            if step < 1:
                raise ValueError(f"cron 步长必须 > 0：{part!r}")
            values.update(range(lo, hi + 1, step))
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            if a > b or a < lo or b > hi:
                raise ValueError(f"cron 区间越界：{part!r}（允许 {lo}-{hi}）")
            values.update(range(a, b + 1))
        else:
            v = int(part)
            if not lo <= v <= hi:
                raise ValueError(f"cron 值越界：{part!r}（允许 {lo}-{hi}）")
            values.add(v)
    if idx == 4:                       # 7 与 0 都表示周日
        if 7 in values:
            values.discard(7)
            values.add(0)
    return frozenset(values), restricted


def parse(expr: str) -> CronSpec:
    if not isinstance(expr, str):
        raise ValueError("cron 必须是字符串")
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron 必须是 5 段（分 时 日 月 周），收到 {expr!r}")
    parsed = [_field(p, i) for i, p in enumerate(parts)]
    return CronSpec(minute=parsed[0][0], hour=parsed[1][0], dom=parsed[2][0],
                    month=parsed[3][0], dow=parsed[4][0],
                    dom_restricted=parsed[2][1], dow_restricted=parsed[4][1],
                    raw=" ".join(parts))


def validate(expr: str) -> str:
    parse(expr)
    return " ".join(expr.split())


def next_fire(expr: str, after: datetime | None = None, max_days: int = 400) -> datetime | None:
    """返回严格晚于 after 的下一次触发（带东八区时区）。"""
    spec = parse(expr)
    n = after or datetime.now(CN_TZ)
    if n.tzinfo is None:
        n = n.replace(tzinfo=CN_TZ)
    else:
        n = n.astimezone(CN_TZ)
    # 从分钟后一分钟开始，按天找第一个匹配日，再取该日最早匹配时刻
    cursor = n.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for offset in range(max_days + 1):
        day = cursor.date() + timedelta(days=offset)
        if not spec.matches_day(day):
            continue
        for hour in sorted(spec.hour):
            for minute in sorted(spec.minute):
                cand = datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ)
                if cand >= cursor:
                    return cand
    return None


__all__ = ["CronSpec", "parse", "validate", "next_fire", "FIELD_NAMES"]
