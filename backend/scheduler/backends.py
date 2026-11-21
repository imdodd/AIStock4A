"""调度运行时：APScheduler 优先，未安装时退回标准库内置实现。

为什么要双后端：方案把 apscheduler 定为可选依赖（缺失时服务照常跑）。当前机器
确实没装，但仍要能完整验证 job_defs / 追补 / 动态增删。两个后端共用同一套
``add / remove / reschedule / next_run / list`` 接口，core 与 API 不感知差异。

APScheduler 档严格按已确认结论：MemoryJobStore、coalesce=True、
max_instances=1、misfire_grace_time=7200、时区 Asia/Shanghai，
**不用 SQLAlchemyJobStore**（避免 pickle job 对象 + 拖进 SQLAlchemy）。
内置档用同一个 cron 解析器实现等价语义。
"""
from __future__ import annotations

import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .calendar import CN_TZ
from .cron import next_fire, validate


def apscheduler_available() -> bool:
    return importlib.util.find_spec("apscheduler") is not None


class BackendBase:
    name = "base"

    def add(self, job_id: str, cron: str, callback, replace: bool = True) -> None:
        raise NotImplementedError

    def remove(self, job_id: str) -> bool:
        raise NotImplementedError

    def reschedule(self, job_id: str, cron: str) -> bool:
        raise NotImplementedError

    def next_run(self, job_id: str) -> str | None:
        raise NotImplementedError

    def list(self) -> list[dict]:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    @property
    def running(self) -> bool:
        raise NotImplementedError


class APSchedulerBackend(BackendBase):
    name = "apscheduler"

    def __init__(self, misfire_grace_time: int = 7200):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.jobstores.memory import MemoryJobStore
        self._scheduler = BackgroundScheduler(
            timezone=CN_TZ,
            jobstores={"default": MemoryJobStore()},
            job_defaults={"coalesce": True, "max_instances": 1,
                          "misfire_grace_time": misfire_grace_time},
        )
        self._crons: dict[str, str] = {}
        self._lock = threading.RLock()

    def add(self, job_id: str, cron: str, callback, replace: bool = True) -> None:
        from apscheduler.triggers.cron import CronTrigger
        validate(cron)
        trigger = CronTrigger.from_crontab(cron, timezone=CN_TZ)
        with self._lock:
            self._scheduler.add_job(callback, trigger=trigger, id=job_id,
                                    args=[job_id], replace_existing=replace,
                                    coalesce=True, max_instances=1)
            self._crons[job_id] = " ".join(cron.split())

    def remove(self, job_id: str) -> bool:
        with self._lock:
            if not self._scheduler.get_job(job_id):
                return False
            self._scheduler.remove_job(job_id)
            self._crons.pop(job_id, None)
            return True

    def reschedule(self, job_id: str, cron: str) -> bool:
        from apscheduler.triggers.cron import CronTrigger
        validate(cron)
        with self._lock:
            job = self._scheduler.get_job(job_id)
            if not job:
                return False
            self._scheduler.reschedule_job(
                job_id, trigger=CronTrigger.from_crontab(cron, timezone=CN_TZ))
            self._crons[job_id] = " ".join(cron.split())
            return True

    def next_run(self, job_id: str) -> str | None:
        with self._lock:
            job = self._scheduler.get_job(job_id)
        return job.next_run_time.isoformat() if job and job.next_run_time else None

    def list(self) -> list[dict]:
        with self._lock:
            jobs = self._scheduler.get_jobs()
            return [{"id": j.id, "cron": self._crons.get(j.id, ""),
                     "next_run_at": j.next_run_time.isoformat() if j.next_run_time else None}
                    for j in jobs]

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)


class BuiltinBackend(BackendBase):
    """零依赖轮询实现。秒级 tick，单 worker 串行执行，等价于 max_instances=1。"""
    name = "builtin"

    def __init__(self, misfire_grace_time: int = 7200, tick: float = 0.5):
        self._misfire = misfire_grace_time
        self._tick_interval = tick
        self._jobs: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._inflight = threading.BoundedSemaphore(1)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="astock-job")

    def add(self, job_id: str, cron: str, callback, replace: bool = True) -> None:
        cron = validate(cron)
        with self._lock:
            if job_id in self._jobs and not replace:
                raise ValueError(f"job {job_id!r} 已存在")
            self._jobs[job_id] = {"cron": cron, "callback": callback,
                                  "next": next_fire(cron)}

    def remove(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def reschedule(self, job_id: str, cron: str) -> bool:
        cron = validate(cron)
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return False
            j["cron"] = cron
            j["next"] = next_fire(cron)
            return True

    def next_run(self, job_id: str) -> str | None:
        with self._lock:
            j = self._jobs.get(job_id)
        return j["next"].isoformat() if j and j["next"] else None

    def list(self) -> list[dict]:
        with self._lock:
            return [{"id": k, "cron": v["cron"],
                     "next_run_at": v["next"].isoformat() if v["next"] else None}
                    for k, v in sorted(self._jobs.items())]

    def _dispatch(self, job_id: str, callback) -> None:
        if not self._inflight.acquire(blocking=False):
            return                                   # coalesce：上一条没跑完就合并掉
        def _run():
            try:
                callback(job_id)
            except Exception:
                pass                                 # 具体异常由 core 写进 job_runs
            finally:
                self._inflight.release()
        self._pool.submit(_run)

    def _loop(self) -> None:
        while not self._stop_event.wait(self._tick_interval):
            now = datetime.now(CN_TZ)
            due: list[tuple[str, object]] = []
            with self._lock:
                for job_id, j in list(self._jobs.items()):
                    nxt = j["next"]
                    if nxt and nxt <= now:
                        late = (now - nxt).total_seconds()
                        j["next"] = next_fire(j["cron"], now)
                        if late > self._misfire:
                            continue                 # 过期太久，不补跑
                        due.append((job_id, j["callback"]))
            for job_id, cb in due:
                self._dispatch(job_id, cb)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="astock-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._pool.shutdown(wait=False, cancel_futures=True)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


def select_backend(prefer: str = "auto", **kwargs) -> BackendBase:
    p = (prefer or "auto").strip().lower()
    if p == "builtin":
        return BuiltinBackend(**kwargs)
    if p == "apscheduler":
        if not apscheduler_available():
            raise RuntimeError("SCHEDULER_BACKEND=apscheduler 但未安装：pip install 'apscheduler>=3.10,<4'")
        return APSchedulerBackend(**kwargs)
    if p != "auto":
        raise ValueError(f"SCHEDULER_BACKEND 只能是 auto / apscheduler / builtin，收到 {p!r}")
    return (APSchedulerBackend(**kwargs) if apscheduler_available()
            else BuiltinBackend(**kwargs))


__all__ = ["BackendBase", "APSchedulerBackend", "BuiltinBackend",
           "select_backend", "apscheduler_available"]
