"""EOD 步骤 ⑧：把当日持仓盈亏写进每日历史（一天一条）。

读的是同一次 EOD ② 步骤 build_snapshot.py 刚产出的收盘快照，不额外抓行情：
    data/snapshot/holdings.json   price / chg / cost / quantity / pnl
    data/snapshot/market.json     data_date
同一交易日重跑会覆盖当天记录，历史保留最近 120 条。

手动补录：cd server && python -m jobs.snapshot_pnl
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # server/
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pnl  # noqa: E402

SNAP = os.path.join(DATA_DIR, "snapshot")


def snapshot_rows() -> list[dict]:
    p = os.path.join(SNAP, "holdings.json")
    if not os.path.exists(p):
        raise RuntimeError("缺少 data/snapshot/holdings.json，先跑 python -m jobs.build_snapshot")
    with open(p, encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        raise RuntimeError("holdings.json 结构不是列表")
    return rows


def snapshot_date() -> str:
    try:
        with open(os.path.join(SNAP, "market.json"), encoding="utf-8") as f:
            d = json.load(f).get("date")
        if d:
            return d
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d")


def main() -> dict:
    rows = snapshot_rows()
    rec = pnl.record(rows, date=snapshot_date())
    print(f"[持仓盈亏] {rec['date']} 今日 {rec['day_pnl']:+,.2f} 元 · "
          f"持仓 {rec['total_pnl']:+,.2f} 元 · "
          f"累计 {rec['all_total']:+,.2f} 元（含已实现 {rec['realized_pnl']:+,.2f}）· "
          f"覆盖 {rec['covered']}/{rec['total']} 只")
    print(json.dumps(rec, ensure_ascii=False))
    return rec


if __name__ == "__main__":
    main()
