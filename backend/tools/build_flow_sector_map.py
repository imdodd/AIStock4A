"""生成「东财申万行业(BK)」→「本地申万二级板块(pt)」映射表。

背景：首页行业资金流取东财 clist fs=m:90+t:2，共 496 个行业，
是 2021 版申万的一/二/三级混合清单（如「集成电路制造」是半导体下的细分）；
板块模块用的是本地 124 个申万二级（旧口径，半导体是一个整体）。
两套名单不是同一个概念，纯名称匹配会漏掉一半左右，还会把「银行」错配成
「股份制银行Ⅱ」。本脚本按成分股关系自动建桥，覆盖全部 496 个 BK。

映射优先级：
  L1 exact   名称归一化（去 Ⅰ/Ⅱ/Ⅲ 后缀）后与本地行业同名
  L2 parent  该行业成分股被另一个「能精确对应本地行业」的东财行业覆盖 >= 50%
             （细分 → 父级，例：集成电路制造 BK1329 ⊂ 半导体 BK1036）
  L3 members 与本地 members/pt*.json 成分股重合最多的板块（弱匹配，带 coverage）
  L4 none    找不到对应

产出：
  data/snapshot/flow_sector_map.json   映射结果，供 attach_local() 读取
  data/snapshot/flow_members.json      每个 BK 的成分股代码，便于复现/刷新

用法（在 server/ 目录）：python tools/build_flow_sector_map.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from datasource.sectorflow import CLIST_URL, _get, _norm_name, ranking  # noqa: E402
from datasource.tencent import TencentSource  # noqa: E402

SNAP = os.path.join(DATA_DIR, "snapshot")
MAP_FILE = os.path.join(SNAP, "flow_sector_map.json")
MEM_FILE = os.path.join(SNAP, "flow_members.json")
PARENT_MIN_COVER = 0.50


def fetch_members(bk: str) -> dict:
    """行业成分股；东财 BK 走 clist 翻页，腾讯 PT 直接走 hy2 板块接口。"""
    # 东财不可用时 ranking() 会回落到腾讯 hy2，行里的 bk 是 PT01801xxx，
    # 拿它去请求 b:PT01801xxx 必然全失败。这一类直接用腾讯成分股接口。
    if str(bk).upper().startswith("PT"):
        pairs, seen = [], set()
        for x in (TencentSource().sector_members(str(bk).lower()) or []):
            six = str(x.get("code") or "")[-6:]
            if len(six) != 6 or not six.isdigit() or six in seen:
                continue
            seen.add(six)
            pairs.append((six, str(x.get("name") or "")))
        return {"codes": [c for c, _ in pairs], "names": [n for _, n in pairs],
                "total": len(pairs)}

    # 东财板块成分股（f12 六位代码 / f14 名称），pz 单页上限 100，需翻页。
    codes, names, total, pn = [], [], None, 1
    while pn <= 8:
        q = ("?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
             "&fs=b:%s&fields=f12,f14" % (pn, bk))
        data = (_get(CLIST_URL + q, timeout=10, retry=1).get("data") or {})
        diff = data.get("diff") or []
        total = data.get("total") or 0
        for x in diff:
            c = str(x.get("f12") or "").strip()
            if c:
                codes.append(c)
                names.append(str(x.get("f14") or ""))
        if len(diff) < 100 or len(codes) >= total:
            break
        pn += 1
        time.sleep(0.05)
    return {"codes": codes, "names": names, "total": total or len(codes)}


def load_local() -> tuple[dict, dict]:
    """本地 124 个行业：归一化名 → pt，以及 六位代码 → [pt]。"""
    sp = os.path.join(SNAP, "sectors.json")
    items = []
    if os.path.exists(sp):
        try:
            d = json.load(open(sp, encoding="utf-8"))
            items = d if isinstance(d, list) else (d.get("items") or [])
        except Exception:
            items = []
    if not items:
        # 全新安装的 sectors.json 可能还是空的（板块 K 线不足），退回仓库种子
        mp = os.path.join(DATA_DIR, "raw", "sectors_meta.json")
        try:
            d = json.load(open(mp, encoding="utf-8"))
            items = [dict(code=k, name=(v or {}).get("name"))
                     for k, v in d.items() if isinstance(v, dict)]
        except Exception:
            items = []
    by_name = {_norm_name(s["name"]): s for s in items if s.get("name")}
    rev = defaultdict(set)
    for path in glob.glob(os.path.join(SNAP, "members", "pt*.json")):
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        pt = m.get("code") or os.path.basename(path)[:-5]
        for x in m.get("members", []):
            rev[str(x.get("code", ""))[-6:]].add(pt)
    return by_name, rev


# 增量刷新：成分股关系变动很慢（申万分类一年调整几次、新股上市个别行业才增），
# 没必要每 7 天把 496 个 BK 全抓一遍——那是 1000+ 次 clist 请求，刷新日必撞东财
# 限流。已成功抓过且成员数合理的 BK 在 TTL 内直接复用快照；只抓「新增/失败/过期」
# 的 BK，请求量从 1000+ 降到几十次。--force 可强制全量重抓。
MEMBER_TTL_DAYS = 14           # 成员快照复用期（比映射表 7 天 TTL 略长，安全）
MAX_WORKERS = 3                # 并发从 10 降到 3，避免瞬时突发
REQUEST_GAP = 0.3              # 每个请求后强制间隔，配合 sectorflow 的限流策略


def _load_prev_members() -> tuple[dict, dict]:
    """读上次成员快照及其时间戳，作为增量复用的基础。"""
    prev, stamps = {}, {}
    if os.path.exists(MEM_FILE):
        try:
            d = json.load(open(MEM_FILE, encoding="utf-8"))
            prev = d.get("members") or {}
            stamps = d.get("bk_fetched_at") or {}
        except Exception:
            pass
    return prev, stamps


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="东财行业→本地板块映射表")
    ap.add_argument("--force", action="store_true", help="强制全量重抓成分股（忽略增量缓存）")
    a = ap.parse_args(argv or [])

    rows = ranking()["rows"]
    prev, stamps = ({}, {} if a.force else _load_prev_members())
    now = time.time()

    # 决定哪些 BK 需要真抓：无快照 / 快照过期 / 上次为空 / 本次不在快照里
    todo, members, reused = [], dict(prev), 0
    for r in rows:
        bk = r["bk"]
        old = prev.get(bk)
        ts = stamps.get(bk, 0)
        age_days = (now - ts) / 86400 if ts else 999
        if old and (old.get("codes") or []) and age_days <= MEMBER_TTL_DAYS:
            reused += 1
            continue
        todo.append(r)
        members.pop(bk, None)          # 过期/失败的旧数据不再复用

    print(f"[1/3] 东财申万行业 {len(rows)} 个 · 复用快照 {reused} 个 · "
          f"需抓取 {len(todo)} 个（并发 {MAX_WORKERS}，间隔 {REQUEST_GAP}s）", flush=True)

    failed = []

    def fetch_one(r):
        time.sleep(REQUEST_GAP)        # 每个请求前间隔，抑制突发
        return r, fetch_members(r["bk"])

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fut = [pool.submit(fetch_one, r) for r in todo]
        for i, f in enumerate(as_completed(fut), 1):
            r = None
            try:
                r, mm = f.result()
                members[r["bk"]] = mm
                stamps[r["bk"]] = now    # 记录本次抓取时间，供下轮增量判断
            except Exception as e:  # noqa: BLE001
                if r is None:
                    r = todo[i - 1] if i - 1 < len(todo) else {}
                failed.append({"bk": r.get("bk"), "name": r.get("name"), "error": str(e)})
            if i % 50 == 0:
                print(f"      {i}/{len(todo)}", flush=True)

    by_name, rev = load_local()
    local_name = {s["code"]: s["name"] for s in by_name.values()}

    # 能精确对应本地行业的东财行业，作为「父行业」候选
    parents = []
    for r in rows:
        s = by_name.get(_norm_name(r["name"]))
        if s and members.get(r["bk"], {}).get("codes"):
            parents.append((r["bk"], r["name"], s["code"], set(members[r["bk"]]["codes"])))
    parent_sets = [(bk, nm, pt, cs) for bk, nm, pt, cs in parents]
    print(f"[2/3] 可精确对应的父行业 {len(parent_sets)} 个，开始计算映射…", flush=True)

    out = {}
    for r in rows:
        bk, name = r["bk"], r["name"]
        mm = members.get(bk) or {}
        codes = set(mm.get("codes") or [])
        item = {
            "name": name, "code": None, "local_name": None,
            "match": "none", "coverage": 0.0, "hits": 0,
            "total": mm.get("total", 0), "via": None,
        }
        s = by_name.get(_norm_name(name))
        if s:
            item.update(code=s["code"], local_name=s["name"], match="exact",
                        coverage=1.0, hits=len(codes), via="名称一致")
        elif codes:
            # L2：找一个覆盖了本行业成分股的「父级」东财行业
            best = None
            for pbk, pname, ppt, pcs in parent_sets:
                if pbk == bk or not pcs:
                    continue
                cover = len(codes & pcs) / len(codes)
                if cover >= PARENT_MIN_COVER and (best is None or cover > best[0]):
                    best = (cover, pbk, pname, ppt)
            if best:
                cover, pbk, pname, ppt = best
                item.update(code=ppt, local_name=local_name.get(ppt),
                            match="parent", coverage=round(cover, 3),
                            hits=round(cover * len(codes)),
                            via=f"细分行业 ⊂ {pname}（{pbk}）")
            else:
                # L3：与本地行业成分股快照求重合（弱匹配）
                cnt = Counter()
                for c in codes:
                    for pt in rev.get(c, ()):
                        cnt[pt] += 1
                if cnt:
                    pt, hits = cnt.most_common(1)[0]
                    item.update(code=pt, local_name=local_name.get(pt),
                                match="members", coverage=round(hits / len(codes), 3),
                                hits=hits, via="与本地成分股快照重合")
        out[bk] = item

    stat = Counter(x["match"] for x in out.values())
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "eastmoney clist m:90+t:2 / b:BKxxxx",
        "local_count": len(by_name), "flow_count": len(rows),
        "parent_min_cover": PARENT_MIN_COVER,
        "stats": dict(stat), "failed": failed,
        "map": out,
    }
    with open(MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(MEM_FILE, "w", encoding="utf-8") as f:
        json.dump({"generated_at": payload["generated_at"], "members": members,
                   "bk_fetched_at": stamps}, f, ensure_ascii=False)

    print(f"[3/3] 写入 {MAP_FILE}", flush=True)
    print("      匹配统计:", dict(stat), " 抓取失败:", len(failed),
          f" 本轮实抓 {len(todo)} 个 / 复用 {reused} 个", flush=True)
    for key in ("集成电路制造", "半导体设备", "国有大型银行Ⅲ", "银行", "水力发电", "电信运营商"):
        for bk, x in out.items():
            if x["name"] == key:
                print(f"      {key:10s} {bk} → {x['match']:8s} {x['local_name']} "
                      f"({x['code']}) cover={x['coverage']} via={x['via']}", flush=True)
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
