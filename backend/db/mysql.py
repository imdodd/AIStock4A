"""MySQL 适配器骨架（只留接口，不装驱动、不做默认）。

已确认决策 1：默认内置 SQLite；MySQL / PostgreSQL 通过 repository 层接入。
要启用时做三件事：
  1) 自行安装驱动（例如 pip install pymysql），本仓库不把它写进 requirements.txt；
  2) 在下面实现 connect()，返回一个兼容 DB-API 的连接对象；
  3) 把 db/schema.sql 翻成 MySQL 方言（AUTOINCREMENT / ON CONFLICT 语法不同），
     并提供对应的 Sql 语句生成器 —— repository.py 的 SQL 目前是 SQLite 方言。
在此之前显式报错，绝不静默退回 SQLite，避免「以为写进了 MySQL，其实写进了本地文件」。
"""
from __future__ import annotations

from db.conn import ExternalBackendNotReady


def connect(cfg):
    raise ExternalBackendNotReady(
        "MySQL 后端尚未实现：请安装驱动、实现方言化 DDL 与 SQL 生成器后再启用。"
        f" 当前 STORAGE_BACKEND=mysql，DB_DSN={'<已设置>' if cfg.dsn else '<空>'}。"
        " 临时可设 STORAGE_BACKEND=sqlite 使用内置数据库。"
    )


def ddl_dialect() -> str:
    raise ExternalBackendNotReady("MySQL 版 schema.sql 尚未提供")


__all__ = ["connect", "ddl_dialect"]
