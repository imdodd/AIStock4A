"""把每只跟踪方案自己的频率，变成一条真正的定时任务。

为什么不再用「固定分桶 job」：固定桶只能表达 daily / weekly / quarterly 这种粗档，
而方案里写的是「每周一 08:20」「每周一、周四 08:20」这种具体到星期和时刻的约定。
桶的触发时间跟方案 label 对不上时，界面写着周一早八、任务实际周五下午跑，
等于两头都不准。现在每条 active 方案一条自定义 job_def，id 固定为 track-<code>：

  方案频率 → cron → job_def → handler → jobs.tracking_review

「跟踪」是显式动作：只有用户点了跟踪、方案被 activate，才会出现这条 job。
取消跟踪或删掉持仓时同步移除；绝不会因为「这只股票在持仓里」就自动跟踪。
"""
from __future__ import annotations

import os
import re

import tracking

PREFIX = "track-"
DAY_MAP = {"MO": "1", "TU": "2", "WE": "3", "TH": "4",
           "FR": "5", "SA": "6", "SU": "0"}

RRULE_PART = re.compile(r"([A-Z]+)=([^;]+)")


def job_id(code: str) -> str:
    return PREFIX + str(code).strip().lower()


def cron_of(freq: dict | None) -> str:
    """FREQ=WEEKLY;BYDAY=MO,TH;BYHOUR=8;BYMINUTE=20 → 20 8 * * 1,4

    周字段一律用**标准 crontab 口径**（0/7=周日，1=周一 … 5=周五），跟
    ``FIXED_JOBS``、``job_defs``、``EOD_CRON`` 和文档保持一致；APScheduler 后端
    在 ``backends.crontab_to_apscheduler`` 里统一做 (n-1)%7 的映射。
    以前这里输出 APScheduler 口径（MO→0），碰上 builtin 后端就会整天错位一天。
    """
    f = freq or {}
    raw = str(f.get("rrule") or "")
    parts = {k.upper(): v for k, v in RRULE_PART.findall(raw)}
    kind = str(f.get("kind") or parts.get("FREQ") or "weekly").lower()
    hour = parts.get("BYHOUR") or "8"
    minute = parts.get("BYMINUTE") or "20"
    days = parts.get("BYDAY") or "MO"
    dow = ",".join(DAY_MAP.get(d.strip().upper(), "1") for d in days.split(",") if d.strip())
    if not dow:
        dow = "1"
    if kind == "daily":
        return f"{minute} {hour} * * 1-5"        # 交易日，非交易日由调度器跳过
    if kind == "monthly":
        day = parts.get("BYMONTHDAY") or "1"
        return f"{minute} {hour} {day} * *"
    if kind == "yearly":
        month = parts.get("BYMONTH") or "1"
        day = parts.get("BYMONTHDAY") or "1"
        return f"{minute} {hour} {day} {month} *"
    return f"{minute} {hour} * * {dow}"          # weekly / twice_weekly / event 都落这里


def enabled() -> bool:
    """影子/单测实例没开调度器，别给它们也写 job_defs"""
    return (os.environ.get("SCHEDULER_ENABLED") or "").strip() == "1"


def _sched():
    from scheduler.core import get_scheduler
    return get_scheduler()


def upsert(code: str, sched=None) -> dict | None:
    """方案激活或改频率后调用。没有方案/已停用的，顺手把 job 撤掉。"""
    if not enabled() and sched is None:
        return None
    m = tracking.load(code)
    s = sched or _sched()
    jid = job_id(code)
    if not m or (m.get("status") or "active") != "active":
        s.delete_custom(jid)
        return None
    freq = m.get("frequency") or {}
    payload = {
        "id": jid,
        "job_type": "tracking_review",
        "cron": cron_of(freq),
        "params": {"code": code, "cad": freq.get("kind") or "weekly"},
        "enabled": 1,
        "note": f"{m.get('name') or code} 跟踪（按方案频率 {freq.get('label') or ''}）",
    }
    old = s.state.get_def(jid)
    if old:
        return s.patch_custom(jid, {k: payload[k] for k in
                                    ("job_type", "cron", "params", "enabled", "note")})
    return s.add_custom(payload)


def remove(code: str, sched=None) -> bool:
    if not enabled() and sched is None:
        return False
    s = sched or _sched()
    return bool(s.delete_custom(job_id(code)))


def sync_all(sched=None) -> dict:
    """服务启动时把库和 job_defs 对齐：active 的补建，多余的清理。

    数据库/服务重启过、或有人直接改过 meta.json 时靠这一步自愈。
    """
    if not enabled() and sched is None:
        return {"skipped": "scheduler_disabled"}
    s = sched or _sched()
    active = {c for c, row in (tracking.list_all() or {}).items()
              if (row.get("status") or "active") == "active"}
    made, removed, failed = [], [], []
    for c in sorted(active):
        try:
            r = upsert(c, s)
            (made if r else removed).append(c)
        except Exception as e:                     # 一只坏方案不许拖垮启动
            failed.append({"code": c, "error": f"{type(e).__name__}: {e}"})
    for d in s.state.list_defs():
        jid = str(d.get("id") or "")
        if jid.startswith(PREFIX) and jid[len(PREFIX):] not in active:
            s.delete_custom(jid)
            removed.append(jid[len(PREFIX):])
    return {"active": len(active), "created_or_updated": made,
            "removed": sorted(set(removed)), "failed": failed}


__all__ = ["PREFIX", "job_id", "cron_of", "enabled", "upsert", "remove", "sync_all"]
