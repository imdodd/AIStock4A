"""修复板块 K 线量纲污染（腾讯 pt 源快照覆盖东财 BK 源）。

事故背景（2026-11-24）：
  universe.all_codes() 曾把 124 个 pt 板块混入 fetch_daily 的抓取池，
  fetch_daily 用腾讯源抓 pt 指数，其数值量纲与东财 BK 板块指数不同
  （白酒Ⅱ：腾讯 5 万级 vs 东财 4 千级）。merge 按日期覆盖后，同一条 K
  线序列里出现量纲断层：图上 Y 轴被旧快照撑到 4 万，近期 K 线被压成平线。

为什么不能用绝对阈值（10000）判定污染：
  申万二级板块指数本来就有一万以上的（如 pt01801102 长期在 1.2~1.7 万），
  用绝对阈值会把正常数据误判成污染，反而用错源覆盖掉正确历史。
  正确判据是「同一序列内部出现 3 倍以上的量纲跳变」。

修复策略：
  1. 对每个 pt 板块计算相邻滑动窗口中位数之比，找出量纲跳变点。
  2. 把序列按跳变点切成段，段中位数按 3 倍以内归并；少数派段即污染段。
  3. 若该板块有东财 BK 映射且东财可达，抓近 500 根按日期覆盖污染段；
     抓不到就直接剔除污染段（宁可少几根，也不能留下断层）。
  4. 原子写；--dry-run / --check 只读不写。

用法（backend/ 目录）：
  python -m jobs.repair_sector_kline_scale --dry-run
  python -m jobs.repair_sector_kline_scale --check      # 有断层时退出码 1
  python -m jobs.repair_sector_kline_scale              # 执行修复
  python -m jobs.repair_sector_kline_scale --only pt01801125
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from data_paths import DATA_DIR, DEFAULT_DATA_DIR

RAW = os.path.join(DATA_DIR, "raw")
KLINE_FILE = os.path.join(RAW, "klines.json")
MAP_FILE = os.path.join(RAW, "sector_bk_map.json")
MAP_FALLBACK = os.path.join(DEFAULT_DATA_DIR, "raw", "sector_bk_map.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")

RATIO_LIMIT = 3.0       # 段间中位数超过 3 倍视为量纲断层
WINDOW = 10             # 判断跳变点用的前后窗口长度
FETCH_LIMIT = 500       # 东财重抓根数
STREAK_LIMIT = 5        # 连续失败 N 个即暂停抓数，改用剔除兜底


def log(msg: str, fh=None) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def _read(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _nodes_of(v) -> list[dict]:
    return (v.get("nodes") if isinstance(v, dict) else v) or []


def _median(vals: list[float]) -> float:
    return statistics.median(vals) if vals else 0.0


def find_jumps(nodes: list[dict], ratio: float = RATIO_LIMIT,
               window: int = WINDOW) -> list[int]:
    """返回量纲跳变点的下标（跳变发生在该下标与前一个下标之间）。"""
    closes = [float(b.get("close") or 0) for b in nodes]
    out = []
    for i in range(1, len(nodes)):
        prev = _median([x for x in closes[max(0, i - window):i] if x > 0])
        nxt = _median([x for x in closes[i:i + window] if x > 0])
        if prev > 0 and nxt > 0 and (prev / nxt > ratio or nxt / prev > ratio):
            out.append(i)
    return out


def outlier_indexes(nodes: list[dict], ratio: float = RATIO_LIMIT,
                    window: int = WINDOW) -> list[int]:
    """按跳变点切段，归并同量纲段，返回少数派（污染）段的下标。"""
    jumps = find_jumps(nodes, ratio, window)
    if not jumps:
        return []
    bounds = [0] + jumps + [len(nodes)]
    segs = [list(range(bounds[k], bounds[k + 1])) for k in range(len(bounds) - 1)]
    seg_med = [_median([float(nodes[i].get("close") or 0)
                        for i in seg if (nodes[i].get("close") or 0) > 0]) for seg in segs]
    # 以最长段的量纲为基准；与其相差 3 倍以上的段都是污染
    base_i = max(range(len(segs)), key=lambda k: len(segs[k]))
    base = seg_med[base_i]
    if base <= 0:
        return []
    bad = []
    for k, seg in enumerate(segs):
        m = seg_med[k]
        if m > 0 and (m / base > ratio or base / m > ratio):
            bad.extend(seg)
    return bad


def _atomic_write(raw: dict) -> None:
    tmp = KLINE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, KLINE_FILE)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="修复板块K线量纲断层（腾讯pt快照覆盖东财）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    ap.add_argument("--check", action="store_true", help="只校验，发现断层退出码 1")
    ap.add_argument("--ratio", type=float, default=RATIO_LIMIT)
    ap.add_argument("--window", type=int, default=WINDOW)
    ap.add_argument("--limit", type=int, default=FETCH_LIMIT)
    ap.add_argument("--only", default="", help="只处理逗号分隔的板块代码")
    args = ap.parse_args(argv)

    os.makedirs(LOG_DIR, exist_ok=True)
    fh = open(os.path.join(LOG_DIR, f"repair-kline-scale-{datetime.now():%Y-%m-%d}.log"),
              "a", encoding="utf-8")
    raw = _read(KLINE_FILE) or {}
    mp = (_read(MAP_FILE) or _read(MAP_FALLBACK) or {}).get("map") or {}
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    pts = [c for c in raw if str(c).lower().startswith("pt")
           and (not only or c in only)]

    polluted = []
    for c in pts:
        nodes = _nodes_of(raw[c])
        bad = outlier_indexes(nodes, args.ratio, args.window)
        if bad:
            polluted.append((c, bad))
    log(f"扫描 {len(pts)} 个板块 · 量纲断层 {len(polluted)} 个 · 段间阈值 {args.ratio} 倍", fh)
    for c, bad in polluted:
        nodes = _nodes_of(raw[c])
        log(f"   {c}: 污染 {len(bad)}/{len(nodes)} 根，日期 "
            f"{nodes[bad[0]]['date']} ~ {nodes[bad[-1]]['date']}", fh)

    if args.dry_run or args.check:
        log(f"[{dry_label(args)}] 未写盘", fh)
        fh.close()
        return 1 if args.check and polluted else 0

    from datasource import sectorflow

    fixed, failed, dropped = 0, 0, 0
    streak = 0
    for i, (pt, bad) in enumerate(polluted, 1):
        nodes = _nodes_of(raw[pt])
        bad_dates = {nodes[j]["date"] for j in bad}
        bk = mp.get(pt)
        fresh = []
        if bk and streak < STREAK_LIMIT:
            fresh = sectorflow.fetch_bk_kline(bk, limit=args.limit)
        if fresh:
            streak = 0
            byd = {b["date"]: b for b in nodes}
            fresh_dates = {b["date"] for b in fresh}
            for b in fresh:
                byd[b["date"]] = b          # 东财覆盖同日（权威源）
            cleaned = [b for d, b in byd.items()
                       if d not in bad_dates or d in fresh_dates]
            how = f"东财 {bk} 重抓 {len(fresh)} 根回填"
        else:
            if bk:
                streak += 1
                why = (sectorflow._MEM.get("last_kline_error") or "").strip()
                log(f"   [{i}/{len(polluted)}] {pt}←{bk} 东财不可用（{why[:80]}），改用剔除", fh)
                if streak >= STREAK_LIMIT:
                    log(f"   连续 {streak} 个抓数失败，后续全部改用剔除", fh)
            cleaned = [b for b in nodes if b["date"] not in bad_dates]
            how = "剔除污染段"
        cleaned.sort(key=lambda x: x["date"])
        if not cleaned:
            log(f"   [{i}/{len(polluted)}] {pt} 剔除后无数据，跳过写回", fh)
            failed += 1
            continue
        raw[pt] = {"nodes": cleaned} if isinstance(raw[pt], dict) else cleaned
        dropped += len(nodes) - len(cleaned)
        fixed += 1
        log(f"   [{i}/{len(polluted)}] {pt} {how} · {len(nodes)} → {len(cleaned)} 根", fh)
        _atomic_write(raw)                  # 每修一个立即落盘，中断不丢已完成部分
        time.sleep(0.15)

    log(f"完成：修复 {fixed} · 跳过 {failed} · 剔除残根 {dropped}", fh)
    fh.close()
    return 0 if fixed == len(polluted) else 1


def dry_label(args) -> str:
    return "CHECK" if args.check else "DRY-RUN"


if __name__ == "__main__":
    raise SystemExit(main())
