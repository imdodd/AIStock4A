"""EOD 一键编排：抓数 -> 快照 -> 提醒

用法：
    python -m jobs.eod                 # 完整管道（默认）
    python -m jobs.eod --skip-fetch    # 跳过抓取，只用本地数据重建快照（离线调试用）
    python -m jobs.eod --force-fetch   # 强制重抓全部标的（补历史时用）
    python -m jobs.eod --skip-research # 跳过个股研究档案生成（首次生成较慢）

退出码：0 成功 / 1 抓取失败但快照可用 / 2 管道中断
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = DATA_DIR
SNAP = os.path.join(DATA, "snapshot")
LOG_DIR = os.path.join(DATA, "logs")
ALERT_FILE = os.path.join(SNAP, "alerts.json")

sys.path.insert(0, HERE)
from config import universe  # noqa: E402


def log(msg, fh=None):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def run(cmd: list[str], fh, title: str) -> bool:
    log(f"[RUN] {title}", fh)
    t = time.time()
    p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    for line in (p.stdout or "").splitlines():
        if line.strip():
            print("   " + line, flush=True)
            fh.write("   " + line + "\n")
    if p.returncode != 0:
        for line in (p.stderr or "").splitlines()[-15:]:
            print("   ! " + line, flush=True)
            fh.write("   ! " + line + "\n")
        log(f"[FAIL] {title} 失败（exit {p.returncode}，{time.time() - t:.1f}s）", fh)
        return False
    log(f"[OK] {title} 完成（{time.time() - t:.1f}s）", fh)
    return True


def build_alerts() -> dict:
    """把自选 + 持仓在近 14 天内触发的信号整理成提醒清单"""
    sig_path = os.path.join(SNAP, "signals.json")
    if not os.path.exists(sig_path):
        return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "items": []}
    signals = json.load(open(sig_path, encoding="utf-8"))

    watch = {c for c, _ in universe.watch_items()}   # 以用户清单为准，前端增删后自动生效
    hold = set(universe.hold_codes())          # 以用户清单为准，前端增删后自动生效
    nms = universe.names()

    # 以全市场最新交易日为基准，回溯 14 个自然日
    latest = max((s["date"] for s in signals), default=None)
    cutoff = None
    if latest:
        from datetime import timedelta
        cutoff = (datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")

    items = []
    for s in signals:
        scope = "holding" if s["code"] in hold else ("watchlist" if s["code"] in watch else None)
        if not scope:
            continue
        if cutoff and s["date"] < cutoff:
            continue
        items.append({
            "code": s["code"], "name": s.get("name") or nms.get(s["code"], s["code"]),
            "type": s["type"], "date": s["date"], "sub": s.get("sub"),
            "price": s.get("price"), "pivot": s.get("pivot"), "stop": s.get("stop"),
            "quality": s.get("quality"), "grade": s.get("grade"),
            "scope": scope,
            "text": ("买点 " + (s.get("sub") or "")
                     + f" · 质量{s.get('quality', '-')}分({s.get('grade', '-')})"
                     + (f" · 止损 {s.get('stop')}" if s.get("stop") else ""))
                    if s["type"] == "buy"
                    else "放量跌破 60 日最低收盘（离场信号）",
        })
    items.sort(key=lambda x: (x["date"], -(x.get("quality") or 0)), reverse=True)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "base_date": latest,
        "window_days": 14,
        "count": len(items),
        "items": items,
    }


def main(argv=None) -> int:
    argv = argv or []
    skip_fetch = "--skip-fetch" in argv
    force_fetch = "--force-fetch" in argv
    skip_research = "--skip-research" in argv

    os.makedirs(LOG_DIR, exist_ok=True)
    fh = open(os.path.join(LOG_DIR, f"eod-{datetime.now():%Y-%m-%d}.log"), "a", encoding="utf-8")
    t0 = time.time()
    log("=" * 66, fh)
    log("EOD 管道开始", fh)

    # ⓪ 板块骨架自愈：仓库自带静态种子，网络可用时每周从线上申万列表刷新一次
    #    代码/名称；种子缺失或从未在线刷新过则立即重建，保证 ① 抓取能把 124 个
    #    申万二级行业纳入。本步失败不阻断主流程（② 已有缺文件容错）。
    meta_path = os.path.join(DATA, "raw", "sectors_meta.json")
    stamp_path = os.path.join(DATA, "cache", "sectors_meta_refreshed.json")
    stale = (not os.path.exists(meta_path)) or (not os.path.exists(stamp_path))
    if not stale:
        try:
            stale = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(stamp_path))).days >= 7
        except Exception:
            stale = True
    if stale:
        run([sys.executable, "-m", "tools.rebuild_sectors_meta"], fh, "⓪ 重建申万二级板块骨架")
    else:
        log("○ 申万板块骨架 7 天内已刷新，跳过", fh)

    fetch_ok = True
    if skip_fetch:
        log("○ 跳过抓取（--skip-fetch）", fh)
    else:
        cmd = [sys.executable, "-m", "jobs.fetch_daily"]
        if force_fetch:
            cmd.append("--force")
        fetch_ok = run(cmd, fh, "① 抓取行情与情绪")
        if not fetch_ok:
            log("  抓取失败，继续用本地数据构建快照", fh)

    snap_ok = run([sys.executable, "-m", "jobs.build_snapshot"], fh, "② 构建阶段/关键点快照")
    if not snap_ok:
        log("[FAIL] 快照构建失败，管道中止", fh)
        fh.close()
        return 2

    log("[RUN] ③ 生成提醒清单", fh)
    alerts = build_alerts()
    os.makedirs(SNAP, exist_ok=True)
    json.dump(alerts, open(ALERT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"[OK] 提醒 {alerts['count']} 条（基准日 {alerts.get('base_date')}） -> {ALERT_FILE}", fh)
    for a in alerts["items"][:6]:
        log(f"   · {a['name']}({a['code']}) {a['date']} {a['text']}", fh)

    # ④ 全市场「代码->名称」表：全局搜索靠它覆盖新股 / 科创板 / 北交所，
    #    每周刷新一次即可（约 8 秒）；失败不影响 EOD 主流程。
    names_path = os.path.join(SNAP, "stock_names.json")
    stale = True
    if os.path.exists(names_path):
        try:
            stale = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(names_path))).days >= 7
        except Exception:
            stale = True
    if stale:
        run([sys.executable, "-m", "tools.build_stock_names"], fh, "④ 刷新全市场股票名称表")
    else:
        log("○ 股票名称表 7 天内已更新，跳过", fh)

    # ⑤ 东财资金流行业（496 个）-> 本地申万二级（124 个）成分股映射：
    #    申万分类调整 / 新股上市后仍能正确定位，每 7 天刷新一次（约 1 分钟），
    #    脚本内部对单个行业取数失败会跳过并继续，不会中断 EOD。
    map_path = os.path.join(SNAP, "flow_sector_map.json")
    stale = True
    if os.path.exists(map_path):
        try:
            stale = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(map_path))).days >= 7
        except Exception:
            stale = True
    if stale:
        run([sys.executable, "-m", "tools.build_flow_sector_map"], fh, "⑤ 刷新行业口径映射表")
    else:
        log("○ 行业口径映射表 7 天内已更新，跳过", fh)

    # ⑥ 行业成分股阶段快照：成员源已切到东财申万 496 个 BK 的全量成分股
    #    （exact + 三级 parent 反查汇总到 124 个 pt，含科创板/北交所/次新股）。
    #    以前这一步只在手动执行 jobs/fetch_members 或前端首次下钻时生成，
    #    EOD 不刷新，导致板块下钻/行业异动/搜索行业标签长期滞后。
    #    必须在 ⑤ 之后：本步直接读 ⑤ 产出的 flow_sector_map.json / flow_members.json。
    #    全量约 6~10 分钟，单行业失败会保留旧快照，不中断主流程。
    if skip_fetch:
        log("○ 跳过成分股阶段快照（--skip-fetch）", fh)
    else:
        run([sys.executable, "-m", "jobs.fetch_members", "--all", "--force"], fh,
            "⑥ 刷新行业成分股阶段快照（东财申万全量）")

    # ⑦ 全市场关注池：把 ⑥ 产出的 5109 只成分股阶段/关键点信号汇总、筛选、
    #    排序成「今天值得关注的股票」，写 focus_pool.json 供 /api/signals 读取。
    #    必须在 ⑥ 之后（本步直接读 members/*.json）。约 7 秒，其中对入选的
    #    80 只补精确信号字段与图表缓存。--skip-fetch 是「只用本地数据重建快照」
    #    的离线调试模式，此时跳过 ⑦，避免用不回填行情的池子覆盖正式关注池。
    #    以前关注页读的是 build_snapshot.py 对 klines.json 里 62 只历史股票
    #    算出的 signals.json，池子自指闭环、永远不新增，已由本步取代。
    if skip_fetch:
        log("○ 跳过全市场关注池汇总（--skip-fetch，不覆盖正式池）", fh)
    else:
        run([sys.executable, "-m", "jobs.build_focus_pool"], fh, "⑦ 汇总全市场关注池")

    # ⑧ 持仓每日盈亏：读 ② 刚产出的收盘快照，把今天的「当日盈亏 / 累计盈亏」
    #    落一条到 data/portfolio/pnl_daily.json，供持仓页看连续每日盈亏。
    #    一天一条、同日重跑覆盖；不依赖 ⑥⑦，也不需要联网。
    run([sys.executable, "-m", "jobs.snapshot_pnl"], fh, "⑧ 记录持仓每日盈亏快照")

    # ⑨ 个股研究档案：持仓全量 + 关注池质量分 Top20 常驻生成六章 md，
    #    其余关注股在页面点开时按需生成；每天巡检已有档案的重大公告指纹，
    #    命中变化即重建（不占日配额）。首次生成较慢（每只要写 2500+ 字），
    #    之后只做增量检查。--skip-research 可跳过这一步单独排查。
    if skip_research:
        log("○ 跳过个股研究档案（--skip-research）", fh)
    else:
        run([sys.executable, "-m", "jobs.build_research"], fh,
            "⑨ 生成 / 巡检个股研究档案")

    log(f"EOD 管道结束，总耗时 {time.time() - t0:.1f}s", fh)
    fh.close()
    return 0 if fetch_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
