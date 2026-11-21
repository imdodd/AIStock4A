"""K 线图数据装配（图表 payload）

把「逐日指标序列 + 原始 K 线」压成前端 ECharts 直接可用的结构。
同时供两条路径使用：
    1. jobs/build_snapshot.py  —— 每日快照批量生成
    2. main.py /api/chart      —— 成分股按需现算（行业下钻时点开某只股票）

注意 series 会跳过 MA250 未成形的早期行，与 rows 不是 1:1 对齐，
所以 bars/volumes 必须按日期回取，不能用 len(rows)-len(tail) 的偏移量。
"""
from __future__ import annotations

from .stage import confirmed_sub


def chart_payload(code, name, rows, series, pivots, signals, keep=250):
    tail = series[-keep:]
    by_date = {r["date"]: r for r in rows}
    bars = [by_date[d["date"]] for d in tail]

    bands, prev = [], None
    for i, d in enumerate(tail):
        if d["sub"] != prev:
            bands.append(dict(from_idx=i, sub=d["sub"], stage=d["stage"]))
            prev = d["sub"]

    sig = [s for s in signals if s["date"] >= tail[0]["date"]]
    pv = [p for p in pivots if p["date"] >= tail[0]["date"]]

    # 滚动关键点线：每个交易日的前 60 日最高收盘（利弗莫尔枢轴），供图表阶梯线
    pos = {r["date"]: i for i, r in enumerate(rows)}
    closes = [r["close"] for r in rows]
    pivot_line = []
    for d in tail:
        i = pos[d["date"]]
        pivot_line.append(round(max(closes[max(0, i - 60):i]), 2) if i >= 60 else None)

    return dict(
        code=code, name=name,
        dates=[d["date"] for d in tail],
        bars=[[round(b["open"], 2), round(b["close"], 2), round(b["low"], 2), round(b["high"], 2)] for b in bars],
        volumes=[b["volume"] for b in bars],
        pivot_line=pivot_line,
        ma30=[d["ma30"] for d in tail], ma60=[d["ma60"] for d in tail],
        ma120=[d["ma120"] for d in tail], ma250=[d["ma250"] for d in tail],
        bands=bands,
        pivots=pv[-40:], signals=sig[-40:],
        last=dict(close=tail[-1]["close"], sub=tail[-1]["sub"], stage=tail[-1]["stage"],
                  confirmed=confirmed_sub(series, 3), rs60=tail[-1]["rs60"],
                  dev30=tail[-1]["dev30"], dev250=tail[-1]["dev250"],
                  slope_5w=tail[-1]["slope_5w"], dist52=tail[-1]["dist52"]),
    )
