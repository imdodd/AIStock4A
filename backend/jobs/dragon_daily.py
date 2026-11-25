"""擒龙盘后任务（闸门一建池 + 闸门二验证）

用法（在 backend/ 目录下）：
    python -m jobs.dragon_daily              # 正常盘后：验证上一交易日潜龙 + 建今日池
    python -m jobs.dragon_daily --date 2026-11-25   # 指定交易日（补跑/回测）
    python -m jobs.dragon_daily --skip-pool        # 只验证旧池，不建新池

执行顺序（必须）：
    1) verify_day(上一交易日, 今天)   —— 用今天的涨停池给昨天的潜龙打分定性
    2) build_pool(今天)               —— 今天涨停/触板/首板全收进新池，宽进

放在 EOD 之后跑（15:45）：EOD 15:30 起，抓数与异动扫描已落定，screener
和 sentiment.json 都是当天数据。涨停池走 evidence.zt_pool 的单次缓存，
龙虎榜/资金流只对打分 Top10 拉，全天东财新增请求 ≤ 11 次。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from scheduler.calendar import prev_trading_day, latest_known_trade_date  # noqa: E402
from engines import dragon                                              # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="交易日 YYYY-MM-DD，缺省取最新已知交易日")
    ap.add_argument("--skip-pool", action="store_true", help="只验证，不建新池")
    args = ap.parse_args(argv)

    today = args.date or latest_known_trade_date() or datetime.now().strftime("%Y-%m-%d")
    prev = prev_trading_day(date.fromisoformat(today)).isoformat()
    print(f"[擒龙·盘后] 交易日 {today} · 上一交易日 {prev}", flush=True)

    # 闸门二：验证 prev 池在 today 的表现
    if dragon.load_pool(prev):
        try:
            v = dragon.verify_day(prev, today, verbose=True)
            print(f"   确认龙 {v['counts']['confirmed']} · 退位龙 {v['counts']['retired']} "
                  f"· 只观察 {v['counts']['observe']} · 情绪 {v['sentiment']['regime']}",
                  flush=True)
        except Exception as e:  # noqa: BLE001 单步失败不阻断建新池
            print(f"   ! 闸门二验证失败：{type(e).__name__}: {e}", flush=True)
    else:
        print(f"   ○ 无 {prev} 潜龙池，跳过验证（首次运行）", flush=True)

    # 闸门一：建今天的新池（宽进）
    if not args.skip_pool:
        try:
            p = dragon.build_pool(today, verbose=True)
            print(f"   {today} 潜龙池 {p['count']} 只", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"   ! 闸门一建池失败：{type(e).__name__}: {e}", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
