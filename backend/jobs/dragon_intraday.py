"""擒龙盘中验证（腾讯快照，零东财请求）

用法（在 backend/ 目录下）：
    python -m jobs.dragon_intraday --slot open     # 09:35 开盘验证
    python -m jobs.dragon_intraday --slot mid      # 10:00 验证
    python -m jobs.dragon_intraday --slot close    # 14:45 尾盘验证

只回答一个问题：上一交易日潜龙池里的票，今天此刻是「封板 / 炸板 / 走弱 /
未表态」。全部走腾讯 TencentSource.snapshots()（一次 60 只批量），绝不调用
evidence 的任何东财函数——盘中三个时点碰东财就会把你最头疼的限流再引回来。

结果按 slot 写进 data/dragon/intraday-{date}.json 的 slots 字典，三个时点
共存一份文件，前端可以画晋级轨迹。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from engines import dragon                                              # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=("open", "mid", "close"), required=True)
    ap.add_argument("--date", help="交易日 YYYY-MM-DD，缺省今天")
    ap.add_argument("--pool-date", help="指定潜龙池日期，缺省最近一期")
    args = ap.parse_args(argv)

    today = args.date or datetime.now().strftime("%Y-%m-%d")
    try:
        r = dragon.intraday_check(args.slot, today, args.pool_date, verbose=True)
        print(f"   池 {r['pool_date']} · 情绪 {r['sentiment']['regime']} "
              f"· 来源 {r['source']}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"! 盘中验证失败：{type(e).__name__}: {e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
