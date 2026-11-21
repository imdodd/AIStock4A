"""每日自动抓取（替代手工调 MCP + ingest_raw）

流程：
    1. 先抓 5 个指数 → 用最新交易日作为本次 target_date（自动跳过周末/节假日）
    2. 全量标的（指数 + 124 行业 + 自选 + 持仓 + 信号池）逐批抓取
       · 库里没有的 → 全量 500 根
       · 已有但落后 → 增量 60 根
       · 已是最新 → 跳过
    3. 按日期并集合并进 data/raw/klines.json（分段/增量抓取必需，不能整体覆盖）
    4. 抓市场情绪 → data/raw/sentiment.json

用法：
    python -m jobs.fetch_daily                 # 正常每日增量
    python -m jobs.fetch_daily --force         # 忽略最新判断，全部重抓最近 60 根
    python -m jobs.fetch_daily --only sh000001,sh000688
    python -m jobs.fetch_daily --limit 500 --no-sentiment
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import universe
from datasource import get_source

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(DATA_DIR, "raw")
KLINE_FILE = os.path.join(RAW, "klines.json")
SENTIMENT_FILE = os.path.join(RAW, "sentiment.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")

FULL_LIMIT = 500   # 新标的：一次拿够 276+ 根计算窗口
INC_LIMIT = 60     # 已有标的：只补最近缺口


def log(msg, fh=None):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def load_store() -> dict[str, list[dict]]:
    """读取现有 K 线，并就地做一次格式迁移（旧数据字段是 last，新格式是 close）"""
    if not os.path.exists(KLINE_FILE):
        return {}
    from datasource.base import normalize_nodes
    try:
        raw = json.load(open(KLINE_FILE, encoding="utf-8"))
    except Exception:
        return {}
    store = {}
    for code, v in raw.items():
        nodes = v.get("nodes") if isinstance(v, dict) else v
        bars = normalize_nodes(nodes or [])
        if bars:
            store[code] = bars
    return store


def merge(store: dict, incoming: dict) -> int:
    """按日期并集合并，返回新增或更新的标的数"""
    changed = 0
    for code, bars in incoming.items():
        if not bars:
            continue
        if code not in store:
            store[code] = bars
            changed += 1
            continue
        byd = {b["date"]: b for b in store[code]}
        before = len(byd)
        for b in bars:
            byd[b["date"]] = b
        if len(byd) != before:
            store[code] = [byd[d] for d in sorted(byd)]
            changed += 1
    return changed


def save_store(store: dict):
    os.makedirs(RAW, exist_ok=True)
    payload = {c: {"nodes": bars} for c, bars in store.items()}
    tmp = KLINE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, KLINE_FILE)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="每日行情抓取")
    ap.add_argument("--force", action="store_true", help="忽略最新判断，全部重抓")
    ap.add_argument("--only", default="", help="只抓指定代码，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="覆盖抓取根数")
    ap.add_argument("--no-sentiment", action="store_true", help="跳过市场情绪")
    ap.add_argument("--no-pool", action="store_true", help="不含历史信号股池")
    args = ap.parse_args(argv)

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"fetch-{datetime.now():%Y-%m-%d}.log")
    fh = open(log_path, "a", encoding="utf-8")
    t0 = time.time()

    log("=" * 66, fh)
    log("每日抓取开始", fh)

    store = load_store()
    log(f"本地已有 {len(store)} 个标的", fh)

    codes = [c.strip() for c in args.only.split(",") if c.strip()] if args.only \
        else universe.all_codes(include_pool=not args.no_pool)
    log(f"本次待更新标的 {len(codes)} 个", fh)

    src = get_source()
    try:
        # ---- 第一步：先抓指数，确定最新交易日
        index_codes = list(universe.INDEX_META)
        log("① 抓取指数确定最新交易日 …", fh)
        idx_data = src.fetch_kline(index_codes, limit=INC_LIMIT if not args.force else 60)
        merge(store, idx_data)
        market_date = max((bars[-1]["date"] for bars in idx_data.values()), default=None)
        if not market_date:
            market_date = max((bars[-1]["date"] for bars in store.values()), default=None)
        log(f"   最新交易日 = {market_date}", fh)

        # ---- 第二步：其余标的按需更新
        todo_full, todo_inc, skip = [], [], []
        for c in codes:
            if c in index_codes:
                continue
            bars = store.get(c)
            if not bars:
                todo_full.append(c)
            elif args.force or not market_date or bars[-1]["date"] < market_date:
                todo_inc.append(c)
            else:
                skip.append(c)
        log(f"② 待抓取：新标的 {len(todo_full)} · 增量 {len(todo_inc)} · 已最新跳过 {len(skip)}", fh)

        new_count = 0
        if todo_full:
            log(f"   全量抓取 {len(todo_full)} 个（{FULL_LIMIT} 根）…", fh)
            got = src.fetch_kline(todo_full, limit=args.limit or FULL_LIMIT)
            new_count += merge(store, got)
            log(f"   成功 {len(got)}/{len(todo_full)}", fh)
            missing = [c for c in todo_full if c not in got]
            if missing:
                log(f"   [warn] 未拿到：{','.join(missing[:12])}"
                    f"{' …共%d个' % len(missing) if len(missing) > 12 else ''}", fh)

        if todo_inc:
            log(f"   增量抓取 {len(todo_inc)} 个（{args.limit or INC_LIMIT} 根）…", fh)
            got = src.fetch_kline(todo_inc, limit=args.limit or INC_LIMIT)
            new_count += merge(store, got)
            log(f"   成功 {len(got)}/{len(todo_inc)}", fh)
            missing = [c for c in todo_inc if c not in got]
            if missing:
                log(f"   [warn] 未拿到：{','.join(missing[:12])}"
                    f"{' …共%d个' % len(missing) if len(missing) > 12 else ''}", fh)

        save_store(store)
        usable = sum(1 for v in store.values() if len(v) >= 276)
        log(f"③ 已写入 {KLINE_FILE}（{os.path.getsize(KLINE_FILE)/1048576:.1f} MB）"
            f" 标的 {len(store)} · 可用(≥276根) {usable} · 本次变更 {new_count}", fh)

        # ---- 第三步：市场情绪
        sentiment_ok = False
        if not args.no_sentiment:
            log("④ 抓取市场情绪 …", fh)
            try:
                s = src.fetch_sentiment()
                s["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if market_date and s.get("date") != market_date:
                    log(f"   [warn] 情绪日期 {s.get('date')} 与最新交易日 {market_date} 不一致", fh)
                json.dump(s, open(SENTIMENT_FILE, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
                log(f"   上涨 {s['up']} / 下跌 {s['down']} / 涨停 {s['limit_up']} / "
                    f"跌停 {s['limit_down']} → {SENTIMENT_FILE}", fh)
                sentiment_ok = True
            except Exception as e:
                log(f"   [error] 情绪抓取失败：{e}（快照将回退到上一次的值）", fh)

        cost = time.time() - t0
        log(f"完成，耗时 {cost:.1f}s · 接口调用 {src.client.call_count if hasattr(src, 'client') else '-'} 次", fh)
        return 0 if (new_count or sentiment_ok) else 1
    finally:
        src.close()
        fh.close()


if __name__ == "__main__":
    sys.exit(main())
