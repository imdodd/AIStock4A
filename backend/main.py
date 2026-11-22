"""
股票跟踪分析平台 · M0/M1 服务端（FastAPI）

职责（M1）：
  1. 读取 jobs/build_snapshot.py 产出的快照（M2 换成 MySQL 查询，接口契约不变）
  2. 对外提供 /api/* 只读接口
  3. 托管 frontend/ 单页前端

启动（影子期默认 8100，生产 8000 全程不动）：
    cd backend && uvicorn main:app --port 8100
    浏览器打开 http://127.0.0.1:8100/
"""
from __future__ import annotations

# 必须最先执行：仓库根 .env 合进 os.environ，后面的 app_profile / storage_runtime
# 与各模块在导入期就读环境变量。真实环境变量优先，文件不覆盖已有值。
import env_loader
env_loader.load_env()

# 运维页只读静态快照（脱敏、不含密钥值）；失败静默降级，不影响服务。
import ops_config
ops_config.write_snapshot()

import json
import os
import time as _time
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import store
import profile
import tracking

# 读路径开关：默认 files 时这里什么也不做（零侵入）；STORAGE_READ=db 时
# 在进程启动阶段把文件数据自举进 SQLite，并把绿区模块的读函数接到 db.bridge。
# 绿区 6 个存储模块源码一行不改，sha256 仍与生产一致。
import storage_runtime
storage_runtime.install_from_env()

import data_paths
from data_paths import DATA_DIR, safe_slug
HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(DATA_DIR, "snapshot")
CHARTS = os.path.join(SNAP, "charts")
MEMBERS = os.path.join(SNAP, "members")
FRONTEND = os.path.abspath(os.path.join(HERE, "..", "frontend"))

# 进程启动时刻。用来和磁盘上 .py 的 mtime 对比，判断「改了代码但没重启」。
# 只算后端 .py：前端 index.html 每次请求都从磁盘读，改完刷页面就生效，不必重启。
BOOT_TS = _time.time()
CODE_FILES = ("main.py", "data_paths.py", "pnl.py", "stops.py", "store.py",
              "datasource/sectorflow.py", "jobs/eod.py",
              "storage_runtime.py", "db/bridge.py", "db/repository.py")


def _code_state() -> dict:
    """磁盘代码相对当前进程的新鲜度。

    这个检查的价值在于：「修复没生效」在接口层面看不出来——老进程照样返回
    200，只是行为和改之前一样。把 boot_at / code_at 一起暴露，就能一眼分辨
    是代码写错了还是进程没换。
    """
    newest, newest_name = 0.0, ""
    for rel in CODE_FILES:
        p = os.path.join(HERE, *rel.split("/"))
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if m > newest:
            newest, newest_name = m, rel

    def _fmt(t: float) -> str:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""

    return {
        # 与 ops_config 的快照共用同一个进程级时间戳，前端来源校验才不会误报
        "boot_at": ops_config.BOOT_STAMP,
        "code_at": _fmt(newest),
        "code_file": newest_name,
        # 留 2 秒余量，避开启动瞬间写文件造成的误判
        "stale": bool(newest > BOOT_TS + 2),
    }


app = FastAPI(title="股票跟踪分析平台 API", version="0.1.0-M1")

