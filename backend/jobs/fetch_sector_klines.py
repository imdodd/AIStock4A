"""补齐申万二级板块（pt*）的日 K 线：静态种子优先，东财 BK 源兜底。

背景：
  全新安装时，腾讯 pt 行业指数源只回 1 根 K 线，build_snapshot 算不出
  MA250/阶段序列，板块页整块无图、指标停在 sectors_meta 静态种子的旧值。
  补齐有两条来源：
    1) 静态种子 sector_klines_seed.json（随代码入仓，124 板块 312/512 根，末 11-20）
       —— 服务器只读本地文件、零网络请求，从根本上绕开东财对云主机 IP 的频率限流。
    2) 东财 push2his secid=90.BKxxxx —— 种子缺某板块或想刷新到更新日期时才抓。
  默认种子优先：种子命中即合并，绝不联网；只有种子缺失/覆盖不足的板块才走东财。

设计（本地纯增量、不碰现有数据）：
  · 本地已有且末日期 >= 市场最新交易日的板块 → 直接跳过，一根不动。
    因此本机/已在跑的实例执行本脚本几乎全部跳过，零副作用。
  · 待补板块先查静态种子，命中且根数足够 → 按日期并集合并（零网络）。
  · 种子未覆盖的板块才抓东财 BK 日 K；单板块有效根数 < MIN_BARS 不覆盖。
  · 连续失败 STREAK_LIMIT 次才暂停，成功即清零，支持断点续跑。
  · 全程原子写 klines.json；任一步异常都不会损坏现有文件。

用法（在 backend/ 目录）：
  python -m jobs.fetch_sector_klines                 # 增量补齐（种子优先，只补缺/旧/不足）
  python -m jobs.fetch_sector_klines --no-seed       # 跳过种子，强制走东财
  python -m jobs.fetch_sector_klines --dry-run       # 只报告待补清单，不抓不写
  python -m jobs.fetch_sector_klines --force         # 忽略新鲜度，重抓全部板块
  python -m jobs.fetch_sector_klines --min 60        # 自定义历史充足阈值
  python -m jobs.fetch_sector_klines --only pt01801081,...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from data_paths import DATA_DIR, DEFAULT_DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

RAW = os.path.join(DATA_DIR, "raw")
KLINE_FILE = os.path.join(RAW, "klines.json")
META_FILE = os.path.join(RAW, "sectors_meta.json")
# pt→BK 映射是随代码入仓的静态种子（全实例共用），可能存在默认目录而非实例目录。
# 优先实例目录（运维若手动刷新过），否则回落默认入仓目录。
MAP_FILE = os.path.join(RAW, "sector_bk_map.json")
MAP_FILE_FALLBACK = os.path.join(DEFAULT_DATA_DIR, "raw", "sector_bk_map.json")
# 静态 K 线种子（随代码入仓）：与映射种子同样优先实例目录、回落默认入仓目录
SEED_FILE = os.path.join(RAW, "sector_klines_seed.json")
SEED_FILE_FALLBACK = os.path.join(DEFAULT_DATA_DIR, "raw", "sector_klines_seed.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")

MIN_BARS = 60           # 少于此根数视为历史不足，需要补齐
FULL_LIMIT = 500        # 单次抓取根数（覆盖 276 根计算窗口）
STALE_DAYS = 7          # 末日期落后市场最新交易日超过此天数才判为旧（同周内不折腾）


def log(msg, fh=None):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_store() -> dict[str, list[dict]]:
    """读 klines.json → {code: [bar,...]}（已是标准节点格式）；不存在返回空。"""
    from datasource.base import normalize_nodes
    raw = _read(KLINE_FILE) or {}
    store = {}
    for code, v in raw.items():
        nodes = v.get("nodes") if isinstance(v, dict) else v
        bars = normalize_nodes(nodes or [])
        if bars:
            store[code] = bars
    return store


def save_store(store: dict) -> None:
    os.makedirs(RAW, exist_ok=True)
    payload = {c: {"nodes": bars} for c, bars in store.items()}
    tmp = KLINE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, KLINE_FILE)


def merge_bars(old: list[dict] | None, incoming: list[dict]) -> list[dict]:
    """按日期并集合并：新数据覆盖同日旧数据，保留本地独有的历史。"""
    byd = {b["date"]: b for b in (old or [])}
    for b in incoming:
        byd[b["date"]] = b
    return [byd[d] for d in sorted(byd)]


def market_date(store: dict) -> str | None:
    """用非板块标的（指数/个股）的末日期作为市场基准；无则退回全部标的。"""
    idx = [v[-1]["date"] for c, v in store.items()
           if not c.startswith("pt") and v]
    if idx:
        return max(idx)
    return max((v[-1]["date"] for v in store.values() if v), default=None)


def _days_between(a: str, b: str) -> int:
    try:
        from datetime import date
        ya, ma, da = (int(x) for x in a.split("-")[:3])
        yb, mb, db = (int(x) for x in b.split("-")[:3])
        return (date(yb, mb, db) - date(ya, ma, da)).days
    except Exception:
        return 10 ** 6


def main(argv=None) -> int:
    argv = list(argv or [])
    ap = argparse.ArgumentParser(description="补齐申万二级板块日K（东财BK源）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-seed", action="store_true", help="跳过静态种子，强制走东财")
    ap.add_argument("--min", type=int, default=MIN_BARS)
    ap.add_argument("--limit", type=int, default=FULL_LIMIT)
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)

    os.makedirs(LOG_DIR, exist_ok=True)
    fh = open(os.path.join(LOG_DIR, f"sector-kline-{datetime.now():%Y-%m-%d}.log"), "a", encoding="utf-8")
    t0 = time.time()
    log("=" * 60, fh)
    log("板块日K补齐开始", fh)

    meta = _read(META_FILE) or {}
    mapdoc = _read(MAP_FILE) or _read(MAP_FILE_FALLBACK) or {}
    bk_map = mapdoc.get("map") or {}
    if not meta or not bk_map:
        log("[FAIL] 缺少 sectors_meta.json 或 sector_bk_map.json 映射种子", fh)
        fh.close()
        return 1

    only = {c.strip().lower() for c in args.only.split(",") if c.strip()} or None
    store = load_store()
    mdate = market_date(store)

    # 静态种子：优先实例目录，回落默认入仓目录；读失败/禁用则为空
    seed: dict[str, list[dict]] = {}
    seed_asof = ""
    if not args.no_seed:
        sdoc = _read(SEED_FILE) or _read(SEED_FILE_FALLBACK) or {}
        seed_raw = sdoc.get("map") or {}
        seed_asof = sdoc.get("asof") or ""
        for c, v in seed_raw.items():
            nodes = v.get("nodes") if isinstance(v, dict) else v
            bars = [b for b in (nodes or []) if b.get("date")]
            if len(bars) >= args.min:
                seed[c] = bars
    log(f"本地标的 {len(store)} · 市场最新交易日 {mdate} · 映射种子 {len(bk_map)} 条 · "
        f"静态种子 {len(seed)} 板块(末{seed_asof or '无'}){' [已禁用]' if args.no_seed else ''}", fh)

    # 划分待办：已有且新鲜且历史足 → 跳过；其余 → 待补
    todo, skip = [], []
    for pt in meta:
        if only and pt not in only:
            continue
        bk = bk_map.get(pt)
        if not bk:
            skip.append((pt, "无BK映射"))
            continue
        bars = store.get(pt) or []
        if not args.force and bars and len(bars) >= args.min and \
                (not mdate or bars[-1]["date"] >= mdate or
                 _days_between(bars[-1]["date"], mdate) <= STALE_DAYS):
            skip.append((pt, f"已最新({len(bars)}根,{bars[-1]['date']})"))
            continue
        todo.append((pt, bk, len(bars)))

    log(f"② 待补 {len(todo)} · 跳过 {len(skip)}", fh)
    for pt, why in skip[:6]:
        log(f"   skip {pt}: {why}", fh)

    if args.dry_run:
        for pt, bk, n in todo[:20]:
            log(f"   todo {pt} → {bk} (本地{n}根)", fh)
        log(f"[DRY-RUN] 未抓数、未写盘", fh)
        fh.close()
        return 0

    if not todo:
        log("全部板块已最新，无需抓取，klines.json 未改动", fh)
        fh.close()
        return 0

    from datasource import sectorflow

    got, failed, short = 0, 0, 0
    from_seed = 0                   # 本次由静态种子补齐的板块数（零网络）
    streak = 0                      # 连续失败计数（仅东财抓取路径）：单个板块偶发失败跳过即可
    STREAK_LIMIT = 5
    for i, (pt, bk, n) in enumerate(todo, 1):
        src = "种子"
        fresh = seed.get(pt) or []
        if not fresh:
            src = "东财"
            fresh = sectorflow.fetch_bk_kline(bk, limit=args.limit)
        if len(fresh) < args.min:
            short += 1
            if src == "东财":
                streak += 1
                why = (sectorflow._MEM.get("last_kline_error") or "").strip()
                extra = f"；上游异常：{why}" if why else ""
            else:
                streak = 0          # 种子路径不消耗上游配额
                extra = ""
            log(f"   [{i}/{len(todo)}] {pt}←{bk} [{src}] 仅 {len(fresh)} 根(<{args.min})，"
                f"保留本地 {n} 根（连续失败 {streak}）{extra}", fh)
            if src == "东财" and streak >= STREAK_LIMIT:
                log(f"   [{i}/{len(todo)}] 连续 {streak} 个失败，判定上游不可用，"
                    f"剩余 {len(todo)-i} 个跳过并保留原数据；下次重跑从未补的继续", fh)
                failed += len(todo) - i
                break
            if src == "东财":
                time.sleep(min(0.5 * (2 ** (streak - 1)), 8))
            continue
        streak = 0                  # 成功即清零
        store[pt] = merge_bars(store.get(pt), fresh)
        got += 1
        if src == "种子":
            from_seed += 1
        else:
            time.sleep(0.15)
        log(f"   [{i}/{len(todo)}] {pt}←{bk} [{src}] {len(fresh)} 根 → 合并后 {len(store[pt])} 根"
            f" (末 {store[pt][-1]['date']})", fh)

    # 防复发校验：腾讯 pt 快照一旦混进东财 BK 序列，会出现 3 倍以上的量纲
    # 断层（2026-11-24 事故）。写盘前统一剔除少数派段，宁可少几根也不留断层。
    from jobs.repair_sector_kline_scale import outlier_indexes
    fixed_jumps = 0
    for pt in [c for c in store if str(c).lower().startswith("pt")]:
        nodes = store[pt]
        bad = outlier_indexes(nodes)
        if not bad:
            continue
        bad_dates = {nodes[j]["date"] for j in bad}
        cleaned = [b for b in nodes if b["date"] not in bad_dates]
        if not cleaned:
            log(f"   [量纲校验] {pt} 剔除后将为空，保留原样并告警", fh)
            continue
        store[pt] = cleaned
        fixed_jumps += 1
        log(f"   [量纲校验] {pt} 剔除断层 {len(nodes)} → {len(cleaned)} 根", fh)

    if got or fixed_jumps:
        save_store(store)
        log(f"③ 写入 {KLINE_FILE}：本次补齐 {got} 个板块（种子 {from_seed} · 东财 {got-from_seed}）· "
            f"历史不足保留 {short} · 失败/熔断 {failed}", fh)
    else:
        log(f"③ 无板块补齐成功（不足 {short} · 失败 {failed}），klines.json 未改动", fh)

    log(f"完成，耗时 {time.time()-t0:.1f}s", fh)
    fh.close()
    return 0 if (got or not todo) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
