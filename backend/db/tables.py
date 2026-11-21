"""表清单与「payload → 索引列」投影规则。

这是 repository 与 importer 共用的唯一事实来源：新增一张表只改这里 + schema.sql，
不用去翻散落在各处的 SQL 字符串。

project(payload, key) 返回索引列的值。所有索引列都允许为 None —— 它们只服务于
查询与排序，真实数据永远以 payload 为准，绝不从这里还原业务对象。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any


def _holdings(row: dict, key: dict) -> dict:
    return {"name": row.get("name"), "cost": row.get("cost"),
            "quantity": row.get("quantity")}


def _watchlist(row: dict, key: dict) -> dict:
    return {"name": row.get("name")}


def _stops(row: dict, key: dict) -> dict:
    return {}


def _realized(row: dict, key: dict) -> dict:
    return {"code": row.get("code"), "date": row.get("date")}


def _pnl_daily(row: dict, key: dict) -> dict:
    return {}


def _plans(row: dict, key: dict) -> dict:
    freq = row.get("frequency") or {}
    return {"name": row.get("name"), "status": row.get("status"),
            "cad": freq.get("kind") if isinstance(freq, dict) else None}


def _periods(row: dict, key: dict) -> dict:
    return {}


def _news(row: dict, key: dict) -> dict:
    items = row.get("items") or []
    return {"item_count": len(items) if isinstance(items, list) else None}


@dataclass(frozen=True)
class TableSpec:
    name: str
    group: str                      # core | extra | system
    pk: tuple[str, ...]             # 不含 owner，owner 由 repository 统一注入
    columns: tuple[str, ...]        # 从 payload 投影出的索引列
    source: str                     # 相对 data/ 的源（说明用；glob 在 importer 里展开）
    project: Callable[[dict, dict], dict[str, Any]]


TABLES: dict[str, TableSpec] = {
    t.name: t for t in (
        TableSpec("holdings", "core", ("code",), ("name", "cost", "quantity"),
                  "portfolio/holdings.json", _holdings),
        TableSpec("watchlist", "core", ("code",), ("name",),
                  "portfolio/watchlist.json", _watchlist),
        TableSpec("stops", "core", ("scope",), (),
                  "portfolio/stops.json", _stops),
        TableSpec("realized", "core", ("fingerprint",), ("code", "date"),
                  "portfolio/realized.json", _realized),
        TableSpec("tracking_plans", "core", ("code",), ("name", "status", "cad"),
                  "tracking/*/meta.json", _plans),
        TableSpec("tracking_periods", "core", ("code", "date"), (),
                  "tracking/*/periods/*.json", _periods),
        TableSpec("news_daily", "core", ("date",), ("item_count",),
                  "news/*.json", _news),
        # 实测补充：portfolio/pnl_daily.json 依赖当日快照，事后不可重建
        TableSpec("pnl_daily", "extra", ("date",), (),
                  "portfolio/pnl_daily.json", _pnl_daily),
    )
}

CORE_TABLES: tuple[str, ...] = tuple(n for n, s in TABLES.items() if s.group == "core")
EXTRA_TABLES: tuple[str, ...] = tuple(n for n, s in TABLES.items() if s.group == "extra")
ASSET_TABLES: tuple[str, ...] = CORE_TABLES + EXTRA_TABLES
# 系统表不进 importer 的资产对账：它们由运行时写入，没有对应的 data/ 源文件。
SYSTEM_TABLES: tuple[str, ...] = ("job_runs", "job_defs", "schema_meta")

__all__ = ["TableSpec", "TABLES", "CORE_TABLES", "EXTRA_TABLES",
           "ASSET_TABLES", "SYSTEM_TABLES"]
