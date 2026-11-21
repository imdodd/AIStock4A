"""利弗莫尔 Market Key 六档记录引擎（工程化口径）

原书（How to Trade in Stocks, 1940）把记录纸分成六列：
  上升侧（黑墨）：次级回升 Secondary Rally / 自然回升 Natural Rally / 上升趋势 Upward Trend
  下降侧（红墨）：次级回撤 Secondary Reaction / 自然回撤 Natural Reaction / 下降趋势 Downward Trend

原书只给了三条原则：幅度不足 6 点不记（噪音过滤）、换列即在离开那列划线（关键点）、
逆向波动要分「自然」与「次级」两档。**但它没有给「自然 vs 次级」的可执行判据**，
这正是本模块要解决的建模问题，为此实现两种候选口径供对照：

  pos（位置口径）：创新高/新低才进趋势列；逆向高点越过上一个同向摆动点 → 自然，否则次级
  amp（幅度口径）：创新高/新低才进趋势列；逆向幅度 >= 2 x thr → 自然，>= thr → 次级

共同基础：
  1. 摆动点 ZigZag，thr = K x ATR20（逐日自适应）
  2. 幅度 < thr 的逆向波动 → 噪音，不占列
  3. 每次换列，离开那列的最后一条价格划线 → 该列关键点
"""
from __future__ import annotations

from .stage import _atr

ATR_WIN = 20
ATR_K = 1.2
AMP_K = 2.0

SEC_UP, NAT_UP, UP = "次级回升", "自然回升", "上升趋势"
SEC_DN, NAT_DN, DN = "次级回撤", "自然回撤", "下降趋势"
NOISE = "噪音"

ALL_COLS = (SEC_UP, NAT_UP, UP, SEC_DN, NAT_DN, DN, NOISE)
MODES = ("pos", "amp")


def _thr(rows, i, k):
    a = _atr(rows, ATR_WIN, i)
    if not a or a <= 0:
        return rows[i]["close"] * 0.02
    return k * a


def zigzag(rows, k=ATR_K):
    """提取摆动点 [{idx,date,price,kind:'H'|'L',thr,tentative?}]"""
    n = len(rows)
    out = []
    if n < ATR_WIN + 2:
        return out

    thr = [_thr(rows, i, k) for i in range(n)]
    hi = lo = 0
    dirn = 0
    for i in range(1, n):
        p = rows[i]["close"]
        t = thr[i]
        if dirn == 0:
            if p > rows[hi]["close"]:
                hi = i
            if p < rows[lo]["close"]:
                lo = i
            if hi > lo and p >= rows[lo]["close"] + t:
                out.append(dict(idx=lo, date=rows[lo]["date"], price=rows[lo]["close"], kind="L", thr=thr[lo]))
                dirn, hi = 1, i
            elif hi > lo and p <= rows[hi]["close"] - t:
                out.append(dict(idx=hi, date=rows[hi]["date"], price=rows[hi]["close"], kind="H", thr=thr[hi]))
                dirn, lo = -1, i
        elif dirn == 1:
            if p > rows[hi]["close"]:
                hi = i
            elif p <= rows[hi]["close"] - t:
                out.append(dict(idx=hi, date=rows[hi]["date"], price=rows[hi]["close"], kind="H", thr=thr[hi]))
                dirn, lo = -1, i
        else:
            if p < rows[lo]["close"]:
                lo = i
            elif p >= rows[lo]["close"] + t:
                out.append(dict(idx=lo, date=rows[lo]["date"], price=rows[lo]["close"], kind="L", thr=thr[lo]))
                dirn, hi = 1, i

    # 收尾：把当前方向尚未被反向确认的极值也输出（tentative），
    # 否则最后一段单边行情会整段丢失，导致「当前列/趋势」停留在旧状态。
    if dirn == 1 and not (out and out[-1]["kind"] == "H" and out[-1]["idx"] == hi):
        out.append(dict(idx=hi, date=rows[hi]["date"], price=rows[hi]["close"], kind="H",
                        thr=thr[hi], tentative=True))
    elif dirn == -1 and not (out and out[-1]["kind"] == "L" and out[-1]["idx"] == lo):
        out.append(dict(idx=lo, date=rows[lo]["date"], price=rows[lo]["close"], kind="L",
                        thr=thr[lo], tentative=True))
    return out


