"""交易所「异常波动 / 严重异常波动」监测：给擒龙每只标的打监管标签。

为什么擒龙需要这一层
--------------------
连板高度不是无限的。沪深交易所《交易规则》里的异常波动条款规定：股价在
连续 3 个交易日累计涨跌幅**偏离值**达到阈值，公司必须发布「股票交易异常
波动公告」并核实说明；连续 10 个交易日内出现 3 次同向异常波动，或 10 日
累计偏离值达到 +100%，就构成「严重异常波动」，交易所可以要求停牌核查、
对异常账户重点监控乃至限制交易。

游资「卡异动」算的就是这笔账：还剩多少涨幅额度、这一板封下去会不会把票
送进严重异常波动、值不值得在第几板出货。所以这个指标不是花边，而是判断
连板还能不能继续的直接依据，必须和四支柱放在同一层看。

口径（阈值）
------------
              当日涨停幅度   3 日偏离阈值   10 日      30 日
  主板            10%           ±20%       +100%     +200%
  ST 主板          5%           ±15%       +100%     +200%
  创业板/科创板     20%           ±30%       +100%     +200%
  北交所           30%           ±40%       +150%     +300%

偏离值 = 个股累计涨跌幅 − 对应指数同期累计涨跌幅（都用收盘价复合计算），
对应指数：沪市主板→上证指数、深市主板→深证成指、创业板→创业板指、
科创板→科创50、北交所→北证50。取不到指数时退化为纯个股涨幅（note 里标明）。

两个必须说清的近似
------------------
1. 交易所按「公司公告的异动次数」计数，我们只能从行情反推：把最近 10 个
   交易日里每根满足阈值的 3 日窗口算一次，相邻窗口间隔 <3 个交易日视为
   同一波（连板期间天天满足，不能算 N 次）。所以 cnt 是下限估计，不是
   交易所口径的原值，界面写「近 10 日异动 ≈N 次」。
2. 剩余额度按「指数不再上涨」的保守假设折算成还能涨几个板；指数若继续涨，
   额度会略宽。折算用复合收益率反解，不是简单减法。

本模块只读本地缓存，网络抓取集中在 ensure_bars（建池 / 验证 / 回补时各一次）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from data_paths import DATA_DIR

RAW = Path(DATA_DIR) / "raw"
CACHE = RAW / "regwatch_klines.json"
BARS_KEEP = 80          # 缓存保留的日K根数（30 日口径 + 缓冲）

# 阈值表：3 日偏离 / 10 日累计 / 30 日累计
SPEC = {
    "main": {"board": "主板", "limit": 0.10, "th3": 0.20, "th10": 1.00, "th30": 2.00},
    "st": {"board": "ST 主板", "limit": 0.05, "th3": 0.15, "th10": 1.00, "th30": 2.00},
    "gem": {"board": "创业板", "limit": 0.20, "th3": 0.30, "th10": 1.00, "th30": 2.00},
    "star": {"board": "科创板", "limit": 0.20, "th3": 0.30, "th10": 1.00, "th30": 2.00},
    "bj": {"board": "北交所", "limit": 0.30, "th3": 0.40, "th10": 1.50, "th30": 3.00},
}

LEVEL_LABEL = {
    "severe": "严重异常波动",
    "warn": "异动 2 次 · 再 1 次触发严重",
    "alert": "已触发异常波动",
    "near": "临界 · 再涨即触发异动",
    "none": "正常",
}
LEVEL_SHORT = {
    "severe": "严重异动",
    "warn": "异动×2",
    "alert": "异动",
    "near": "临界异动",
    "none": "",
}


# ---------------------------------------------------------------- 缓存读写
# 进程内只留一份：/api/dragon 每请求都会碰一次，文件 1MB 级别，逐次 json.load
# 会白白多花十几毫秒。用 mtime 判断是否要重新读。
_MEM: dict = {"mtime": None, "data": {}}


def _load_cache() -> dict:
    try:
        mt = CACHE.stat().st_mtime
    except OSError:
        return {}
    if _MEM["mtime"] == mt:
        return _MEM["data"]
    try:
        with open(CACHE, encoding="utf-8") as fh:
            d = json.load(fh)
        d = d if isinstance(d, dict) else {}
    except Exception:
        return {}
    _MEM.update(mtime=mt, data=d)
    return d


def _save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)
    os.replace(tmp, CACHE)
    try:
        _MEM.update(mtime=CACHE.stat().st_mtime, data=cache)
    except OSError:
        _MEM["data"] = cache


# ---------------------------------------------------------------- 板块口径
def board_key(code: str, name: str = "") -> str:
    """代码 → 板块键。920xxx 是北交所新代码段（本项目前缀可能写成 sz920…）。"""
    c = (code or "").lower()
    if c.startswith("bj") or c[2:5] == "920":
        return "bj"
    if c.startswith("sz30"):
        return "gem"
    if c.startswith("sh68"):
        return "star"
    # ST 只在主板改口径（5cm）；创业板 ST 仍是 20cm，不降档
    if "ST" in (name or "").upper():
        return "st"
    return "main"


def index_of(code: str) -> str:
    c = (code or "").lower()
    if c.startswith("sh60"):
        return "sh000001"
    if c.startswith("sz00"):
        return "sz399001"
    if c.startswith("sz30"):
        return "sz399006"
    if c.startswith("sh68"):
        return "sh000688"
    return "bj899050"


def spec_of(code: str, name: str = "") -> dict:
    return dict(SPEC[board_key(code, name)], key=board_key(code, name), idx=index_of(code))


# ---------------------------------------------------------------- 日K缓存
def ensure_bars(codes: list[str], upto: str | None = None,
                allow_fetch: bool = True, verbose: bool = False) -> dict:
    """确保 codes 的日K在缓存里且覆盖到 upto（含）。返回整个缓存。

    只抓「缺失或不够新」的票：每天盘后建池时 300 多只约 3~6 秒，
    盘中 / 历史回看都直接读缓存，不再联网。
    """
    cache = _load_cache()
    need = []
    for c in dict.fromkeys([x for x in codes if x]):
        node = cache.get(c)
        last = (node or {}).get("d") or []
        if not last or (upto and last[-1] < upto):
            need.append(c)
    if not need or not allow_fetch:
        return cache
    try:
        from datasource.tencent import TencentSource
        got = TencentSource().fetch_kline(need, limit=BARS_KEEP) or {}
    except Exception as e:
        if verbose:
            print(f"  [监管] 日K抓取失败：{e}")
        return cache
    for code, bars in got.items():
        rows = bars[-BARS_KEEP:] if bars else []
        if not rows:
            continue
        cache[code] = {"d": [b["date"] for b in rows],
                       "c": [round(float(b["close"]), 3) for b in rows],
                       "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    _save_cache(cache)
    if verbose:
        print(f"  [监管] 日K补齐 {len(got)}/{len(need)} 只")
    return cache


def _series(cache: dict, code: str, upto: str | None):
    node = cache.get(code) or {}
    ds, cs = node.get("d") or [], node.get("c") or []
    if upto:
        # 回看历史日时不能把之后的K线算进来，否则过去那天的「异动」会用未来数据
        cut = len(ds)
        for i, d in enumerate(ds):
            if d > upto:
                cut = i
                break
        ds, cs = ds[:cut], cs[:cut]
    return ds, cs


# ---------------------------------------------------------------- 计算
def _dev(cs: list[float], ics: list[float] | None, n: int) -> float | None:
    """n 日累计涨跌幅偏离值（个股 − 指数）。指数缺失则退化为纯涨幅。"""
    if len(cs) <= n:
        return None
    stock = cs[-1] / cs[-1 - n] - 1
    if not ics or len(ics) <= n:
        return stock
    return stock - (ics[-1] / ics[-1 - n] - 1)


def _room(cs: list[float], ics: list[float] | None, n: int, th: float) -> float | None:
    """距触发阈值还剩多少涨幅（复合口径，正数=还剩空间，≤0=已破）。

    条件： cs[-1]*(1+r)/cs[-1-n] - 1 - idx_n >= th
    解出 r： r >= (1 + th + idx_n) * cs[-1-n] / cs[-1] - 1
    """
    if len(cs) <= n:
        return None
    idx_n = 0.0
    if ics and len(ics) > n:
        idx_n = ics[-1] / ics[-1 - n] - 1
    return (1 + th + idx_n) * cs[-1 - n] / cs[-1] - 1


def _boards(room: float | None, limit: float) -> int | None:
    """剩余空间还能装下几个整板（向下取整，>0 才有意义）。"""
    if room is None:
        return None
    if room <= 0:
        return 0
    # 复合： (1+limit)^k <= 1+room
    k = 0
    while (1 + limit) ** (k + 1) <= 1 + room and k < 20:
        k += 1
    return k


def _count_events(cs: list[float], ics: list[float] | None,
                  th3: float, days: int = 10) -> int:
    """近 days 个交易日里满足 3 日偏离阈值的「波」数（相邻 <3 日算同一波）。"""
    if len(cs) < 4:
        return 0
    last = len(cs) - 1
    start = max(3, last - days + 1)
    hits = []
    for e in range(start, last + 1):
        d3 = _dev_n(cs, ics, e, 3)
        if d3 is not None and d3 >= th3:
            hits.append(e)
    cnt, prev = 0, -99
    for e in hits:
        if e - prev >= 3:
            cnt += 1
            prev = e
    return cnt


def _dev_n(cs: list[float], ics: list[float] | None, end: int, n: int) -> float | None:
    """以 end 为终点（含）的 n 日偏离。"""
    if end - n < 0:
        return None
    stock = cs[end] / cs[end - n] - 1
    if not ics or len(ics) <= end or end - n < 0:
        return stock
    return stock - (ics[end] / ics[end - n] - 1)


def evaluate(code: str, name: str = "", lbc: int | None = None,
             cache: dict | None = None, trade_date: str | None = None) -> dict | None:
    """单只标的的监管状态。K线不足（新股 / 缓存未到那天）返回 None。"""
    cache = cache if cache is not None else _load_cache()
    sp = spec_of(code, name)
    ds, cs = _series(cache, code, trade_date)
    if len(cs) < 11:
        return None
    ids, ics = _series(cache, sp["idx"], trade_date)
    if not ics or len(ics) < 11:
        ics = None
    # 个股与指数对齐到较短长度（指数缺当日时不能拿旧值硬顶）
    if ics is not None and ids and ds:
        if ids[-1] != ds[-1]:
            # 指数缺最新一天：砍掉个股多余的那根，避免错位比较
            n = min(len(cs), len(ics))
            cs, ics = cs[-n:], ics[-n:]

    d3 = _dev(cs, ics, 3)
    d10 = _dev(cs, ics, 10)
    d30 = _dev(cs, ics, 30) if len(cs) > 30 else None
    cnt = _count_events(cs, ics, sp["th3"])
    room3 = _room(cs, ics, 3, sp["th3"])
    room10 = _room(cs, ics, 10, sp["th10"])
    b3 = _boards(room3, sp["limit"])
    b10 = _boards(room10, sp["limit"])

    # 分档：严重 > 2 次 > 1 次 > 临界 > 正常
    if (d10 is not None and d10 >= sp["th10"]) or (d30 is not None and d30 >= sp["th30"]) or cnt >= 3:
        level = "severe"
    elif cnt == 2:
        level = "warn"
    elif cnt == 1 or (d3 is not None and d3 >= sp["th3"]):
        level = "alert"
    elif (d3 is not None and d3 >= sp["th3"] * 0.72) or (b3 == 0 and (lbc or 0) >= 2):
        level = "near"
    else:
        level = "none"

    pct = lambda v: None if v is None else round(v * 100, 1)
    return {
        "level": level,
        "label": LEVEL_LABEL[level],
        "short": LEVEL_SHORT[level],
        "board": sp["board"],
        "limit": sp["limit"],
        "th3": sp["th3"], "th10": sp["th10"], "th30": sp["th30"],
        "d3": pct(d3), "d10": pct(d10), "d30": pct(d30),
        "cnt": cnt, "cnt_need": 3,
        "room_pct": pct(room3), "room_boards": b3,
        "room10_pct": pct(room10), "room10_boards": b10,
        "idx": sp["idx"] if ics is not None else None,
        "as_of": ds[-1] if ds else None,
        "lbc": lbc,
    }


def annotate(items: list[dict], trade_date: str | None = None,
             allow_fetch: bool = True, verbose: bool = False) -> list[dict]:
    """给池子 / 验证条目批量写 reg 字段（就地修改并返回）。"""
    codes = [i.get("code") for i in items if i.get("code")]
    if not codes:
        return items
    idx_codes = sorted({index_of(c) for c in codes})
    cache = ensure_bars(list(dict.fromkeys(codes + idx_codes)), upto=trade_date,
                        allow_fetch=allow_fetch, verbose=verbose)
    for it in items:
        code = it.get("code")
        if not code:
            it["reg"] = None
            continue
        lbc = it.get("now_lbc") if it.get("now_lbc") is not None else it.get("lbc")
        try:
            it["reg"] = evaluate(code, it.get("name") or "", int(lbc or 0), cache, trade_date)
        except Exception:
            it["reg"] = None
    return items


def stats(items: list[dict]) -> dict:
    """给页面顶部一行汇总：严重 / 异动 / 临界 各几只。"""
    out = {"severe": 0, "warn": 0, "alert": 0, "near": 0, "none": 0}
    for it in items or []:
        lv = ((it.get("reg") or {}).get("level")) or "none"
        out[lv] = out.get(lv, 0) + 1
    return out
