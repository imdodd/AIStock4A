"""持仓盈亏汇总 + 每日盈亏历史。

口径（金额单位：元），与前端 prototype/index.html 的 calcPnlSummary() 保持一致：

    今日盈亏 = Σ (现价 − 昨收) × 数量      昨收 = 现价 / (1 + 当日涨跌幅/100)
             但「新建仓日」例外：added_at 日期 ≥ 数据基准日（as_of，缺省取今天）
             的持仓按 (现价 − 成本) × 数量 起算 —— 当天买入的仓位昨收与你无关，
             不能把加入前的涨跌记成你的今日盈亏（day_basis 标记 cost / prev_close）
    持仓盈亏 = Σ (现价 − 成本) × 数量      当前持仓的未实现浮动盈亏
    累计盈亏 = 持仓盈亏 + 已清仓已实现盈亏  删除持仓时按当时现价结转进 realized 台账

- 只统计**填了数量**的持仓；没填数量的股票不编造股数，只用「等权平均涨跌幅 /
  平均收益率」给一个整体参考，绝不混进金额合计。
- 删除持仓 ≠ 盈亏蒸发：DELETE /api/holdings/{code} 会把该持仓按删除时刻的
  (现价 − 成本) × 数量 写进 data/portfolio/realized.json（已实现台账），
  此后「持仓盈亏」变小、「累计盈亏」保持不变。
- 历史按交易日一天一条落盘，从首次运行开始累积。同日重跑覆盖当天记录，
  不做「用今天的持仓数量倒推过去」的模拟（那样得到的是假历史）。

数据源：/api/holdings 的实时视图或 jobs/build_snapshot.py 产出的
data/snapshot/holdings.json，两者字段一致（price / chg / cost / quantity / pnl）。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(DATA_DIR, "portfolio")
HISTORY_PATH = os.path.join(DIR, "pnl_daily.json")
REALIZED_PATH = os.path.join(DIR, "realized.json")
NOTES_PATH = os.path.join(DIR, "realized_notes.json")   # 清仓交易笔记，独立文件不污染台账
KEEP_DAYS = 120                      # 约半年交易日，够画趋势又不至于无限膨胀

_lock = threading.RLock()


# ---------------------------------------------------------------- 基础计算
def _num(v):
    """宽松转 float；None / '' / NaN / inf 一律当缺值"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def prev_close_of(price, chg):
    """由现价与当日涨跌幅(%)反推昨收；涨跌幅缺失或异常时返回 None"""
    p, c = _num(price), _num(chg)
    if p is None or c is None or c <= -100:
        return None
    return p / (1 + c / 100.0)


def row_pnl(row: dict, ref_date: str | None = None) -> dict:
    """单只持仓的盈亏分解。

    ref_date：数据基准日（快照/历史记录对应的交易日）。

    新建仓判定用「加入日 vs 行情所属交易日」，优先级：
        1) row["as_of"] —— 该股 K 线最后一根的日期，最精确（停牌股也能各自判断）
        2) ref_date     —— 调用方给的快照日期
        3) 本地今天     —— 前两者都没有时的兜底
    added_at 日期 ≥ 基准日 → 视为当日/盘后新建仓，今日盈亏按成本价起算。

    为什么不能用「本地今天」当基准：周末与节假日里 chg 仍是上一个交易日的涨幅。
    例如周五收盘涨停、周六晚登记持仓（成本=周五收盘价），到周日一看，若拿自然日
    去比，周六 < 周日 → 判成老仓 → 把周五那根涨停算成"今日盈亏"，凭空多出几千块。
    """
    price = _num(row.get("price"))
    qty = _num(row.get("quantity"))
    cost = _num(row.get("cost"))
    chg = _num(row.get("chg"))
    prev = prev_close_of(price, chg)
    has_qty = bool(price is not None and qty is not None and qty > 0)
    entry = str(row.get("added_at") or "")[:10] or None
    ref = (str(row.get("as_of") or "")[:10]
           or (str(ref_date)[:10] if ref_date else "")
           or datetime.now().strftime("%Y-%m-%d"))
    new_today = bool(entry and entry >= ref and cost is not None)
    if has_qty and new_today:
        day, day_basis = (price - cost) * qty, "cost"      # 新建仓：成本价起算
    elif has_qty and prev is not None:
        day, day_basis = (price - prev) * qty, "prev_close"
    else:
        day, day_basis = None, None
    total = (price - cost) * qty if has_qty and cost is not None else None
    return {
        "code": row.get("code"),
        "name": row.get("name") or row.get("code"),
        "price": price,
        "prev_close": round(prev, 4) if prev is not None else None,
        "chg": chg,
        "cost": cost,
        "quantity": qty,
        "added_at": row.get("added_at"),
        "as_of": ref,                 # 实际用到的基准日，便于核对「今日」到底是哪一天
        "has_qty": has_qty,
        "day_basis": day_basis,
        "day_pnl": round(day, 2) if day is not None else None,
        "total_pnl": round(total, 2) if total is not None else None,
        "market_value": round(price * qty, 2) if has_qty else None,
        "cost_value": round(cost * qty, 2) if has_qty and cost is not None else None,
    }


