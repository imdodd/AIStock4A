#!/usr/bin/env python3
"""读路径切换验收：证明 STORAGE_READ=db 真的生效，且业务数据逐字段不变。

全程 :memory: 库、源文件只读、**不改任何业务文件**（探针只写内存库再删掉）。

检查分四组：
  A 默认档零侵入：files 档不替换任何函数；
  B db 档接通：自举、读写函数接线、8 张表条数、无回退；
  C 逐字段等价：同一份夹具，files 快照 vs db 快照 vs「把文件目录藏起来」快照；
  D 探针：只往库里插一条文件里不存在的数据，读函数必须立刻看见；删掉后恢复。

用法：
  python tools/verify_read_switch.py --src backend/data --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.dirname(__file__))

from db import ASSET_TABLES, dumps, load_config  # noqa: E402
import storage_runtime as sr  # noqa: E402
import news, pnl, store, stops, tracking, watchstore  # noqa: E402

HIDDEN = os.path.join(ROOT, "__no_such_data_for_storage_probe__")


def snapshot() -> dict:
    """与路由层看到的业务对象同形状的一组读函数结果。

    刻意不调 stops.load()：它带懒重算会写盘；stops 的等价性由
    verify_storage.py 与下面的库内 payload 检查覆盖。
    """
    plans = tracking.list_all()
    return {
        "holdings": store.load(),
        "holdings_codes": store.codes(),
        "watchlist": watchstore.load(),
        "watchlist_codes": watchstore.codes(),
        "watchlist_items": watchstore.items(),
        "realized": pnl.load_realized(),
        "realized_total": pnl.realized_total(),
        "history": pnl.load_history(),
        "history10": pnl.history(10),
        "pnl_stats": pnl.stats(),
        "news_dates": news.list_dates(),
        "news_index": news.index(30),
        "news_latest": news.load(),
        "news_status": news.status(),
        "plans": plans,
        "plan_details": {c: {"meta": tracking.load(c),
                             "periods": tracking.period_list(c)} for c in sorted(plans)},
    }


def _paths(src: str) -> list[tuple]:
    """6 个绿区模块的全部文件入口，统一指向 src（与 verify_storage.bind_files 同口径）。"""
    port = os.path.join(src, "portfolio")
    return [
        (store, "DIR", port),
        (store, "PATH", os.path.join(port, "holdings.json")),
        (watchstore, "DIR", port),
        (watchstore, "PATH", os.path.join(port, "watchlist.json")),
        (watchstore, "SNAPSHOT", os.path.join(src, "snapshot", "watchlist.json")),
        (pnl, "DIR", port),
        (pnl, "HISTORY_PATH", os.path.join(port, "pnl_daily.json")),
        (pnl, "REALIZED_PATH", os.path.join(port, "realized.json")),
        (news, "DIR", os.path.join(src, "news")),
        (news, "SECTORS_JSON", os.path.join(src, "snapshot", "sectors.json")),
        (news, "PURGE_LOG", os.path.join(src, "news", "_purge.log")),
        (tracking, "DIR", os.path.join(src, "tracking")),
        (tracking, "MEMBERS", os.path.join(src, "snapshot", "members")),
        (stops, "STORE_PATH", os.path.join(port, "holdings.json")),
        (stops, "OUT_PATH", os.path.join(port, "stops.json")),
        (stops, "SNAP_CHARTS", os.path.join(src, "snapshot", "charts")),
    ]


def apply_paths(src: str) -> dict:
    """把文件态读函数绑到指定源目录（只改本进程的模块常量，不动任何文件）。"""
    saved = {}
    for mod, name, value in _paths(src):
        key = (mod.__name__, name)
        saved.setdefault(key, getattr(mod, name, None))
        setattr(mod, name, value)
    return saved


def restore_files(saved: dict) -> None:
    import importlib
    for (mod_name, n), value in saved.items():
        if value is not None:
            setattr(importlib.import_module(mod_name), n, value)


def hide_files() -> dict:
    """把全部文件入口指向一个不存在的目录，证明 db 档不依赖文件。"""
    return apply_paths(HIDDEN)


def compare(name: str, left, right, checks: list) -> None:
    if left == right:
        checks.append({"name": name, "match": True})
        return
    l, r = dumps(left), dumps(right)
    pos = next((i for i, (x, y) in enumerate(zip(l, r)) if x != y), min(len(l), len(r)))
    checks.append({"name": name, "match": False, "first_diff_at": pos,
                   "left_excerpt": l[max(0, pos - 50):pos + 120],
                   "right_excerpt": r[max(0, pos - 50):pos + 120]})


def run(src: str) -> dict:
    """对外入口：把文件态函数绑到 --src，跑完恢复，保证同进程复用不留副作用。"""
    saved_src = apply_paths(src)
    try:
        return _run(src)
    finally:
        restore_files(saved_src)


def _run(src: str) -> dict:
    checks: list[dict] = []
    cfg_files = replace(load_config(), read_from="files", path=":memory:")
    cfg_db = replace(load_config(), read_from="db", path=":memory:")

    # A 默认档
    sr.install(cfg=cfg_files, src=src, force=True)
    s_files = sr.state()
    checks.append({"name": "A1 files 档 mode=files",
                   "match": s_files["mode"] == "files"})
    checks.append({"name": "A2 files 档未替换任何函数",
                   "match": not s_files["patched"] and not s_files["writes"]
                   and getattr(store.load, "__storage__", None) is None})
    snap_files = snapshot()

    # B db 档
    sr.install(cfg=cfg_db, src=src, force=True)
    s_db = sr.state()
    repo = sr._state["repo"]
    checks.append({"name": "B1 db 档 mode=db 且无回退",
                   "match": s_db["mode"] == "db" and s_db["fallback_reason"] is None
                   and s_db["repo_ready"]})
    checks.append({"name": "B2 自举来自文件",
                   "match": (s_db["bootstrap"] or {}).get("mode") == "from_files"})
    checks.append({"name": "B3 读函数替换 >= 14", "match": len(s_db["patched"]) >= 14,
                   "detail": len(s_db["patched"])})
    checks.append({"name": "B4 写后同步包装 >= 10", "match": len(s_db["writes"]) >= 10,
                   "detail": len(s_db["writes"])})
    counts = {t: repo.count(t) for t in ASSET_TABLES}
    checks.append({"name": "B5 资产表自举条数 > 0（至少 holdings；其余随 eod 生成）",
                   "match": counts.get("holdings", 0) > 0, "detail": counts})
    checks.append({"name": "B6 stops 表 payload == stops.json",
                   "match": _stops_equal(src, repo)})

    # C 逐字段等价
    snap_db = snapshot()
    for k in sorted(snap_files):
        compare(f"C/{k} files vs db", snap_files[k], snap_db[k], checks)
    saved = hide_files()
    try:
        snap_hidden = snapshot()
        for k in sorted(snap_files):
            compare(f"C/{k} files vs 文件不可见", snap_files[k], snap_hidden[k], checks)
    finally:
        restore_files(saved)

    # D 探针：只有库里有的数据，读函数必须看见
    probe = "sz999999"
    repo.put_row("holdings", {"code": probe},
                 {"code": probe, "name": "探针", "cost": None, "quantity": None})
    checks.append({"name": "D1 库内新增 holdings 立刻可读",
                   "match": probe in store.codes()
                   and any(r.get("code") == probe for r in store.load()),
                   "detail": probe})
    repo.delete_row("holdings", {"code": probe})
    checks.append({"name": "D2 删除探针后恢复",
                   "match": probe not in store.codes()})

    day = "2099-01-01"
    repo.put_row("news_daily", {"date": day},
                 {"date": day, "items": [], "counts": {}, "summary": "探针"})
    checks.append({"name": "D3 库内新增 news 立刻可读",
                   "match": day in news.list_dates()
                   and (news.load(day) or {}).get("date") == day})
    repo.delete_row("news_daily", {"date": day})
    checks.append({"name": "D4 删除探针后恢复",
                   "match": day not in news.list_dates()})

    bad = [c for c in checks if not c["match"]]
    return {"ok": not bad, "src": src, "checks": len(checks),
            "passed": len(checks) - len(bad), "failed": len(bad),
            "state": {k: v for k, v in s_db.items() if k != "bootstrap"},
            "bootstrap": s_db.get("bootstrap"), "table_counts": counts,
            "details": checks}


def _stops_equal(src: str, repo) -> bool:
    p = os.path.join(src, "portfolio", "stops.json")
    if not os.path.isfile(p):
        return True
    with open(p, encoding="utf-8") as f:
        file_obj = json.load(f)
    from db import bridge as b
    return b.db_stops_get(repo) == file_obj


def main() -> int:
    ap = argparse.ArgumentParser(description="读路径切换验收（内存库、不落盘）")
    ap.add_argument("--src", default=os.path.join(BACKEND, "data"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    import contextlib
    with contextlib.redirect_stdout(sys.stderr if a.json else sys.stdout):
        res = run(os.path.abspath(a.src))
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(f"[read-switch] {res['passed']}/{res['checks']} 通过，失败 {res['failed']}")
        for c in res["details"]:
            if not c["match"]:
                print("  FAIL", c["name"], json.dumps(c, ensure_ascii=False)[:400])
        print("  bootstrap =", json.dumps(res["bootstrap"], ensure_ascii=False)[:300])
        print("  tables =", json.dumps(res["table_counts"], ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
