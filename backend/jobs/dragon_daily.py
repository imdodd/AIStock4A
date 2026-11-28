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

from scheduler.calendar import (prev_trading_day, last_trading_day,          # noqa: E402
                                latest_known_trade_date)
from engines import dragon                                              # noqa: E402
from datasource import evidence                                          # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="交易日 YYYY-MM-DD，缺省取本地最近交易日（含当日）")
    ap.add_argument("--skip-pool", action="store_true", help="只验证，不建新池")
    args = ap.parse_args(argv)

    # 交易日取「本地日历最近交易日」，不取 snapshot/market.json 的行情基准日。
    # 快照 date 代表上游实际抓到的最新数据，EOD 抓数失败时会滞后（如 26 日 EOD
    # 因腾讯 K 线限流只跑到一半，快照仍停在 25 日）。若把它当 today，验证链会
    # 整体错后一天：用前天数据验昨天池子，还把昨天池子重复建一遍。
    today = args.date or last_trading_day().isoformat()
    prev = prev_trading_day(date.fromisoformat(today)).isoformat()
    print(f"[擒龙·盘后] 交易日 {today} · 上一交易日 {prev}", flush=True)

    # 数据就绪检查：快照日期落后于交易日 → 明确告警，不再静默错日。
    # 涨停池是闸门二的裁判数据源；若当日池为空（接口限流/未抓到），宁可跳过
    # 验证，也不要用空池把全部潜龙判成"未晋级"写坏 verdict。
    snap = latest_known_trade_date()
    if snap and snap < today:
        print(f"   ! 行情快照停在 {snap}，晚于交易日 {today}：EOD 可能未完成，"
              f"请确认当日涨停池与情绪已落盘。", flush=True)
    try:
        import json as _json
        _sent = _json.load(open(dragon.SENTIMENT_FILE, encoding="utf-8")) or {}
        sent_day = _sent.get("date")
        if sent_day and sent_day < today:
            print(f"   ! 情绪数据停在 {sent_day}，晚于交易日 {today}："
                  f"闸门二会用旧情绪判定冰点/热度，请确认 EOD 已刷新情绪。", flush=True)
    except Exception:  # noqa: BLE001 缺文件/解析失败由 sentiment_state 处理
        pass
    zt_today = evidence.zt_pool(today) or {}
    if not zt_today:
        print(f"   ! {today} 涨停池为空（接口限流或当日数据未落盘），"
              f"跳过闸门二验证，避免用空池误判全部潜龙退位。", flush=True)
    else:
        print(f"   当日涨停池 {len(zt_today)} 只（闸门二裁判数据就绪）", flush=True)

    # 闸门二：验证 prev 池在 today 的表现（涨停池空则跳过）
    if zt_today and dragon.load_pool(prev):
        try:
            v = dragon.verify_day(prev, today, verbose=True)
            print(f"   确认龙 {v['counts']['confirmed']} · 晋级未确认 {v['counts']['pending']} "
                  f"· 退位龙 {v['counts']['retired']} "
                  f"· 只观察 {v['counts']['observe']} · 情绪 {v['sentiment']['regime']}",
                  flush=True)
        except Exception as e:  # noqa: BLE001 单步失败不阻断建新池
            print(f"   ! 闸门二验证失败：{type(e).__name__}: {e}", flush=True)
    elif not zt_today:
        pass  # 上面已告警
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
