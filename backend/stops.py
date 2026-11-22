"""持仓动态止损引擎（每日收盘后自动调整）。

用户口径：动态滚动止盈止损 —— 破位，或跌破成本线 8%，即离场。

三条规则，止损价取三者中"仍低于现价"的最大值（最保守的一条主导）：

  R1 cost_stop  成本底线   = 成本 × (1 - 8%)          —— 亏损硬纪律，永远有效
  R2 trail_stop 滚动止盈   = 持仓期最高收盘 × (1 - 8%) —— 浮盈后止损跟着最高价上移，只上不下
  R3 pivot_stop 关键破位线 = 最近一个有效枢轴价        —— Weinstein 框架的阶段支撑，
                              收盘跌破视为趋势破位（枢轴来自信号引擎 scan()，60 日高低转折）

判定：
  status = exit   今日收盘已 ≤ 止损线（或跌破枢轴）→ 触发离场提醒，注明依据
  status = warn   现价距止损线 < 3%                → 接近预警
  status = hold   正常持有

产出 data/portfolio/stops.json：
  { date, updated_at, items: {code: {...}}, history: {code: [{date, stop, close, rule, status}]} }
history 每天一条、保留 120 天，前端画轨迹、展示"止损只上不下"的过程。

每日调整入口：jobs/build_snapshot.py 尾部调 recompute()；
服务运行中数据过期时，/api/holdings 的懒更新也会现场重算（see main.py）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, date as _date

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_CHARTS = os.path.join(DATA_DIR, "snapshot", "charts")
STORE_PATH = os.path.join(DATA_DIR, "portfolio", "holdings.json")
OUT_PATH = os.path.join(DATA_DIR, "portfolio", "stops.json")

COST_DD = 0.08        # 跌破成本线 8%
TRAIL_DD = 0.08       # 滚动回撤容忍 8%（与成本线对称）
WARN_NEAR = 3.0       # 距止损 <3% 预警
HISTORY_KEEP = 120    # 轨迹保留天数

RULE_NAMES = {
    "cost": "成本底线",
    "trail": "滚动止盈",
    "pivot": "关键破位",
}


# ---------------------------------------------------------------- 基础
def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _chart(code: str) -> dict | None:
    ch = _load_json(os.path.join(SNAP_CHARTS, f"{code}.json"), None)
    if ch and ch.get("bars") and ch.get("dates"):
        return ch
    return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 单只计算
def compute_one(code: str, item: dict, chart: dict | None) -> dict | None:
    """对一只持仓计算动态止损。

    item: store 条目（cost / added_at / ...）
    chart: snapshot/charts/{code}.json（dates + bars[pivots]）
    """
    cost = _f(item.get("cost"))
    if not cost:
        return None
    close = _f((item.get("price") if isinstance(item.get("price"), (int, float)) else None))
    if chart is None:
        # 没有K线：只能给成本底线
        return {
            "stop": round(cost * (1 - COST_DD), 2), "rule": "cost",
            "status": "hold", "cost_stop": round(cost * (1 - COST_DD), 2),
            "trail_stop": None, "pivot_stop": None, "peak_close": None, "peak_date": None,
            "pivot": None, "pivot_date": None, "pivot_kind": None,
            "close": close, "distance_pct": None, "triggered": [],
            "rationale": [f"成本底线 {cost * (1 - COST_DD):.2f}（成本 {cost:.2f} 的 -{COST_DD * 100:.0f}%）；"
                          f"无K线数据，滚动止盈与破位线暂不可算"],
            "held_days": None, "bars_n": 0,
        }

    dates = chart["dates"]
    bars = chart["bars"]            # 列序 [open, close, low, high]（见 engines/chart.py chart_payload）
    closes = [float(b[1]) for b in bars]
    close = closes[-1]

    # 持仓期 = added_at 当天（含）之后的K线
    added = str(item.get("added_at") or "")[:10]
    try:
        add_d = _date.fromisoformat(added)
    except ValueError:
        add_d = None
    if add_d:
        idx0 = next((i for i, d in enumerate(dates) if d >= added), len(dates))
    else:
        idx0 = 0
    held_closes = closes[idx0:] if idx0 < len(closes) else closes[-1:]
    held_dates = dates[idx0:] if idx0 < len(dates) else dates[-1:]
    if not held_closes:                      # added_at 晚于最后K线（极端）：用最后一根兜底
        held_closes, held_dates = closes[-1:], dates[-1:]

    peak = max(held_closes)
    peak_date = held_dates[held_closes.index(peak)]

    cost_stop = cost * (1 - COST_DD)
    trail_stop = peak * (1 - TRAIL_DD)

    # 最近一个有效枢轴（买入日之后确认的优先；没有就用全序列最后一个）
    pivots = chart.get("pivots") or []
    pv = None
    for p in reversed(pivots):
        if add_d is None or str(p.get("date") or "")[:10] >= added:
            pv = p
            break
    if pv is None and pivots:
        pv = pivots[-1]
    pivot_stop = _f(pv.get("price")) if pv else None

    cands = {"cost": cost_stop, "trail": trail_stop}
    if pivot_stop:
        cands["pivot"] = pivot_stop

    # 触发判定：收盘已跌破的规则（纪律优先——破线就是破线，止损线不悄悄下移）
    triggered = sorted(
        [k for k, v in cands.items() if close <= v],
        key=lambda k: -cands[k],
    )
    active = {k: v for k, v in cands.items() if close > v}
    if triggered:
        # 收盘跌破最高那条线 → 触发离场提醒（用户可自行复核后决定执行）
        rule = triggered[0]
        stop = cands[rule]
        status = "exit"
    else:
        rule = max(active, key=lambda k: active[k])
        stop = active[rule]
        status = "warn" if (close - stop) / close * 100 < WARN_NEAR else "hold"

    # 依据文案
    rationale = [
        f"成本底线 {cost_stop:.2f}（成本 {cost:.2f} 的 -{COST_DD * 100:.0f}%）",
        f"滚动止盈 {trail_stop:.2f}（持仓期最高收盘 {peak:.2f}@{peak_date[5:]} 的 -{TRAIL_DD * 100:.0f}%）",
    ]
    if pivot_stop:
        rationale.append(f"破位线 {pivot_stop:.2f}（{pv.get('kind','')}枢轴 {str(pv.get('date',''))[:10]}，"
                         f"收盘跌破即趋势破位）")
    if triggered:
        why = "；".join(f"{RULE_NAMES.get(k, k)}线 {cands[k]:.2f}" for k in triggered)
        rationale.append(f"⚠ 今日收盘 {close:.2f} 已跌破：{why}")
    else:
        gap = (close - stop) / close * 100
        rationale.append(f"当前生效：{RULE_NAMES.get(rule, rule)} {stop:.2f}，距止损 {gap:.1f}%"
                         + ("，接近预警" if status == "warn" else ""))

    return {
        "stop": round(stop, 2), "rule": rule, "status": status,
        "cost_stop": round(cost_stop, 2), "trail_stop": round(trail_stop, 2),
        "pivot_stop": round(pivot_stop, 2) if pivot_stop else None,
        "peak_close": round(peak, 2), "peak_date": peak_date,
        "pivot": round(_f(pv.get("price")), 2) if pv else None,
        "pivot_date": str(pv.get("date") or "")[:10] if pv else None,
        "pivot_kind": pv.get("kind") if pv else None,
        "close": close,
        "distance_pct": round((close - stop) / close * 100, 2),
        "triggered": triggered,
        "rationale": rationale,
        "held_days": len(held_closes),
        "bars_n": len(bars),
    }


# ---------------------------------------------------------------- 全量重算
def recompute(force: bool = False, save: bool = True) -> dict:
    """对全部持仓重算止损；数据已是当日时可跳过（force=True 强制）。

    save=False 只算不落盘；落盘失败也不影响本次返回（读路径不该被写权限挡住）。
    """
    holdings = _load_json(STORE_PATH, [])
    out = _load_json(OUT_PATH, {})
    today = None
    items, history = {}, out.get("history") or {}
    # 交易日兜底：全新安装时部分持仓还没有 K 线缓存（_chart 返回 None），
    # today 会一直是 None，导致写进 history 的条目 date=null，前端 x.date.slice() 崩。
    # 用上一次成功的交易日 / 系统日期兜底，保证每条轨迹都有可渲染的日期。
    fallback_date = out.get("date") or datetime.now().strftime("%Y-%m-%d")
    # 清洗历史里已存在的无日期条目（老数据兼容）
    for _code in list(history):
        history[_code] = [x for x in (history[_code] or [])
                          if isinstance(x, dict) and x.get("date")]

    for it in holdings:
        code = it.get("code")
        if not code:
            continue
        ch = _chart(code)
        if ch and not today:
            today = ch["dates"][-1]
        r = compute_one(code, it, ch)
        if r:
            items[code] = r
            h = history.setdefault(code, [])
            entry_date = today or fallback_date
            entry = {"date": entry_date, "stop": r["stop"], "close": r["close"],
                     "rule": r["rule"], "status": r["status"]}
            if h and h[-1].get("date") == today:
                # 同一天重算（比如盘后补了成本价）：替换当天那条，不要再追加一行
                if h[-1].get("stop") != r["stop"] or h[-1].get("status") != r["status"]:
                    h[-1] = entry
            else:
                h.append(entry)
            history[code] = h[-HISTORY_KEEP:]

    data = {
        "date": today or fallback_date,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
        "history": history,
    }
    if save:
        try:
            os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
            with open(OUT_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception as e:
            # 文件被占用 / 只读 / 权限不足：本次结果照常返回，只是这一轮不落盘
            print(f"[stops] 落盘失败（本次结果仅在内存生效）：{type(e).__name__}: {e}")
    return data


def _needs_recompute(data: dict | None, holdings: list[dict]) -> bool:
    """落盘的止损结果是否已经和当前持仓清单对不上。

    收盘后算过一次，用户在盘后补成本价 / 新增持仓 / 删除持仓，都会让落盘结果过期：
    新加的票在 items 里根本没有条目，前端就会误报「未设置成本价」。
    """
    items = (data or {}).get("items") or {}
    codes = set()
    for it in holdings:
        code = it.get("code")
        if not code:
            continue
        codes.add(code)
        cost = _f(it.get("cost"))
        cur = items.get(code)
        if not cost:
            if cur:
                return True                 # 成本价被清空：旧条目要作废
            continue
        if not cur:
            return True                     # 新增持仓 / 刚补上成本
        if abs((_f(cur.get("cost_stop")) or 0.0) - cost * (1 - COST_DD)) > 0.011:
            return True                     # 成本价改过，止损线要跟着动
    if any(code not in codes for code in items):
        return True                         # 已删除持仓的残留条目
    return False


def load(current_trade_date: str | None = None, force: bool = False) -> dict:
    """读止损数据；落盘结果过期就自动重算。

    两个过期条件：① 交易日变了（每日动态调整的懒更新）；② 持仓清单/成本价变了
    （盘后补成本、加仓、删仓），见 _needs_recompute。
    """
    data = _load_json(OUT_PATH, None)
    stale = _needs_recompute(data, _load_json(STORE_PATH, []))
    if force or stale or not data or (current_trade_date and data.get("date") != current_trade_date):
        try:
            data = recompute()
        except Exception as e:
            print(f"[stops] 重算失败：{type(e).__name__}: {e}")
            data = data or {"date": None, "items": {}, "history": {}}
    return data


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    import sys
    d = recompute()
    print(f"交易日 {d['date']} · 更新 {d['updated_at']}")
    for code, r in d["items"].items():
        print(f"  {code:10s} 止损 {r['stop']:>8.2f} ({RULE_NAMES.get(r['rule'], r['rule'])}) "
              f"{r['status']:5s} 距离 {r['distance_pct'] if r['distance_pct'] is not None else '—'}% "
              f"现价 {r['close']}")
        for line in r["rationale"]:
            print(f"      {line}")
    sys.exit(0)
