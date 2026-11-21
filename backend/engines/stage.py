"""
温斯坦阶段引擎（日线口径，唯一口径来源）

判定树（无记忆，逐日可回放）：
    if close > MA250:
        if slope_5w > 0.3:                      # 年线明确上行
            align & rs60>0            -> S2标准   (MA30>MA60>MA120>MA250 且跑赢基准)
            close>=MA30 & close>=MA60 -> S2发展
            else                      -> S2回调
        elif slope_5w >= -0.3:                  # 年线走平
            dist52>=-8 & dev30>0 & dev60>0 -> S2突破
            dev30<0 & dev60<0              -> S3
            else                           -> S1末临界
        else:                                    # 年线明显下行
            dev30>0 & dev60>0 & chg20>0    -> S2尝试
            else                           -> S3
    else:
        close > MA120 -> S1末 (dev250>-7 and chg20>0) else S1
        else          -> S4

参数（engine_config 默认值）：
    SLOPE_UP = 0.3   年线 5 周斜率上行阈值(%)
    SLOPE_FLAT = -0.3  走平下限(%)
    DIST52_NEAR = -8   距 52 周高点"接近"阈值(%)
    DEV_NEAR = -7      S1末 相对年线偏离阈值(%)
"""
from __future__ import annotations

SLOPE_UP = 0.3
SLOPE_FLAT = -0.3
DIST52_NEAR = -8.0
DEV_NEAR = -7.0
SLOPE_LOOKBACK = 25          # 5 周 ≈ 25 个交易日
YEAR_LINE = 250

S2_SET = ("S2标准", "S2发展", "S2回调", "S2突破", "S2尝试")
SUB_TO_STAGE = {
    "S2标准": 2, "S2发展": 2, "S2回调": 2, "S2突破": 2, "S2尝试": 2,
    "S1末": 1, "S1末临界": 1, "S1": 1,
    "S3": 3, "S4": 4,
}
STAGE_NAME = {1: "Stage 1 筑底", 2: "Stage 2 上升", 3: "Stage 3 顶部", 4: "Stage 4 下跌"}


def _sma(vals, n, i):
    if i + 1 < n:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def _atr(rows, n, i):
    if i < 1 or i + 1 < n:
        return None
    trs = []
    for k in range(i - n + 1, i + 1):
        h, l, pc = rows[k]["high"], rows[k]["low"], rows[k - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def compute_series(rows, bench_by_date, keep=260):
    """rows: 升序日K [{date,open,high,low,close,volume,amount}]
    返回逐日指标列表（仅保留最后 keep 个交易日，供图表与判定）"""
    dates = [r["date"] for r in rows]
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]
    bench = [bench_by_date.get(d) for d in dates]

    out = []
    for i in range(len(rows)):
        p = closes[i]
        ma30, ma60 = _sma(closes, 30, i), _sma(closes, 60, i)
        ma120, ma250 = _sma(closes, 120, i), _sma(closes, YEAR_LINE, i)
        if None in (ma30, ma60, ma120, ma250):
            continue
        ma250_prev = _sma(closes, YEAR_LINE, i - SLOPE_LOOKBACK)
        if ma250_prev is None or i < 60:
            continue
        slope_5w = (ma250 / ma250_prev - 1) * 100
        dev30 = (p / ma30 - 1) * 100
        dev60 = (p / ma60 - 1) * 100
        dev120 = (p / ma120 - 1) * 100
        dev250 = (p / ma250 - 1) * 100
        chg20 = (p / closes[i - 20] - 1) * 100
        chg60 = (p / closes[i - 60] - 1) * 100
        b0, b60 = bench[i], bench[i - 60]
        rs60 = (chg60 - (b60 and (b0 / b60 - 1) * 100)) if (b0 and b60) else 0.0
        hi52 = max(closes[max(0, i - 249):i + 1])
        dist52 = (p / hi52 - 1) * 100
        align = ma30 > ma60 > ma120 > ma250

        # ---- 判定树 ----
        if p > ma250:
            if slope_5w > SLOPE_UP:
                if align and rs60 > 0:
                    sub = "S2标准"
                elif p >= ma30 and p >= ma60:
                    sub = "S2发展"
                else:
                    sub = "S2回调"
            elif slope_5w >= SLOPE_FLAT:
                if dist52 >= DIST52_NEAR and dev30 > 0 and dev60 > 0:
                    sub = "S2突破"
                elif dev30 < 0 and dev60 < 0:
                    sub = "S3"
                else:
                    sub = "S1末临界"
            else:
                if dev30 > 0 and dev60 > 0 and chg20 > 0:
                    sub = "S2尝试"
                else:
                    sub = "S3"
        else:
            if p > ma120:
                sub = "S1末" if (dev250 > DEV_NEAR and chg20 > 0) else "S1"
            else:
                sub = "S4"

        out.append(dict(
            date=dates[i], close=round(p, 2), volume=vols[i],
            ma30=round(ma30, 2), ma60=round(ma60, 2),
            ma120=round(ma120, 2), ma250=round(ma250, 2),
            slope_5w=round(slope_5w, 2), dev30=round(dev30, 1), dev60=round(dev60, 1),
            dev120=round(dev120, 1), dev250=round(dev250, 1),
            chg20=round(chg20, 2), chg60=round(chg60, 2), rs60=round(rs60, 1),
            dist52=round(dist52, 1), align=bool(align),
            sub=sub, stage=SUB_TO_STAGE[sub],
        ))
    return out[-keep:]


def extract_transitions(series, min_hold=1):
    """从逐日序列提取阶段切换事件（按细分态变化，含降噪）。
    min_hold: 同一新状态至少连续出现 N 天才记为切换（1=原始逐日，3=确认口径）"""
    trans = []
    prev = None
    for idx, d in enumerate(series):
        cur = d["sub"]
        if cur == prev:
            continue
        if min_hold > 1:
            nxt = series[idx:idx + min_hold]
            if len(nxt) < min_hold or any(x["sub"] != cur for x in nxt):
                continue
        trans.append(dict(date=d["date"], frm=prev, to=cur,
                          frm_stage=SUB_TO_STAGE[prev] if prev else None,
                          to_stage=d["stage"], close=d["close"]))
        prev = cur
    return trans


def confirmed_sub(series, n=3):
    """UI 徽标用：当前状态需连续 n 个交易日成立，否则回退到上一个稳定状态"""
    if not series:
        return None
    cur = series[-1]["sub"]
    tail = series[-n:]
    if len(tail) == n and all(x["sub"] == cur for x in tail):
        return cur
    for d in reversed(series[:-1]):
        if d["sub"] != cur:
            return d["sub"]
    return cur
