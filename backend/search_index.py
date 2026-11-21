"""全平台搜索索引：股票（代码 / 名称）+ 申万二级行业（代码 / 名称）。

数据来源全部是本地快照，不联网、不依赖任何连接器：
  - data/snapshot/sectors.json        124 个申万二级行业
  - data/snapshot/members/pt*.json   4480 只成分股（代码、名称、所属行业、阶段）
  - data/portfolio/{holdings,watchlist}.json   用户池
  - data/snapshot/signals.json / data/cases/*/digest.json / data/profile / charts
    补充名称与「这只票能在哪个模块找到」的标签

索引在进程内缓存 5 分钟（EOD 重建快照后自然刷新），首次构建约 0.2 秒。
"""
from __future__ import annotations

import glob
import json
import os
import re
import threading
import time

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(DATA_DIR, "snapshot")
MEMBERS = os.path.join(SNAP, "members")
PORTFOLIO = os.path.join(DATA_DIR, "portfolio")
CASES = os.path.join(DATA_DIR, "cases")

_CACHE: dict = {"at": 0.0, "sectors": [], "stocks": []}
_LOCK = threading.Lock()
TTL = 300                      # 秒
_TAG_BONUS = {"持仓": 40, "自选": 30, "异动": 12, "信号": 10, "档案": 4, "K线": 2}


