"""标的清单（单一事实来源）

抓取任务与快照构建都从这里取名单，避免两处各写一份导致漏抓。
改动自选/持仓只需改这个文件。
"""
from __future__ import annotations

import json
import os

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(DATA_DIR, "raw")

INDEX_META = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
    "sh000300": "沪深300",
}

# 自选观察池
#
# 开源版不内置任何真实清单：自选以 data/watchlist.json 为准（前端「关注」页
# 可直接增删），本常量只作首次运行的空兜底。
# 想预置几只标的便于试跑：cp examples/watchlist.example.json data/watchlist.json
WATCHLIST: list[tuple[str, str]] = []

# 实际持仓
#
# 同样从代码里剥离：持仓以 data/portfolio/holdings.json 为准，首次运行由
# store.py 用本常量播种（空 = 空持仓，在前端「持仓」页添加即可）。
# 示例：cp examples/holdings.example.json data/portfolio/holdings.json
HOLDINGS: list[tuple[str, str]] = []


def watch_items() -> list[tuple[str, str]]:
    """自选代码清单：以用户维护的清单（watchstore）为准，读不到再回退到本文件常量。

    前端删除/新增自选后，每日抓取与快照构建会自动跟随，不必改代码。
    """
    try:
        import watchstore
        rows = watchstore.items()
        if rows:
            return rows
    except Exception:
        pass
    return list(WATCHLIST)


def sector_codes() -> list[str]:
    """124 个申万二级行业代码（pt*）"""
    p = os.path.join(RAW, "sectors_meta.json")
    if not os.path.exists(p):
        return []
    try:
        return list(json.load(open(p, encoding="utf-8")).keys())
    except Exception:
        return []


def signal_pool() -> list[str]:
    """历史抓过的个股（信号股池），保证关键点可回溯"""
    p = os.path.join(RAW, "klines.json")
    if not os.path.exists(p):
        return []
    try:
        store = json.load(open(p, encoding="utf-8"))
    except Exception:
        return []
    known = set(INDEX_META) | {c for c, _ in watch_items()} | {c for c, _ in HOLDINGS}
    return [c for c in store if not c.startswith("pt") and c not in known]


def hold_codes() -> list[str]:
    """持仓代码：以用户维护的清单（store）为准，读不到再回退到本文件的常量

    这样前端/接口里增删持仓后，每日抓取任务会自动覆盖到新加的标的。
    """
    try:
        import store as portfolio_store          # 延迟导入，避免模块级循环依赖
        codes = portfolio_store.codes()
        if codes:
            return codes
    except Exception:
        pass
    return [c for c, _ in HOLDINGS]


def all_codes(include_pool: bool = True) -> list[str]:
    """全部需要每日更新的标的（去重、保持稳定顺序）"""
    seen, out = set(), []
    groups = [list(INDEX_META), [c for c, _ in watch_items()], hold_codes(), sector_codes()]
    if include_pool:
        groups.append(signal_pool())
    for g in groups:
        for c in g:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def names() -> dict[str, str]:
    """代码 → 中文名（行情接口不带名字，这里用本地映射补）"""
    m = dict(INDEX_META)
    m.update({c: n for c, n in watch_items()})
    m.update({c: n for c, n in HOLDINGS})
    try:                                     # 用户自己加的持仓，名字以清单为准
        import store as portfolio_store
        for r in portfolio_store.load():
            if r.get("name") and r["name"] != r["code"]:
                m[r["code"]] = r["name"]
    except Exception:
        pass
    for fn in ("sectors_meta.json", "stocks_meta.json"):
        p = os.path.join(RAW, fn)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for k, v in d.items():
            if isinstance(v, dict) and v.get("name"):
                m.setdefault(k, v["name"])
            elif isinstance(v, str):
                m.setdefault(k, v)
    return m
