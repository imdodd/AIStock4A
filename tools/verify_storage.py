"""文件态 vs DB 态的逐函数对账（阶段 3）。

只做一件事：把同一份源数据分别交给「原来的存储模块」和「DB 版 bridge」，
逐个公开函数比对返回值。全部一致，才说明将来把 store.* 换成 repository
不会改变 43 个路由看到的业务数据。

用法：
  python tools/verify_storage.py --src backend/data --db :memory:
  python tools/verify_storage.py --src <生产 data> --db :memory: --json

源目录全程只读；文件版通过改写模块级路径常量指向 --src，不改任何文件内容。
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

from db import ASSET_TABLES, SqliteRepository, dumps, fingerprint, load_config  # noqa: E402
from db import bridge as b  # noqa: E402
from importer import collect  # noqa: E402

import news, pnl, tracking, watchstore, store  # noqa: E402


def bind_files(src: str) -> None:
    """让现有存储模块读 --src 而不是 backend/data（只改本进程内的全局常量）。"""
    port = os.path.join(src, "portfolio")
    store.DIR = port
    store.PATH = os.path.join(port, "holdings.json")
    watchstore.DIR = port
    watchstore.PATH = os.path.join(port, "watchlist.json")
    watchstore.SNAPSHOT = os.path.join(src, "snapshot", "watchlist.json")
    news.DIR = os.path.join(src, "news")
    news.SECTORS_JSON = os.path.join(src, "snapshot", "sectors.json")
    pnl.DIR = port
    pnl.HISTORY_PATH = os.path.join(port, "pnl_daily.json")
    pnl.REALIZED_PATH = os.path.join(port, "realized.json")
    tracking.DIR = os.path.join(src, "tracking")
    tracking.MEMBERS = os.path.join(src, "snapshot", "members")


def _key(obj) -> str:
    return dumps(obj)


def compare(name: str, left, right, as_set: bool = False) -> dict:
    if as_set and isinstance(left, list) and isinstance(right, list):
        ls = {fingerprint(x) for x in left if isinstance(x, dict)}
        rs = {fingerprint(x) for x in right if isinstance(x, dict)}
        good = ls == rs
        detail = {"left": len(ls), "right": len(rs),
                  "only_left": len(ls - rs), "only_right": len(rs - ls)}
    else:
        good = _key(left) == _key(right)
        detail = {"left_type": type(left).__name__, "right_type": type(right).__name__}
        if not good:
            l = _key(left)
            r = _key(right)
            i = next((i for i, (x, y) in enumerate(zip(l, r)) if x != y),
                     min(len(l), len(r)))
            detail["first_diff_at"] = i
            detail["left_excerpt"] = l[max(0, i - 40):i + 120]
            detail["right_excerpt"] = r[max(0, i - 40):i + 120]
    return {"name": name, "match": good, **detail}


def run(src: str, cfg) -> dict:
    bind_files(src)
    collected, warns = collect(src, ASSET_TABLES)
    repo = SqliteRepository(cfg)
    try:
        with repo.tx():
            for t in ASSET_TABLES:
                repo.replace_all(t, collected[t])

        checks = []
        checks.append(compare("holdings.load / db_holdings_load",
                              store.load(), b.db_holdings_load(repo), as_set=True))
        checks.append(compare("holdings.codes / db_holdings_codes",
                              store.codes(), b.db_holdings_codes(repo), as_set=True))
        checks.append(compare("watchlist.load / db_watchlist_load",
                              watchstore.load(), b.db_watchlist_load(repo), as_set=True))
        checks.append(compare("watchlist.items / db_watchlist_items",
                              watchstore.items(), b.db_watchlist_items(repo), as_set=True))
        checks.append(compare("pnl.load_realized / db_realized_list",
                              pnl.load_realized(), b.db_realized_list(repo), as_set=True))
        checks.append(compare("pnl.load_history / db_pnl_history",
                              pnl.load_history(), b.db_pnl_history(120, repo), as_set=True))

        # 止损是整体对象，直接读原始 JSON 与库里 payload 比
        stops_path = os.path.join(src, "portfolio", "stops.json")
        stops_file = None
        if os.path.isfile(stops_path):
            with open(stops_path, encoding="utf-8") as f:
                stops_file = json.load(f)
        checks.append(compare("stops.json / db_stops_get",
                              stops_file, b.db_stops_get(repo)))

        checks.append(compare("tracking.list_all / db_plans_index",
                              tracking.list_all(), b.db_plans_index(repo)))
        for code in sorted(tracking.list_all().keys()):
            checks.append(compare(f"tracking.load({code}) / db_plan_get",
                                  tracking.load(code), b.db_plan_get(code, repo)))
            checks.append(compare(f"tracking.period_list({code}) / db_period_list",
                                  tracking.period_list(code), b.db_period_list(code, repo),
                                  as_set=True))

        checks.append(compare("news.list_dates / db_news_dates",
                              news.list_dates(), b.db_news_dates(10 ** 6, repo)))
        checks.append(compare("news.index / db_news_index",
                              news.index(30), b.db_news_index(30, repo)))
        for day in news.list_dates():
            checks.append(compare(f"news.load({day}) / db_news_get",
                                  news.load(day), b.db_news_get(day, repo)))

        bad = [c for c in checks if not c["match"]]
        return {"ok": not bad, "src": os.path.abspath(src),
                "db": "<memory>" if cfg.memory else cfg.path,
                "checks": len(checks), "passed": len(checks) - len(bad),
                "failed": len(bad), "details": checks,
                "warnings": warns,
                "table_counts": repo.stats()["tables"]}
    finally:
        repo.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="文件态 vs DB 态逐函数对账")
    ap.add_argument("--src", help="源 data/ 目录（或设 ASTOCK_SRC）")
    ap.add_argument("--db", help="SQLite 路径；:memory: 不落盘")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    src = a.src or os.environ.get("ASTOCK_SRC") or ""
    if not src:
        print("必须显式指定 --src（或 ASTOCK_SRC），工具不内置私有路径")
        return 2
    cfg = load_config()
    if a.db:
        cfg = replace(cfg, path=a.db)
    res = run(os.path.abspath(src), cfg)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(f"[verify] {res['passed']}/{res['checks']} 通过，失败 {res['failed']}")
        for c in res["details"]:
            if not c["match"]:
                print("  FAIL", c["name"], json.dumps(c, ensure_ascii=False)[:300])
        print("  tables =", json.dumps(res["table_counts"], ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
