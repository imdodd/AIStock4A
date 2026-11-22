"""调度总控：把「谁在什么时候触发」与「触发后做什么」分开。

启动顺序：读档位 → 建状态表 → 注册固定分桶 job → 从 job_defs 重建自定义 job
→ start() → 后台线程做启动追补。

两条硬护栏：
  * ``SCHEDULER_DRY_RUN`` 默认 **开**：不装依赖、没配 Key、影子期都不会真去抓行情；
  * 交易日过滤 + ``job_runs`` 原子去重：同一交易日同一任务只跑一次，
    内置调度与外部自动化任务并存时也不会重复写快照。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Any

from db.repository import get_repository
from . import calendar as cal
from .backends import BackendBase, apscheduler_available, select_backend
from .cron import validate
from .handlers import FIXED_JOBS, JOB_HANDLERS
from .state import JobState


def _flag(name: str, default: bool) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int((os.environ.get(name) or "").strip() or default))
    except ValueError:
        return default


class Scheduler:
    def __init__(self, repo=None, backend: BackendBase | None = None):
        self.repo = repo or get_repository(fresh=True)
        self.state = JobState(self.repo)
        self.backend = backend or select_backend(
            os.environ.get("SCHEDULER_BACKEND", "auto"),
            misfire_grace_time=int(os.environ.get("SCHEDULER_MISFIRE", "7200")))
        self.dry_run = _flag("SCHEDULER_DRY_RUN", True)
        self.catchup = _flag("SCHEDULER_CATCHUP", True)
        self.trading_days_only = _flag("SCHEDULER_TRADING_DAYS_ONLY", True)
        self.eod_cron = (os.environ.get("EOD_CRON") or "").strip()
        self._fixed: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._started = False
        # 看门狗：定时巡检 next_run_at，停在过去超过阈值就自愈调度器。
        # 默认开，可用 SCHEDULER_WATCHDOG=0 关闭；间隔/阈值也可环境变量调整。
        self._watchdog_enabled = _flag("SCHEDULER_WATCHDOG", True)
        self._watchdog_interval = _int_env("SCHEDULER_WATCHDOG_INTERVAL", 120, 30)
        self._watchdog_stale = _int_env("SCHEDULER_WATCHDOG_STALE", 900, 120)
        self._stop_event = threading.Event()
        self._heals: list[dict] = []

    # ------------------------------------------------------------ job 元信息
    def _fixed_defs(self) -> list[dict]:
        out = []
        for raw in FIXED_JOBS:
            j = dict(raw)
            j["custom"] = False
            j["dedupe"] = "slot" if j["job_type"] == "news_round" else "trade_day"
            if j["id"] == "eod_close" and self.eod_cron:
                j["cron"] = self.eod_cron
            out.append(j)
        return out

    def _all_defs(self) -> dict[str, dict]:
        defs = {j["id"]: j for j in self._fixed_defs()}
        for d in self.state.list_defs():
            defs[d["id"]] = {**d, "custom": True, "dedupe": "slot"}
        return defs

    def _run_key(self, job: dict, now: datetime) -> str:
        if job.get("dedupe") == "slot":
            return now.strftime("%Y-%m-%dT%H:%M")
        return cal.last_trading_day(now.date()).isoformat()

    # ------------------------------------------------------------ 执行
    def _run(self, job_id: str, reset: bool = False,
             trade_date: str | None = None) -> dict:
        job = self._all_defs().get(job_id)
        if not job:
            return {"ok": False, "error": f"未知任务 {job_id!r}"}
        now = datetime.now(cal.CN_TZ)
        # trade_date 由追补显式传入时，目标本身已是交易日，不再过当天日历。
        # all_days 的任务（消息采集）故意绕开这道门：周末的政策和海外消息
        # 同样要进库，周一开盘前才不会一片空白。
        if trade_date is None and self.trading_days_only and not job.get("all_days") \
                and not cal.is_trading_day(now.date()) and not reset:
            return {"ok": True, "skipped": "non_trading_day", "job": job_id}

        # job_runs.job_type 存「任务实例 id」而不是 handler 名：5 个 cad 分桶
        # 共用 tracking_review，用 handler 名去重会互相挤掉。
        key = trade_date or self._run_key(job, now)
        with self._lock:
            if reset:
                self.state.reset_run(job_id, key)
            if not self.state.try_start(job_id, key):
                return {"ok": True, "skipped": "duplicate", "job": job_id, "key": key}

        handler = JOB_HANDLERS.get(job["job_type"])
        if handler is None:
            self.state.finish(job_id, key, "error", {"error": "未注册的 job_type",
                                                     "job_type": job["job_type"]})
            return {"ok": False, "error": "未注册的 job_type", "job": job_id}
        ctx: dict[str, Any] = {"dry_run": self.dry_run, "repo": self.repo,
                               "trade_date": key, "now": now.isoformat()}
        try:
            result = handler(job.get("params") or {}, ctx)
            failed = bool(result.get("error")) or (
                isinstance(result.get("returncode"), int) and result["returncode"] != 0)
            status = "error" if failed else "ok"
        except Exception as e:
            result, status = {"error": f"{type(e).__name__}: {e}"}, "error"
        self.state.finish(job_id, key, status, result)
        return {"ok": status == "ok", "status": status, "job": job_id,
                "key": key, "result": result}

    # ------------------------------------------------------------ 注册 / 生命周期
    def _register(self, job: dict) -> None:
        jid = job["id"]
        self.backend.add(jid, job["cron"], self._run, replace=True)
        with self._lock:
            self._fixed[jid] = {"cron": job["cron"], "job_type": job["job_type"]}

    def start(self) -> dict:
        if self._started:
            return self.status()
        for j in self._fixed_defs():
            try:
                self._register(j)
            except ValueError as e:
                raise RuntimeError(f"固定 job {j['id']} 的 cron 非法：{e}") from e
        for d in self.state.list_defs(only_enabled=True):
            if d["job_type"] not in JOB_HANDLERS:
                continue
            self.backend.add(d["id"], d["cron"], self._run, replace=True)
        self.backend.start()
        self._started = True
        if self.catchup:
            threading.Thread(target=self.run_catchup, name="astock-catchup",
                             daemon=True).start()
        if self._watchdog_enabled:
            threading.Thread(target=self._watchdog_loop, name="astock-watchdog",
                             daemon=True).start()
        return self.status()

    def run_catchup(self) -> list[dict]:
        """补跑「最近交易日缺跑」的任务；同一交易日由 job_runs 去重。

        周日启动也会以周五为目标，避免把「今天不是交易日」误当成「不用补」。
        """
        results = []
        day = cal.last_trading_day().isoformat()
        for j in self._fixed_defs():
            if j["dedupe"] != "trade_day":
                continue
            if self.state.has_success(j["id"], day):
                results.append({"ok": True, "skipped": "already_done",
                                "job": j["id"], "key": day})
                continue
            results.append(self._run(j["id"], trade_date=day))
        return results

    def stop(self) -> None:
        self._stop_event.set()
        if self._started:
            self.backend.stop()
            self._started = False

    # ------------------------------------------------------------ 看门狗
    def _watchdog_loop(self) -> None:
        """每 interval 秒巡检一次；独立线程、异常不外泄，自身永不退出。"""
        while not self._stop_event.wait(self._watchdog_interval):
            try:
                self._watchdog_tick()
            except Exception as e:                       # noqa: BLE001
                self._heals.append({"at": datetime.now(cal.CN_TZ).isoformat(),
                                    "action": "error",
                                    "error": f"{type(e).__name__}: {e}"})
                del self._heals[:-10]

    def _watchdog_tick(self) -> None:
        if not self._started or not self.backend.running:
            return
        now = datetime.now(cal.CN_TZ)
        defs = self._all_defs()
        stale: list[tuple[str, float]] = []
        missing: list[str] = []
        for jid, job in defs.items():
            if not int(job.get("enabled", 1)):
                continue
            raw = self.backend.next_run(jid)
            if not raw:
                missing.append(jid)
                continue
            try:
                nxt = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                stale.append((jid, -1.0))
                continue
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=cal.CN_TZ)
            lag = (now - nxt).total_seconds()
            if lag > self._watchdog_stale:
                stale.append((jid, lag))

        if not stale and not missing:
            return

        def _still_due(jid: str) -> bool:
            raw = self.backend.next_run(jid)
            if not raw:
                return True
            try:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=cal.CN_TZ)
                return dt <= now
            except (TypeError, ValueError):
                return True

        # 有 job 停在过去 → 底层调度线程大概率已停摆，整体重建才有救。
        healed = bool(self.backend.heal()) if stale else False
        # 没注册上的（next 缺失）单独补；重建后仍停在过去的也再兜一次。
        for jid in missing + [j for j, _ in stale if _still_due(j)]:
            job = defs.get(jid)
            if job:
                self.backend.add(jid, job["cron"], self._run, replace=True)

        rec = {"at": now.isoformat(),
               "stale": [{"id": j, "lag_sec": int(lag)} for j, lag in stale],
               "missing": missing, "healed": healed}
        self._heals.append(rec)
        del self._heals[:-10]
        print(f"[watchdog] 检测到调度停摆，已自愈：stale={stale} "
              f"missing={missing} backend_healed={healed}", flush=True)

    # ------------------------------------------------------------ 自定义 job CRUD
    def add_custom(self, payload: dict) -> dict:
        jid = str(payload.get("id") or "").strip()
        jtype = str(payload.get("job_type") or "").strip()
        cron = str(payload.get("cron") or "").strip()
        if not jid or not jtype or not cron:
            raise ValueError("id / job_type / cron 均必填")
        if jid in self._all_defs() and not self.state.get_def(jid):
            raise ValueError(f"{jid!r} 与固定任务重名，请换一个 id")
        if jtype not in JOB_HANDLERS:
            raise ValueError(f"job_type 必须是 {sorted(JOB_HANDLERS)} 之一")
        cron = validate(cron)
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params 必须是对象")
        enabled = int(bool(payload.get("enabled", 1)))
        row = self.state.put_def(jid, jtype, cron, params, enabled,
                                 payload.get("note"))
        if enabled and self._started:
            self.backend.add(jid, cron, self._run, replace=True)
        else:
            self.backend.remove(jid)
        return row

    def patch_custom(self, job_id: str, patch: dict) -> dict | None:
        row = self.state.get_def(job_id)
        if not row:
            return None
        for field in ("job_type", "cron", "params", "enabled", "note"):
            if field in patch:
                row[field] = patch[field]
        if row["job_type"] not in JOB_HANDLERS:
            raise ValueError(f"job_type 必须是 {sorted(JOB_HANDLERS)} 之一")
        row["cron"] = validate(str(row["cron"]))
        row["enabled"] = int(bool(row["enabled"]))
        if not isinstance(row["params"] or {}, dict):
            raise ValueError("params 必须是对象")
        saved = self.state.put_def(job_id, row["job_type"], row["cron"],
                                   row["params"], row["enabled"], row["note"])
        if self._started:
            if saved["enabled"]:
                if self.backend.next_run(job_id):
                    self.backend.reschedule(job_id, row["cron"])
                else:
                    self.backend.add(job_id, row["cron"], self._run, replace=True)
            else:
                self.backend.remove(job_id)
        return saved

    def delete_custom(self, job_id: str) -> bool:
        removed = self.state.delete_def(job_id)
        self.backend.remove(job_id)
        return removed

    def run_now(self, job_id: str) -> dict:
        if job_id not in self._all_defs():
            return {"ok": False, "error": f"未知任务 {job_id!r}"}
        return self._run(job_id, reset=True)

    # ------------------------------------------------------------ 状态
    def status(self) -> dict:
        defs = self._all_defs()
        jobs = self.backend.list()
        by_id = {j["id"]: j for j in jobs}
        return {
            "enabled": True, "running": self.backend.running,
            "backend": self.backend.name,
            "apscheduler_available": apscheduler_available(),
            "dry_run": self.dry_run, "catchup": self.catchup,
            "trading_days_only": self.trading_days_only,
            "calendar": cal.describe(),
            "fixed_jobs": len(FIXED_JOBS),
            "custom_jobs": len(self.state.list_defs()),
            "registered": len(jobs),
            "watchdog": {"enabled": self._watchdog_enabled,
                         "interval_sec": self._watchdog_interval,
                         "stale_sec": self._watchdog_stale,
                         "heals": list(self._heals[-5:])},
            "jobs": [{"id": k, "job_type": v["job_type"], "cron": v["cron"],
                      "custom": v.get("custom", False),
                      "enabled": int(v.get("enabled", 1)),
                      "next_run_at": by_id.get(k, {}).get("next_run_at"),
                      "last_success": self.state.last_success(k)}
                     for k, v in sorted(defs.items())],
            "runs": self.state.list_runs(10),
            "handlers": sorted(JOB_HANDLERS),
        }


_singleton: Scheduler | None = None
_singleton_lock = threading.RLock()


def get_scheduler(repo=None, fresh: bool = False) -> Scheduler:
    global _singleton
    if fresh:
        return Scheduler(repo=repo)
    with _singleton_lock:
        if _singleton is None:
            _singleton = Scheduler(repo=repo)
        return _singleton


__all__ = ["Scheduler", "get_scheduler"]