# 跨域只放行本机自访问（前端由本服务托管，页面与 API 同源，正常使用不依赖 CORS）。
# 之前的 allow_origins=["*"] 会让任意恶意网页跨域读写本机接口（含持仓增删与
# 密钥配置联动），必须收紧。个别特殊部署需要额外来源时用 CORS_ALLOW_ORIGINS 配置。
_CORS_ORIGINS = [x.strip() for x in (os.environ.get("CORS_ALLOW_ORIGINS") or "").split(",")
                 if x.strip()] or [
    "http://127.0.0.1:8000", "http://localhost:8000",
    "http://127.0.0.1:8100", "http://localhost:8100",
    "http://127.0.0.1:8101", "http://localhost:8101",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


def _load(name: str) -> Any:
    p = os.path.join(SNAP, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# 路径分量校验失败（../、斜杠等穿越输入）→ 统一转成 400，不暴露 500 堆栈
@app.exception_handler(data_paths.BadParam)
async def _bad_param_handler(request, exc):
    return JSONResponse(status_code=400,
                        content={"code": 1, "message": str(exc)})


def _meta() -> dict:
    m = _load("market.json") or {}
    return {
        "data_date": m.get("date"),
        "generated_at": m.get("generated_at"),
        "coverage": m.get("coverage", {}),
        "engine": {
            "stage": "温斯坦日线 9 细分态判定树（MA250 / 年线5周斜率±0.3% / RS60 / 均线排列）",
            "pivot": "利弗莫尔 前60日最高收盘关键点，量≥1.3×50日均量，止损=关键点−1.5×ATR20，100分质量分",
            "confirm": "徽标口径=连续3个交易日同态确认；底层 stage_states 保留逐日原值",
        },
    }


@app.get("/api/health")
def health():
    return {"code": 0, "data": {"ok": True, "snapshot": os.path.exists(SNAP), **_code_state()}}


@app.get("/api/search")
def search(q: str = "", limit: int = 24):
    """全平台搜索：股票代码 / 名称 + 申万二级行业代码 / 名称。

    结果带 tags（持仓 / 自选 / 异动 / 信号 / 档案 / K线），前端据此决定
    点击后跳到哪个模块定位。索引纯本地（行业 124 + 全 A 股约 5800 只），
    进程内缓存 5 分钟，不联网、不依赖连接器。
    """
    import search_index
    limit = max(1, min(limit, 60))
    return {"code": 0, "data": search_index.search(q, limit), "meta": _meta()}


@app.get("/api/market")
def market():
    data = _load("market.json")
    if not data:
        raise HTTPException(503, "快照未生成：先运行 python -m jobs.build_snapshot")
    return {"code": 0, "data": data, "meta": _meta()}


@app.get("/api/sectors")
def sectors(stage: int | None = None, sub: str | None = None, sort: str = "rs60", limit: int = 0):
    rows = _load("sectors.json") or []
    if stage:
        rows = [r for r in rows if r.get("stage") == stage]
    if sub:
        rows = [r for r in rows if sub in (r.get("sub") or "")]
    if sort in ("rs60", "dev30", "dev250", "chg"):
        rows.sort(key=lambda r: (r.get(sort) is None, -(r.get(sort) or 0)))
    if limit:
        rows = rows[:limit]
    return {"code": 0, "data": rows, "meta": _meta()}


@app.get("/api/transitions")
def transitions(days: int = 60, kind: str | None = None, type: str | None = None):
    rows = _load("transitions.json") or []
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if type == "up":      # 1→2 突破（进第二阶段）
        rows = [r for r in rows if (r.get("frm_stage") or 1) <= 1 and r.get("to_stage") == 2]
    elif type == "down":  # 2→3 / 3→4
        rows = [r for r in rows if (r.get("to_stage") or 0) > (r.get("frm_stage") or 0)]
    return {"code": 0, "data": rows[:120], "meta": _meta()}


def _focus_pool() -> dict:
    """全市场关注池（jobs/build_focus_pool.py 每日盘后产出）。

    旧版关注页读 signals.json —— 那是 data/raw/klines.json 里 62 只历史股票
    跑出来的事件流，池子自指闭环、不会新增股票。现在统一走 focus_pool.json
    （东财 BK 全量成分股去重 5109 只 → 按阶段/档位/质量分筛出几十只）。
    新文件缺失时才回退旧文件，保证汇总任务失败时页面不会白屏。
    """
    d = _load("focus_pool.json")
    if isinstance(d, dict) and d.get("items"):
        return d
    return {"meta": {"source": "legacy_signals", "picked": None},
            "items": _load("signals.json") or []}


@app.get("/api/signals")
def signals(type: str | None = None, grade: str | None = None, limit: int = 200):
    rows = _focus_pool()["items"]
    if type in ("buy", "sell"):
        rows = [r for r in rows if r.get("type") == type]
    if grade:
        rows = [r for r in rows if r.get("grade") == grade]
    return {"code": 0, "data": rows[:limit], "meta": _meta()}


@app.get("/api/focus/pool")
def focus_pool():
    """关注池口径元信息（扫描只数 / 入选只数 / 规则 / 数据日），供前端标题展示。"""
    d = _focus_pool()
    return {"code": 0,
            "data": {"meta": d.get("meta") or {}, "items": d.get("items") or [],
                     "warmed": d.get("warmed") or {}, "counts": d.get("counts") or {}},
            "meta": _meta()}


def _watchlist_view() -> list[dict]:
    """自选行情视图 = 当日快照 ∩ 用户清单；清单里新增但快照还没跑到的补占位行。"""
    snap = _load("watchlist.json") or []
    by_code = {r.get("code"): r for r in snap if isinstance(r, dict) and r.get("code")}
    import watchstore
    out = []
    for code, name in watchstore.items():
        row = by_code.get(code)
        if row:
            out.append(dict(row, name=row.get("name") or name))
        else:
            out.append(dict(code=code, name=name, price=None, chg=None, sub=None,
                            stage=None, rs60=None, dist52=None, pivot=None,
                            stop=None, has_chart=False, alert=None, pending=True))
    return out


@app.get("/api/watchlist")
def watchlist():
    return {"code": 0, "data": _watchlist_view(), "meta": _meta()}


class WatchIn(BaseModel):
    code: str
    name: str | None = None


@app.post("/api/watchlist")
def watchlist_add(body: WatchIn):
    """加入自选（已在清单里则只补名字）。行情视图下次收盘快照后补全。"""
    import watchstore
    code = store.norm_code(body.code)
    if not code:
        raise HTTPException(400, "代码格式不对：请输入 6 位数字，或带 sh/sz/bj 前缀")
    name = body.name or _resolve_name(code)
    try:
        row, created = watchstore.add(code, name=name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if created:
        try:
            _chart_data(code)              # 先把 K 线算好并缓存，避免前端点开空白
        except Exception:
            pass
    return {"code": 0,
            "data": dict(item=row, created=created, list=_watchlist_view()),
            "meta": _meta()}


@app.delete("/api/watchlist/{code}")
def watchlist_delete(code: str):
    """从自选清单删除（只删清单与当日快照里的视图，不动行情/K线缓存）"""
    import watchstore
    if not watchstore.remove(code):
        raise HTTPException(404, f"{code} 不在自选清单里")
    return {"code": 0, "data": dict(removed=code, list=_watchlist_view()), "meta": _meta()}


@app.get("/api/holdings")
def holdings():
    """持仓列表 = 用户清单（data/portfolio/holdings.json）+ 实时行情视图

    行情部分每次请求现算（读图表缓存，缺失则现场抓），所以前端增删后立刻能看到
    最新的阶段 / 关键点 / 盈亏，不依赖有没有重跑快照。
    """
    return {"code": 0, "data": _holdings_view(), "meta": _meta()}


@app.get("/api/holdings/pnl")
def holdings_pnl():
    """持仓盈亏汇总：今日盈亏 / 累计盈亏 / 覆盖率 / 各股贡献 + 每日历史

    与前端 calcPnlSummary() 同口径；这里用实时视图现算，便于接口层核对。
    """
    import pnl
    as_of = (_load("market.json") or {}).get("date")
    return {"code": 0,
            "data": {"summary": pnl.summarize(_holdings_view(), as_of=as_of),
                     "history": pnl.history(10)},
            "meta": _meta()}


@app.get("/api/holdings/pnl/history")
def holdings_pnl_history(limit: int = 10):
    """每日盈亏历史（EOD 落盘，一天一条）"""
    import pnl
    return {"code": 0, "data": pnl.history(limit), "meta": _meta()}


@app.get("/api/holdings/pnl/stats")
def holdings_pnl_stats():
    """盈亏统计：按日 / 按月聚合 + 已清仓已实现台账"""
    import pnl
    return {"code": 0, "data": pnl.stats(), "meta": _meta()}


@app.get("/api/holdings/pnl/calendar")
def holdings_pnl_calendar(month: str | None = None):
    """盈亏日历：按月返回每个交易日的盈亏金额 + 当日各持仓股的盈亏明细

    month 形如 2026-11；不传则取最新记录所在月份。
    明细从 EOD 开始落盘的那天起才有，早期日期只返回汇总，前端会标注「无个股明细」。
    """
    import pnl
    as_of = (_load("market.json") or {}).get("date")
    return {"code": 0,
            "data": pnl.calendar(month, rows=_holdings_view(), as_of=as_of),
            "meta": _meta()}


@app.get("/api/holdings/realized")
def holdings_realized():
    """清仓股查看：逐条清仓流水 + 按股票聚合的已实现盈亏与战绩"""
    import pnl
    return {"code": 0, "data": pnl.realized_summary(), "meta": _meta()}


class RealizedNoteIn(BaseModel):
    key: str                       # 台账条目标识：code|清仓日|入库时间
    note: str | None = None        # 传空 = 删除该条笔记


@app.put("/api/holdings/realized/note")
def holdings_realized_note(body: RealizedNoteIn):
    """给某笔清仓交易写/改交易笔记。

    笔记单独落在 data/portfolio/realized_notes.json，不写回台账 ——
    台账是金额的唯一真源，不该被文本编辑污染。
    """
    import pnl
    saved = pnl.save_note(body.key, body.note)
    if saved is None:
        raise HTTPException(404, "找不到对应的清仓记录（key 不匹配台账）")
    return {"code": 0, "data": saved, "meta": _meta()}


class HoldingIn(BaseModel):
    code: str
    name: str | None = None
    cost: float | None = None
    quantity: float | None = None
    note: str | None = None


class HoldingPatch(BaseModel):
    cost: float | None = None
    quantity: float | None = None
    name: str | None = None
    note: str | None = None


@app.post("/api/holdings")
def holdings_add(body: HoldingIn):
    """加入持仓（已在持仓里则更新成本/数量）"""
    code = store.norm_code(body.code)
    if not code:
        raise HTTPException(400, "代码格式不对：请输入 6 位数字，或带 sh/sz/bj 前缀，如 002897 / sz002897")
    name = body.name or _resolve_name(code)
    try:
        row, created = store.add(code, name=name, cost=body.cost, quantity=body.quantity)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _chart_data(code)                      # 先把 K 线算好并缓存，避免前端点开空白
    if body.note:
        store.update(code, note=body.note)
    return {"code": 0,
            "data": dict(item=_holding_row(row), created=created, list=_holdings_view()),
            "meta": _meta()}


@app.patch("/api/holdings/{code}")
def holdings_patch(code: str, body: HoldingPatch):
    """改成本价 / 数量 / 备注"""
    row = store.update(code, cost=body.cost, quantity=body.quantity,
                       name=body.name, note=body.note)
    if not row:
        raise HTTPException(404, f"{code} 不在持仓列表里")
    return {"code": 0, "data": dict(item=_holding_row(row), list=_holdings_view()), "meta": _meta()}


@app.delete("/api/holdings/{code}")
def holdings_delete(code: str):
    """从持仓列表删除：把 (现价 − 成本) × 数量 结转为已实现盈亏，累计盈亏保持不变。

    只删清单、结转台账，不动行情缓存。数量/成本缺失时只记审计条目，不影响金额。
    """
    norm = store.norm_code(code)
    row = next((r for r in _holdings_view() if r.get("code") == norm), None)
    if not row:
        raise HTTPException(404, f"{code} 不在持仓列表里")
    if not store.remove(norm):
        raise HTTPException(404, f"{code} 不在持仓列表里")
    import pnl
    as_of = (_load("market.json") or {}).get("date")
    realized = pnl.record_realized(norm, row.get("name"), row.get("price"),
                                   row.get("cost"), row.get("quantity"), date=as_of,
                                   buy_date=row.get("added_at"))
    # 删掉持仓 = 不再跟踪这只股票：方案停用 + 专属 job 撤掉。
    # 历史期次和简报保留，随时能查，只是不再往里追加新的一期。
    tracked = bool(tracking.load(norm))
    job_removed = False
    if tracked:
        tracking.remove(norm)
        try:
            import tracking_jobs
            job_removed = tracking_jobs.remove(norm)
        except Exception as e:
            print(f"[tracking] 删持仓后撤销任务失败 {norm}: {type(e).__name__}: {e}")
    return {"code": 0,
            "data": dict(removed=norm, realized=realized,
                         tracking_stopped=tracked, job_removed=job_removed,
                         realized_total=pnl.realized_total(),
                         list=_holdings_view()),
            "meta": _meta()}


# ---------------------------------------------------------------- 个股档案
@app.get("/api/profile/{code}")
def stock_profile(code: str, refresh: int = 0):
    """基本信息 + 财务摘要 + 券商研报概述（缓存 24 小时）"""
    d = profile.profile(code, refresh=bool(refresh))
    if not d.get("basic") and not d.get("finance"):
        raise HTTPException(404, f"取不到 {code} 的档案数据")
    return {"code": 0, "data": d, "meta": _meta()}


# ---------------------------------------------------------------- 个股研究档案
# 注意：具体路径要先于 /api/research/{code} 声明，否则 status 会被当成 code 匹配。
@app.get("/api/research/status")
def research_status(code: str | None = None):
    """档案总览 / 单只生成状态（前端按需生成后轮询这里）"""
    import research
    s = research.status()
    if code:
        s["job"] = s["jobs"].get(code) or {}
        s["exists"] = os.path.exists(os.path.join(DATA_DIR, "research", f"{code}.md"))
    return {"code": 0, "data": s, "meta": _meta()}


@app.post("/api/research/{code}/refresh")
def research_refresh(code: str, force: int = 0):
    """按需生成 / 强制重建。后台线程执行，立即返回 queued。"""
    import research
    return {"code": 0, "data": research.generate_async(code, force=bool(force)),
            "meta": _meta()}


@app.get("/api/research/{code}")
def research_get(code: str):
    """读取落盘的六章档案（不联网、不调模型）"""
    import research
    return {"code": 0, "data": research.read(code), "meta": _meta()}


# ---------------------------------------------------------------- 持仓跟踪
class TrackingIn(BaseModel):
    action: str = "plan"                     # plan | activate
    plan: dict | None = None
    frequency: dict | None = None
    dimensions: list | None = None


class TrackingPatch(BaseModel):
    status: str | None = None                # active | paused | stopped
    frequency: dict | None = None


@app.get("/api/tracking")
def tracking_list():
    """哪些持仓股已启动跟踪（持仓表用）"""
    return {"code": 0, "data": tracking.list_all(), "meta": _meta()}


@app.get("/api/tracking/{code}")
def tracking_detail(code: str):
    meta = tracking.load(code)
    periods = tracking.period_list(code)
    if not meta:
        raise HTTPException(404, f"{code} 尚未启动跟踪")
    # 环比 / 口径分段 / 健康度都在读接口现算：历史期次已落盘，改口径不能污染历史
    dec = tracking.decorate(meta, periods)
    try:
        import ai as _ai
        llm_ok = bool(_ai.config())
    except Exception:
        llm_ok = False
    job = None
    try:
        import tracking_jobs
        from scheduler.core import get_scheduler
        jd = get_scheduler().state.get_def(tracking_jobs.job_id(code))
        if jd:
            # APScheduler 的 from_crontab 是 0=周一；scheduler.cron.next_fire
            # 用的是 0=周日，两者差一天。这里按实际在跑的 APScheduler 算，
            # 否则界面会把「每周一 08:20」显示成周日。
            try:
                from apscheduler.triggers.cron import CronTrigger
                nxt = CronTrigger.from_crontab(jd["cron"]).get_next_fire_time(
                    None, datetime.now())
            except Exception:
                from scheduler.cron import next_fire
                nxt = next_fire(jd["cron"])
            job = {"id": jd["id"], "cron": jd["cron"],
                   "enabled": jd.get("enabled"),
                   "next": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None}
    except Exception:
        pass
    return {"code": 0, "data": dict(
        meta=meta, periods=dec["periods"], cohorts=dec["cohorts"],
        since=dec["since"], cohort_count=dec["cohort_count"],
        # 后端产出通道的现场：最近一次跑了什么、模型配没配、历史诊断
        run=tracking.latest_run(code), runs=tracking.runs(code, 5), llm=llm_ok,
        job=job,
    ), "meta": _meta()}


@app.post("/api/tracking/{code}")
def tracking_post(code: str, body: TrackingIn):
    """action=plan 生成方案草案；action=activate 保存并启用"""
    prof = profile.profile(code)
    name = _resolve_name(code) or (prof.get("basic") or {}).get("name") or code
    if body.action == "plan":
        plan = tracking.build_plan(code, name, prof)
        return {"code": 0, "data": plan, "meta": _meta()}

    plan = body.plan or tracking.build_plan(code, name, prof)
    plan.update(code=code, name=plan.get("name") or name, status="active")
    if body.frequency:
        plan["frequency"] = body.frequency
    if body.dimensions:
        plan["dimensions"] = body.dimensions
    plan.setdefault("automation", dict(id=None, state="pending_agent"))
    saved = tracking.save(plan)
    # 只有显式点「启动跟踪」才会走到 activate，这里才登记定时任务。
    # 失败不回滚方案：方案本身有效，运维能从 /api/jobs 补登记。
    try:
        import tracking_jobs
        tracking_jobs.upsert(code)
    except Exception as e:
        print(f"[tracking] 定时任务登记失败 {code}: {type(e).__name__}: {e}")
    return {"code": 0, "data": saved, "meta": _meta()}


@app.patch("/api/tracking/{code}")
def tracking_patch(code: str, body: TrackingPatch):
    meta = tracking.load(code)
    if not meta:
        raise HTTPException(404, f"{code} 尚未启动跟踪")
    if body.status:
        meta["status"] = body.status
    if body.frequency:
        meta["frequency"] = body.frequency
    saved = tracking.save(meta)
    if body.frequency:
        try:
            import tracking_jobs
            tracking_jobs.upsert(code)       # 改频率要同步改专属 job 的 cron
        except Exception as e:
            print(f"[tracking] 更新定时任务失败 {code}: {type(e).__name__}: {e}")
    return {"code": 0, "data": saved, "meta": _meta()}


@app.delete("/api/tracking/{code}")
def tracking_delete(code: str):
    if not tracking.remove(code):
        raise HTTPException(404, f"{code} 尚未启动跟踪")
    stopped_job = False
    try:
        import tracking_jobs
        stopped_job = tracking_jobs.remove(code)   # 取消跟踪 = 同时撤掉定时 job
    except Exception as e:
        print(f"[tracking] 撤销定时任务失败 {code}: {type(e).__name__}: {e}")
    return {"code": 0, "data": dict(stopped=code, job_removed=stopped_job,
                                    index=tracking.list_all()), "meta": _meta()}


class BackfillIn(BaseModel):
    weeks: int = 8
    force: bool = False


@app.post("/api/tracking/{code}/backfill")
def tracking_backfill(code: str, body: BackfillIn):
    """首期回填：按方案维度补最近 N 期（只补能从历史数据算出的维度）"""
    if not tracking.load(code):
        raise HTTPException(404, f"{code} 尚未启动跟踪")
    from jobs.backfill_tracking import backfill
    made = backfill(code, body.weeks, body.force)
    return {"code": 0, "data": dict(made=made, periods=tracking.period_list(code)), "meta": _meta()}


# ---- 跟踪期：后端自己跑（不再依赖会话侧自动化） ----
_TRACK_RUNNING: dict[str, float] = {}


class RunIn(BaseModel):
    force: bool = False


@app.post("/api/tracking/{code}/run")
def tracking_run(code: str, body: RunIn | None = None):
    """手动跑一期。后台起子进程，立刻返回，结果由 /runs 轮询。

    同步跑会让浏览器等到 http 超时（一层检索就要 10~20 秒，四层跑满可能一分钟），
    所以这里只负责「点火」，进度看 runs 记录。
    """
    if not tracking.load(code):
        raise HTTPException(404, f"{code} 尚未启动跟踪")
    import subprocess
    import sys
    now = _time.time()
    last = _TRACK_RUNNING.get(code) or 0
    if now - last < 120:
        return {"code": 0, "data": dict(started=False, reason="上一次还在跑，稍等",
                                        since=int(now - last)), "meta": _meta()}
    args = [sys.executable, "-m", "jobs.tracking_review", "--code", code]
    if body and body.force:
        args.append("--force")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        subprocess.Popen(args, cwd=HERE, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        raise HTTPException(500, f"启动失败：{type(e).__name__}: {e}")
    _TRACK_RUNNING[code] = now
    return {"code": 0, "data": dict(started=True, command=" ".join(args)), "meta": _meta()}


@app.get("/api/tracking/{code}/runs")
def tracking_runs(code: str, limit: int = 8):
    if not tracking.load(code):
        raise HTTPException(404, f"{code} 尚未启动跟踪")
    return {"code": 0, "data": dict(runs=tracking.runs(code, limit),
                                    latest=tracking.latest_run(code)), "meta": _meta()}


# ---------------------------------------------------------------- 界面风格
class ThemeIn(BaseModel):
    theme: str


@app.get("/api/ui/theme")
def ui_theme_get():
    import ui_config
    return {"code": 0, "data": ui_config.describe(), "meta": _meta()}


@app.post("/api/ui/theme")
def ui_theme_set(body: ThemeIn):
    import ui_config
    try:
        t = ui_config.set_theme(body.theme)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 0, "data": {"theme": t}, "meta": _meta()}


@app.get("/api/alerts")
def alerts():
    """站内提醒：自选 + 持仓 在最近 14 个自然日内触发的买卖点

    优先读 EOD 管道生成的 alerts.json（jobs.eod 产出），缺失时回退为实时计算，
    保证前端在只跑过 build_snapshot 的环境里也能出提醒。
    """
    snap = _load("alerts.json")
    if snap and isinstance(snap.get("items"), list):
        return {"code": 0, "data": snap["items"],
                "meta": dict(_meta(), generated_at=snap.get("generated_at"),
                             base_date=snap.get("base_date"))}

    watch = {r["code"] for r in (_load("watchlist.json") or [])}
    hold = {r["code"] for r in (_load("holdings.json") or [])}
    m = _load("market.json") or {}
    data_date = m.get("date") or ""
    try:
        from datetime import date as _date, timedelta as _td
        y, mo, d = (int(x) for x in data_date.split("-"))
        cutoff = (_date(y, mo, d) - _td(days=14)).isoformat()
    except Exception:
        cutoff = ""
    out = []
    for s in (_load("signals.json") or []):
        if s["code"] not in watch and s["code"] not in hold:
            continue
        if cutoff and s["date"] < cutoff:
            continue
        out.append(dict(s, scope="holding" if s["code"] in hold else "watchlist"))
    out.sort(key=lambda s: s["date"], reverse=True)
    return {"code": 0, "data": out[:30], "meta": _meta()}


@app.get("/api/chart/{code}")
def chart(code: str, refresh: int = 0, period: str = "day"):
    """K 线图数据。

    period=day      → 原有日线（快照缓存 / 现场生成，含阶段带、信号、枢轴）
    period=week/month/year → 本地日K聚合的多周期K（见 engines/periods.py）

    快照里已有的标的直接读文件；行业下钻点到的成分股没有预生成文件，
    这里改用公开行情接口现场抓 K 线并计算（约 0.3 秒），结果落盘缓存。
    """
    # 板块指数（pt 开头）：K 线由 EOD 从东财 BK 源补齐后写入 klines.json，
    # 再由 build_snapshot 生成 charts/pt*.json。这里优先读该缓存并正常返回，
    # 前端点板块才能出图。只有缓存确实不存在/无数据时才回落空图，
    # 避免前端拿到 null 再 .slice() 崩；绝不现场走腾讯 pt 源（只回 1 根）。
    if str(code).lower().startswith("pt"):
        try:
            p = os.path.join(CHARTS, f"{safe_slug(code)}.json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if d and d.get("dates"):
                    name = d.get("name")
                    if not name or name == code:
                        d["name"] = _name_of(code) or name or code
                    return {"code": 0, "data": d, "meta": _meta()}
        except Exception:
            pass
        empty = dict(code=code, name=_name_of(code) or code,
                     dates=[], bars=[], volumes=[], pivot_line=[],
                     ma30=[], ma60=[], ma120=[], ma250=[], bands=[],
                     pivots=[], signals=[], last=None)
        return {"code": 0, "data": empty, "meta": _meta()}
    if period != "day":
        from engines.periods import build as build_period
        d = build_period(code, period, name=_name_of(code))
        if not d:
            cn = {"week": "周", "month": "月", "year": "年"}.get(period, period)
            raise HTTPException(404, (
                f"{code} 暂无可用于聚合{cn}K的日K：未纳入每日批量历史库，也没有日K图缓存，"
                "在线补历史也未成功。请先在日K视图打开一次该标的（会自动缓存），再切周期；"
                "若是新上市标的，则历史交易日确实不足。"
            ))
        return {"code": 0, "data": d, "meta": _meta()}
    d = _chart_data(code, refresh=bool(refresh))
    if not d:
        raise HTTPException(404, f"无 {code} 的图表数据（该标的尚未抓取历史K线，接口也未能取到）")
    return {"code": 0, "data": d, "meta": _meta()}


# ---------------------------------------------------------------- 成分股下钻
from datasource.tencent import TencentSource          # noqa: E402
from engines.stage import compute_series, STAGE_NAME    # noqa: E402
from engines.pivot import scan                         # noqa: E402
from engines.chart import chart_payload                # noqa: E402

_SRC: TencentSource | None = None
_BENCH: dict[str, float] | None = None
_BENCH_DATE: str | None = None
_MEMBER_NAMES: dict[str, str] | None = None
_STOCK_NAMES: dict[str, str] | None = None


def _src() -> TencentSource:
    global _SRC
    if _SRC is None:
        _SRC = TencentSource()
    return _SRC


def _bench() -> dict[str, float]:
    """沪深300 收盘序列（按日期缓存，避免每次请求都去抓）"""
    global _BENCH, _BENCH_DATE
    today = __import__("time").strftime("%Y-%m-%d")
    if _BENCH is not None and _BENCH_DATE == today:
        return _BENCH
    rows = _src().fetch_kline(["sh000300"], limit=420).get("sh000300") or []
    _BENCH = {r["date"]: r["close"] for r in rows}
    _BENCH_DATE = today
    return _BENCH


def _all_stock_names() -> dict[str, str]:
    """全 A 股「代码→名称」表（tools/build_stock_names.py 生成），首次加载后常驻内存。"""
    global _STOCK_NAMES
    if _STOCK_NAMES is None:
        d = _load("stock_names.json") or {}
        _STOCK_NAMES = {r["code"]: r.get("name") or r["code"]
                        for r in (d.get("items") or []) if r.get("code")}
    return _STOCK_NAMES


def _name_of(code: str) -> str:
    for fn in ("watchlist.json", "holdings.json"):
        for r in (_load(fn) or []):
            if r.get("code") == code:
                return r.get("name") or code
    # 异动页下钻 / 全局搜索点进来的成分股往往不在自选与持仓快照里，
    # 以前会退化成代码显示；这里用全市场名称表兜底。
    return _all_stock_names().get(code) or code


def _resolve_name(code: str) -> str | None:
    """代码 → 中文名：先查自选/持仓快照，再查成分股索引，最后问行情接口"""
    n = _name_of(code)
    if n and n != code:
        return n
    n = _find_member_name(code)
    if n:
        return n
    try:
        q = _src().quotes([code])
        if q and q.get(code, {}).get("name"):
            return q[code]["name"]
    except Exception:
        pass
    return None


def _chart_data(code: str, refresh: bool = False) -> dict | None:
    """读图表缓存；缓存落后于最新交易日或缺失时现场重算"""
    try:
        safe_slug(code, "股票代码")     # 拒绝 ..、斜杠等路径穿越输入
    except ValueError:
        return None
    p = os.path.join(CHARTS, f"{safe_slug(code)}.json")
    if os.path.exists(p) and not refresh:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = None
        if d:
            market_date = (_load("market.json") or {}).get("date")
            if not market_date or (d.get("dates") and d["dates"][-1] >= market_date):
                return d
    return _build_chart(code)


def _holding_row(item: dict, track_idx: dict | None = None) -> dict:
    """清单条目 + 图表缓存 → 前端要的持仓行"""
    code = item["code"]
    name = item.get("name") or ""
    cost, qty = item.get("cost"), item.get("quantity")
    row = dict(code=code, name=name or code, cost=cost, quantity=qty,
               added_at=item.get("added_at"), note=item.get("note"),
               as_of=None,          # 该股行情所属交易日，由下方 K 线缓存回填
               price=None, chg=None, sub=None, stage=None, stage_name=None,
               sub_confirmed=None, dist52=None, rs60=None, pivot=None, stop=None,
               last_signal=None, has_chart=False, pnl=None, market_value=None,
               tracking=(track_idx or {}).get(code))
    ch = _chart_data(code)
    sector, sub_sector = tracking.sector_of(code)
    row["sector"] = sector
    if not ch:
        return row
    l = ch["last"]
    bars = ch.get("bars") or []
    prev = bars[-2][1] if len(bars) >= 2 else None
    sig = (ch.get("signals") or [None])[-1]
    pivots = ch.get("pivots") or []
    row.update(
        name=(name if name and name != code else (ch.get("name") or code)),
        as_of=(ch.get("dates") or [None])[-1],   # 这根 K 线的交易日，盈亏基准日用它
        price=l["close"],
        chg=(round((l["close"] / prev - 1) * 100, 2) if prev else None),
        sub=l["sub"], stage=l["stage"], stage_name=STAGE_NAME.get(l["stage"]),
        sub_confirmed=l["confirmed"], dist52=l["dist52"], rs60=l["rs60"],
        pivot=(pivots[-1]["price"] if pivots else None),
        stop=(sig.get("stop") if sig else None),
        last_signal=sig, has_chart=True,
        pnl=(round((l["close"] / cost - 1) * 100, 2) if cost else None),
        market_value=(round(l["close"] * qty, 2) if qty else None),
    )
    return row


def _holdings_view() -> list[dict]:
    """整个持仓列表的视图，保持用户在清单里的顺序"""
    try:
        tracking._reindex()   # 自动化任务直接写盘不触发索引，这里每次刷新保证期数/判定同步
    except Exception:
        pass
    idx = tracking.list_all()
    rows = [_holding_row(item, idx) for item in store.load()]
    # ---- 动态止损合并（每日收盘重算；数据过期时懒更新）----
    try:
        import stops as stops_mod
        # 最新交易日：取持仓K线缓存里最大的末根日期
        td = None
        for r in rows:
            ch = _chart_data(r["code"])
            if ch and ch.get("dates"):
                d = ch["dates"][-1]
                if d and (td is None or d > td):
                    td = d
        sd = stops_mod.load(current_trade_date=td)
        for r in rows:
            item = sd.get("items", {}).get(r["code"])
            if item:
                r["stop_dyn"] = item
                r["stop_history"] = (sd.get("history") or {}).get(r["code"]) or []
    except Exception as e:
        print(f"[holdings] 止损合并失败：{type(e).__name__}: {e}")
    return rows


def _build_chart(code: str) -> dict | None:
    """现场抓 K 线 → 算阶段 → 产出图表 payload，并写缓存"""
    try:
        safe_slug(code, "股票代码")     # 写缓存文件名同样要过穿越校验
        bars = _src().fetch_kline([code], limit=420).get(code)
        if not bars:
            return None
        rows = bars
        bench = _bench()
        series = compute_series(rows, bench, keep=len(rows))
        if not series:
            return None
        pivots, sigs = scan(rows, series)
        name = _name_of(code)
        if name == code:
            # 从成分股快照里补名字（一次扫描所有行业索引太重，只查已缓存的行业文件）
            name = _find_member_name(code) or code
        d = chart_payload(code, name, rows, series, pivots, sigs)
        os.makedirs(CHARTS, exist_ok=True)
        with open(os.path.join(CHARTS, f"{code}.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        return d
    except Exception as e:  # 网络异常不应该把整个接口打成 500
        print(f"[chart] {code} 现场生成失败：{e}")
        return None


def _find_member_name(code: str) -> str | None:
    """在已生成的成分股快照里查中文名（首次调用建一次全局索引，之后 O(1)）"""
    global _MEMBER_NAMES
    if _MEMBER_NAMES is None:
        _MEMBER_NAMES = {}
        if os.path.isdir(MEMBERS):
            for fn in os.listdir(MEMBERS):
                if not fn.endswith(".json") or fn.startswith("_"):
                    continue
                try:
                    with open(os.path.join(MEMBERS, fn), encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue
                for m in d.get("members", []):
                    if m.get("code"):
                        _MEMBER_NAMES.setdefault(m["code"], m.get("name"))
    return _MEMBER_NAMES.get(code)


def _load_members(code: str) -> dict | None:
    try:
        safe_slug(code, "板块代码")
    except ValueError:
        return None
    p = os.path.join(MEMBERS, f"{code}.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _build_members(code: str) -> dict | None:
    """该行业还没有快照时现场生成（约 2 秒：取成分股 + 并发抓 K 线 + 算阶段）"""
    try:
        safe_slug(code, "板块代码")
    except ValueError:
        return None
    sectors = {s["code"]: s for s in (_load("sectors.json") or [])}
    meta = sectors.get(code)
    if not meta:
        return None
    try:
        from jobs.fetch_members import build_sector
        return build_sector(code, meta, _src(), _bench())
    except Exception as e:
        print(f"[members] {code} 现场生成失败：{e}")
        return None


def _live_calibrate_members(data: dict) -> dict:
    """成分股价格类字段用实时快照校准（阶段/RS60 等指标口径仍为快照 as_of 日）。

    成员快照是每天一次的全量计算（约 6 分钟），盘中不会重跑；但现价/涨跌/
    距52周高可以直接从实时快照覆盖，成本是每 60 只一次请求。
    """
    rows = data.get("members") or []
    if not rows:
        return data
    try:
        snaps = _src().snapshots([r.get("code") for r in rows])
    except Exception as e:
        print(f"[members] 实时校准失败：{e}")
        return data
    n = 0
    qdate = None
    for r in rows:
        q = snaps.get(r.get("code"))
        if not q or q.get("price") is None:
            continue
        r["price"] = r["close"] = q["price"]
        r["chg"] = q.get("chg")
        h52 = q.get("high52")
        if h52:
            try:
                r["dist52"] = round((q["price"] / h52 - 1) * 100, 1)
            except Exception:
                pass
        if q.get("quote_date"):
            qdate = q["quote_date"]
        n += 1
    if n:
        data = dict(data, live_as_of=datetime.now().strftime("%Y-%m-%d %H:%M"),
                    live_quote_date=qdate, live_calibrated=n)
    return data


@app.get("/api/sector/{code}/members")
def sector_members(code: str, stage: int | None = None, sub: str | None = None, refresh: int = 0):
    """某个行业的成分股阶段明细（板块 → 个股下钻）

    返回全部成分股，前端负责按阶段/细分态筛选；服务端也支持 stage/sub 过滤便于排查。
    """
    data = None if refresh else _load_members(code)
    if not data:
        data = _build_members(code)
    if not data:
        raise HTTPException(404, f"{code} 不是有效的行业代码，或该行业暂无成分股数据")

    data = _live_calibrate_members(data)

    rows = data.get("members", [])
    if stage:
        rows = [r for r in rows if r.get("stage") == stage]
    if sub:
        rows = [r for r in rows if r.get("sub") == sub]
    if stage or sub:
        data = dict(data, members=rows, filtered=len(rows))
    return {"code": 0, "data": data, "meta": _meta()}


@app.get("/api/members/index")
def members_index():
    """哪些行业已有成分股快照（前端用来提示"可下钻"）"""
    p = os.path.join(MEMBERS, "_index.json")
    if not os.path.exists(p):
        return {"code": 0, "data": {}, "meta": _meta()}
    with open(p, encoding="utf-8") as f:
        return {"code": 0, "data": json.load(f), "meta": _meta()}


# ---------------------------------------------------------------- 异动扫描
@app.get("/api/live/{code}")
def live_bar(code: str):
    """当日实时行情（含今开/最高/最低/量），供前端拼进 K 线最后一根"""
    from datasource.tencent import TencentSource
    try:
        q = TencentSource().snapshots([code.lower()]).get(code.lower())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"实时行情取数失败：{e}")
    if not q or not q.get("price"):
        raise HTTPException(status_code=404, detail="该代码暂无实时行情")
    today = datetime.now().strftime("%Y-%m-%d")
    return {"code": 0, "data": {
        **q,
        "date": q.get("quote_date") or today,
        "time": datetime.now().strftime("%H:%M"),
        "is_today": (q.get("quote_date") or today) == today,
    }, "meta": _meta()}


_INTRADAY: dict[str, tuple[float, dict]] = {}      # code:days → (抓取时间, 数据)


_SECTORFLOW = {"t": 0.0, "data": None}


@app.get("/api/market/flow")
def market_flow():
    """申万行业主力资金流排行，按主力净流入降序；带本地阶段/RS60 富化。

    主源东财（496 个一/二/三级，带超大单拆分），东财限流时自动切腾讯（124 个
    二级，与本地板块一一对应），两者都失败则回落上次成功抓取的磁盘缓存。
    返回里 source / stale / asof / note 说明当前口径，前端据此提示。
    """
    import time as _t
    from datasource.sectorflow import attach_local, ranking, upstream_state
    now = _t.time()
    if _SECTORFLOW["data"] and now - _SECTORFLOW["t"] < 300:
        return {"code": 0, "data": _SECTORFLOW["data"], "meta": _meta()}
    try:
        d = ranking()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"资金流取数失败：{type(e).__name__}: {e}")
    payload = {
        "rows": attach_local(d.get("rows") or []),
        "source": d.get("source"), "stale": bool(d.get("stale")),
        "asof": d.get("asof"), "note": d.get("note") or "",
        "upstream": d.get("upstream") or upstream_state(),
    }
    _SECTORFLOW["t"] = now
    _SECTORFLOW["data"] = payload
    return {"code": 0, "data": payload, "meta": _meta()}


@app.get("/api/market/flow/history")
def market_flow_history(bk: str, days: int = 15):
    """单板块近 N 日主力资金流（默认 15 日），供 15 日连续对比。

    BK 代码（东财）返回逐日 + 超大/大单拆分；PT 代码（腾讯备源）没有逐日历史，
    返回「今日 / 近5日 / 近20日」三段摘要，结构一致、前端可直接画柱图。
    """
    from datasource.sectorflow import history
    try:
        d = history(bk, days)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"板块资金流历史取数失败：{type(e).__name__}: {e}")
    if not d.get("days"):
        raise HTTPException(404, f"{bk} 无资金流历史数据")
    return {"code": 0, "data": d, "meta": _meta()}


_FLOW_SECTOR: dict[str, tuple[float, dict]] = {}


@app.get("/api/market/flow/sector")
def market_flow_sector(bk: str):
    """单个东财申万行业的详情：真实成分股 + 近 15 日资金 + 本地近似板块。

    首页资金流的行业清单（496 个）与板块模块（124 个本地申万二级）不是同一套
    分类，像「集成电路制造」在本地根本没有同名行。这个接口让板块页可以直接
    展示该资金流行业自己的成分股，而不是只给一个含糊的近似板块。
    """
    import time as _t
    from datasource.sectorflow import (CLIST_URL, _get, _load_map, _num,
                                       attach_local, history, ranking)
    bk = str(bk or "").strip().upper()
    if not bk.startswith("BK"):
        raise HTTPException(400, f"板块代码不合法：{bk}")
    hit = _FLOW_SECTOR.get(bk)
    if hit and _t.time() - hit[0] < 300:
        return {"code": 0, "data": hit[1], "meta": _meta()}

    rows = attach_local(ranking()["rows"])
    row = next((r for r in rows if r.get("bk") == bk), None)
    name = (row or {}).get("name") or bk
    members, pn = [], 1
    try:
        while pn <= 5:
            q = ("?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2&fid=f62"
                 "&fs=b:%s&fields=f12,f14,f2,f3,f62,f184" % (pn, bk))
            d = (_get(CLIST_URL + q, timeout=10, retry=1).get("data") or {})
            diff = d.get("diff") or []
            for x in diff:
                c = str(x.get("f12") or "").strip()
                if not c:
                    continue
                pfx = "sh" if c.startswith(("6", "9")) else ("bj" if c.startswith(("4", "8")) else "sz")
                members.append({
                    "code": pfx + c, "name": x.get("f14"),
                    "price": _num(x.get("f2")), "chg": _num(x.get("f3")),
                    "main": _num(x.get("f62")), "main_pct": _num(x.get("f184")),
                })
            if len(diff) < 100 or len(members) >= (d.get("total") or 0):
                break
            pn += 1
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"{name} 成分股取数失败：{type(e).__name__}: {e}")

    try:
        days = history(bk, 15).get("days") or []
    except Exception:
        days = []
    mp = (_load_map() or {}).get(bk) or {}
    data = {
        "bk": bk, "name": name, "total": len(members),
        "members": sorted(members, key=lambda m: (m["main"] is not None, m["main"] or 0), reverse=True),
        "days": days,
        "local": {
            "code": (row or {}).get("code"), "name": (row or {}).get("local_name"),
            "match": (row or {}).get("match") or mp.get("match") or "none",
            "coverage": (row or {}).get("coverage") or mp.get("coverage") or 0,
            "via": (row or {}).get("via") or mp.get("via"),
            "hits": (row or {}).get("hits") or mp.get("hits") or 0,
            "total": (row or {}).get("total") or mp.get("total") or 0,
            "stage": (row or {}).get("stage"), "rs60": (row or {}).get("rs60"),
        },
    }
    _FLOW_SECTOR[bk] = (_t.time(), data)
    return {"code": 0, "data": data, "meta": _meta()}


# ---------------------------------------------------------------- 每日消息
@app.get("/api/news/index")
def news_index(limit: int = 30):
    """可用日期清单 + 每日条数与摘要，供消息页切换日期。"""
    import news
    return {"code": 0, "data": news.index(limit), "meta": _meta()}


@app.get("/api/news")
def news_daily(date: str | None = None):
    """某日的重要资讯与影响分析简报；不传 date 返回最新一期。"""
    import news
    d = news.load(date)
    if not d:
        raise HTTPException(404, f"暂无 {date or '任何日期'} 的资讯简报（每日自动化任务尚未产出）")
    return {"code": 0, "data": d, "meta": _meta()}


@app.get("/api/intraday/{code}")
def intraday_points(code: str, days: int = 1, date: str | None = None):
    """分时（价格 / 均价 / 分钟量）。

    两种用法：
      · 不传 date —— 当日分时，供 K 线图切换成"分时"视图；days=2..5 可取多日
      · 传 date=2026-11-04 —— 指定交易日的历史分时，供日K下方联动子图用
        （近 5 日用 1 分钟粒度，更早退到 5 分钟粒度，历史日结果落盘只算一次）

    当日数据 30 秒缓存（够看，又不至于被东财限流）；收盘后同一份数据整天有效。
    """
    import time as _t
    from datasource.evidence import trend_points, trend_on
    code = code.lower()
    key = f"{code}:{date}" if date else f"{code}:{days}"
    now = _t.time()
    hit = _INTRADAY.get(key)
    if hit and now - hit[0] < 30:
        return {"code": 0, "data": hit[1], "meta": _meta()}
    try:
        d = trend_on(code, date) if date else trend_points(code, days)
    except Exception as e:
        raise HTTPException(502, f"分时取数失败：{type(e).__name__}: {e}")
    if not d.get("points"):
        raise HTTPException(404, f"无 {code} 的分时数据（可能停牌或代码无效）")
    _INTRADAY[key] = (now, d)
    return {"code": 0, "data": d, "meta": _meta()}


@app.get("/api/screener")
def screener(date: str | None = None):
    """量能异动清单；date 不传则返回最近一期，同时带回可用日期列表"""
    import screener
    d = screener.load(date)
    return {"code": 0, "data": {"dates": screener.dates(), "current": d}, "meta": _meta()}


@app.post("/api/screener/scan")
def screener_scan():
    """手动触发全市场量能异动扫描（约 30~60 秒）"""
    import screener
    try:
        d = screener.scan()
        return {"code": 0, "data": {"dates": screener.dates(), "current": d}, "meta": _meta()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ai/case/{code}")
def ai_case(code: str, date: str | None = None, refresh: int = 0):
    """异动证据包（公告/龙虎榜/资金流/分时/涨停池 + 本地盘口板块），当日缓存"""
    from datasource import evidence
    try:
        d = evidence.build_case(code, date, refresh=bool(refresh))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"证据获取失败：{e}")
    return {"code": 0, "data": d, "meta": _meta()}


@app.get("/api/ai/brief/{code}")
def ai_brief(code: str, date: str | None = None, refresh: int = 0):
    """异动解读：规则归因打底 + 预生成/模型叙述（source 表示当前生效通道）"""
    import ai as ai_mod
    from datasource import evidence
    try:
        case = evidence.build_case(code, date)
        d = ai_mod.brief(case, refresh=bool(refresh))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"解读生成失败：{e}")
    d["quote"], d["tech"] = case.get("quote"), case.get("tech")
    d["industry"] = case.get("industry")
    d["source_status"] = case.get("source_status")
    return {"code": 0, "data": d, "meta": _meta()}


class ChatIn(BaseModel):
    messages: list[dict] = []
    date: str | None = None


@app.post("/api/ai/chat/{code}")
def ai_chat(code: str, body: ChatIn):
    """多轮对话（SSE 流式）。无模型通道时返回一段说明文本，不报错"""
    import ai as ai_mod
    from fastapi.responses import StreamingResponse
    from datasource import evidence
    try:
        case = evidence.build_case(code, body.date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"证据获取失败：{e}")

    def gen():
        try:
            for chunk in ai_mod.chat_stream(case, body.messages):
                yield "data: " + json.dumps({"delta": chunk}, ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"error": str(e)}, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    p = os.path.join(FRONTEND, "index.html")
    if not os.path.exists(p):
        return JSONResponse({"code": 404, "message": "frontend/index.html 不存在"}, status_code=404)
    # 页面迭代频繁：禁用缓存，确保浏览器每次都拿最新 HTML/JS，
    # 避免旧脚本 + 新接口/新数据结构组合导致的"图表空白"类问题。
    return FileResponse(p, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


if os.path.isdir(FRONTEND):
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


# ---------------------------------------------------------------- Key 自助配置
# 只有 POST 端点：影子差分按 openapi 的 GET 路由集合比对（见 tools/diff_apis.py），
# 纯 POST 不进那份清单，所以默认开启也不打破 32:32。HOST 非本机时 mount() 会
# 自行拒绝，避免写密钥的接口暴露到公网。设 CONFIG_API_ENABLED=0 可整体关闭。
if (os.getenv("CONFIG_API_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off"):
    try:
        import config_api
        config_api.mount(app)
    except Exception as _ce:                     # noqa: BLE001
        print(f"[config] 挂载失败，服务照常运行：{type(_ce).__name__}: {_ce}")


# ---------------------------------------------------------------- 可选调度层
# 默认关闭：影子期不注册任何新路由，保证原有 32 个 GET 路由集合与响应逐字段一致。
# 设 SCHEDULER_ENABLED=1 才挂载 /api/jobs/*；启动失败也只降级调度，不影响 Web 服务。
# SCHEDULER_DRY_RUN 默认开，未显式关闭前不会真的抓行情或写业务数据。
if (os.getenv("SCHEDULER_ENABLED") or "").strip().lower() in ("1", "true", "yes"):
    try:
        from scheduler.api import mount as mount_scheduler, router as scheduler_router
        app.include_router(scheduler_router)
        mount_scheduler(app)
    except Exception as _e:                     # noqa: BLE001
        print(f"[scheduler] 挂载失败，服务照常运行：{type(_e).__name__}: {_e}")