def summarize(rows: list[dict], as_of: str | None = None) -> dict:
    """整组持仓的当日 / 累计盈亏汇总"""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    items = [row_pnl(r, ref_date=as_of) for r in rows]
    covered = [x for x in items if x["has_qty"]]

    def _sum(key):
        vals = [x[key] for x in covered if x[key] is not None]
        return round(sum(vals), 2) if vals else 0.0

    day, total = _sum("day_pnl"), _sum("total_pnl")
    mv, cv = _sum("market_value"), _sum("cost_value")
    realized = round(sum(r["realized_pnl"] for r in load_realized()
                         if r.get("realized_pnl") is not None), 2)
    prev_mv = mv - day
    chgs = [_num(r.get("chg")) for r in rows]
    chgs = [c for c in chgs if c is not None]
    rates = [_num(r.get("pnl")) for r in rows]
    rates = [c for c in rates if c is not None]

    return {
        "as_of": as_of,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "day_pnl": day,
        "total_pnl": total,          # = 持仓盈亏（未实现），字段名保留兼容旧前端
        "realized_pnl": realized,    # 已清仓已实现盈亏合计
        "all_total": round(total + realized, 2),   # 累计盈亏 = 持仓 + 已实现
        "market_value": mv,
        "cost_value": cv,
        "day_pct": round(day / prev_mv * 100, 2) if prev_mv else None,
        "total_pct": round(total / cv * 100, 2) if cv else None,
        "covered": len(covered),
        "total": len(rows),
        "missing_qty": [x["code"] for x in items if not x["has_qty"]],
        # 全持仓等权参考：没填数量也能看到组合整体方向，明确不是金额
        "avg_chg": round(sum(chgs) / len(chgs), 2) if chgs else None,
        "avg_pnl": round(sum(rates) / len(rates), 2) if rates else None,
        "items": items,
    }


# ---------------------------------------------------------------- 每日历史
def load_history() -> list[dict]:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    return sorted((r for r in rows if isinstance(r, dict) and r.get("date")),
                  key=lambda r: r["date"])


def history(limit: int = 10) -> list[dict]:
    return load_history()[-max(1, min(int(limit or 10), 120)):]


def _slim_item(x: dict) -> dict:
    """每日记录里只留画日历/看贡献要用的字段，别把整个 row 塞进历史文件"""
    return {
        "code": x.get("code"), "name": x.get("name"),
        "day_pnl": x.get("day_pnl"), "total_pnl": x.get("total_pnl"),
        "chg": x.get("chg"), "price": x.get("price"),
        "quantity": x.get("quantity"), "day_basis": x.get("day_basis"),
    }