def _is_natural(barrier, val, amp, t, mode, amp_k, up=True):
    """逆向波动是否「推进了结构」→ 自然级；否则次级。
    pos 口径：越过本轮逆向波动的极值即自然（barrier=None 表示本轮第一波，直接算自然）
    amp 口径：逆向幅度 >= amp_k x thr 即自然
    """
    if mode == "pos":
        if barrier is None:
            return True
        return val > barrier if up else val < barrier
    return amp >= amp_k * t


def build_records(rows, swings, mode="pos", k=ATR_K, amp_k=AMP_K):
    """逐段归档。返回 (records, state)

    趋势确认用「双向确认线」：
      key_up = 最近一次创新高那一段的起点低点 → 跌破即确认转跌
      key_dn = 最近一次创新低那一段的起点高点 → 突破即确认转涨
    这样确认线随趋势推进自动上移/下移，等价于原书「主趋势列留下的关键点」。
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")

    recs = []
    counts = {c: 0 for c in ALL_COLS}
    if len(swings) < 2:
        return recs, dict(mode=mode, col=None, trend=None, counts=counts, k=k, amp_k=amp_k)

    trend = None
    up_ext = dn_ext = None      # 本轮趋势极值
    key_up = key_dn = None      # 趋势确认线
    rebound_high = None     # 本轮下降趋势中最高的反弹点
    reaction_low = None     # 本轮上升趋势中最低的回撤点
    cur_col = None

    for j in range(len(swings) - 1):
        a, b = swings[j], swings[j + 1]
        amp = abs(b["price"] - a["price"])
        t = b["thr"]
        barrier = key_mark = None
        col_from = cur_col
        reversed_ = False

        if b["kind"] == "H":                        # ---- 向上段 ----
            H = b["price"]
            if trend == "down":
                if key_dn is not None and H > key_dn:
                    col, key_mark = UP, key_dn
                    trend, up_ext, key_up, key_dn = "up", H, a["price"], None
                    reversed_ = True
                else:
                    barrier = rebound_high if mode == "pos" else round(amp_k * t, 3)
                    col = NAT_UP if _is_natural(rebound_high, H, amp, t, mode, amp_k, True) else SEC_UP
                    rebound_high = H if rebound_high is None else max(rebound_high, H)
            elif trend == "up":
                if up_ext is None or H > up_ext:
                    col, key_mark = UP, a["price"]
                    up_ext, key_up = H, a["price"]
                else:
                    # 上升趋势中出现「反弹不创新高」= 买盘衰竭，属次级回升
                    barrier = up_ext
                    col = SEC_UP
            else:
                col, key_mark = UP, a["price"]
                trend, up_ext, key_up = "up", H, a["price"]
        else:                                       # ---- 向下段 ----
            L = b["price"]
            if trend == "up":
                if key_up is not None and L < key_up:
                    col, key_mark = DN, key_up
                    trend, dn_ext, key_dn, key_up = "down", L, a["price"], None
                    reversed_ = True
                    reaction_low, rebound_high = None, None
                else:
                    barrier = reaction_low if mode == "pos" else round(amp_k * t, 3)
                    col = NAT_DN if _is_natural(reaction_low, L, amp, t, mode, amp_k, False) else SEC_DN
                    reaction_low = L if reaction_low is None else min(reaction_low, L)
            elif trend == "down":
                if dn_ext is None or L < dn_ext:
                    col, key_mark = DN, a["price"]
                    dn_ext, key_dn = L, a["price"]
                else:
                    barrier = dn_ext
                    # 下降趋势中出现「回撤不创新低」= 卖盘衰竭，属次级回撤
                    col = SEC_DN
            else:
                col, key_mark = DN, a["price"]
                trend, dn_ext, key_dn = "down", L, a["price"]

        if amp < t:                                 # 噪音不占列
            col, key_mark = NOISE, None

        cur_col = col if col != NOISE else cur_col
        counts[col] += 1
        recs.append(dict(
            idx=b["idx"], date=b["date"], price=b["price"], kind=b["kind"],
            seg="up" if b["kind"] == "H" else "down",
            start_idx=a["idx"], start_date=a["date"], start_price=a["price"],
            amp=round(amp, 3), amp_pct=round(amp / a["price"] * 100, 2) if a["price"] else 0.0,
            thr=round(t, 3),
            barrier=round(barrier, 3) if isinstance(barrier, (int, float)) else None,
            col=col, changed=(col != col_from), reversed=reversed_,
            key_mark=round(key_mark, 3) if isinstance(key_mark, (int, float)) else None,
            trend_after=trend,
            up_ext=round(up_ext, 3) if up_ext else None,
            dn_ext=round(dn_ext, 3) if dn_ext else None,
        ))

    state = dict(mode=mode, col=cur_col, trend=trend, counts=counts, k=k, amp_k=amp_k,
                 up_ext=round(up_ext, 3) if up_ext else None,
                 dn_ext=round(dn_ext, 3) if dn_ext else None,
                 key_up=round(key_up, 3) if key_up else None,
                 key_dn=round(key_dn, 3) if key_dn else None,
                 rebound_high=round(rebound_high, 3) if rebound_high else None,
                 reaction_low=round(reaction_low, 3) if reaction_low else None)
    return recs, state


def analyze(rows, mode="pos", k=ATR_K, amp_k=AMP_K):
    sw = zigzag(rows, k)
    recs, state = build_records(rows, sw, mode=mode, k=k, amp_k=amp_k)
    return dict(swings=sw, records=recs, state=state)


def summary(state):
    c = state["counts"]
    n = sum(c.values()) or 1
    return dict(
        mode=state["mode"],
        当前列=state["col"],
        趋势="上升" if state["trend"] == "up" else ("下降" if state["trend"] == "down" else "—"),
        自然回升=c[NAT_UP], 次级回升=c[SEC_UP], 上升趋势=c[UP],
        自然回撤=c[NAT_DN], 次级回撤=c[SEC_DN], 下降趋势=c[DN], 噪音=c[NOISE],
        逆向总=c[NAT_UP] + c[SEC_UP] + c[NAT_DN] + c[SEC_DN],
        自然占比=round((c[NAT_UP] + c[NAT_DN]) / max(1, c[NAT_UP] + c[SEC_UP] + c[NAT_DN] + c[SEC_DN]) * 100, 1),
    )


# ---------------------------------------------------------------------------
# 按关键点确认的买卖点（严格 point-in-time：只用确认日当天已知的信息）
# ---------------------------------------------------------------------------
VOL_WIN = 50


def _vol_ratio(rows, i, win=VOL_WIN):
    """确认日成交量 / 前 win 日均量。原书强调关键点突破要有量能配合。"""
    lo = max(0, i - win)
    base = [r["volume"] for r in rows[lo:i] if r.get("volume")]
    if not base or not rows[i].get("volume"):
        return None
    m = sum(base) / len(base)
    return round(rows[i]["volume"] / m, 2) if m > 0 else None


def trade_signals(records, rows, win=VOL_WIN):
    """按「当时已确认的关键点」生成买卖点。

    规则（原书：换列即划线，价格越过那条线就是行动时刻）：
      · 下降趋势中突破 key_dn → 换入上升趋势列 → **买点 B**
      · 上升趋势中跌破 key_up → 换入下降趋势列 → **卖点 S**
    同级换列（自然/次级）只移动行动线，不产生买卖点。

    强调：全部按确认日结算（收盘价成交），不预知后续走势；
    因此允许出现「买了就跌、卖了又涨」的假信号，这正是要观察的成本。
    """
    sigs = []
    for r in records:
        if not r["changed"] or not r["reversed"]:
            continue
        if r["col"] == UP:
            t = "B"
        elif r["col"] == DN:
            t = "S"
        else:
            continue
        km = r["key_mark"]
        if km is None:
            continue
        # 关键点（key_mark）在段起点之前就已确定，是当时已知的唯一触发线。
        # 因此真正的行动日是「段内首次越过这条线的那一天」，而不是 ZigZag 确认极值的日子——
        # 后者往往已经离关键点很远（价格从 55 涨到 68 才确认前高，那时再买就晚了）。
        hit = None
        for i in range(r["start_idx"], r["idx"] + 1):
            c = rows[i]["close"]
            if (t == "B" and c > km) or (t == "S" and c < km):
                hit = i
                break
        i = hit if hit is not None else r["idx"]
        sigs.append(dict(
            idx=i, date=rows[i]["date"], type=t,
            price=round(rows[i]["close"], 2),
            trigger=km,                     # 被越过的那条关键点
            col=r["col"],
            vol_ratio=_vol_ratio(rows, i, win),
            lag=r["idx"] - i,               # 距摆动极值确认还有多少交易日（确认滞后度）
        ))
    return sigs


def pair_trades(sigs, rows=None, hard_stop=None):
    """把买卖点配成交易。

    hard_stop: 原书的硬止损纪律（如 0.10 = 亏损 10% 无条件离场）。
               给定时，若持仓期内收盘跌破买价 x (1-hard_stop)，提前在那天平仓并标 exit='stop'。
               原书原话：亏损绝不允许超过本金 10%，且永不摊平亏损。
    """
    trades, open_buy = [], None
    for s in sigs:
        if s["type"] == "B":
            if open_buy is None:
                open_buy = s
        else:
            if open_buy is not None:
                sell, exit_kind = s, "key"
                if hard_stop and rows:
                    line = open_buy["price"] * (1 - hard_stop)
                    for i in range(open_buy["idx"] + 1, s["idx"] + 1):
                        if rows[i]["close"] < line:
                            sell = dict(idx=i, date=rows[i]["date"],
                                        price=round(rows[i]["close"], 2),
                                        trigger=round(line, 2), type="S", col=s["col"],
                                        vol_ratio=_vol_ratio(rows, i), lag=0)
                            exit_kind = "stop"
                            break
                pnl = (sell["price"] / open_buy["price"] - 1) * 100 if open_buy["price"] else 0.0
                trades.append(dict(
                    kind="round", buy=open_buy, sell=sell, exit=exit_kind,
                    pnl=round(pnl, 2),
                    bars=sell["idx"] - open_buy["idx"],
                    days=(_d(sell["date"]) - _d(open_buy["date"])).days,
                    result="win" if pnl > 0 else "loss",
                ))
                open_buy = None
            else:
                trades.append(dict(kind="close_prev", sell=s))
    if open_buy is not None:
        trades.append(dict(kind="open", buy=open_buy))
    return trades


def _d(s):
    from datetime import date
    y, m, d = (int(x) for x in s.split("-"))
    return date(y, m, d)


def trade_stats(sigs, trades, rows):
    """交易统计：胜率、累计收益（复利）、以及同期买入持有的对照。"""
    closed = [t for t in trades if t["kind"] == "round"]
    wins = [t for t in closed if t["result"] == "win"]
    nav = 1.0
    for t in closed:
        nav *= (1 + t["pnl"] / 100)
    # 同期买入持有：从第一笔买点确认日收盘 到 最新收盘
    bh = None
    buys = [s for s in sigs if s["type"] == "B"]
    if buys:
        p0 = buys[0]["price"]
        p1 = rows[-1]["close"]
        bh = round((p1 / p0 - 1) * 100, 2) if p0 else None
    return dict(
        buys=len(buys), sells=len([s for s in sigs if s["type"] == "S"]),
        closed=len(closed), wins=len(wins), losses=len(closed) - len(wins),
        win_rate=round(len(wins) / len(closed) * 100, 1) if closed else None,
        cum=round((nav - 1) * 100, 2), buy_hold=bh,
        avg_win=round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else None,
        avg_loss=round(sum(t["pnl"] for t in closed if t["result"] == "loss")
                       / max(1, len(closed) - len(wins)), 2) if closed else None,
        holding="持仓中" if trades and trades[-1]["kind"] == "open" else "空仓",
    )
