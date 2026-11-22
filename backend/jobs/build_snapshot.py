"""
EOD 快照构建（M0）：原始 K 线 -> 温斯坦阶段 + 利弗莫尔关键点 + 质量分 -> data/snapshot/*.json

用法：
    python -m jobs.build_snapshot          # 在 server/ 目录下执行

口径：engines/stage.py（9 细分态判定树）+ engines/pivot.py（60 日最高收盘关键点 / 1.3x 量 / ATR 止损 / 100 分质量分）
本脚本是 M2 正式 EOD 管道的雏形：把「取数」换成 Tushare 适配器即可，下游完全复用。
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.stage import compute_series, extract_transitions, confirmed_sub, SUB_TO_STAGE, STAGE_NAME
from engines.pivot import scan
from engines.chart import chart_payload
from config import universe
import store

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(DATA_DIR, "raw")
SNAP = os.path.join(DATA_DIR, "snapshot")
CHARTS = os.path.join(SNAP, "charts")

INDEX_META = universe.INDEX_META
WATCHLIST = universe.watch_items()      # 用户维护的自选清单（前端可增删），不再写死常量
HOLDINGS = universe.HOLDINGS

# 兜底情绪（仅在 data/raw/sentiment.json 缺失时使用，正常由 jobs.fetch_daily 写入）
SENTIMENT_FALLBACK = {
    "date": "2026-10-30",
    "up": 892, "down": 4602, "flat": 83, "total": 5577,
    "limit_up": 45, "limit_down": 11,
    "up_ratio": 15.99, "down_ratio": 82.52, "updown_ratio": 0.19,
    "new_high_20": 195, "new_low_20": 3163,
    "new_high_250": 34, "new_low_250": 1191,
    "source": "fallback-constant",
}


def load_sentiment():
    """情绪优先读抓取产物；缺失时回退常量并明确告警，避免误以为是最新数据"""
    p = os.path.join(RAW, "sentiment.json")
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            if d.get("total") or d.get("up"):
                return d
        except Exception as e:
            print(f"[warn] sentiment.json 解析失败：{e}")
    print("[warn] 未找到 sentiment.json，情绪回退为兜底常量 —— 请先运行 python -m jobs.fetch_daily")
    return dict(SENTIMENT_FALLBACK)


SENTIMENT = load_sentiment()


def norm(nodes):
    """兼容两种落盘格式：新格式（close）/ 旧格式（last）"""
    rows = []
    for n in nodes:
        close = n.get("close")
        if close is None:
            close = n.get("last")
        rows.append(dict(date=n["date"], open=n["open"], high=n["high"], low=n["low"],
                         close=close, volume=n.get("volume") or 0,
                         amount=n.get("amount") or 0))
    return rows


def main():
    raw = json.load(open(os.path.join(RAW, "klines.json"), encoding="utf-8"))
    _smeta_path = os.path.join(RAW, "sectors_meta.json")
    if os.path.exists(_smeta_path):
        smeta = json.load(open(_smeta_path, encoding="utf-8"))
    else:
        print("[WARN] sectors_meta.json 缺失，按空板块表继续（可放置公开样例种子）")
        smeta = {}
    stu = json.load(open(os.path.join(RAW, "stocks_meta.json"), encoding="utf-8")) if os.path.exists(os.path.join(RAW, "stocks_meta.json")) else {}
    os.makedirs(CHARTS, exist_ok=True)

    # 基准容错：全新安装若沪深300尚未落盘，不能让整个快照构建崩掉。
    bench_rows = norm(raw["sh000300"]["nodes"]) if raw.get("sh000300") else []
    bench = {r["date"]: r["close"] for r in bench_rows}

    charts, sectors, signals_all, trans_all, index_cards = {}, [], [], [], []

    def process(code, name, kind):
        nodes = raw[code]["nodes"]
        rows = norm(nodes)
        series = compute_series(rows, bench, keep=len(rows))
        if not series:
            return None
        pivots, sigs = scan(rows, series)
        for s in sigs:
            s.update(code=code, name=name, kind=kind)
        last = series[-1]
        trans = extract_transitions(series, min_hold=1)
        charts[code] = chart_payload(code, name, rows, series, pivots, sigs)
        return dict(rows=rows, series=series, sigs=sigs, trans=trans, last=last)

    # ---- 指数 ----
    for code, name in INDEX_META.items():
        if code not in raw or code == "sh000300":
            continue
        r = process(code, name, "index")
        if not r:
            # 历史不足（全新安装时指数只有 60 根，算不出 MA250）时保留占位卡片：
            # 至少显示最新点位与涨跌幅，阶段/指标留空由前端标「数据积累中」，
            # 否则四大指数整块消失，用户会误以为功能坏了。数据攒够后自动转正。
            nodes = norm(raw[code]["nodes"]) if raw.get(code) else []
            last = nodes[-1] if nodes else None
            prev = nodes[-2] if len(nodes) >= 2 else None
            index_cards.append(dict(
                code=code, name=name,
                price=(last["close"] if last else None),
                chg=(round((last["close"] / prev["close"] - 1) * 100, 2)
                     if last and prev and prev.get("close") else None),
                sub=None, sub_confirmed=None, stage=None, stage_name="数据积累中",
                rs60=None, dev250=None, slope_5w=None, dist52=None, ma250=None,
                no_data=True,
            ))
            continue
        l = r["last"]
        index_cards.append(dict(
            code=code, name=name, price=l["close"],
            chg=round((l["close"] / r["series"][-2]["close"] - 1) * 100, 2),
            sub=l["sub"], sub_confirmed=confirmed_sub(r["series"], 3), stage=l["stage"],
            stage_name=STAGE_NAME[l["stage"]], rs60=l["rs60"], dev250=l["dev250"],
            slope_5w=l["slope_5w"], dist52=l["dist52"], ma250=l["ma250"],
            no_data=False,
        ))
        recent = [t for t in r["trans"] if t["frm"] is None or t["date"] >= r["series"][-160]["date"]]
        for t in recent[-6:]:
            t.update(code=code, name=name, kind="index")
            trans_all.append(t)

    # ---- 板块 ----
    for code, meta in smeta.items():
        name = meta["name"]
        if code not in raw:
            sectors.append(dict(code=code, name=name, stage=meta.get("stage"), sub=meta.get("sub"),
                                sub_confirmed=meta.get("sub"),
                                has_chart=False, price=None, chg=0, rs60=meta["rs60"],
                                dev30=meta["dev30"], dev250=meta["dev250"],
                                slope_5w=meta["slope"], dist52=meta["dist52"], changed=None))
            continue
        r = process(code, name, "sector")
        if not r:
            # 板块指数历史不足时（全新安装时腾讯 pt 只回 1 根 K 线）也要保留
            # 板块条目，否则 sectors.json 为空会让下游行业映射/成分股/关注池
            # 整条链失效。指标先用种子里的静态值，等 K 线攒够再自动转正。
            sectors.append(dict(code=code, name=name, stage=meta.get("stage"), sub=meta.get("sub"),
                                sub_confirmed=meta.get("sub"),
                                has_chart=False, price=None, chg=0, rs60=meta["rs60"],
                                dev30=meta["dev30"], dev250=meta["dev250"],
                                slope_5w=meta["slope"], dist52=meta["dist52"], changed=None))
            continue
        l = r["last"]
        last_chg = [t for t in r["trans"] if t["frm"]][-1:] or [{}]
        sectors.append(dict(
            code=code, name=name, stage=l["stage"], sub=l["sub"],
            sub_confirmed=confirmed_sub(r["series"], 3),
            has_chart=True, price=l["close"],
            chg=round((l["close"] / r["series"][-2]["close"] - 1) * 100, 2),
            rs60=l["rs60"], dev30=l["dev30"], dev250=l["dev250"],
            slope_5w=l["slope_5w"], dist52=l["dist52"],
            changed=(last_chg[0].get("date") if last_chg else None),
            changed_from=(last_chg[0].get("frm") if last_chg else None),
        ))
        for t in r["trans"][-4:]:
            if t["frm"]:
                t.update(code=code, name=name, kind="sector")
                trans_all.append(t)

    # ---- 个股（信号池） ----
    # 名称解析优先级：行情抓取元数据 > 静态自选 > 静态持仓 > 用户持仓清单。
    # 此前漏了后两级，深夜加持仓次日才跑 EOD 时，信号里的名字会固化成代码。
    names = dict(stu)
    names.update({c: n for c, n in WATCHLIST})
    names.update({c: n for c, n in HOLDINGS})
    try:
        import store as portfolio_store          # 用户维护的持仓，名字以清单为准
        for r in portfolio_store.load():
            if r.get("name") and r["name"] != r["code"]:
                names[r["code"]] = r["name"]
    except Exception:
        pass
    for code in raw:
        if code.startswith("pt") or code in INDEX_META:
            continue
        name = names.get(code) or code
        r = process(code, name, "stock")
        if not r:
            continue
        for s in r["sigs"][-6:]:
            s["kind"] = "stock"
            signals_all.append(s)

    signals_all.sort(key=lambda s: (s["date"], s.get("quality", 0)), reverse=True)
    trans_all.sort(key=lambda t: t["date"], reverse=True)

    dist = {1: 0, 2: 0, 3: 0, 4: 0}
    for s in sectors:
        if s["stage"]:
            dist[s["stage"]] += 1

    watch = []
    for code, name in WATCHLIST:
        name = names.get(code, name)
        ch = charts.get(code)
        if not ch:
            continue
        l = ch["last"]
        recent = [s for s in signals_all if s["code"] == code and s["date"] >= ch["dates"][-8]][:1]
        last_pv = ch["pivots"][-1] if ch["pivots"] else None
        watch.append(dict(code=code, name=name, price=l["close"],
                          chg=round((l["close"] / ch["bars"][-2][1] - 1) * 100, 2),
                          sub=l["sub"], stage=l["stage"], stage_name=STAGE_NAME[l["stage"]],
                          sub_confirmed=l["confirmed"], rs60=l["rs60"], dist52=l["dist52"],
                          pivot=(last_pv["price"] if last_pv else None),
                          stop=(recent[0]["stop"] if recent and recent[0].get("stop") else None),
                          has_chart=True,
                          alert=(recent[0] if recent else None)))

    holds = []
    for item in store.load():
        code, name = item["code"], item.get("name") or item["code"]
        ch = charts.get(code)
        if not ch:
            continue
        l = ch["last"]
        last_pv = ch["pivots"][-1] if ch["pivots"] else None
        last_sig = ch["signals"][-1] if ch["signals"] else None
        cost, qty = item.get("cost"), item.get("quantity")
        holds.append(dict(code=code, name=name, price=l["close"],
                          chg=round((l["close"] / ch["bars"][-2][1] - 1) * 100, 2),
                          cost=cost, quantity=qty,
                          added_at=item.get("added_at"), note=item.get("note"),
                          pnl=(round((l["close"] / cost - 1) * 100, 2) if cost else None),
                          sub=l["sub"], stage=l["stage"], stage_name=STAGE_NAME[l["stage"]],
                          sub_confirmed=l["confirmed"], dist52=l["dist52"], rs60=l["rs60"],
                          pivot=(last_pv["price"] if last_pv else None),
                          stop=(last_sig["stop"] if last_sig and last_sig.get("stop") else None),
                          last_signal=last_sig,
                          has_chart=True))

    market = dict(
        date=max((raw[c]["nodes"][-1]["date"] for c in INDEX_META
                  if c in raw and raw[c].get("nodes")), default=SENTIMENT_FALLBACK["date"]),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        indices=index_cards, sentiment=SENTIMENT,
        distribution=[dict(stage=k, name=STAGE_NAME[k], count=v) for k, v in sorted(dist.items())],
        s2_list=[s["name"] for s in sectors if s["sub"] in ("S2标准", "S2发展", "S2突破", "S2尝试")],
        coverage=dict(sectors_total=len(smeta), sectors_with_chart=sum(1 for s in sectors if s["has_chart"]),
                      stocks=sum(1 for c in charts if not c.startswith("pt") and c not in INDEX_META)),
    )

    def dump(name, obj):
        p = os.path.join(SNAP, name)
        json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        print(f"  {name}: {os.path.getsize(p)/1024:.1f} KB")

    print("[snapshot]")
    dump("market.json", market)
    dump("sectors.json", sectors)
    dump("transitions.json", trans_all[:80])
    dump("signals.json", signals_all[:80])
    dump("watchlist.json", watch)
    dump("holdings.json", holds)

    # 持仓动态止损（每日收盘后重算：成本-8% / 滚动-8% / 枢轴破位）
    try:
        import stops
        sd = stops.recompute()
        n_exit = sum(1 for r in sd.get("items", {}).values() if r.get("status") == "exit")
        n_warn = sum(1 for r in sd.get("items", {}).values() if r.get("status") == "warn")
        print(f"[stops] {len(sd.get('items', {}))} 只 · 触发离场 {n_exit} · 接近预警 {n_warn}")
    except Exception as e:
        print(f"[stops] 重算失败（不阻塞快照）：{type(e).__name__}: {e}")

    total = 0
    for code, ch in charts.items():
        json.dump(ch, open(os.path.join(CHARTS, f"{code}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        total += 1
    print(f"  charts/: {total} files")

    print(f"[done] 指数 {len(index_cards)} | 板块 {len(sectors)} (有历史 {market['coverage']['sectors_with_chart']}) "
          f"| 信号 {len(signals_all)} | 转折 {len(trans_all)} | 阶段分布 {dist}")


if __name__ == "__main__":
    main()