def record(rows: list[dict], date: str | None = None) -> dict:
    """把当日汇总 + 个股明细写入历史；同一天重跑覆盖，保留最近 KEEP_DAYS 条

    items 是「当天各持仓股赚了多少」的快照。历史盈亏无法用今天的持仓数量倒推，
    所以只在当天记一次；早期记录只有汇总、没有 items，calendar() 会如实标成无明细。
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    s = summarize(rows, as_of=date)
    rec = {
        "date": date,
        "generated_at": s["generated_at"],
        "day_pnl": s["day_pnl"],
        "total_pnl": s["total_pnl"],
        "realized_pnl": s["realized_pnl"],
        "all_total": s["all_total"],
        "market_value": s["market_value"],
        "cost_value": s["cost_value"],
        "covered": s["covered"],
        "total": s["total"],
        "items": [_slim_item(x) for x in s["items"]],
    }
    with _lock:
        os.makedirs(DIR, exist_ok=True)
        hist = [r for r in load_history() if r.get("date") != date]
        hist.append(rec)
        hist.sort(key=lambda r: r["date"])
        hist = hist[-KEEP_DAYS:]
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
        os.replace(tmp, HISTORY_PATH)
    return rec


def calendar(month: str | None = None, rows: list[dict] | None = None,
             as_of: str | None = None) -> dict:
    """按月返回每日盈亏（含当日个股明细），供日历热力图使用。

    month 形如 2026-11，缺省取最新一条记录所在月份。
    rows 只在「当天已落汇总、但 EOD 还没写明细」时用于实时补齐 —— 也就是刚收盘
    那段时间点开日历也能看到个股拆分，不必等到夜里 EOD 跑完。
    """
    daily = load_history()
    if not month:
        month = (daily[-1]["date"][:7] if daily else datetime.now().strftime("%Y-%m"))
    month = str(month)[:7]
    days = []
    for r in daily:
        if (r.get("date") or "")[:7] != month:
            continue
        cell = dict(r)
        cell["items"] = [x for x in (r.get("items") or []) if isinstance(x, dict)]
        days.append(cell)

    # 实时补最新一天：只有当这条记录就是今天、且确实还没写明细时才补
    if days and rows and not days[-1]["items"]:
        today = datetime.now().strftime("%Y-%m-%d")
        if days[-1].get("date") == today:
            s = summarize(rows, as_of=as_of or today)
            days[-1]["items"] = [_slim_item(x) for x in s["items"]]
            days[-1]["detail_live"] = True

    # 明细从哪一天开始有的：前端据此解释早期日期为什么点开是空的。
    # 一天都还没落过（首次上线当天）就按今天算 —— 下一轮 EOD 就会写进去。
    detail_from = next((r["date"] for r in daily if r.get("items")), None)
    detail_from = detail_from or datetime.now().strftime("%Y-%m-%d")
    months = sorted({(r.get("date") or "")[:7] for r in daily if r.get("date")})
    month_pnl = round(sum((r.get("day_pnl") or 0) for r in days), 2)
    wins = sum(1 for r in days if (r.get("day_pnl") or 0) > 0)
    pick = lambda r: ({"date": r.get("date"), "day_pnl": r.get("day_pnl")} if r else None)  # noqa: E731
    return {
        "month": month,
        "days": days,
        "months": [m for m in months if m],
        "detail_from": detail_from,
        "detail_days": sum(1 for r in days if r.get("items")),
        "month_pnl": month_pnl,
        "win_days": wins,
        "trade_days": sum(1 for r in days if r.get("day_pnl") is not None),
        "best": pick(max(days, key=lambda r: r.get("day_pnl") or 0)) if days else None,
        "worst": pick(min(days, key=lambda r: r.get("day_pnl") or 0)) if days else None,
    }


# ---------------------------------------------------------------- 已实现台账
def load_realized() -> list[dict]:
    """已清仓标的的已实现盈亏台账（删除持仓时写入，一条一次结转）"""
    if not os.path.exists(REALIZED_PATH):
        return []
    try:
        with open(REALIZED_PATH, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("code")]


def realized_total() -> float:
    return round(sum(r["realized_pnl"] for r in load_realized()
                     if isinstance(r.get("realized_pnl"), (int, float))), 2)


def realized_summary() -> dict:
    """清仓股台账：逐条记录 + 按股票聚合。

    「清仓」有两种情形都会进这张表：手动删除持仓、止损触发后清仓。
    同一只股票可能反复进出，所以既给逐条流水（entries，按日期倒序），
    也给按代码合并的战绩（stocks）—— 后者才是判断"这只票到底赚没赚"的口径。
    """
    rows = load_realized()
    notes = load_notes()
    entries = []
    for r in rows:
        p, c, q = _num(r.get("price")), _num(r.get("cost")), _num(r.get("quantity"))
        amt = _num(r.get("realized_pnl"))
        buy_date = (str(r.get("buy_date"))[:10] if r.get("buy_date") else None)
        sell_date = str(r.get("date") or "")[:10] or None
        hold_days = None
        if buy_date and sell_date:
            try:
                from datetime import date as _date
                hold_days = (_date.fromisoformat(sell_date) - _date.fromisoformat(buy_date)).days
            except ValueError:
                hold_days = None
        key = entry_key(r)
        entries.append({
            "key": key,
            "date": sell_date,                       # 卖出日期
            "buy_date": buy_date,                    # 买入日期（老记录可能为空）
            "code": r.get("code"),
            "name": r.get("name") or r.get("code"),
            "price": p,                              # 卖出价格
            "cost": c,                               # 买入价格（均价）
            "quantity": q,                           # 买入数量 = 卖出数量
            "realized_pnl": amt,
            # 收益率按买卖价算，与股数无关；成本缺失时不编造
            "ret_pct": round((p / c - 1) * 100, 2) if (p is not None and c) else None,
            "cost_amount": round(c * q, 2) if (c is not None and q is not None) else None,
            "sell_amount": round(p * q, 2) if (p is not None and q is not None) else None,
            "hold_amount": round(p * q, 2) if (p is not None and q is not None) else None,
            "hold_days": hold_days,
            "note": (notes.get(key) or {}).get("note"),
            "recorded_at": r.get("recorded_at"),
        })
    entries.sort(key=lambda x: (x.get("date") or "", x.get("recorded_at") or ""), reverse=True)

    bag: dict[str, dict] = {}
    for e in entries:
        k = e["code"] or "-"
        cell = bag.setdefault(k, {
            "code": e["code"], "name": e["name"], "times": 0,
            "realized_pnl": 0.0, "quantity": 0.0, "cost_amt": 0.0,
            "sell_amt": 0.0, "last_date": None, "dates": [],
        })
        cell["times"] += 1
        cell["last_date"] = max(cell["last_date"] or "", e.get("date") or "")
        cell["dates"].append(e.get("date"))
        if e["realized_pnl"] is not None:
            cell["realized_pnl"] += e["realized_pnl"]
        if e["quantity"] is not None:
            cell["quantity"] += e["quantity"]
        if e["cost"] is not None and e["quantity"] is not None:
            cell["cost_amt"] += e["cost"] * e["quantity"]
        if e["price"] is not None and e["quantity"] is not None:
            cell["sell_amt"] += e["price"] * e["quantity"]

    stocks = []
    for cell in bag.values():
        cell["realized_pnl"] = round(cell["realized_pnl"], 2)
        # 累计收益率 = 总卖出金额 / 总成本金额 − 1（跨多次进出，按金额加权才对）
        cell["ret_pct"] = (round((cell["sell_amt"] / cell["cost_amt"] - 1) * 100, 2)
                           if cell["cost_amt"] else None)
        cell["cost_amt"] = round(cell["cost_amt"], 2)
        cell["sell_amt"] = round(cell["sell_amt"], 2)
        cell["dates"] = sorted({d for d in cell["dates"] if d})
        stocks.append(cell)
    stocks.sort(key=lambda x: x["realized_pnl"], reverse=True)

    amts = [e["realized_pnl"] for e in entries if e["realized_pnl"] is not None]
    win = [a for a in amts if a > 0]
    loss = [a for a in amts if a < 0]
    return {
        "entries": entries,
        "stocks": stocks,
        "total": round(sum(amts), 2),
        "count": len(entries),
        "wins": len(win),
        "losses": len(loss),
        "win_rate": round(len(win) / len(amts) * 100, 1) if amts else None,
        "gross_win": round(sum(win), 2),
        "gross_loss": round(sum(loss), 2),
        "stock_count": len(stocks),
    }


def entry_key(e: dict) -> str:
    """台账条目的稳定标识：代码 | 清仓日 | 入库时间。

    同一只股票同一天清仓两次也能区分开（recorded_at 精确到秒），
    用作交易笔记的键 —— 笔记单独存，不动台账本身。
    """
    return "|".join(str(e.get(k) or "") for k in ("code", "date", "recorded_at"))


def load_notes() -> dict:
    if not os.path.exists(NOTES_PATH):
        return {}
    try:
        with open(NOTES_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def save_note(key: str, note: str | None) -> dict | None:
    """写/删一条交易笔记。key 必须能在台账里找到，防止写进无主孤键。"""
    if not any(entry_key(e) == key for e in load_realized()):
        return None
    with _lock:
        os.makedirs(DIR, exist_ok=True)
        d = load_notes()
        text = (note or "").strip()
        if text:
            d[key] = {"note": text, "updated_at": datetime.now().isoformat(timespec="seconds")}
        else:
            d.pop(key, None)          # 清空即删除，别留一堆空键
        tmp = NOTES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, NOTES_PATH)
    return {"key": key, "note": text}


def record_realized(code: str, name: str | None, price, cost, quantity,
                    date: str | None = None, buy_date: str | None = None) -> dict | None:
    """删除持仓时把 (现价 − 成本) × 数量 结转为已实现盈亏。

    数量 / 成本 / 现价缺任何一项就只记审计条目（realized_pnl=None），
    不影响累计盈亏金额 —— 算不出来就不装作有。
    buy_date 取持仓的 added_at，用来算持有天数与买入日期；老记录没有这个字段时为 None。
    """
    p, c, q = _num(price), _num(cost), _num(quantity)
    amount = (p - c) * q if (p is not None and c is not None
                             and q is not None and q > 0) else None
    entry = {
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "code": code,
        "name": name or code,
        "realized_pnl": round(amount, 2) if amount is not None else None,
        "price": p,
        "cost": c,
        "quantity": q,
        "buy_date": (str(buy_date)[:10] if buy_date else None),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        os.makedirs(DIR, exist_ok=True)
        rows = load_realized()
        rows.append(entry)
        tmp = REALIZED_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        os.replace(tmp, REALIZED_PATH)
    return entry


# ---------------------------------------------------------------- 统计（按日 / 按月）
def stats() -> dict:
    """按日 / 按月盈亏统计。

    - daily：全部每日记录（一天一条，最多 KEEP_DAYS 条）
    - monthly：按自然月聚合。月盈亏 = Σ 当月每日盈亏 + Σ 当月已实现结转，
      另给月末的持仓盈亏 / 累计盈亏快照。
    """
    daily = load_history()
    realized = load_realized()
    months: dict[str, dict] = {}
    for r in daily:
        m = (r.get("date") or "")[:7]
        if not m:
            continue
        cell = months.setdefault(m, {"month": m, "days": 0, "day_pnl": 0.0,
                                     "realized": 0.0, "end_date": None,
                                     "end_total_pnl": None, "end_all_total": None})
        cell["days"] += 1
        cell["day_pnl"] += r.get("day_pnl") or 0.0
        cell["end_date"] = r.get("date")
        cell["end_total_pnl"] = r.get("total_pnl")
        # 月末累计：当日记录自带 realized_pnl（新记录）就用它；旧记录没有就用台账当前合计
        rec_realized = r.get("realized_pnl")
        if rec_realized is None:
            rec_realized = realized_total()
        cell["end_all_total"] = round((r.get("total_pnl") or 0) + rec_realized, 2)
    for r in realized:
        m = (r.get("date") or "")[:7]
        if m in months and r.get("realized_pnl") is not None:
            months[m]["realized"] += r["realized_pnl"]
    monthly = []
    for m in sorted(months):
        cell = months[m]
        cell["day_pnl"] = round(cell["day_pnl"], 2)
        cell["realized"] = round(cell["realized"], 2)
        cell["pnl"] = round(cell["day_pnl"] + cell["realized"], 2)
        monthly.append(cell)
    return {
        "realized_total": realized_total(),
        "realized_entries": realized,
        "daily": daily,
        "monthly": monthly,
    }


if __name__ == "__main__":
    # 手动补当天一条：python -m pnl（在 server 目录执行）
    snap = os.path.join(DATA_DIR, "snapshot")
    holds = json.load(open(os.path.join(snap, "holdings.json"), encoding="utf-8"))
    m = json.load(open(os.path.join(snap, "market.json"), encoding="utf-8"))
    print(json.dumps(record(holds, date=m.get("date")), ensure_ascii=False))
