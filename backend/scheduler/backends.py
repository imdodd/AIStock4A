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
from .cron import next_fire, parse as parse_cron, validate


def apscheduler_available() -> bool:
    return importlib.util.find_spec("apscheduler") is not None


def crontab_to_apscheduler(expr: str) -> str:
    """把标准 crontab 的周字段转换成 APScheduler 口径。

    两套口径的歧义就出在这一段：
      * 标准 crontab（本工程的 ``scheduler.cron``、``job_defs``、文档、
        前端 placeholder、``EOD_CRON`` 默认值全都按这套）：0=周日，1=周一 … 5=周五；
      * APScheduler 的 ``day_of_week``：0=周一 … 6=周日。

    ``CronTrigger.from_crontab`` 名义上吃标准 crontab，实际把第 5 段原样塞进
    ``day_of_week``，于是 ``1-5`` 变成周二到周六、``0-4`` 变成周一到周五。
    实测过：``from_crontab('30 15 * * 1-5')`` 的下一次是**周二**。

    既然文件、接口、文档都以标准 crontab 为准，就在这一层做映射，别去动
    builtin 后端（它已经是标准的）。映射：crontab n → (n-1) % 7。
    """
    parts = expr.split()
    if len(parts) != 5 or parts[4] == "*":
        return " ".join(parts) if len(parts) == 5 else expr
    spec = parse_cron(expr)                       # 顺带完成合法性校验
    mapped = sorted({(d - 1) % 7 for d in spec.dow})
    return " ".join(parts[:4] + [",".join(str(x) for x in mapped)])


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

    def heal(self) -> bool:
        """底层调度线程停摆时整体自愈；健康时返回 False。"""
        return False

    @property
    def running(self) -> bool:
        raise NotImplementedError


class APSchedulerBackend(BackendBase):
    name = "apscheduler"

    def __init__(self, misfire_grace_time: int = 7200):
        self._misfire = misfire_grace_time
        self._crons: dict[str, str] = {}
        self._callbacks: dict[str, object] = {}
        self._lock = threading.RLock()
        self._scheduler = self._build_scheduler()

    def _build_scheduler(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.jobstores.memory import MemoryJobStore
        return BackgroundScheduler(
            timezone=CN_TZ,
            jobstores={"default": MemoryJobStore()},
            job_defaults={"coalesce": True, "max_instances": 1,
                          "misfire_grace_time": self._misfire},
        )

    def add(self, job_id: str, cron: str, callback, replace: bool = True) -> None:
        from apscheduler.triggers.cron import CronTrigger
        validate(cron)
        trigger = CronTrigger.from_crontab(crontab_to_apscheduler(cron), timezone=CN_TZ)
        with self._lock:
            self._scheduler.add_job(callback, trigger=trigger, id=job_id,
                                    args=[job_id], replace_existing=replace,
                                    coalesce=True, max_instances=1)
            self._crons[job_id] = " ".join(cron.split())
            self._callbacks[job_id] = callback

    def remove(self, job_id: str) -> bool:
        with self._lock:
            if not self._scheduler.get_job(job_id):
                return False
            self._scheduler.remove_job(job_id)
            self._crons.pop(job_id, None)
            self._callbacks.pop(job_id, None)
            return True

    def reschedule(self, job_id: str, cron: str) -> bool:
        from apscheduler.triggers.cron import CronTrigger
        validate(cron)
        with self._lock:
            job = self._scheduler.get_job(job_id)
            if not job:
                return False
            self._scheduler.reschedule_job(
                job_id, trigger=CronTrigger.from_crontab(
                    crontab_to_apscheduler(cron), timezone=CN_TZ))
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

    def heal(self) -> bool:
        """调度线程停摆时重建实例并回填全部 job。

        系统休眠、时钟跳变或某轮任务抛异常，都可能让 APScheduler 的主循环
        失去唤醒（表现：next_run_time 停在过去不再前进）。仅 reschedule 单个
        job 无法救回已停摆的调度线程，必须新建实例；shutdown(wait=False) 不
        等待正在执行的任务，配合 core 的 job_runs 去重不会重复入库。
        """
        with self._lock:
            from apscheduler.triggers.cron import CronTrigger
            was_running = bool(self._scheduler.running)
            try:
                if was_running:
                    self._scheduler.shutdown(wait=False)
            except Exception:                              # noqa: BLE001
                pass
            self._scheduler = self._build_scheduler()
            for jid, cron in list(self._crons.items()):
                cb = self._callbacks.get(jid)
                if cb is None:
                    continue
                self._scheduler.add_job(
                    cb, trigger=CronTrigger.from_crontab(
                        crontab_to_apscheduler(cron), timezone=CN_TZ),
                    id=jid, args=[jid], replace_existing=True,
                    coalesce=True, max_instances=1)
            if was_running:
                self._scheduler.start()
            return True

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

    def heal(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            # stop() 会关闭线程池；重启时须换新池，否则 submit 抛 RuntimeError。
            self._pool = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="astock-job")
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop,
                                            name="astock-scheduler", daemon=True)
            self._thread.start()
            return True

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
           "select_backend", "apscheduler_available", "crontab_to_apscheduler"]
