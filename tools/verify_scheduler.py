"""阶段 4 验收：日历 / cron / job_defs / 动态增删 / 追补去重 / 零侵入。

全部在内存库里跑，不写任何业务数据、不联网、不启动真实取数。
用法： python tools/verify_scheduler.py [--tracking-dir <只读目录>] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)
os.environ.setdefault("ASTOCK_DB_PATH", ":memory:")
# 关掉自动追补线程，改由脚本显式调用，避免与断言竞争同一条 job_runs
os.environ.setdefault("SCHEDULER_CATCHUP", "0")

from db.repository import get_repository                     # noqa: E402
from scheduler import calendar as cal, cron, handlers       # noqa: E402
from scheduler.backends import BuiltinBackend, apscheduler_available, select_backend  # noqa: E402
from scheduler.core import Scheduler                        # noqa: E402

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f"  ·  {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracking-dir", default="", help="只读跟踪方案目录（可选，验证真实工作集读取）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    print("A · 交易日历")
    sunday = datetime.strptime("2026-11-15", "%Y-%m-%d").date()
    monday = datetime.strptime("2026-11-16", "%Y-%m-%d").date()
    check("周末不是交易日", cal.is_trading_day(sunday) is False)
    check("工作日是交易日", cal.is_trading_day(monday) is True)
    check("最近交易日回退到周五", cal.last_trading_day(sunday).isoformat() == "2026-11-13")
    check("上一/下一交易日", cal.prev_trading_day(monday).isoformat() == "2026-11-13"
          and cal.next_trading_day(sunday).isoformat() == "2026-11-16")
    _orig_holidays, _orig_extra = cal.holidays, cal.extra_sessions
    try:
        cal.holidays = lambda path=None: {monday.isoformat()}
        check("手工休市日生效（不硬编码节假日）", cal.is_trading_day(monday) is False)
        cal.holidays = lambda path=None: set()
        cal.extra_sessions = lambda path=None: {sunday.isoformat()}
        check("调休开市日可覆盖周末", cal.is_trading_day(sunday) is True)
    finally:
        cal.holidays, cal.extra_sessions = _orig_holidays, _orig_extra
    check("快照基准日可读且不等于本地今天",
          bool(cal.latest_known_trade_date()) and
          cal.latest_known_trade_date() != datetime.now(cal.CN_TZ).date().isoformat(),
          str(cal.latest_known_trade_date()))

    print("B · cron 解析")
    nxt = cron.next_fire("0 16 * * 1-5", datetime(2026, 11, 15, 22, 40, tzinfo=cal.CN_TZ))
    check("5 段 cron 下次触发", nxt is not None and nxt.isoformat().startswith("2026-11-16T16:00"),
          nxt.isoformat() if nxt else "")
    check("*/n 步长", cron.parse("*/5 * * * *").minute == frozenset(range(0, 60, 5)))
    check("周 7 归一到周日 0", 0 in cron.parse("0 0 * * 7").dow)
    for bad in ("* * * *", "99 * * * *", "x * * * *", "*/0 * * * *"):
        try:
            cron.validate(bad)
            check(f"非法 cron 被拒 {bad!r}", False)
        except ValueError:
            check(f"非法 cron 被拒 {bad!r}", True)

    print("C · 表结构与固定分桶")
    repo = get_repository(fresh=True)
    tables = [r[0] for r in repo.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    check("job_defs / job_runs 表已建", "job_defs" in tables and "job_runs" in tables)
    check("schema 版本 1.1", repo.stats()["schema_version"] == "1.1")
    check("固定 job 数恒定 7（不随标的增长）", len(handlers.FIXED_JOBS) == 7,
          f"{len(handlers.FIXED_JOBS)} 个")
    check("5 个 cad 分桶齐备",
          {j["params"].get("cad") for j in handlers.FIXED_JOBS
           if j["job_type"] == "tracking_review"} ==
          {"daily", "twice_weekly", "weekly", "quarterly"})
    check("auto 后端选择与依赖探测一致",
          select_backend("auto", misfire_grace_time=10).name ==
          ("apscheduler" if apscheduler_available() else "builtin"))

    print("D · 追补与去重（dry-run）")
    s = Scheduler(repo=repo, backend=BuiltinBackend())
    check("默认 dry_run 开启（不会真抓行情）", s.dry_run is True)
    s.start()
    check("7 个固定 job 全部注册", s.backend.list() and len(s.backend.list()) == 7)
    first = s.run_catchup()
    check("启动追补按最近交易日补 6 条", len([r for r in first if r.get("status") == "ok"]) == 6,
          f"{len(first)} 项")
    # run_catchup 的返回项自带 key(交易日) 与 result(dry_run 标记)，直接用，
    # 不再硬编码日期、也不依赖 get_run 的键格式（历史上这两处都随版本变过）。
    eod_run = next((r for r in first if r.get("job") == "eod_close" and r.get("status") == "ok"), None)
    detail = (eod_run or {}).get("result") or {}
    check("dry-run 不产生子进程调用", detail.get("dry_run") is True and "returncode" not in detail)
    again = s.run_catchup()
    check("重复追补不增生（already_done）",
          all(r.get("skipped") == "already_done" for r in again))
    r1 = s._run("track_daily", trade_date="2026-11-12")
    r2 = s._run("track_daily", trade_date="2026-11-12")
    check("同一交易日同一任务只跑一次", r1.get("status") == "ok" and r2.get("skipped") == "duplicate")
    # 非交易日跳过只看"当天"（core.py:79 显式传 trade_date 会绕过日历）。
    # 用 monkeypatch 模拟今天是非交易日，不再假设脚本恰好在非交易日运行。
    _orig_is_trading = cal.is_trading_day
    try:
        cal.is_trading_day = lambda *_a, **_k: False
        check("非交易日触发被跳过", s._run("track_daily").get("skipped") == "non_trading_day")
    finally:
        cal.is_trading_day = _orig_is_trading

    print("E · 动态自定义 job（新增持仓类需求）")
    row = s.add_custom({"id": "probe_2min", "job_type": "noop",
                        "cron": "*/2 * * * *", "params": {"tag": "x"}})
    check("运行中新增不重启即生效",
          row["id"] == "probe_2min" and bool(s.backend.next_run("probe_2min")))
    before = s.backend.next_run("probe_2min")
    s.patch_custom("probe_2min", {"cron": "0 3 * * *"})
    after = s.backend.next_run("probe_2min")
    check("改 cron 后 next_run_at 同步变化", before != after and "T03:00" in after,
          f"{before} → {after}")
    s.patch_custom("probe_2min", {"enabled": 0})
    check("禁用后立即从运行时摘除", s.backend.next_run("probe_2min") is None)
    s.patch_custom("probe_2min", {"enabled": 1})
    check("重新启用后恢复注册", bool(s.backend.next_run("probe_2min")))
    s.stop()
    s2 = Scheduler(repo=repo, backend=BuiltinBackend())   # 模拟服务重启，MemoryJobStore 清空
    s2.start()
    check("重启后从 job_defs 自动重建自定义任务",
          bool(s2.backend.next_run("probe_2min")) and len(s2.state.list_defs()) == 1)
    check("重启后固定任务也重建", len(s2.backend.list()) == 8)
    manual = s2.run_now("probe_2min")
    check("手工触发入口可用", manual.get("status") == "ok")
    check("删除后不再触发",
          s2.delete_custom("probe_2min") and s2.backend.next_run("probe_2min") is None
          and len(s2.state.list_defs()) == 0)
    s2.stop()

    print("F · 工作集（新增持仓不新增 job）")
    n_before = len(handlers.FIXED_JOBS)
    _orig = handlers.work_items
    try:
        handlers.work_items = lambda cad=None, **kw: (
            [{"code": "sh900001", "cad": "daily"}, {"code": "sh900002", "cad": "daily"}]
            if cad == "daily" else [])
        out = handlers.h_tracking_review({"cad": "daily"}, {"dry_run": True})
        check("job 每次触发扫工作集，新增标的自动纳入",
              out.get("items") == 2 and len(handlers.FIXED_JOBS) == n_before,
              f"items={out.get('items')}, job 数仍为 {len(handlers.FIXED_JOBS)}")
    finally:
        handlers.work_items = _orig
    if a.tracking_dir:
        items = _orig("weekly", data_dir=a.tracking_dir)
        check("真实目录可读出跟踪方案", len(items) > 0, f"{len(items)} 条")
    else:
        print("  [-- ] 真实目录检查跳过（传 --tracking-dir 可启用）")

    print("G · 零侵入：默认不挂载调度路由")
    os.environ.pop("SCHEDULER_ENABLED", None)
    # main.py 导入期会写运维快照；离线自检不能覆盖常驻实例的 ops-config.<port>.json
    _snap_saved = os.environ.get("ASTOCK_OPS_SNAPSHOT")
    os.environ["ASTOCK_OPS_SNAPSHOT"] = "0"
    import main as web
    if _snap_saved is None:
        os.environ.pop("ASTOCK_OPS_SNAPSHOT", None)
    else:
        os.environ["ASTOCK_OPS_SNAPSHOT"] = _snap_saved
    paths = [getattr(r, "path", "") for r in web.app.routes]
    check("主服务路由集合未增加 /api/jobs",
          not any(p.startswith("/api/jobs") for p in paths), f"{len(paths)} 条路由")
    check("新增路由只在独立 router 上",
          len(__import__("scheduler.api", fromlist=["router"]).router.routes) == 8)

    passed = sum(1 for r in RESULTS if r["ok"])
    failed = len(RESULTS) - passed
    if a.json:
        print(json.dumps({"passed": passed, "failed": failed, "results": RESULTS},
                         ensure_ascii=False, indent=1))
    else:
        print(f"\n  通过 {passed} · 失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
