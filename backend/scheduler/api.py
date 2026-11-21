"""调度 API（可选挂载）。

只有当 ``SCHEDULER_ENABLED=1`` 时 main.py 才 include 这个 router —— 影子期
默认不注册，保证原有 32 个 GET 路由集合与响应逐字段零差异。

接口：
  GET    /api/jobs/status        运行档位 + 日历 + job 列表 + 最近运行
  GET    /api/jobs               自定义 job_defs
  GET    /api/jobs/runs          最近 job_runs
  GET    /api/jobs/calendar      交易日历判定
  POST   /api/jobs               新增自定义 cron
  PATCH  /api/jobs/{id}          改 cron / enabled / params
  DELETE /api/jobs/{id}          删除
  POST   /api/jobs/{id}/run      手工触发（清当日锁后重跑）
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException
from typing import Any

from . import calendar as cal
from .core import get_scheduler

router = APIRouter(prefix="/api/jobs", tags=["scheduler"])

_holder: dict[str, Any] = {"scheduler": None, "startup_error": None}


def _ok(data: Any) -> dict:
    return {"code": 0, "data": data}


def _sched():
    if _holder["scheduler"] is None:
        _holder["scheduler"] = get_scheduler()
    return _holder["scheduler"]


def _fail(e: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


@router.get("/status")
def status():
    try:
        data = _sched().status()
    except Exception as e:                       # 库不可写等环境问题不许拖垮服务
        return _ok({"enabled": True, "running": False, "error": f"{type(e).__name__}: {e}"})
    if _holder.get("startup_error"):
        data["startup_error"] = _holder["startup_error"]
    if _holder.get("tracking_sync"):
        data["tracking_sync"] = _holder["tracking_sync"]
    return _ok(data)


@router.get("")
def list_defs():
    s = _sched()
    return _ok({"defs": s.state.list_defs(), "handlers": sorted(s.status()["handlers"])})


@router.get("/runs")
def list_runs(limit: int = 30):
    return _ok(_sched().state.list_runs(limit))


@router.get("/calendar")
def calendar_info(day: str | None = None):
    return _ok(cal.describe(day))


@router.post("")
def add_def(payload: dict = Body(...)):
    try:
        return _ok(_sched().add_custom(payload))
    except (ValueError, KeyError) as e:
        raise _fail(e) from e


@router.patch("/{job_id}")
def patch_def(job_id: str, patch: dict = Body(...)):
    try:
        row = _sched().patch_custom(job_id, patch)
    except (ValueError, KeyError) as e:
        raise _fail(e) from e
    if row is None:
        raise HTTPException(status_code=404, detail=f"自定义任务 {job_id!r} 不存在")
    return _ok(row)


@router.delete("/{job_id}")
def delete_def(job_id: str):
    return _ok({"deleted": _sched().delete_custom(job_id)})


@router.post("/{job_id}/run")
def run_now(job_id: str):
    return _ok(_sched().run_now(job_id))


def mount(app, scheduler=None) -> None:
    """注册 startup/shutdown。由 main.py 在开关打开时调用。"""
    if scheduler is not None:
        _holder["scheduler"] = scheduler

    def _startup() -> None:
        # 调度是可选能力：数据库不可写 / 依赖缺失都不允许让 Web 服务起不来。
        try:
            _sched().start()
            # 每只 active 方案按自己的频率补一条 job_def；已取消/删持仓的清掉。
            # 服务重启、或有人直接改过 meta.json 时靠这里自愈。
            try:
                from tracking_jobs import sync_all
                _holder["tracking_sync"] = sync_all(_sched())
            except Exception as e:                   # noqa: BLE001
                _holder["tracking_sync"] = {"error": f"{type(e).__name__}: {e}"}
            _holder["startup_error"] = None
        except Exception as e:                   # noqa: BLE001
            _holder["startup_error"] = f"{type(e).__name__}: {e}"
            print(f"[scheduler] 启动失败，服务照常运行：{_holder['startup_error']}")

    def _shutdown() -> None:
        s = _holder.get("scheduler")
        if s is not None:
            s.stop()

    if hasattr(app, "add_event_handler"):
        app.add_event_handler("startup", _startup)
        app.add_event_handler("shutdown", _shutdown)
        return
    # 新版 FastAPI 移除了 on_event/add_event_handler：改用 lifespan 注入。
    # main.py 没有自定义 lifespan，直接替换默认实现即可。
    import contextlib

    @contextlib.asynccontextmanager
    async def _lifespan(_app):
        _startup()
        try:
            yield
        finally:
            _shutdown()

    app.router.lifespan_context = _lifespan


__all__ = ["router", "mount"]
