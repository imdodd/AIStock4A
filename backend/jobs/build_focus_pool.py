"""全市场关注池（每日盘后汇总，替代旧的 62 只固定信号池）

做什么：
    EOD 第 ⑥ 步 jobs/fetch_members.py 已经对东财 BK 全量成分股（去重约 5109 只）
    跑过温斯坦阶段 + 利弗莫尔关键点算法，结果落在 data/snapshot/members/*.json，
    但以前只有「板块 → 成分股」下钻能看，关注页读的仍是 build_snapshot.py 对
    data/raw/klines.json 里 62 只历史股票算的 signals.json。

    本任务把 members 快照里的信号汇总、筛选、排序成「今天值得关注的股票」，
    写 data/snapshot/focus_pool.json，由 /api/signals 与 /api/focus/pool 消费。
    不再额外抓全市场行情：只对入选的几十只补一次精确信号字段与图表缓存。

口径（用户 2026-11-08 确认）：
    买点  近 10 个交易日 + 阶段 S1/S2 + A/B 档 + 质量分降序，取 Top 60
    卖点  近 10 个交易日（卖点无质量分/档位，且多在 S4），
          按「持仓或自选优先 → 信号日 → 破位幅度」取 Top 20
    合计 80 只。「近 10 个交易日」用沪深300 本地日 K 的交易日历，不是自然日。

用法（在 backend/ 目录下）：
    python -m jobs.build_focus_pool                 # 汇总 + 预热图表缓存
    python -m jobs.build_focus_pool --no-warm       # 只汇总，不抓行情（离线调试）
    python -m jobs.build_focus_pool --buy 80 --sell 30

产出：
    data/snapshot/focus_pool.json
    data/snapshot/charts/{code}.json   （仅补缺失/落后的，不覆盖已有的前复权快照）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from data_paths import DATA_PATH
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from engines.stage import compute_series, STAGE_NAME          # noqa: E402
from engines.pivot import scan                                # noqa: E402
from engines.chart import chart_payload                       # noqa: E402
from datasource.tencent import TencentSource                  # noqa: E402

RAW = DATA_PATH /"raw"
SNAP = DATA_PATH /"snapshot"
MEMBERS = SNAP / "members"
CHARTS = SNAP / "charts"
OUT = SNAP / "focus_pool.json"
BENCH = "sh000300"

WINDOW_DAYS = 10
BUY_STAGES = {1, 2}
BUY_GRADES = {"A", "B"}
BUY_LIMIT = 60
SELL_LIMIT = 20
KLINE_LIMIT = 420


def log(msg: str):
    print(msg, flush=True)


def window_start() -> str | None:
    """近 N 个交易日的起始日（含当天共 N 个），取沪深300 本地日 K 的日期轴。

    用交易日历而不是自然日，是为了避免长假后把两三周前的老信号误判成「近期」。
    """
    try:
        d = json.load(open(RAW / "klines.json", encoding="utf-8"))
        ds = sorted({n["date"] for n in d[BENCH]["nodes"] if n.get("date")})
        if len(ds) >= WINDOW_DAYS:
            return ds[-WINDOW_DAYS]
        return ds[0] if ds else None
    except Exception as e:
        log(f"[warn] 交易日历读取失败（{e}），退回不过滤日期")
        return None


def market_date() -> str | None:
    try:
        return (json.load(open(SNAP / "market.json", encoding="utf-8")) or {}).get("date")
    except Exception:
        return None


def members_as_of() -> str | None:
    """members 快照里最新的信号交易日。

    单行业抓取失败时会保留旧快照，所以这个日期可能落后于 market.date；
    把它写进 meta，前端口径提示才不会把旧信号说成是今天的。
    """
    try:
        idx = json.load(open(MEMBERS / "_index.json", encoding="utf-8"))
        ds = [v.get("as_of") for v in idx.values() if isinstance(v, dict) and v.get("as_of")]
        return max(ds) if ds else None
    except Exception:
        return None


def related_codes() -> set[str]:
    """持仓 + 自选：卖点排序时优先，保证「自己手里的票破位」一定看得见。"""
    out: set[str] = set()
    try:
        import watchstore
        out |= {c for c, _ in watchstore.items()}
    except Exception:
        pass
    try:
        import store
        out |= set(store.codes())
    except Exception:
        pass
    try:
        from config import universe
        out |= {c for c, _ in universe.watch_items()} | set(universe.hold_codes())
    except Exception:
        pass
    return out


def collect() -> tuple[dict[str, dict], int]:
    """扫 124 个行业快照 → 按 code 去重（同一只股票可能挂在多个行业下）。

    返回 (code → 主记录, members 原始行数)。主记录取信号日最新的一条，
    industries 保留它出现的全部行业名，供前端展示归属。
    """
    bag: dict[str, list[tuple[str, dict]]] = {}
    total_rows = 0
    for f in sorted(MEMBERS.glob("pt*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        ind = d.get("name") or f.stem
        for m in d.get("members") or []:
            code = m.get("code")
            if not code:
                continue
            total_rows += 1
            bag.setdefault(code, []).append((ind, m))

    out: dict[str, dict] = {}
    for code, entries in bag.items():
        entries.sort(key=lambda e: (e[1].get("sig_date") or "",
                                    e[1].get("quality") or 0), reverse=True)
        row = dict(entries[0][1])
        seen, inds = set(), []
        for ind, _ in entries:
            if ind and ind not in seen:
                seen.add(ind)
                inds.append(ind)
        row["industries"] = inds
        out[code] = row
    return out, total_rows


def break_pct(m: dict) -> float:
    """卖点破位幅度：现价相对 60 日最低收盘关键点的偏离（负值越深破得越狠）"""
    p, c = m.get("pivot"), m.get("close")
    if not p or not c:
        return 0.0
    return c / p - 1


def pick(by_code: dict[str, dict], cut: str | None, related: set[str],
         buy_limit: int, sell_limit: int) -> dict:
    buys, sells = [], []
    for m in by_code.values():
        sig, dt = m.get("sig"), m.get("sig_date") or ""
        if not sig or not dt or (cut and dt < cut):
            continue
        if sig == "buy" and m.get("stage") in BUY_STAGES and m.get("grade") in BUY_GRADES:
            buys.append(m)
        elif sig == "sell":
            sells.append(m)

    # 买点：质量分主序，同分按信号日、RS60；卖点：相关标的优先，其次新鲜度、破位深度
    buys.sort(key=lambda m: (m.get("sig_date") or "", m.get("rs60") or -999), reverse=True)
    buys.sort(key=lambda m: m.get("quality") or 0, reverse=True)
    sells.sort(key=lambda m: (m.get("sig_date") or "", -abs(break_pct(m))), reverse=True)
    sells.sort(key=lambda m: 1 if m.get("code") in related else 0, reverse=True)
    return {
        "buys": buys[:buy_limit], "sells": sells[:sell_limit],
        "buy_candidates": len(buys), "sell_candidates": len(sells),
    }


def to_row(m: dict, related: set[str]) -> dict:
    """字段对齐前端 signalRows() / renderSignalTable() 的 13 列。

    price / vol_ratio 由 enrich() 用真实信号回填；抓不到时留空显示「—」，不编数。
    """
    code = m["code"]
    inds = m.get("industries") or []
    return {
        "code": code,
        "name": m.get("name") or code,
        "type": m.get("sig"),
        "date": m.get("sig_date"),
        "stage": m.get("stage"),
        "stage_name": STAGE_NAME.get(m.get("stage")),
        "sub": m.get("sub"),
        "pivot": m.get("pivot"),
        "price": None,
        "vol_ratio": None,
        "quality": m.get("quality"),
        "grade": m.get("grade"),
        "stop": m.get("stop"),
        "close": m.get("close"),
        "chg": m.get("chg"),
        "rs60": m.get("rs60"),
        "dist52": m.get("dist52"),
        "dev250": m.get("dev250"),
        "slope_5w": m.get("slope_5w"),
        "industry": inds[0] if inds else None,
        "industries": inds,
        "related": code in related,
        "n_sig": 1,                       # 新池按股票去重，不再展示历史 ×N 角标
        "sig_dates": [m.get("sig_date")],
        "source": "focus_pool",
    }


def _need_chart(code: str, as_of: str | None) -> bool:
    p = CHARTS / f"{code}.json"
    if not p.exists():
        return True
    try:
        d = json.load(open(p, encoding="utf-8"))
        dates = d.get("dates") or []
        return not dates or bool(as_of and dates[-1] < as_of)
    except Exception:
        return True


def enrich(items: list[dict], as_of: str | None) -> tuple[int, int]:
    """对入选股票批量抓一次日 K：回填精确信号字段 + 补齐图表缓存。

    fetch_kline 内部并发（60 只约 1 秒），单只失败不影响其它；
    已有且最新的图表缓存不覆盖，避免用新浪不复权数据盖掉旧的前复权快照。
    """
    codes = [x["code"] for x in items]
    if not codes:
        return 0, 0
    src = TencentSource(verbose=False)
    try:
        bench_rows = src.fetch_kline([BENCH], limit=KLINE_LIMIT).get(BENCH) or []
        bench = {r["date"]: r["close"] for r in bench_rows}
        bars = src.fetch_kline(codes, limit=KLINE_LIMIT)
    finally:
        src.close()

    names = {x["code"]: x["name"] for x in items}
    sig_idx: dict[str, dict[str, dict]] = {}
    CHARTS.mkdir(parents=True, exist_ok=True)
    for code, rows in bars.items():
        if not rows or len(rows) < 270:
            continue
        try:
            series = compute_series(rows, bench, keep=len(rows))
            if not series:
                continue
            pivots, sigs = scan(rows, series)
            sig_idx[code] = {s["date"]: s for s in sigs}
            if _need_chart(code, as_of):
                d = chart_payload(code, names.get(code) or code, rows, series, pivots, sigs)
                tmp = CHARTS / f"{code}.json.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, CHARTS / f"{code}.json")
        except Exception as e:
            log(f"  [warn] {code} 图表/信号重算失败：{type(e).__name__}: {e}")

    filled = 0
    for it in items:
        s = (sig_idx.get(it["code"]) or {}).get(it["date"])
        if not s:
            continue
        for k in ("price", "vol_ratio", "quality", "grade", "stop", "pivot", "status"):
            if s.get(k) is not None:
                it[k] = s[k]
        filled += 1
    return filled, len(bars)


def build(no_warm: bool = False, buy_limit: int = BUY_LIMIT,
          sell_limit: int = SELL_LIMIT) -> dict:
    t0 = time.time()
    cut = window_start()
    as_of = market_date()
    sig_as_of = members_as_of()
    related = related_codes()
    by_code, rows_total = collect()
    if not by_code:
        raise RuntimeError("members 快照为空，先跑 python -m jobs.fetch_members --all --force")

    picked = pick(by_code, cut, related, buy_limit, sell_limit)
    buys, sells = picked["buys"], picked["sells"]
    items = [to_row(m, related) for m in buys + sells]
    log(f"[关注池] 成分股快照 {rows_total} 行 → 去重 {len(by_code)} 只 · "
        f"近 {WINDOW_DAYS} 个交易日自 {cut} · 信号截至 {sig_as_of}（大盘 {as_of}）")
    log(f"[关注池] 候选：买点 {picked['buy_candidates']} 只 / "
        f"卖点 {picked['sell_candidates']} 只 → 入选 买点 {len(buys)} + "
        f"卖点 {len(sells)} = {len(items)} 只")

    warmed = {"quotes": 0, "matched": 0}
    if not no_warm and items:
        try:
            matched, quotes = enrich(items, as_of)
            warmed = {"quotes": quotes, "matched": matched}
            log(f"[关注池] 行情回填 {matched}/{len(items)} 只 · 取到 K 线 {quotes} 只")
            # 回填的是本次重算的精确质量分（与右侧图表同源，实测与 members 差 -1~+3 分）。
            # 按它重排买点，保证「质量分排序」和列表里显示的分数是同一个数；卖点顺序不动。
            buy_rows = sorted((x for x in items if x["type"] == "buy"),
                              key=lambda x: x.get("quality") or 0, reverse=True)
            sell_rows = [x for x in items if x["type"] == "sell"]
            items[:] = buy_rows + sell_rows
        except Exception as e:
            log(f"[warn] 行情回填整批失败（不影响列表）：{type(e).__name__}: {e}")

    rules = {
        "window": f"近 {WINDOW_DAYS} 个交易日（沪深300 交易日历，自 {cut}）",
        "buy": f"买点：阶段 S1/S2 + A/B 档 + 质量分降序，Top {buy_limit}",
        "sell": f"卖点：持仓/自选优先 → 信号日 → 破位幅度，Top {sell_limit}（卖点无质量分/档位）",
        "engine": "温斯坦 9 细分态 + 利弗莫尔前 60 日关键点（量≥1.3×MA50，100 分质量分）",
        "source": "东财申万 BK 全量成分股 → members 阶段快照",
    }
    now = datetime.now().isoformat(timespec="seconds")
    out = {
        "date": as_of,
        "generated_at": now,
        "signals_as_of": sig_as_of,
        "universe": len(by_code),
        "scanned_rows": rows_total,
        "window_from": cut,
        "rules": rules,
        "counts": {
            "buy_candidates": picked["buy_candidates"],
            "sell_candidates": picked["sell_candidates"],
            "buy": len(buys), "sell": len(sells), "picked": len(items),
        },
        "warmed": warmed,
        "meta": {
            "date": as_of, "signals_as_of": sig_as_of, "universe": len(by_code),
            "picked": len(items), "buy": len(buys), "sell": len(sells),
            "window_from": cut, "window_days": WINDOW_DAYS, "generated_at": now,
            "rules": rules,
        },
        "items": items,
    }
    SNAP.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    log(f"[关注池] → {OUT.name}（{os.path.getsize(OUT) / 1024:.1f} KB，"
        f"耗时 {time.time() - t0:.1f}s）")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="汇总全市场关注池")
    ap.add_argument("--no-warm", action="store_true", help="不抓行情、不预热图表缓存")
    ap.add_argument("--buy", type=int, default=BUY_LIMIT, help="买点入选上限")
    ap.add_argument("--sell", type=int, default=SELL_LIMIT, help="卖点入选上限")
    a = ap.parse_args()
    r = build(no_warm=a.no_warm, buy_limit=a.buy, sell_limit=a.sell)
    print(json.dumps(r["counts"], ensure_ascii=False))
