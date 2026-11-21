"""DB 版存取函数：与现有 store / watchstore / news / tracking / pnl 同形状。

定位（很重要）：这一层**只做存取，不重写业务规则**。
聚合统计（news.save）、盈亏汇总（pnl.record）、止损计算（stops.recompute）等
仍由原来的绿区模块负责；它们算好完整 payload 之后交给这里落库即可。

这样做有两个好处：
  - 绿区模块一行不改，sha256 仍与生产一致，43 个路由响应不受任何影响；
  - verify_storage.py 可以拿同一份夹具，逐函数比对「文件版」与「DB 版」结果，
    在真正切换读路径之前就证明语义等价。

当前主链路默认仍是 files；切换点在后续阶段（把 store.* 等调用换成这里），
本文件不自动接管任何请求。
"""
from __future__ import annotations

from typing import Any

from db.repository import Repository, fingerprint, get_repository


def _repo(repo: Repository | None) -> Repository:
    # 必须是进程内单例：:memory: 库每次新连接都是空的，逐函数链式调用会读不到刚写的数据。
    return repo if repo is not None else get_repository()


# ---------------------------------------------------------------- 持仓 / 自选
def db_holdings_load(repo: Repository | None = None) -> list[dict]:
    # 有序清单：必须保持文件里的先后顺序，不能按 code 排序（见 list_rows_insertion）
    return [r for r in _repo(repo).list_rows_insertion("holdings") if r.get("code")]


def db_holdings_codes(repo: Repository | None = None) -> list[str]:
    return [r["code"] for r in db_holdings_load(repo)]


def db_holdings_put(row: dict, repo: Repository | None = None) -> dict:
    _repo(repo).put_row("holdings", {"code": row["code"]}, row)
    return row


def db_holdings_remove(code: str, repo: Repository | None = None) -> bool:
    return _repo(repo).delete_row("holdings", {"code": code})


def db_watchlist_load(repo: Repository | None = None) -> list[dict]:
    return [r for r in _repo(repo).list_rows_insertion("watchlist") if r.get("code")]


def db_watchlist_items(repo: Repository | None = None) -> list[tuple[str, str]]:
    return [(r["code"], r.get("name") or r["code"]) for r in db_watchlist_load(repo)]


def db_watchlist_put(row: dict, repo: Repository | None = None) -> dict:
    _repo(repo).put_row("watchlist", {"code": row["code"]}, row)
    return row


def db_watchlist_remove(code: str, repo: Repository | None = None) -> bool:
    return _repo(repo).delete_row("watchlist", {"code": code})


# ---------------------------------------------------------------- 台账 / 止损
def db_stops_get(repo: Repository | None = None) -> dict | None:
    return _repo(repo).get_row("stops", {"scope": "default"})


def db_stops_put(data: dict, repo: Repository | None = None) -> dict:
    _repo(repo).put_row("stops", {"scope": "default"}, data)
    return data


def db_realized_list(repo: Repository | None = None) -> list[dict]:
    return _repo(repo).list_rows("realized")


def db_realized_append(entry: dict, repo: Repository | None = None) -> dict:
    _repo(repo).put_row("realized", {"fingerprint": fingerprint(entry)}, entry)
    return entry


def db_pnl_history(limit: int = 10, repo: Repository | None = None) -> list[dict]:
    rows = sorted((r for r in _repo(repo).list_rows("pnl_daily") if r.get("date")),
                  key=lambda r: r["date"])
    return rows[-max(1, min(int(limit or 10), 120)):]


def db_pnl_put(rec: dict, repo: Repository | None = None) -> dict:
    _repo(repo).put_row("pnl_daily", {"date": rec["date"]}, rec)
    return rec


# ---------------------------------------------------------------- 跟踪方案
def db_plan_get(code: str, repo: Repository | None = None) -> dict | None:
    return _repo(repo).get_row("tracking_plans", {"code": code})


def db_plan_put(meta: dict, repo: Repository | None = None) -> dict:
    _repo(repo).put_row("tracking_plans", {"code": meta["code"]}, meta)
    return meta


def db_plan_remove(code: str, repo: Repository | None = None) -> dict | None:
    """与 tracking.remove 一致：软删除，status=stopped，不真删。"""
    r = _repo(repo)
    meta = r.get_row("tracking_plans", {"code": code})
    if not meta:
        return None
    meta = dict(meta)
    meta["status"] = "stopped"
    from datetime import datetime
    meta["stopped_at"] = datetime.now().isoformat(timespec="seconds")
    r.put_row("tracking_plans", {"code": code}, meta)
    return meta


def db_period_put(code: str, period: dict,
                  repo: Repository | None = None) -> dict:
    from datetime import datetime
    period.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    _repo(repo).put_row("tracking_periods",
                        {"code": code, "date": period["date"]}, period)
    return period


def db_period_list(code: str, repo: Repository | None = None) -> list[dict]:
    rows = _repo(repo).list_rows("tracking_periods", code=code)
    return sorted(rows, key=lambda x: x.get("date", ""), reverse=True)


def db_plans_index(repo: Repository | None = None) -> dict:
    """从 plans + periods 重建 index.json —— 库里不需要存这份可重建产物。"""
    r = _repo(repo)
    idx: dict[str, dict] = {}
    for meta in r.list_rows("tracking_plans"):
        code = meta.get("code")
        if not code:
            continue
        periods = db_period_list(code, r)
        idx[code] = dict(
            name=meta.get("name"), status=meta.get("status"),
            freq_label=(meta.get("frequency") or {}).get("label"),
            dims=len(meta.get("dimensions") or []),
            last_date=periods[0]["date"] if periods else None,
            last_verdict=periods[0].get("verdict") if periods else None,
            n_periods=len(periods),
            automation_id=(meta.get("automation") or {}).get("id"),
            automation_state=(meta.get("automation") or {}).get("state"),
        )
    return idx


# ---------------------------------------------------------------- 资讯
def db_news_dates(limit: int = 30, repo: Repository | None = None) -> list[str]:
    dates = sorted((r.get("date") for r in _repo(repo).list_rows("news_daily")
                    if r.get("date")), reverse=True)
    return dates[:limit]


def db_news_get(date: str | None = None,
                repo: Repository | None = None) -> dict | None:
    r = _repo(repo)
    if date:
        return r.get_row("news_daily", {"date": date})
    dates = db_news_dates(1, r)
    return r.get_row("news_daily", {"date": dates[0]}) if dates else None


def db_news_put(payload: dict, repo: Repository | None = None) -> dict:
    """接收 news.save() 已聚合好的完整日文件，不在这里重算 counts / heat。"""
    _repo(repo).put_row("news_daily", {"date": payload["date"]}, payload)
    return payload


def db_news_index(limit: int = 30, repo: Repository | None = None) -> dict:
    r = _repo(repo)
    all_dates = db_news_dates(10 ** 6, r)
    rows = []
    for day in all_dates[:limit]:
        d = db_news_get(day, r) or {}
        rows.append(dict(date=day, items=len(d.get("items") or []),
                         summary=d.get("summary") or "",
                         generated_at=d.get("generated_at") or "",
                         bull=(d.get("counts") or {}).get("利多", 0),
                         bear=(d.get("counts") or {}).get("利空", 0)))
    return {"dates": all_dates[:limit],
            "latest": all_dates[0] if all_dates else None,
            "rows": rows, "total": len(all_dates)}


__all__ = [n for n in globals() if n.startswith("db_")]
