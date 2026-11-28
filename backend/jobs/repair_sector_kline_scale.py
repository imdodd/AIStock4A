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

RATIO_LIMIT = 3.0       # 段间中位数超过 3 倍视为量纲断层（仅看窗口，用于「无跳空」的缓变断层）
SEG_RATIO = 1.5         # 切段后判定「哪一段是另一种量纲」的阈值，低于此视为同口径
SEG_LOCAL = 30          # 计算段的量纲代表值时取的局部根数（贴近断层的那一端）
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


def _gap_days(a: dict, b: dict) -> int:
    """相邻两根之间的自然日间隔；解析失败返回一个大数（让跳空判据失效，宁漏不错）。"""
    try:
        d1 = datetime.strptime(str(a.get("date"))[:10], "%Y-%m-%d")
        d2 = datetime.strptime(str(b.get("date"))[:10], "%Y-%m-%d")
        return (d2 - d1).days
    except Exception:
        return 999


GAP_LIMIT = 0.25        # 单日开盘偏离前收超过该比例即视为量纲断层（指数无除权）
MIN_BASE_SEG = 3        # 末段至少这么多根才敢拿它当量纲基准


def _gap_hit(nodes: list[dict], i: int, limit: float = GAP_LIMIT) -> bool:
    """第 i 根相对前一根是否发生「不可能自然出现」的跳空。

    板块指数没有除权，单日开盘偏离前收 25% 以上只可能是两套量纲硬拼。
    中间断档多日（假期/漏抓）时累计涨幅合法地超 25%，不算——所以要求间隔 ≤5 天。
    """
    if i <= 0 or i >= len(nodes):
        return False
    prev_close = float(nodes[i - 1].get("close") or 0)
    cur_open = float(nodes[i].get("open") or 0)
    if prev_close <= 0 or cur_open <= 0:
        return False
    if _gap_days(nodes[i - 1], nodes[i]) > 5:
        return False
    gap = cur_open / prev_close
    return gap > 1 + limit or gap < 1 - limit


def find_jumps(nodes: list[dict], ratio: float = RATIO_LIMIT,
               window: int = WINDOW) -> list[int]:
    """返回量纲跳变点的下标（跳变发生在该下标与前一个下标之间）。

    两条判据取并集，因为单靠哪一种都会漏：

    A) 隔夜跳空：open/前收 偏离 >25%。板块指数没有除权的概念，单日跳空
       25% 以上在真实行情里不可能出现，只可能是两套量纲硬拼在一起。
       这条判据精确，且不受窗口稀释影响。

    B) 前后窗口中位数比 >ratio。用于断层不是「一刀切」而是几天内过渡的
       情况。注意它对窗口敏感：2026-11-27 的「元件」真实跳空 3.23 倍，
       用 10 日窗口中位数只有 2.93 倍，卡在 3.0 阈值下漏检——所以不能
       只靠这一条，也不宜把阈值往下调（连续暴涨的板块会被误判）。
    """
    closes = [float(b.get("close") or 0) for b in nodes]
    out = []
    for i in range(1, len(nodes)):
        # 只在两日相邻时用跳空判据：中间断档多日（假期/接口漏抓）时累计涨幅
        # 合法地超过 25%，用 open/前收 会误判成断层。
        if _gap_hit(nodes, i):
            out.append(i)
            continue
        prev = _median([x for x in closes[max(0, i - window):i] if x > 0])
        nxt = _median([x for x in closes[i:i + window] if x > 0])
        if prev > 0 and nxt > 0 and (prev / nxt > ratio or nxt / prev > ratio):
            out.append(i)
    return out