def _j(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _list_of(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        for k in ("items", "rows", "data", "list"):
            if isinstance(v.get(k), list):
                return v[k]
    return []


def _norm(s) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def _digits(code: str) -> str:
    """sz001308 -> 001308；pt01801081 -> 01801081"""
    return re.sub(r"^[a-z]+", "", str(code or "").lower())


def _tag_of(stocks: dict, code: str, name: str | None, tag: str) -> None:
    rec = stocks.get(code)
    if rec is None:
        rec = {"kind": "stock", "code": code, "name": name or code,
               "sector_code": None, "sector_name": None,
               "stage": None, "sub": None, "tags": []}
        stocks[code] = rec
    elif name and (not rec.get("name") or rec["name"] == code):
        rec["name"] = name
    if tag not in rec["tags"]:
        rec["tags"].append(tag)


def build() -> dict:
    sectors: dict[str, dict] = {}
    for s in _list_of(_j(os.path.join(SNAP, "sectors.json"))):
        code = s.get("code")
        if not code:
            continue
        name = s.get("name") or code
        sectors[code] = {
            "kind": "sector", "code": code, "name": name,
            "sector_code": code, "sector_name": name,
            "stage": s.get("stage"), "sub": s.get("sub"),
            "rs60": s.get("rs60"), "chg": s.get("chg"), "tags": [],
        }

    stocks: dict[str, dict] = {}
    for f in sorted(glob.glob(os.path.join(MEMBERS, "pt*.json"))):
        d = _j(f)
        if not isinstance(d, dict):
            continue
        sc, sn = d.get("code"), d.get("name") or ""
        if sc and sc not in sectors:
            sectors[sc] = {"kind": "sector", "code": sc, "name": sn or sc,
                           "sector_code": sc, "sector_name": sn or sc,
                           "stage": d.get("sector_stage"), "sub": d.get("sector_sub"),
                           "rs60": None, "chg": None, "tags": []}
        for m in d.get("members") or []:
            c = m.get("code")
            if not c or c in stocks:
                continue
            stocks[c] = {
                "kind": "stock", "code": c, "name": m.get("name") or c,
                "sector_code": sc, "sector_name": sn,
                "stage": m.get("stage"), "sub": m.get("sub"),
                "price": m.get("price"), "chg": m.get("chg"), "tags": [],
            }

    # 全市场「代码→名称」表补齐：覆盖科创板 / 北交所等不在行业成分股快照里的标的。
    # 这份表由 tools/build_stock_names.py 离线生成，搜索时纯本地读，不联网。
    names = _j(os.path.join(SNAP, "stock_names.json")) or {}
    for r in _list_of(names):
        c = r.get("code")
        if not c:
            continue
        if c not in stocks:
            stocks[c] = {"kind": "stock", "code": c, "name": r.get("name") or c,
                         "sector_code": None, "sector_name": None,
                         "stage": None, "sub": None, "tags": []}
        elif (not stocks[c].get("name") or stocks[c]["name"] == c) and r.get("name"):
            stocks[c]["name"] = r["name"]

    # ---- 用户池与模块标签（决定前端点击结果后跳到哪个 tab）----
    rows = _j(os.path.join(PORTFOLIO, "holdings.json"))
    for r in _list_of(rows):
        if r.get("code"):
            _tag_of(stocks, r["code"], r.get("name"), "持仓")

    try:
        import watchstore
        for code, name in watchstore.items():
            _tag_of(stocks, code, name, "自选")
    except Exception:
        for r in _list_of(_j(os.path.join(SNAP, "watchlist.json"))):
            if r.get("code"):
                _tag_of(stocks, r["code"], r.get("name"), "自选")

    # 关注池优先读每日全市场汇总（focus_pool.json），缺失才回退旧的 62 只信号池。
    # 全局搜索点「信号」标签会跳到关注页定位，两边必须是同一份名单。
    pool = _j(os.path.join(SNAP, "focus_pool.json")) or {}
    signal_rows = (pool.get("items") if isinstance(pool, dict) else None) or \
        _list_of(_j(os.path.join(SNAP, "signals.json")))
    for r in _list_of(signal_rows):
        if r.get("code"):
            _tag_of(stocks, r["code"], r.get("name"), "信号")

    digests = sorted(glob.glob(os.path.join(CASES, "*", "digest.json")))
    if digests:
        for r in _list_of((_j(digests[-1]) or {}).get("items")):
            if r.get("code"):
                _tag_of(stocks, r["code"], r.get("name"), "异动")

    for f in glob.glob(os.path.join(DATA_DIR, "profile", "*.json")):
        d = _j(f)
        if isinstance(d, dict) and d.get("code"):
            _tag_of(stocks, d["code"], d.get("name"), "档案")

    for f in glob.glob(os.path.join(SNAP, "charts", "*.json")):
        base = os.path.basename(f)[:-5]
        if not re.match(r"^(sh|sz|bj)\d{6}$", base):
            continue
        d = _j(f)
        if isinstance(d, dict) and d.get("name") and d["name"] != base:
            _tag_of(stocks, base, d["name"], "K线")

    for rec in stocks.values():
        rec.setdefault("tags", [])
        rec["name"] = str(rec["name"] or rec["code"]).strip()
        rec["where"] = ((rec.get("sector_name") or "全市场清单 · 未纳入行业快照")
                        + (" · 成分股" if rec.get("sector_code") else ""))
        rec["hay"] = _norm(rec["name"]) + "|" + _norm(rec["code"]) + "|" + _norm(rec.get("sector_name"))
    for rec in sectors.values():
        rec["name"] = str(rec["name"] or rec["code"]).strip()
        rec["hay"] = _norm(rec["name"]) + "|" + _norm(rec["code"])

    return {"sectors": list(sectors.values()), "stocks": list(stocks.values())}


def _index() -> dict:
    now = time.time()
    if _CACHE["sectors"] and now - _CACHE["at"] < TTL:
        return _CACHE
    with _LOCK:
        if _CACHE["sectors"] and now - _CACHE["at"] < TTL:
            return _CACHE
        d = build()
        _CACHE.update({"at": time.time(), "sectors": d["sectors"], "stocks": d["stocks"]})
        return _CACHE


def _score(rec: dict, q: str, qd: str, qprefix: str | None) -> int:
    name = _norm(rec["name"])
    code = _norm(rec["code"])
    digits = _digits(code)
    is_sector = rec["kind"] == "sector"
    sc = 0
    # 查询自带 sh/sz/bj/pt 前缀却与标的前缀不符：仍允许数字部分命中，
    # 但大幅降分，避免「sz603198」被当成「sh603198」的精确匹配。
    prefix_bad = bool(qprefix and not code.startswith(qprefix))
    if not prefix_bad and (q == code or (qd and digits == qd)):
        sc = 300
    elif q == name:
        sc = 290
    elif name.startswith(q):
        sc = 260 if is_sector else 240
    elif not prefix_bad and qd and len(qd) >= 2 and digits.startswith(qd):
        sc = 230
    elif q in name:
        sc = 220 if is_sector else 180      # 搜「银行」时行业分类排在个股前面
    elif q in code:
        sc = 170
    elif qd and len(qd) >= 3 and qd in digits:
        sc = 150
    elif q in _norm(rec.get("sector_name")) and len(q) >= 2:
        sc = 60                            # 「半导体」顺带带出少量成分股
    else:
        return 0
    if prefix_bad:
        sc -= 110
    if is_sector:
        sc += 6
    for t in rec.get("tags") or []:
        sc += _TAG_BONUS.get(t, 0)
    return sc


def search(q: str, limit: int = 24) -> dict:
    q = _norm(q)
    if not q:
        return {"items": [], "total": 0, "q": q, "index_size": 0}
    m = re.fullmatch(r"([a-z]{2})?(\d+)", q)
    qd = m.group(2) if m else ""
    qprefix = m.group(1) if m else None
    ix = _index()
    hits = []
    for rec in ix["sectors"] + ix["stocks"]:
        s = _score(rec, q, qd, qprefix)
        if s:
            hits.append((s, rec))
    hits.sort(key=lambda x: (-x[0], x[1]["kind"] != "sector", x[1]["code"]))
    items = []
    weak = 0                                # 「行业名带出的成分股」最多 5 只，别刷屏
    for s, rec in hits:
        if len(items) >= limit:
            break
        if s < 100 and rec["kind"] == "stock":
            weak += 1
            if weak > 5:
                continue
        items.append({k: v for k, v in rec.items() if k not in ("hay",)} | {"score": s})
    return {"items": items, "total": len(hits), "q": q,
            "index_size": len(ix["sectors"]) + len(ix["stocks"])}


if __name__ == "__main__":
    import sys
    term = sys.argv[1] if len(sys.argv) > 1 else "半导体"
    r = search(term)
    print(f"q={term!r} total={r['total']} index={r['index_size']}")
    for x in r["items"][:12]:
        print(f"  {x['score']:4d} {x['kind']:6s} {x['code']:12s} {x['name']:10s} {x.get('sector_name') or '':10s} {','.join(x.get('tags') or [])}")
