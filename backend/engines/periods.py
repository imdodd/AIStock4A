"""多周期K线聚合：日K → 周 / 月 / 年K。

背景：图位需要周线、月线、年线视角。日K有三个来源，覆盖的代码范围不同：

  L1 data/raw/klines.json          每日批量抓取的固定池（指数/持仓/自选/板块 + 少量成分股），
                                   约 505 根日K（板块）/ 363~545 根（个股），历史最长
  L2 data/raw/klines_extra.json    本模块在线补齐的长历史日K（落盘后免联网）
  L3 data/snapshot/charts/{code}.json
                                   个股详情页现场抓的日K缓存，chart_payload 只留最近 250 根；
                                   异动扫描下钻出来的成分股（不在批量池里）日K就在这里

历史教训：初版只读 L1，导致「日K能画、周K提示本地日K不足」——异动页点开的
成分股（如 sz001308 康冠科技）根本不在批量池里。现在三级回退，并且历史偏短时
再向行情源在线补一次长历史（新浪直连约 0.2 秒 / 640 根，带冷却 + 落盘 + 硬超时），
失败也不影响出图。

为什么不直接向接口要周/月K：
  - 腾讯 fqkline 对个股支持 period=week/month，但对板块 pt 代码只回 1 根；
  - 板块日K本身来自 westock/降级源的合成序列，多周期口径最好与日K一致。
所以统一从本地日K聚合：个股、板块、指数一套逻辑，口径完全一致。

聚合规则：
  周   ISO 日历周（isocalendar 的年+周号，跨年周归属唯一），代表日=该周最后交易日
  月   按 YYYY-MM 分组
  年   按 YYYY 分组
  开=首日开盘，收=末日收盘，高=max，低=min，量=sum
  最后一根永远是"未走完的周期"，保留（与行情软件一致）。

成交量单位归一：raw 库个别标的末根混入过"股"（比手大 100 倍），聚合前先按
中位数折算（与前端 withLiveBar 同一套逻辑），避免一根异常值顶爆量副图。

MA 参数按周期取行业常用值（键名沿用前端固定的 ma30/60/120/250 四条）：
  周 MA5/10/20/60 · 月 MA3/6/12/24 · 年 MA3/5
数据不够的窗口补 null，前端 connectNulls 处理。
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_KLINE = os.path.join(DATA_DIR, "raw", "klines.json")
EXTRA_KLINE = os.path.join(DATA_DIR, "raw", "klines_extra.json")
CHARTS = os.path.join(DATA_DIR, "snapshot", "charts")

VALID = ("week", "month", "year")
MA_KEYS = ["ma30", "ma60", "ma120", "ma250"]
MA_PARAMS = {
    "week": (5, 10, 20, 60),
    "month": (3, 6, 12, 24),
    "year": (3, 5),
}
MA_NOTE = {
    "week": "周K · MA5/10/20/60",
    "month": "月K · MA3/6/12/24",
    "year": "年K · MA3/5",
}
PERIOD_NAME = {"week": "周线", "month": "月线", "year": "年线"}

MIN_BARS = 10             # 少于 10 根日K没有聚合意义
LONG_ENOUGH = 400         # 达到这个根数就不再在线补历史
ONLINE_TIMEOUT = 8        # 秒；超时直接用现有日K出图，不拖死请求
RETRY_COOLDOWN = 600      # 在线补失败后的冷却秒数，避免每次切周期都重试

_RAW_LOCK = threading.Lock()
_RAW_CACHE: dict = {"mtime": None, "data": None}
_EXTRA_LOCK = threading.Lock()
_TRIED_ONLINE: dict[str, float] = {}   # code → 上次尝试时间戳（失败冷却）
_TRIED_LOCK = threading.Lock()


# ------------------------------------------------------------------ 各级日K来源
def _raw_all() -> dict:
    """按 mtime 缓存整库，避免每次切周期都重读几 MB 的 JSON"""
    try:
        m = os.path.getmtime(RAW_KLINE)
    except OSError:
        return {}
    with _RAW_LOCK:
        if _RAW_CACHE["data"] is None or _RAW_CACHE["mtime"] != m:
            try:
                with open(RAW_KLINE, encoding="utf-8") as f:
                    _RAW_CACHE["data"] = json.load(f)
                _RAW_CACHE["mtime"] = m
            except Exception:
                return {}
        return _RAW_CACHE["data"] or {}


def _from_nodes(nodes) -> list[dict]:
    """原始节点（raw / extra）→ 标准 bar，升序去重"""
    from datasource.base import normalize_nodes
    bars = normalize_nodes(nodes or [])
    seen, out = set(), []
    for b in bars:
        d = b.get("date")
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(b)
    out.sort(key=lambda x: x["date"])
    return out


def _from_chart_cache(code: str) -> list[dict]:
    """图表缓存的 dates/bars/volumes 反解回标准 bar。

    chart_payload 的 bars 列序是 [open, close, low, high]（不是 OHLC 常规序），
    这里必须按这个顺序拆，否则收盘会取到最高价。
    """
    p = os.path.join(CHARTS, f"{code}.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []
    dates = d.get("dates") or []
    bars = d.get("bars") or []
    vols = d.get("volumes") or []
    out = []
    for i, dt in enumerate(dates):
        if i >= len(bars):
            break
        b = bars[i]
        if not isinstance(b, (list, tuple)) or len(b) < 4:
            continue
        try:
            o, c, lo, hi = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
        except (TypeError, ValueError):
            continue
        if min(o, c, lo, hi) <= 0:
            continue
        out.append({
            "date": str(dt), "open": o, "close": c, "low": lo, "high": hi,
            "volume": float(vols[i] or 0) if i < len(vols) else 0.0,
        })
    out.sort(key=lambda x: x["date"])
    return out


def _candidates(code: str) -> list[tuple[list[dict], str]]:
    """[(日K, 来源说明)]，调用方取根数最多的一路"""
    out = []
    v = _raw_all().get(code)
    nodes = v.get("nodes") if isinstance(v, dict) else v
    b = _from_nodes(nodes)
    if b:
        out.append((b, "每日批量日K库"))

    try:
        with open(EXTRA_KLINE, encoding="utf-8") as f:
            ex = json.load(f)
        ev = ex.get(code)
        enodes = ev.get("nodes") if isinstance(ev, dict) else ev
        b = _from_nodes(enodes)
        if b:
            out.append((b, "在线补齐的历史日K"))
    except Exception:
        pass

    b = _from_chart_cache(code)
    if b:
        out.append((b, "个股日K图缓存"))
    return out


def _save_extra(code: str, bars: list[dict]) -> None:
    with _EXTRA_LOCK:
        try:
            with open(EXTRA_KLINE, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d[code] = {"nodes": bars, "bars_n": len(bars)}
        os.makedirs(os.path.dirname(EXTRA_KLINE), exist_ok=True)
        tmp = EXTRA_KLINE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, EXTRA_KLINE)


def _fetch_long(code: str) -> list[dict] | None:
    """现场补长历史日K。腾讯 qfq 与日K图同口径但常被 501 限流，
    所以优先走新浪直连（实测 640 根约 0.2 秒），失败再退回 datasource 的降级链。
    """
    from datasource import tencent as _tx
    try:
        b = _tx._sina_kline(code, 640)          # 不复权，成交量单位是「股」
        if b and len(b) > MIN_BARS:
            bars = _from_nodes(b)
            for x in bars:                      # 统一成「手」，与腾讯源口径一致
                x["volume"] = (x.get("volume") or 0) / 100
            return bars
    except Exception:
        pass
    try:
        b = (_tx.TencentSource().fetch_kline([code], 640) or {}).get(code)
        if b and len(b) > MIN_BARS:
            return _from_nodes(b)
    except Exception:
        pass
    return None


def _ensure_long(code: str) -> list[dict] | None:
    """历史太短时补一次长日K；带进程内冷却、硬超时与落盘，绝不让前端干等。

    腾讯 501 限流时 datasource 会自动降级到新浪；pt 板块代码在线接口取不到
    有效序列，直接跳过（它们本来就在批量池里）。
    """
    if code.startswith("pt"):
        return None
    import time
    now = time.time()
    with _TRIED_LOCK:
        if now - _TRIED_ONLINE.get(code, 0) < RETRY_COOLDOWN:
            return None
        _TRIED_ONLINE[code] = now
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        bars = pool.submit(_fetch_long, code).result(timeout=ONLINE_TIMEOUT)
    except Exception as e:
        print(f"[periods] {code} 在线补齐失败（用现有日K继续）：{type(e).__name__}: {e}")
        bars = None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if bars and len(bars) > MIN_BARS:
        _save_extra(code, bars)
        print(f"[periods] {code} 在线补齐历史日K {len(bars)} 根")
        return bars
    return None


def _load_day(code: str) -> tuple[list[dict] | None, str]:
    """返回 (日K, 来源)；三级回退 + 短历史在线补长"""
    cands = _candidates(code)
    if not cands:
        # 连图表缓存都没有（首次直接用 #period= 钩子打开）→ 只能现场抓
        long_bars = _ensure_long(code)
        if long_bars:
            return long_bars, "在线补齐的历史日K"
        return None, ""
    bars, source = max(cands, key=lambda x: len(x[0]))
    if len(bars) < LONG_ENOUGH:
        long_bars = _ensure_long(code)
        if long_bars and len(long_bars) > len(bars):
            bars, source = long_bars, "在线补齐的历史日K"
    return (bars, source) if bars and len(bars) >= MIN_BARS else (None, source)


def _norm_volume(bars: list[dict]) -> list[dict]:
    """中位数归一：把混入的"股"折算回"手"（异常大值 /100）"""
    vols = sorted(b.get("volume") or 0 for b in bars)
    vols = [v for v in vols if v > 0]
    if not vols:
        return bars
    med = vols[len(vols) // 2]
    out = []
    for b in bars:
        b = dict(b)
        v = b.get("volume") or 0
        while med and v > med * 20:
            v /= 100
        b["volume"] = v
        out.append(b)
    return out


def _group_key(d: str, period: str) -> str:
    if period == "week":
        y, w, _ = date.fromisoformat(d).isocalendar()
        return f"{y}-W{w:02d}"
    if period == "month":
        return d[:7]
    return d[:4]


def aggregate(bars: list[dict], period: str) -> list[dict]:
    """日K升序 → 周期K升序；代表日取周期内最后一个交易日"""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for b in bars:
        k = _group_key(b["date"], period)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(b)
    out = []
    for k in order:
        g = groups[k]
        out.append({
            "date": g[-1]["date"],
            "open": g[0]["open"],
            "close": g[-1]["close"],
            "high": max(b["high"] for b in g),
            "low": min(b["low"] for b in g),
            "volume": sum(b.get("volume") or 0 for b in g),
        })
    return out


def build(code: str, period: str, name: str | None = None) -> dict | None:
    """产出与前端 klineOption 兼容的周期K payload。

    bands/signals/pivots 留空：温斯坦阶段与买卖点是日线口径，周期K上不硬套；
    水平参考线（止损/成本等）前端照常叠加，价格线在任何周期都有意义。
    """
    if period not in VALID:
        return None
    if not name or name == code:
        # 异动页下钻出来的成分股不在名称索引里，从日K图缓存补中文名
        try:
            with open(os.path.join(CHARTS, f"{code}.json"), encoding="utf-8") as f:
                cn = (json.load(f) or {}).get("name")
            if cn and cn != code:
                name = cn
        except Exception:
            pass
    bars_day, source = _load_day(code)
    if not bars_day:
        return None
    bars_day = _norm_volume(bars_day)
    agg = aggregate(bars_day, period)
    if not agg:
        return None

    closes = [b["close"] for b in agg]
    params = MA_PARAMS[period]
    ma_out = {}
    for key, n in zip(MA_KEYS, params):
        ma_out[key] = [
            round(sum(closes[i - n + 1: i + 1]) / n, 2) if i >= n - 1 else None
            for i in range(len(closes))
        ]
    for key in MA_KEYS:                      # 年K只给 2 条 MA，缺的补 null 占位
        ma_out.setdefault(key, [None] * len(closes))

    return {
        "code": code,
        "name": name or code,
        "period": period,
        "period_name": PERIOD_NAME[period],
        "dates": [b["date"] for b in agg],
        "bars": [[b["open"], b["close"], b["low"], b["high"]] for b in agg],
        "volumes": [round(b["volume"]) for b in agg],
        "ma30": ma_out["ma30"], "ma60": ma_out["ma60"],
        "ma120": ma_out["ma120"], "ma250": ma_out["ma250"],
        "pivot_line": [None] * len(agg),
        "bands": [],
        "signals": [],
        "pivots": [],
        "last": {"close": closes[-1], "date": agg[-1]["date"]},
        "ma_note": MA_NOTE[period],
        "range": [bars_day[0]["date"], bars_day[-1]["date"]],
        "day_bars_n": len(bars_day),
        "day_source": source,
        "short_history": len(agg) < 3,      # 前端给"历史偏短"警示
    }


if __name__ == "__main__":
    import sys
    c = sys.argv[1] if len(sys.argv) > 1 else "sh603198"
    for p in VALID:
        d = build(c, p)
        if d:
            print(f"{c} {p:5s} {len(d['dates'])} 根 · {d['dates'][0]} ~ {d['dates'][-1]} · "
                  f"末根收 {d['last']['close']} · {d['ma_note']} · 源={d['day_source']}")
        else:
            print(f"{c} {p:5s} 无数据")
