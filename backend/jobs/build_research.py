"""盘后生成个股研究档案：常驻池全量 + 重大公告巡检 + 其余按需。

口径（用户 2026-11-10 确认）：
    常驻  持仓全量 + 关注池买点按质量分 Top 20，每天自动检查、缺失/过期/证据变化才生成；
    按需  关注池其余 60 只不预生成，页面点开时走 /api/research/{code}/refresh 后台生成；
    重建  重大公告指纹变化、财报期/机构目标价变化、30 天 TTL，任一命中即重建。

用法（在 server/ 目录下）：
    python -m jobs.build_research                 # 常驻池 + 重大公告巡检
    python -m jobs.build_research --limit 3       # 只跑前 3 只（做样例/调试）
    python -m jobs.build_research --only sz002371 --force
    python -m jobs.build_research --full-check    # 对全部已有档案做完整证据 hash 检查
    python -m jobs.build_research --workers 1     # 串行（排查模型超时时用）

说明：首次生成会比较慢（每只要写六章 + 依赖因素），默认 4 并发；
之后每天只做增量检查，几乎不调用模型。EOD 第 ⑨ 步调用本任务。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from data_paths import DATA_PATH
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import research  # noqa: E402

SNAP = DATA_PATH /"snapshot"


def log(msg: str):
    print(msg, flush=True)


def holding_codes() -> list[str]:
    try:
        import store
        return list(store.codes())
    except Exception:
        try:
            rows = json.load(open(SNAP / "holdings.json", encoding="utf-8"))
            rows = rows if isinstance(rows, list) else rows.get("items", [])
            return [r["code"] for r in rows if r.get("code")]
        except Exception:
            return []


def focus_top(n: int = 20) -> list[str]:
    """关注池买点按质量分降序，取 Top N（卖点不进常驻池）。"""
    try:
        d = json.load(open(SNAP / "focus_pool.json", encoding="utf-8"))
    except Exception:
        return []
    rows = [x for x in (d.get("items") or []) if x.get("type") == "buy" and x.get("code")]
    rows.sort(key=lambda x: x.get("quality") or 0, reverse=True)
    return [x["code"] for x in rows[:n]]


def resident_codes(top: int = 20) -> list[str]:
    out, seen = [], set()
    for c in holding_codes() + focus_top(top):
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成个股基本面研究档案")
    ap.add_argument("--limit", type=int, default=0, help="只处理常驻池前 N 只")
    ap.add_argument("--only", type=str, default="", help="只处理指定代码")
    ap.add_argument("--top", type=int, default=20, help="关注池常驻只数")
    ap.add_argument("--force", action="store_true", help="常驻池全部强制重建")
    ap.add_argument("--full-check", action="store_true", help="对全部已有档案做完整证据检查")
    # 并发从 4 降到 2：EOD 15:30 后常驻池 + 巡检会集中打东财公告/F10 接口，
    # 并发过高是盘后突发限流的主因之一。降并发 + 巡检间隔把尖峰摊平。
    ap.add_argument("--workers", type=int, default=2, help="并发数（默认 2，抑制东财突发）")
    ap.add_argument("--inspect-gap", type=float, default=0.5,
                    help="巡检每只票之间的间隔秒数（默认 0.5，避免连续打 F10）")
    ap.add_argument("--no-inspect", action="store_true", help="跳过重大公告巡检")
    ap.add_argument("--no-batch", action="store_true",
                    help="巡检不走批量粗筛，逐只打 F10（回退旧行为，排查用）")
    a = ap.parse_args(argv)

    # 开源版默认不带 data/.secrets/llm.json。没有模型通道时生成模板档案即可，
    # 不该让 EOD 第 ⑨ 步整步 FAIL（V2 配了密钥时行为不变）。
    import ai
    no_llm = not bool(ai.config())
    if no_llm:
        log("○ 未配置模型通道，本轮按 fallback 生成基础档案（不调用模型）")

    t0 = time.time()
    if a.only:
        targets = [a.only]
    else:
        targets = resident_codes(a.top)
        if a.limit:
            targets = targets[:a.limit]
    log(f"[研究档案] 常驻池 {len(targets)} 只（持仓全量 + 关注池质量分 Top {a.top}）· 并发 {a.workers}")

    results = []

    def run_one(code, kind="resident", force=False):
        try:
            return code, research.generate(code, force=force, kind=kind, skip_llm=no_llm)
        except Exception as e:
            return code, {"code": code, "error": f"{type(e).__name__}: {e}"}

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        fs = [pool.submit(run_one, c, "resident", a.force) for c in targets]
        for fut in as_completed(fs):
            code, r = fut.result()
            done += 1
            results.append(r)
            if r.get("error"):
                log(f"  [{done}/{len(targets)}] ✗ {code} {r['error']}")
            elif r.get("skipped"):
                log(f"  [{done}/{len(targets)}] ○ {code} {r['skipped']}")
            else:
                log(f"  [{done}/{len(targets)}] ✓ {code} {r.get('mode')} · "
                    f"{r.get('valuation')} · {r.get('generated_at')}")

    # 重大公告巡检：所有已有档案（含已掉出关注池的），只打一次公告接口；
    # 命中变化或 --full-check 时走完整证据检查，重建不占日配额。
    inspected = rebuilt = 0
    if not a.no_inspect and not a.only:
        existing = [c for c in research.existing_codes() if c not in set(targets)]
        weekly = datetime.now().weekday() == 0
        full = a.full_check or weekly
        # 2026-11-27：先用批量公告接口粗筛，只对「可能有新公告」的票做 F10 精查。
        # 85 只巡检从 85 次 F10 降到 1~5 次批量 + 少数几次精查。
        # 周一/--full-check 要查财报与目标价变化，与公告无关，仍走全量。
        # 周一/--full-check 要查财报与目标价变化，与公告无关，仍走全量。
        if not existing:
            hot: dict[str, str] = {}
        elif a.no_batch or full:
            hot = {c: "?" for c in existing}
        else:
            hot = research.major_hot(existing)
        log(f"[研究档案] 巡检已有档案 {len(existing)} 只"
            f"（{'完整证据检查' if full else '重大公告指纹'}）"
            + ("· 逐只模式" if (a.no_batch or full) else f"· 批量粗筛命中 {len(hot)} 只需精查"))
        todo = []
        for i, c in enumerate(existing):
            if c not in hot:
                continue
            if i and a.inspect_gap > 0:
                time.sleep(a.inspect_gap)   # 摊平连续 F10 请求，避免瞬间突发触发限流
            inspected += 1
            hit = research.major_changed(c)
            if not hit and not full:
                # 精查过且指纹没变 → 记住「已看到哪天」，明天不再重复精查
                research.mark_notice_seen(c, hot.get(c) or "")
            if not hit and full:
                try:
                    need, _ = research.needs_refresh(c, research.evidence(c))
                    hit = need
                except Exception as e:
                    log(f"  ! {c} 完整检查失败：{e}")
            if hit:
                todo.append(c)
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
            fs = [pool.submit(run_one, c, "maintenance", True) for c in todo]
            for fut in as_completed(fs):
                code, r = fut.result()
                if not r.get("error"):
                    rebuilt += 1
                log(f"  ↻ {code} 重建：{r.get('mode') or r.get('error')}")

    ok = sum(1 for r in results if not r.get("error") and not r.get("skipped"))
    skip = sum(1 for r in results if r.get("skipped"))
    err = sum(1 for r in results if r.get("error"))
    log(f"[研究档案] 完成：新建/重建 {ok} · 跳过 {skip} · 失败 {err} · "
        f"精查 {inspected} · 公告触发重建 {rebuilt} · "
        f"档案总数 {len(research.existing_codes())} · 耗时 {time.time() - t0:.1f}s")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
