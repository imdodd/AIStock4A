"""东财申万行业(BK)成分股 → 本地 124 个 pt 板块的成员源。

背景：腾讯 `getBoardRankList?board_code=pt01801xxx` 只返回主板 + 部分创业板，
科创板 0 只、北交所 0 只（半导体只有 59 只，东财同口径 188 只），导致
板块下钻、行业异动、搜索行业标签都缺一大块。

本模块不联网，只读三份已有离线快照：
  data/snapshot/flow_sector_map.json   496 个 BK → 本地 pt 的映射（exact/parent/members/none）
  data/snapshot/flow_members.json      496 个 BK 的全量成分股（六位代码 + 中文名）
  data/snapshot/stock_names.json       全市场 A 股代码 → 名称（用于补全前缀、过滤 B 股/退市）

汇总规则（按 pt 反查 BK）：
  exact   名称与本地二级行业一致 → 全量收
  parent  三级细分 ⊂ 该二级（集成电路制造 ⊂ 半导体）→ 并入，去重
  members 仅当该 pt 没有任何 exact/parent 时才用弱匹配结果，避免污染
  none    无对应，回退腾讯源（由调用方处理）

用法：
  from datasource.eastmoney_members import sector_members
  rows = sector_members("pt01801081")   # [{code: "sh688652", name: "京仪装备"}, ...]
"""
from __future__ import annotations

import json
import os
from collections import defaultdict

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(DATA_DIR, "snapshot")
MAP_FILE = os.path.join(SNAP, "flow_sector_map.json")
MEM_FILE = os.path.join(SNAP, "flow_members.json")
NAMES_FILE = os.path.join(SNAP, "stock_names.json")

_CACHE: dict = {}


def _load() -> dict:
    """三份快照合成内存索引，进程内只解析一次。"""
    if _CACHE.get("ready"):
        return _CACHE
    with open(MAP_FILE, encoding="utf-8") as f:
        mdoc = json.load(f)
    mp = mdoc["map"]
    with open(MEM_FILE, encoding="utf-8") as f:
        fm = json.load(f)["members"]
    with open(NAMES_FILE, encoding="utf-8") as f:
        d = json.load(f)
    items = d.get("items", []) if isinstance(d, dict) else d

    # 六位代码 → (带市场前缀的完整代码, 中文名)。全市场表已过滤退市/定转，
    # 也把 B 股(200xxx/900xxx)排除在外，所以这里匹配不上就直接丢弃。
    by6: dict[str, tuple[str, str]] = {}
    for x in items or []:
        if not isinstance(x, dict):
            continue
        c = str(x.get("code") or "").strip()
        if c:
            by6.setdefault(c[-6:], (c, str(x.get("name") or c)))

    rev: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for bk, v in mp.items():
        pt, match = (v or {}).get("code"), (v or {}).get("match")
        if pt and match in ("exact", "parent", "members"):
            rev[pt][match].append(bk)

    _CACHE.update(ready=True, rev=rev, fm=fm, by6=by6,
                  map_date=mdoc.get("generated_at"))
    return _CACHE


def bks_of(ptcode: str) -> tuple[list[str], str]:
    """该本地板块对应的东财 BK 列表，以及匹配口径。"""
    by = _load()["rev"].get(ptcode) or {}
    bks = list(by.get("exact") or []) + list(by.get("parent") or [])
    if bks:
        return list(dict.fromkeys(bks)), "exact+parent"
    bks = list(by.get("members") or [])
    return list(dict.fromkeys(bks)), "members" if bks else "none"


def sector_members(ptcode: str) -> list[dict]:
    """本地 pt 板块的东财全量成分股，格式与 TencentSource.sector_members 兼容。

    返回 [{code, name}]，按代码升序；无候选或快照缺失时返回空列表，
    调用方据此回退腾讯源，避免写空快照。
    """
    c = _load()
    bks, _ = bks_of(ptcode)
    if not bks:
        return []
    out, seen = [], set()
    for bk in bks:
        d = c["fm"].get(bk) or {}
        codes = d.get("codes") or []
        names = d.get("names") or []
        for i, six in enumerate(codes):
            hit = c["by6"].get(str(six).strip()[-6:])
            if not hit:
                continue
            code, fallback_name = hit
            if code in seen:
                continue
            seen.add(code)
            nm = names[i] if i < len(names) and names[i] else fallback_name
            out.append({"code": code, "name": nm})
    out.sort(key=lambda x: x["code"])
    return out


def coverage() -> dict:
    """诊断用：每个 pt 的成分股数量、匹配口径、BK 数。"""
    c = _load()
    out = {}
    for pt in c["rev"]:
        bks, via = bks_of(pt)
        out[pt] = {"count": len(sector_members(pt)), "via": via, "bks": len(bks)}
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    cv = coverage()
    print("板块数", len(cv), "成分股合计(含重复)", sum(x["count"] for x in cv.values()))
    for pt in ("pt01801081", "pt01801093", "pt01801782"):
        print(pt, cv.get(pt), "样例", sector_members(pt)[:3])
