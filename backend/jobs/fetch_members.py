"""行业成分股阶段快照（板块 → 个股的下钻数据）

做什么：
    对每个申万二级行业，取其成分股 → 抓日 K → 跑温斯坦 9 细分态 → 汇总成
    「这个行业里哪些股票处在哪个阶段」，供前端「点行业 → 看成分股」使用。

为什么单独一个任务：
    行业级阶段只看板块指数，回答不了「这个行业里买哪只」。
    温斯坦方法的标准动作是「先选强势行业，再在强势行业里选强势股」，
    这一步就是把行业拆成个股。

数据源（2026-11-08 切换）：
    成分股  datasource/eastmoney_members.py → 东财申万 496 个 BK 的全量成分股，
            按 flow_sector_map.json 反查汇总到本地 124 个 pt（exact + 细分 parent），
            覆盖科创板/北交所/次新股；无候选行业才回退腾讯 pt 板块。
    日 K    datasource/tencent.py（腾讯公开接口，无需授权，可无人值守跑）。
            北交所 bj* 腾讯目前只回 1 根 K 线，无法算 MA250，直接计入
            insufficient 并跳过请求，不写空数据。

用法（在 server/ 目录下）：
    python -m jobs.fetch_members --all               # 全部 124 个行业
    python -m jobs.fetch_members --stages 1,2        # 只跑 S1/S2 家族（复盘重点）
    python -m jobs.fetch_members --sectors pt01801081,pt01801015
    python -m jobs.fetch_members --all --force       # 忽略当日缓存强制重算

产出：
    data/snapshot/members/{ptcode}.json   每个行业的成分股阶段明细
    data/snapshot/members/_index.json     行业级索引（行数、分布、更新时间）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datasource.tencent import TencentSource
from engines.stage import compute_series, confirmed_sub, STAGE_NAME
from engines.pivot import scan

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(DATA_DIR, "raw")
SNAP = os.path.join(DATA_DIR, "snapshot")
MEMBERS = os.path.join(SNAP, "members")

# 算 MA250 需要 250 根 + 斜率回看 25 根，420 根留有足够余量
KLINE_LIMIT = 420
BENCH = "sh000300"


def load_bench(src: TencentSource) -> dict[str, float]:
    """沪深300 收盘序列，作为 RS60 的基准"""
    got = src.fetch_kline([BENCH], limit=KLINE_LIMIT)
    rows = got.get(BENCH) or []
    if not rows:
        print("[warn] 沪深300 基准抓取失败，RS60 将退化为个股 60 日涨幅")
    return {r["date"]: r["close"] for r in rows}


def analyze(rows, bench) -> dict | None:
    """单只股票：算阶段 + 最近一笔关键点信号"""
    if not rows or len(rows) < 270:
        return None
    series = compute_series(rows, bench, keep=len(rows))
    if not series:
        return None
    last = series[-1]
    _, sigs = scan(rows, series)
    sig = sigs[-1] if sigs else None
    prev_close = series[-2]["close"] if len(series) > 1 else last["close"]
    return {
        "close": round(last["close"], 2),
        "chg": round((last["close"] / prev_close - 1) * 100, 2),
        "stage": last["stage"],
        "sub": last["sub"],
        "sub_confirmed": confirmed_sub(series, 3),
        "rs60": last["rs60"],
        "dev30": last["dev30"],
        "dev250": last["dev250"],
        "slope_5w": last["slope_5w"],
        "dist52": last["dist52"],
        "chg20": last["chg20"],
        "ma250": last["ma250"],
        # 最近一笔关键点动作（买点/卖点），超过 40 个交易日就不再提示
        "sig": (sig["type"] if sig and _recent(sig["date"], last["date"], 40) else None),
        "sig_date": (sig["date"] if sig and _recent(sig["date"], last["date"], 40) else None),
        "pivot": (sig["pivot"] if sig else None),
        "stop": (sig.get("stop") if sig else None),
        "quality": (sig.get("quality") if sig else None),
        "grade": (sig.get("grade") if sig else None),
    }


def _recent(d1: str, d2: str, days: int) -> bool:
    """粗略判断 d1 是否在 d2 之前 days 个自然日内（同一交易日历下够用）"""
    try:
        from datetime import date
        y1, m1, dd1 = (int(x) for x in d1.split("-"))
        y2, m2, dd2 = (int(x) for x in d2.split("-"))
        return (date(y2, m2, dd2) - date(y1, m1, dd1)).days <= days * 1.5
    except Exception:
        return False


def build_sector(ptcode: str, meta: dict, src: TencentSource, bench, force=False, as_of=None) -> dict | None:
    out_path = os.path.join(MEMBERS, f"{ptcode}.json")
    # 断点续跑：当日已算过且有效就跳过（补数据时只重跑缺的那几个行业）
    if not force and as_of and os.path.exists(out_path):
        try:
            old = json.load(open(out_path, encoding="utf-8"))
            if old.get("analyzed", 0) > 0 and old.get("as_of") == as_of:
                print(f"  [cached] 已是 {as_of} 的数据，跳过")
                return old
        except Exception:
            pass

    # 成分股以「东财 BK 全量」为准：腾讯 pt 板块没有科创板/北交所，
    # 且半导体只给 59 只（东财 188 只）。东财源失败才回退腾讯，保证不写空快照。
    members, member_source = [], "eastmoney"
    try:
        from datasource.eastmoney_members import sector_members as em_members
        members = em_members(ptcode)
    except Exception as e:                                    # 快照缺失/损坏
        print(f"  [warn] {ptcode} 东财成分股源失败：{e}")
    if not members:
        members = src.sector_members(ptcode)
        member_source = "tencent"
    if not members:
        print(f"  [skip] {meta.get('name', ptcode)}：接口无成分股")
        return None

    # 北交所 K 线源暂不支持（实测只回 1 根），不浪费请求，直接计入未分析
    unsupported = [m["code"] for m in members if m["code"].startswith("bj")]
    codes = [m["code"] for m in members if not m["code"].startswith("bj")]
    bars = src.fetch_kline(codes, limit=KLINE_LIMIT)
    if not bars:
        # 整批取不到：打印真实原因，不要写空快照覆盖掉旧数据。
        # 以前写死"多为接口限流"，DNS 失败/网络断连/上游空数据都报同一句，
        # 排查时会被误导去治限流而放过真正的网络问题。
        from datasource import tencent as _tx
        diag = _tx.last_kline_diag or {}
        tx = diag.get("tx") or "无记录"
        sina = diag.get("sina") or "无记录"
        mode = "新浪兜底" if diag.get("sina_only") else "腾讯主源"
        print(f"  [fail] {meta.get('name', ptcode)}：K 线一只都没取到（{len(codes)} 只，"
              f"{mode}）；腾讯={tx}；新浪={sina}。保留旧快照不覆盖。")
        return None

    unsupported_set = set(unsupported)
    rows_out, dist = [], {1: 0, 2: 0, 3: 0, 4: 0}
    insufficient = list(unsupported)
    for m in members:
        if m["code"] in unsupported_set:
            continue
        r = analyze(bars.get(m["code"]), bench)
        if not r:
            insufficient.append(m["code"])
            continue
        dist[r["stage"]] += 1
        rows_out.append(dict(m, **r))

    # 默认排序：阶段优先（S2 → S1 → S3 → S4），同阶段按 RS60 降序
    rows_out.sort(key=lambda x: (x["stage"] not in (2, 1), x["stage"], -(x["rs60"] or -999)))

    payload = {
        "code": ptcode,
        "name": meta.get("name") or ptcode,
        "sector_stage": meta.get("stage"),
        "sector_sub": meta.get("sub"),
        "as_of": max((r["date"] for b in bars.values() if b for r in b), default=None),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "member_source": member_source,
        "total": len(members),
        "analyzed": len(rows_out),
        "insufficient": len(insufficient),
        "unsupported_board": len(unsupported),
        "dist": {str(k): v for k, v in dist.items()},
        "members": rows_out,
    }
    # 原子写入 + 重试：Windows 上偶发文件被另一进程占用会抛 OSError 22，
    # 以前直接写目标文件会截断旧快照，还会让整个 124 行业的循环直接退出。
    os.makedirs(MEMBERS, exist_ok=True)
    tmp_path = out_path + ".tmp"
    for attempt in range(3):
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, out_path)
            break
        except OSError as e:
            if attempt == 2:
                print(f"  [fail] {ptcode} 写入失败，保留旧快照：{e}")
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return None
            time.sleep(0.3 * (attempt + 1))
    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全部行业")
    ap.add_argument("--stages", default="", help="只跑指定阶段，如 1,2")
    ap.add_argument("--sectors", default="", help="指定行业代码，逗号分隔")
    ap.add_argument("--force", action="store_true", help="忽略当日缓存")
    args = ap.parse_args()

    sectors = json.load(open(os.path.join(SNAP, "sectors.json"), encoding="utf-8"))
    by_code = {s["code"]: s for s in sectors}

    if args.sectors:
        targets = [c.strip() for c in args.sectors.split(",") if c.strip()]
    elif args.stages:
        want = {int(x) for x in args.stages.split(",") if x.strip().isdigit()}
        targets = [s["code"] for s in sectors if s.get("stage") in want]
    elif args.all:
        targets = [s["code"] for s in sectors]
    else:
        # 默认只跑有交易价值的：S1 / S2 家族
        targets = [s["code"] for s in sectors if s.get("stage") in (1, 2)]

    print(f"[members] 目标行业 {len(targets)} 个")
    src = TencentSource(verbose=True)
    bench = load_bench(src)
    print(f"  基准沪深300：{len(bench)} 个交易日")

    index = {}
    idx_path = os.path.join(MEMBERS, "_index.json")
    if os.path.exists(idx_path):
        try:
            index = json.load(open(idx_path, encoding="utf-8"))
        except Exception:
            index = {}

    as_of = max(bench) if bench else None
    t0 = time.time()
    ok = 0
    for n, code in enumerate(targets, 1):
        meta = by_code.get(code, {})
        print(f"[{n}/{len(targets)}] {meta.get('name', code)} ({code}) stage={meta.get('stage')}")
        try:
            p = build_sector(code, meta, src, bench, args.force, as_of)
        except Exception as e:                # 单个行业异常不能拖垮整批
            print(f"      [error] {code} 计算异常，跳过：{type(e).__name__}: {e}")
            p = None
        if not p:
            continue
        ok += 1
        index[code] = {
            "name": p["name"], "sector_stage": p["sector_stage"], "sector_sub": p["sector_sub"],
            "as_of": p["as_of"], "total": p["total"], "analyzed": p["analyzed"],
            "member_source": p.get("member_source"),
            "unsupported_board": p.get("unsupported_board", 0),
            "dist": p["dist"], "updated": p["generated_at"],
        }
        print(f"     成分 {p['total']}（{p.get('member_source')}）· 可分析 {p['analyzed']}"
              f" · 无历史/不支持 {p['insufficient']} · 分布 {p['dist']}")

    os.makedirs(MEMBERS, exist_ok=True)
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[done] {ok}/{len(targets)} 个行业，用时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