def _seg_rep(nodes: list[dict], seg: list[int]) -> float:
    """段的量纲代表值：取段末尾贴近断层的局部中位数，而不是整段中位数。

    整段中位数会被长期趋势稀释：pt01801115 的段跨 2024-10 ~ 2026-11，
    整段中位约 3500，而断层前实际在 4900、断层后 3000，整段比只有 1.17，
    远低于阈值 —— 明明断得很干净却漏检。取尾部 30 根就得到 1.6 倍。
    """
    if not seg:
        return 0.0
    tail = seg[-SEG_LOCAL:]
    return _median([float(nodes[i].get("close") or 0) for i in tail
                    if (nodes[i].get("close") or 0) > 0])


def outlier_indexes(nodes: list[dict], ratio: float = RATIO_LIMIT,
                    window: int = WINDOW) -> list[int]:
    """按跳变点切段，归并同量纲段，返回少数派（污染）段的下标。"""
    jumps = find_jumps(nodes, ratio, window)
    if not jumps:
        return []
    bounds = [0] + jumps + [len(nodes)]
    segs = [list(range(bounds[k], bounds[k + 1])) for k in range(len(bounds) - 1)]
    seg_med = [_seg_rep(nodes, seg) for seg in segs]
    # 以「最后一段」的量纲为基准，而不是最长段。
    #
    # 每日增量都由权威源（东财 clist/kline）写到末端，历史长段才是可能被旧快照
    # 污染的少数派。原来看最长段，在「长历史 + 短新增」时会把结论判反：2026-11-27
    # 的元件是 314 根旧口径 + 10 根东财真值，取最长段就变成「保护错的、剔除对的」。
    # 末段太短时（刚发生过一次断层、新口径还没长出来）退回最长段，避免误判。
    if len(segs[-1]) >= MIN_BASE_SEG:
        base_i = len(segs) - 1
    else:
        base_i = max(range(len(segs)), key=lambda k: len(segs[k]))
    base = seg_med[base_i]
    if base <= 0:
        return []
    # 硬断点（隔夜跳空 >25%）是断层本身的直接证据，不需要再用量级比确认哪一段
    # 是异口径：断点两侧必然不同源。此时以末段为基准，把最后一个硬断点之前的
    # 历史全部标为污染。
    #
    # 为什么必须单独处理：养殖业等 14 个板块 11-11→11-12 跳空 30%，前段 308 根
    # 是旧口径（东财真值 3772，本地存 2894），但段代表值 3535/2894 只有 1.22 倍，
    # 低于 SEG_RATIO=1.5 —— 于是「跳变点已确认、段归并又放过」，图上照样断成两截。
    # 量级比只适合判断缓变断层，扛不住口径差 20%~50% 这种「半对半错」的情况。
    if base_i == len(segs) - 1:
        hard = [j for j in jumps if _gap_hit(nodes, j)]
        if hard:
            cut = max(hard)
            rest = [j for j in jumps if j > cut]
            if not rest:
                return list(range(cut))
    bad = []
    for k, seg in enumerate(segs):
        m = seg_med[k]
        # 归并用 SEG_RATIO 而不是 RATIO_LIMIT：跳空判据已在 find_jumps 里确认过
        # 断层存在，这里只负责判断「哪一段是另一种口径」，不需要再要求 3 倍。
        # 元件段比 2.32、pt01801096 段比 2.75 —— 用 3.0 会把它们全放过（漏检）。
        if m > 0 and (m / base > SEG_RATIO or base / m > SEG_RATIO):
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
    ap.add_argument("--rebuild", action="store_true",
                    help="跳过断层检测，对 --only 指定的板块直接用东财重抓覆盖"
                         "（用于「剔除后只剩个位数根」的板块补回历史）")
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
        if args.rebuild:
            # 强制重建：整个序列都视为待覆盖，用东财权威源重抓
            polluted.append((c, list(range(len(nodes)))))
            continue
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
            if args.rebuild:
                # 重建模式下抓不到就什么都别做：此时「剔除」等于把整段历史删光
                log(f"   [{i}/{len(polluted)}] {pt}←{bk} 东财不可用，跳过（不剔除历史）", fh)
                failed += 1
                continue
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
