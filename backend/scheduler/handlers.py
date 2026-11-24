"""任务处理器注册表与「工作集」查询。

核心约定（对应方案里的动态任务三分类）：
  ① 工作集变化（新增持仓） → 不新增 job，job 每次触发扫 tracking_plans；
  ② 频率档变化             → 按 cad 分桶，固定 5 个 job；
  ③ 真·自定义 cron         → job_defs + 运行时 add_job。

handler 签名统一为 ``(params: dict, ctx: dict) -> dict``，返回值必须可 JSON 序列化，
会被写进 job_runs.detail。真实取数一律走子进程调用现有 jobs 模块（保留 CLI 手工
入口、不污染主进程的 sys.argv），且默认 dry_run 只记录不执行。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from typing import Callable

import data_paths
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
DATA_DIR = data_paths.DATA_DIR

CAD_BUCKETS = ("daily", "twice_weekly", "weekly", "quarterly", "event")


def work_items(cad: str | None = None, data_dir: str | None = None,
               repo=None) -> list[dict]:
    """扫跟踪方案的工作集。优先文件（主链路 STORAGE_READ=files 未切换），
    文件为空时退回 tracking_plans 表，方便后续切读路径后无缝衔接。
    """
    root = data_dir or DATA_DIR
    items: list[dict] = []
    for p in sorted(glob.glob(os.path.join(root, "tracking", "*", "meta.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        freq = meta.get("frequency") or {}
        kind = freq.get("kind") if isinstance(freq, dict) else None
        if cad and kind != cad:
            continue
        items.append({"code": meta.get("code") or os.path.basename(os.path.dirname(p)),
                      "name": meta.get("name"), "cad": kind,
                      "status": meta.get("status") or "active"})
    if items or repo is None:
        return items
    try:
        rows = repo.list_rows("tracking_plans")
    except Exception:
        return items
    for row in rows:
        freq = row.get("frequency") or {}
        kind = freq.get("kind") if isinstance(freq, dict) else None
        if cad and kind != cad:
            continue
        if (row.get("status") or "active") != "active":
            continue
        items.append({"code": row.get("code"), "name": row.get("name"),
                      "cad": kind, "status": "active"})
    return items


def _subprocess(module: str, args: list[str], ctx: dict) -> dict:
    """真实执行：另起进程跑现有 CLI，主服务不受影响。"""
    cmd = [sys.executable, "-m", module, *args]
    try:
        # nssm 服务的 stdout 可能是 GBK；强制子进程用 UTF-8，避免日志符号触发
        # UnicodeEncodeError，也保证 job_runs.detail 里的中文证据不乱码。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(cmd, cwd=BACKEND, capture_output=True, text=True,
                           timeout=int(ctx.get("timeout", 900)), encoding="utf-8",
                           errors="replace", env=env)
        return {"command": " ".join(cmd), "returncode": r.returncode,
                "stdout_tail": (r.stdout or "")[-800:],
                "stderr_tail": (r.stderr or "")[-400:]}
    except Exception as e:                    # 超时 / 解释器异常都不许打断调度线程
        return {"command": " ".join(cmd), "error": f"{type(e).__name__}: {e}"}


def _guard(ctx: dict, label: str, extra: dict | None = None) -> dict | None:
    if not ctx.get("dry_run", True):
        return None
    return {"dry_run": True, "would_run": label, **(extra or {})}


def h_noop(params: dict, ctx: dict) -> dict:
    return _guard(ctx, "noop") or {"ok": True, "echo": params}


def _pregen_enabled(params: dict) -> bool:
    """PREGEN_ON_EOD：EOD 成功后是否接着产出 pregen。params 可单独覆盖。"""
    if "pregen" in params:
        return bool(params["pregen"])
    v = (os.environ.get("PREGEN_ON_EOD") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def pregen_chain(params: dict, ctx: dict) -> dict:
    """接线：把「digest → producer → 契约校验 → pregen」串成盘后一步。

    只读已产出的 ``data/cases/{交易日}/digest.json``；缺失时才调用现成的
    ``tools.pregen export``（绿区代码一行不改）。整条链路是**尽力而为**：
    任何一步失败只写进 detail，绝不把已经成功的 EOD 判成 error，也不抛异常
    打断调度线程。影子期 dry_run 默认开，本函数根本不会被调用。
    """
    # params.date 可显式指定补跑的历史交易日（自定义 job_defs 用）；
    # 未指定时沿用调度上下文里的 trade_date。
    day = str(params.get("date") or ctx.get("trade_date") or "")[:10]
    if not day:
        return {"ok": False, "reason": "上下文缺少 trade_date"}
    digest = os.path.join(DATA_DIR, "cases", day, "digest.json")
    steps: list[dict] = []
    if not os.path.isfile(digest):
        r = _subprocess("tools.pregen", ["export"], ctx)
        steps.append({"step": "export_digest", **r})
        if r.get("returncode") != 0:
            return {"ok": False, "reason": f"{day} 无 digest.json 且导出失败",
                    "steps": steps}
        if not os.path.isfile(digest):
            return {"ok": False, "reason": "export 只产出当日 digest，与本次交易日不一致",
                    "steps": steps}
    producer = str(params.get("producer") or os.environ.get("PRODUCER") or "llm_direct")
    limit = params.get("limit")
    args = ["--apply", "--date", day, "--producer", producer, "--json"]
    if limit:
        args += ["--limit", str(limit)]
    r = _subprocess("jobs.build_cases", args, ctx)
    steps.append({"step": "build_cases_apply", **r})
    return {"ok": r.get("returncode") == 0, "date": day, "producer": producer,
            "digest": digest, "steps": steps,
            "stdout_tail": (r.get("stdout_tail") or "")[-400:]}


def h_eod(params: dict, ctx: dict) -> dict:
    g = _guard(ctx, "jobs.eod", {"items": len(work_items(repo=ctx.get("repo")))})
    if g:
        return g                              # dry-run 分录保持原样，验收断言不受影响
    args = list(params.get("args") or [])
    if params.get("skip_fetch") and "--skip-fetch" not in args:
        # 显式要求跳过抓取时（离线重建快照）才加；默认必须是完整管道。
        # 以前无参数时强制 --skip-fetch，导致调度器每天「准时」跑完却一条新
        # 行情都没抓，页面永远停在昨天——这是最隐蔽的假成功。
        args.append("--skip-fetch")
    # 全管道（抓数 + 快照 + 成分股 + 关注池 + 研究档案）正常 8~20 分钟，
    # 首次或成分股缓存失效时更久；沿用 900 秒会把正在干活的子进程杀掉。
    result = _subprocess("jobs.eod", args, {**ctx, "timeout": int(
        params.get("timeout") or ctx.get("timeout") or 2700)})
    if result.get("returncode") == 0 and _pregen_enabled(params):
        try:
            result["pregen"] = pregen_chain(params, ctx)
        except Exception as e:                # noqa: BLE001 产出失败不许拖垮 EOD
            result["pregen"] = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    return result


def h_sector_map(params: dict, ctx: dict) -> dict:
    """行业口径映射表刷新：从 EOD 拆出的独立低频任务。

    原在 EOD ⑤ 每周触发，一次抓 496 个东财行业成分股（1000+ 次 clist 请求），
    和盘后抓数挤在同一出口 IP 必撞限流。改到周日凌晨单独跑，脚本内部带成员
    快照增量复用（TTL 内不重抓），实际请求降到几十次。
    """
    g = _guard(ctx, "tools.build_flow_sector_map")
    if g:
        return g
    args = ["--force"] if params.get("force") else []
    return _subprocess("tools.build_flow_sector_map", args,
                       {**ctx, "timeout": int(params.get("timeout") or 1800)})


def h_pregen(params: dict, ctx: dict) -> dict:
    """独立产出任务：供自定义 job_defs 用（不占 7 个固定 job）。"""
    g = _guard(ctx, "pregen_chain", {"date": str(ctx.get("trade_date") or "")[:10]})
    if g:
        return g
    return pregen_chain(params, ctx)


def h_news_round(params: dict, ctx: dict) -> dict:
    g = _guard(ctx, "jobs.fetch_news")
    if g:
        return g
    # 检索档跟着配置页走：默认 none（固定源、零 Key），配了 Key 后自动切 search_api，
    # llm_websearch = LLM 服务端联网搜索直出 patch（对齐生产 WebSearch 方式）。
    provider = str(params.get("provider") or os.environ.get("SEARCH_PROVIDER") or "none")
    if provider.lower() == "llm_websearch":
        # 独立档：检索+判断合一在 news_websearch 内完成，传 --apply 即可，
        # 不走 fetch_news 的 --provider/--judge 参数。其余档位保持原路径字节不变。
        return _subprocess("jobs.news_websearch", ["--apply"], ctx)
    args = list(params.get("args") or ["--provider", provider, "--apply"])
    # 判断层对齐生产：只对确有增量、且标注到板块/个股的新闻落盘。
    # NEWS_JUDGE=0 可显式关掉（回退事实层直抓）；默认开，避免机械抓取跑偏。
    judge_env = (os.environ.get("NEWS_JUDGE") or "").strip().lower()
    judge_on = params.get("judge")
    if judge_on is None:
        judge_on = judge_env not in ("0", "false", "no", "off")
    if judge_on and "--judge" not in args:
        args = args + ["--judge"]
    return _subprocess("jobs.fetch_news", args, ctx)


def h_tracking_review(params: dict, ctx: dict) -> dict:
    """跟踪期产出：真的去跑 jobs.tracking_review，不再只回报工作集。

    工作集仍然是「每次触发扫 tracking_plans」——新增持仓不需要新 job，
    这也是固定分桶的立身之本。真实取数与模型调用在子进程里做，
    主服务不会被一次慢检索拖住。
    """
    cad = params.get("cad") or "daily"
    only = str(params.get("code") or "").strip().lower()
    # 按方案频率建出来的专属 job 会带 code，只跑这一只；
    # 分桶 job 没有 code，仍按 cad 扫工作集。
    items = work_items(None if only else cad, repo=ctx.get("repo"))
    if only:
        items = [i for i in items if i["code"] == only]
    g = _guard(ctx, "tracking_review", {"cad": cad, "code": only or None,
                                        "items": len(items),
                                        "codes": [i["code"] for i in items][:20]})
    if g:
        return g
    if not items:
        return {"ok": True, "cad": cad, "items": 0,
                "note": f"cad={cad} 下没有启用的跟踪方案"}
    args = ["--code", only] if only else ["--cad", cad]
    if params.get("limit"):
        args += ["--limit", str(int(params["limit"]))]
    if params.get("force"):
        args.append("--force")
    r = _subprocess("jobs.tracking_review", args, ctx)
    r.update({"cad": cad, "items": len(items),
              "codes": [i["code"] for i in items][:20]})
    # 子进程 returncode 非 0 但仍有 JSON 汇总时，把汇总摊到 detail 顶层方便排查
    tail = (r.get("stdout_tail") or "").strip().splitlines()
    for line in reversed(tail):
        line = line.strip()
        if line.startswith("{"):
            try:
                r["summary"] = json.loads(line)
            except ValueError:
                pass
            break
    return r


def h_weekly_review(params: dict, ctx: dict) -> dict:
    return h_tracking_review({"cad": "weekly", **params}, ctx)


def h_quarterly_review(params: dict, ctx: dict) -> dict:
    return h_tracking_review({"cad": "quarterly", **params}, ctx)


def h_event_watch(params: dict, ctx: dict) -> dict:
    """event 类不是 cron 能覆盖的，靠 EOD 公告巡检，这里只做兜底记录。"""
    return _guard(ctx, "event_watch", {"note": "事件驱动，由 EOD 公告巡检触发"}) or {"ok": True}


JOB_HANDLERS: dict[str, Callable[[dict, dict], dict]] = {
    "noop": h_noop,
    "eod": h_eod,
    "sector_map": h_sector_map,
    "pregen": h_pregen,
    "news_round": h_news_round,
    "tracking_review": h_tracking_review,
    "weekly_review": h_weekly_review,
    "quarterly_review": h_quarterly_review,
    "event_watch": h_event_watch,
}

# 固定 job：数量恒定，新增标的只往表里插一行，不 add_job
FIXED_JOBS: tuple[dict, ...] = (
    # 周字段一律用**标准 crontab 口径**：0/7=周日、1=周一 … 5=周五。
    # 历史上这里按 APScheduler 的 0=Mon 写成 0-4，而 builtin 后端按标准 crontab
    # 解释，结果 builtin 下周五漏跑、周日空跑（已被交易日过滤吞掉，表现为
    # 每周少一天数据且不报错）。现已统一：本文件/接口/文档只用标准口径，
    # APScheduler 后端在 backends.crontab_to_apscheduler 里做一次映射。
    {"id": "eod_close", "job_type": "eod", "cron": "30 15 * * 1-5",
     "params": {}, "note": "盘后 EOD 管道（15:30 收盘后留 30 分钟给数据源落定；交易日过滤由 core 执行）"},
    # 行业口径映射表：从 EOD 拆出，周日凌晨 3 点单独跑。原在盘后和抓数一起跑，
    # 496 个行业的成分股翻页是全天最大突发（1000+ 次 clist），是东财限流主因之一。
    # all_days=True 绕开交易日过滤——周日本就不是交易日。
    {"id": "sector_map_refresh", "job_type": "sector_map", "cron": "0 3 * * 0",
     "params": {}, "all_days": True,
     "note": "行业口径映射表（东财BK→本地pt），周日凌晨错峰刷新，增量复用成员快照"},
    # 消息采集不走交易日日历：周末和节假日恰恰是政策、海外市场、公司公告的
    # 高发时段，周一开盘前必须已经在库里。all_days 会绕过 core 的交易日守卫。
    {"id": "news_half_hour", "job_type": "news_round", "cron": "0,30 9-23 * * *",
     "params": {}, "all_days": True,
     "note": "资讯增量，每天（含周末节假日）9:00-23:30 每半小时，覆盖盘后与海外消息"},
    # 跟踪类不再用固定分桶：每只 active 方案在 tracking_jobs.sync_all() 里
    # 按自己的 frequency.rrule 注册一条 track-<code> 任务。分桶的触发时刻和
    # 方案 label 对不上（方案写周一 08:20，桶却是周五 16:25），留着会重复出期。
    # 15:50 在 EOD（15:30 起，约 8 分钟）之后，确保事件巡检读到当天新快照。
    {"id": "track_event", "job_type": "event_watch", "cron": "50 15 * * 1-5",
     "params": {}, "note": "事件维度兜底巡检（在 EOD 完成后）"},
)

__all__ = ["JOB_HANDLERS", "FIXED_JOBS", "CAD_BUCKETS", "work_items"]
