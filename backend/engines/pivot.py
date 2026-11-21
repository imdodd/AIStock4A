"""
利弗莫尔关键点引擎（日线口径）

关键点：前 60 日最高收盘（平台/前高），pivot[i] = max(close[i-60 .. i-1])
买点：close[i] > pivot[i] 且 volume[i] >= 1.3 x MA50(volume)，且阶段属于 S2 家族
止损：pivot[i] - 1.5 x ATR(20)
卖点：close[i] < min(close[i-60 .. i-1]) 且 volume[i] >= 1.3 x MA50(volume)（放量破位）
假突破：触发后 3 个交易日内收盘回落至关键点下方

质量分（100）：收盘位置20 + 量能强度20 + 平台蓄势20 + 热度催化20 + 均线结构10 + 风险扣分10
分档：>=80 A档(第一档) | 65-79 B档 | 50-64 C档 | <50 不入列
"""
from __future__ import annotations
from .stage import S2_SET, _atr

LOOKBACK = 60
VOL_WIN = 50
VOL_K = 1.3
ATR_WIN = 20
ATR_STOP_K = 1.5
FALSE_BREAK_DAYS = 3


def pivot_high(rows, i):
    if i < LOOKBACK:
        return None
    return max(r["close"] for r in rows[i - LOOKBACK:i])


def pivot_low(rows, i):
    if i < LOOKBACK:
        return None
    return min(r["close"] for r in rows[i - LOOKBACK:i])


def vol_ma(vols, i, n=VOL_WIN):
    if i + 1 < n:
        return None
    return sum(vols[i - n + 1:i + 1]) / n


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def score_quality(row, i, rows, series_i, volma):
    """六维质量分，返回 (score, detail)"""
    r = rows[i]; s = series_i
    rng = max(r["high"] - r["low"], 1e-9)
    close_pos = _clamp((r["close"] - r["low"]) / rng) * 20

    ratio = r["volume"] / volma if volma else 1.0
    if ratio <= 1.0:
        vol_score = 0.0
    elif ratio <= 1.3:
        vol_score = (ratio - 1.0) / 0.3 * 10
    else:
        vol_score = 10 + _clamp((ratio - 1.3) / 0.7) * 10

    seg = rows[max(0, i - LOOKBACK):i]
    amp = (max(x["high"] for x in seg) - min(x["low"] for x in seg)) / (sum(x["close"] for x in seg) / len(seg))
    amp_score = _clamp((0.30 - amp) / 0.30) * 20
    flat_days = sum(1 for x in seg if abs(x["close"] / s["ma60"] - 1) <= 0.08)
    flat_score = (flat_days / len(seg)) * 20
    base_score = amp_score * 0.6 + flat_score * 0.4

    chg = (r["close"] / rows[i - 1]["close"] - 1) * 100 if i >= 1 else 0
    heat_score = _clamp(chg / 5.0) * 10 + _clamp(s["rs60"] / 15.0) * 10

    struct = 0.0
    if s["ma30"] > s["ma60"]: struct += 4
    if s["ma60"] > s["ma120"]: struct += 3
    if r["close"] > s["ma120"]: struct += 2
    if series_i.get("slope_5w", 0) > 0: struct += 1

    atr = _atr(rows, ATR_WIN, i) or 0
    atr_ratio = atr / r["close"]
    risk = 5 * (1 - min(abs(s["dist52"]), 40) / 40) + 5 * (1 - min(atr_ratio, 0.08) / 0.08)

    total = close_pos + vol_score + base_score + heat_score + struct + risk
    detail = dict(close_pos=round(close_pos, 1), vol=round(vol_score, 1),
                  base=round(base_score, 1), heat=round(heat_score, 1),
                  struct=round(struct, 1), risk=round(risk, 1))
    return round(total), detail


def grade_of(score):
    if score >= 80: return "A"
    if score >= 65: return "B"
    if score >= 50: return "C"
    return "D"


def scan(rows, series, rs60_map=None):
    """返回 pivots（关键点线）与 signals（买卖点）"""
    vols = [r["volume"] for r in rows]
    # series 与 rows 尾部对齐
    off = len(rows) - len(series)
    pivots, signals = [], []
    prev_hit = False
    prev_break = False

    for i in range(len(rows)):
        ph, pl = pivot_high(rows, i), pivot_low(rows, i)
        if ph is None:
            continue
        si = i - off
        if si < 0 or si >= len(series):
            continue
        s = series[si]
        volma = vol_ma(vols, i)
        if not volma:
            continue

        hit = rows[i]["close"] > ph and rows[i]["volume"] >= VOL_K * volma
        brk = rows[i]["close"] < pl and rows[i]["volume"] >= VOL_K * volma

        if hit and not prev_hit and s["sub"] in S2_SET:
            score, detail = score_quality(rows, i, rows, s, volma)
            atr = _atr(rows, ATR_WIN, i) or 0
            # 假突破判定
            status = "confirmed"
            for k in range(1, FALSE_BREAK_DAYS + 1):
                if i + k < len(rows) and rows[i + k]["close"] < ph:
                    status = "false_break"
                    break
            signals.append(dict(
                type="buy", date=rows[i]["date"], pivot=round(ph, 2),
                price=round(rows[i]["close"], 2), sub=s["sub"], stage=s["stage"],
                vol_ratio=round(rows[i]["volume"] / volma, 2),
                quality=score, grade=grade_of(score), detail=detail,
                atr20=round(atr, 3), stop=round(ph - ATR_STOP_K * atr, 2),
                status=status,
            ))
            pivots.append(dict(date=rows[i]["date"], price=round(ph, 2), kind="high60",
                               atr20=round(atr, 3), stop=round(ph - ATR_STOP_K * atr, 2)))

        if brk and not prev_break:
            atr = _atr(rows, ATR_WIN, i) or 0
            signals.append(dict(
                type="sell", date=rows[i]["date"], pivot=round(pl, 2),
                price=round(rows[i]["close"], 2), sub=s["sub"], stage=s["stage"],
                vol_ratio=round(rows[i]["volume"] / volma, 2),
                quality=0, grade="-", detail={},
                atr20=round(atr, 3), stop=None, status="confirmed",
            ))
            pivots.append(dict(date=rows[i]["date"], price=round(pl, 2), kind="low60",
                               atr20=round(atr, 3), stop=None))

        prev_hit, prev_break = hit, brk

    return pivots, signals
