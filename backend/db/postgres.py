"""PostgreSQL 适配器骨架（只留接口，不装驱动、不做默认）。

同 mysql.py：决策 1 只要求「留接口」，不要求现在实现。启用前需自行安装驱动
（例如 psycopg）、把 db/schema.sql 翻成 PG 方言，并让 repository.py 的
UPSERT 语句走 PG 的 ON CONFLICT 参数化写法。当前一律显式报错，避免静默降级。
"""
from __future__ import annotations

from db.conn import ExternalBackendNotReady


def connect(cfg):
    raise ExternalBackendNotReady(
        "PostgreSQL 后端尚未实现：请安装驱动、实现方言化 DDL 与 SQL 生成器后再启用。"
        f" 当前 STORAGE_BACKEND=postgres，DB_DSN={'<已设置>' if cfg.dsn else '<空>'}。"
        " 临时可设 STORAGE_BACKEND=sqlite 使用内置数据库。"
    )


def ddl_dialect() -> str:
    raise ExternalBackendNotReady("PostgreSQL 版 schema.sql 尚未提供")


__all__ = ["connect", "ddl_dialect"]
