"""astock-v2 存储层。

目录职责：blueprint 里原本拆成 db/（连接）与 repository/（业务接口）两个包，
这里合并为 db/ 一处 —— 两者共用 tables.py 的表定义，再拆一层只会多一次跳转。
外部代码只需要 `from db import get_repository`。

    db/
      schema.sql    DDL（SQLite 方言）
      tables.py     表清单 + payload → 索引列投影（唯一事实来源）
      conn.py       档位 / DSN / 连接 / schema 初始化
      repository.py Repository ABC + SQLite 实现 + 事务 / 工厂
      bridge.py     与现有存储模块同形状的 DB 版存取函数
      mysql.py / postgres.py   只留接口骨架，不装驱动
"""
from __future__ import annotations

from db.conn import (DEFAULT_DB_PATH, SCHEMA_VERSION, ExternalBackendNotReady,
                     StorageConfig, connect, init_schema, load_config, schema_version)
from db.repository import (Repository, SqliteRepository, dumps, fingerprint,
                           get_repository)
from db.tables import (ASSET_TABLES, CORE_TABLES, EXTRA_TABLES, SYSTEM_TABLES,
                       TABLES, TableSpec)

__all__ = ["get_repository", "Repository", "SqliteRepository", "StorageConfig",
           "load_config", "connect", "init_schema", "schema_version",
           "ExternalBackendNotReady", "DEFAULT_DB_PATH", "SCHEMA_VERSION",
           "TABLES", "TableSpec", "CORE_TABLES", "EXTRA_TABLES", "ASSET_TABLES",
           "SYSTEM_TABLES", "dumps", "fingerprint"]
