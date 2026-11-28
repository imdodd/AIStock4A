"""给已落盘的 verdict 补「逐项计分明细」（score_items），零网络。

背景：四支柱原来只存 parts（四根柱各几分）和 reasons（文字理由），前端鼠标停在
柱子上只能给一句笼统规则。要显示「连板 3 板 → 18 分 / 炸板 1 次 → 5 分」这种
逐项公式，就得把每个小项的得分也落盘。新跑出来的 verdict 自带，历史文件用这个
脚本补。

关键约束：**只用能算回原分的组合**。明细是给人核对用的，如果重算总分和落盘的
score 不一致，说明当时的输入（板块资金排名、龙虎榜、题材涨停数）已经变了，宁可
不写，让前端退回「只显示规则」也不写一份和总分对不上的账。

用法：
  python -m jobs.backfill_dragon_score_items            # 全部 verdict
  python -m jobs.backfill_dragon_score_items --dry-run  # 只看匹配率
"""
from __future__ import annotations

import argparse
import json
import sys

from engines import dragon


def _item_for_score(vitem: dict, pitem: dict | None) -> dict:
    """用池子里的原始行 + verdict 留存的 metrics 反推打分入参。

    注意 verdict 只存了 `industry`（如「装修装饰Ⅱ」），而打分用的是池子行的
    `hybk`（「装修装饰」）——两个字段不是一回事，用 industry 顶替会让
    「题材空间板 +3」和「同题材涨停家数」全部对不上（实测 20 条高度/带动偏移
    全是这个原因）。所以优先拿回当天池子行，只把「验证日高度」和「验证日行情」
    换成 verdict 里的。

    封单额 fund 没单独存，用 seal_ratio = fund/10000/amount 反算回来
    （seal_ratio 是 round 过的，只算近似；算完核对总分，对不上就丢弃）。
    """
    m = vitem.get("metrics") or {}
    base = dict(pitem or {})
    if not base:
        base = {"code": vitem.get("code"), "name": vitem.get("name"),
                "hybk": vitem.get("industry"), "industry": vitem.get("industry"),
                "zbc": m.get("zbc"), "fbt": m.get("fbt")}
        q = dict(vitem.get("quote") or {})
        sr = m.get("seal_ratio")
        amount = q.get("amount")
        base["fund"] = (float(sr) * float(amount) * 10000
                        if sr is not None and amount else None)
        base["quote"] = q
    # 验证日口径：晋级后的连板数 + 验证当日行情（池子日那天的 quote 是旧的）
    if m.get("lbc") is not None:
        base["lbc"] = m.get("lbc")
    if vitem.get("quote"):
        base["quote"] = dict(vitem.get("quote") or {})
    base["code"] = vitem.get("code") or base.get("code")
    base["name"] = vitem.get("name") or base.get("name")
    return base


def _rebuild(vitem: dict, pool: dict, rank: list, with_flow: bool):
    pidx = {i.get("code"): i for i in (pool.get("items") or [])}
    item = _item_for_score(vitem, pidx.get(vitem.get("code")))
    tl = dragon._theme_limit_map(pool)
    dragon_arg, flow_arg = {}, {}
    if with_flow:
        dragon_arg = dict(vitem.get("dragon") or {})
        flow_arg = dict(vitem.get("flow") or {})
    s = dragon.score_one(item, zt_today={}, theme_limits=tl, sector_rank=rank,
                         dragon=dragon_arg, flow=flow_arg,
                         effective_lbc=(vitem.get("metrics") or {}).get("lbc"))
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计匹配率，不写盘")
    ap.add_argument("--days", type=int, default=30, help="回溯多少个 verdict")
    args = ap.parse_args(argv)

    vdates = dragon.dates("verdict", args.days)
    if not vdates:
        print("没有 verdict 可补")
        return 1
    rank = dragon._sector_rank()
    total_n = ok_n = 0
    for d in vdates:
        v = dragon.load_verdict(d) or {}
        pool = dragon.load_pool(v.get("pool_date") or d) or {}
        changed = 0
        for vi in v.get("items") or []:
            total_n += 1
            if vi.get("score_items"):
                ok_n += 1          # 已有明细，算已覆盖
                continue
            best = None
            for with_flow in (True, False):
                s = _rebuild(vi, pool, rank, with_flow)
                if s["total"] == vi.get("score"):
                    best = s
                    break
            if not best:
                continue
            vi["score_items"] = best["detail"]
            changed += 1
            ok_n += 1
        if changed and not args.dry_run:
            dragon._dump(dragon._path("verdict", d), v)
        print(f"{d}: 补 {changed} 条" + ("（dry-run 未写盘）" if args.dry_run else ""))
    print(f"合计 {ok_n}/{total_n} 条有明细（{ok_n / total_n:.0%}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
