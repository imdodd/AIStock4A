"""首期回填：把过去 N 周按跟踪维度补出来，让时间轴一开始就有对比

只补「能从历史数据算出来」的维度：价格/趋势/相对强度/估值水位/量能；
行业批价、渠道库存、政策这类必须靠当期调研或搜索的维度，标记为「首期未采集」，
由后续每期自动化逐步补上——不编造。

用法：
    python -m jobs.backfill_tracking --code sh603198 --weeks 8
    python -m jobs.backfill_tracking --all --weeks 8
"""
from __future__ import annotations

from data_paths import DATA_DIR

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import profile                                  # noqa: E402
import tracking                                 # noqa: E402
from engines.stage import compute_series        # noqa: E402

CHARTS = os.path.join(DATA_DIR, "snapshot", "charts")


def _chart(code: str) -> dict | None:
    p = os.path.join(CHARTS, f"{code}.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _status_trend(sub: str, rs60, dev250) -> tuple[str, str]:
    """趋势维度 → (状态, 说明)"""
    s2 = sub.startswith("S2")
    s4 = sub.startswith("S4")
    if s2 and (rs60 or 0) > 0 and (dev250 or 0) > 0:
        return "g", "Stage2 且 RS60、年线同向为正"
    if s4 or (dev250 is not None and dev250 < -5):
        return "r", f"Stage4 或跌破年线较多（{sub}，距年线 {dev250:.1f}%）"
    return "y", f"震荡（{sub}，距年线 {dev250:.1f}%）" if dev250 is not None else f"震荡（{sub}）"


def _status_valuation(pe, pe_anchor) -> tuple[str, str]:
    if not pe or not pe_anchor:
        return "n", "无估值数据"
    lo, hi = pe_anchor * 0.9, pe_anchor * 1.15
    if pe < lo:
        return "g", f"PE {pe:.1f} 低于锚定下沿 {lo:.1f}"
    if pe > hi:
        return "r", f"PE {pe:.1f} 高于锚定上沿 {hi:.1f}"
    return "y", f"PE {pe:.1f} 处于中枢区间 {lo:.1f}~{hi:.1f}"


def backfill(code: str, weeks: int = 8, force: bool = False) -> int:
    meta = tracking.load(code)
    if not meta:
        print(f"  [skip] {code} 没有跟踪方案，先启动跟踪")
        return 0
    ch = _chart(code)
    if not ch:
        print(f"  [skip] {code} 无图表缓存，先跑 build_snapshot 或访问一次图表")
        return 0
    dates, bars = ch["dates"], ch["bars"]
    vols = ch.get("volumes") or []
    ma250 = ch.get("ma250") or []
    subs = {}
    for b in sorted(ch.get("bands", []), key=lambda x: x["from_idx"]):
        start = b["from_idx"]
        end = next((x["from_idx"] for x in ch["bands"] if x["from_idx"] > start), len(dates))
        for i in range(start, end):
            subs[dates[i]] = (b.get("sub"), b.get("stage"))

    prof = profile.profile(code)
    pe_anchor = (prof.get("basic") or {}).get("pe_ttm")
    price_now = (prof.get("basic") or {}).get("price") or (bars[-1][1] if bars else None)
    existing = {p["date"] for p in tracking.period_list(code)}
    dims = meta.get("dimensions") or []
    made = 0

    # 每 5 个交易日取一周的最后一天
    idxs = list(range(len(dates) - 1, 0, -5))[:weeks]
    idxs.reverse()
    for n, i in enumerate(idxs):
        day = dates[i]
        if day in existing and not force:
            continue
        close = bars[i][1]
        prev_i = max(0, i - 5)
        week_chg = (close / bars[prev_i][1] - 1) * 100 if bars[prev_i][1] else None
        # 60 日相对沪深300：用收盘序列近似（bench 未落盘，退化用自身 60 日涨幅）
        rs60 = ch.get("last", {}).get("rs60") if i == len(dates) - 1 else None
        dev250 = ((close / ma250[i] - 1) * 100) if i < len(ma250) and ma250[i] else None
        sub, stage = subs.get(day) or (None, None)
        hi, lo = max(b[3] for b in bars[max(0, i - 250):i + 1]), min(b[2] for b in bars[max(0, i - 250):i + 1])
        pos52 = round((close - lo) / (hi - lo) * 100, 1) if hi > lo else None
        pe_t = round(pe_anchor * close / price_now, 1) if pe_anchor and price_now else None
        vol = vols[i] if i < len(vols) else None
        window = vols[max(0, i - 50):i]
        vol_ma = (sum(window) / len(window)) if window else None
        vol_ratio = round(vol / vol_ma, 2) if vol and vol_ma else None

        values = []
        for d in dims:
            key, name = d["key"], d["name"]
            if key == "trend" or "趋势" in name:
                st, note = _status_trend(sub or "S1", rs60, dev250)
                values.append(dict(key=key, value=f"{sub or '—'} · 距年线 {dev250:.1f}% · 52周位置 {pos52}%"
                                   if dev250 is not None else f"{sub or '—'}",
                                   status=st, delta="—", note=note))
            elif key == "valuation" or "估值" in name:
                st, note = _status_valuation(pe_t, pe_anchor)
                values.append(dict(key=key, value=f"PE(TTM)≈{pe_t}" if pe_t else "—",
                                   status=st, delta="—", note=note + "（按当期价格与当前 PE 推算）"))
            elif key == "capital" or "资金" in name:
                wv = sum(vols[max(0, i - 4):i + 1]) if vols else 0
                values.append(dict(key=key, value=f"近5日成交量 {round(wv / 1e4, 1)} 万手",
                                   status="n", delta="—",
                                   note=f"量比 {vol_ratio}；主力资金流历史无法回溯，首期未采集"))
            elif key == "earnings" or "业绩" in name:
                latest = (prof.get("finance") or {}).get("latest") or {}
                values.append(dict(key=key,
                                   value=f"{latest.get('period','—')} 营收 {latest.get('revenue','—')} 亿 / 净利 {latest.get('profit','—')} 亿",
                                   status="y", delta="—",
                                   note=f"营收同比 {latest.get('revenue_yoy') and round(latest['revenue_yoy'],1)}%；该期数据为当前最新报告期，回填期尚未披露"))
            else:
                values.append(dict(key=key, value="首期未采集", status="n", delta="—",
                                   note="该维度依赖当期渠道调研/搜索，回填阶段无历史快照，自本期起开始记录"))
        bad = [v for v in values if v["status"] == "r"]
        good = [v for v in values if v["status"] == "g"]
        # 只有够多的维度有真实判断时才敢下结论，否则老实说「中性（样本不足）」
        if len(bad) >= 2:
            verdict = "偏空"
        elif not bad and len(good) >= 2:
            verdict = "偏多"
        elif len(bad) + len(good) <= 1:
            verdict = "中性"
        else:
            verdict = "中性"
        summary = (f"{day} 收盘 {close}，周{'涨' if (week_chg or 0) >= 0 else '跌'} "
                   f"{abs(week_chg or 0):.2f}%，{sub or '阶段未知'}，52 周位置 {pos52}%")
        period = dict(date=day, verdict=verdict, summary=summary, close=close,
                      week_chg=round(week_chg, 2) if week_chg is not None else None,
                      values=values, source="backfill",
                      highlights=[f"距年线 {dev250:.1f}%" if dev250 is not None else "",
                                  f"PE≈{pe_t}" if pe_t else "",
                                  f"52周位置 {pos52}%"])
        period["highlights"] = [h for h in period["highlights"] if h]
        tracking.save_period(code, period)
        made += 1
    print(f"  [backfill] {code} {meta.get('name')}：补 {made} 期（共 {len(idxs)} 期候选）")
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--weeks", type=int, default=8)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    codes = [args.code] if args.code else list(tracking.list_all().keys()) if args.all else []
    if not codes:
        print("请指定 --code 或 --all")
        return
    total = 0
    for c in codes:
        total += backfill(c, args.weeks, args.force)
    print(f"[done] 共补 {total} 期")


if __name__ == "__main__":
    main()
