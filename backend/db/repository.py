"""Repository：业务代码只看这一层，不碰 SQL。

接口刻意做得很窄：load / put / delete / list / replace_all / meta。
所有表都是「主键列 + 索引列 + payload」形状，payload 是不可拆的原始 JSON，
因此库读回的对象与文件读出的对象逐字段相等 —— 这是「换存储不改变 API 响应」
的关键保障。

SQL 里的表名与列名全部来自 tables.TABLES 白名单，不接受外部字符串拼接，
避免标识符注入。值一律参数化。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Iterator

from db import conn as conn_mod
from db.tables import ASSET_TABLES, TABLES, TableSpec


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def fingerprint(payload: Any) -> str:
    """只增台账（realized）的幂等键：内容相同即同一条，重复导入不会增生。"""
    return hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Repository(ABC):
    @abstractmethod
    def init_schema(self) -> str: ...

    @abstractmethod
    def put_row(self, table: str, key: dict, payload: dict,
                ts: str | None = None) -> None: ...

    @abstractmethod
    def get_row(self, table: str, key: dict) -> dict | None: ...

    @abstractmethod
    def delete_row(self, table: str, key: dict) -> bool: ...

    @abstractmethod
    def list_rows(self, table: str, **filters) -> list[dict]: ...

    @abstractmethod
    def count(self, table: str) -> int: ...

    @abstractmethod
    def replace_all(self, table: str, items: Iterable[tuple[dict, dict, str]]) -> int: ...

    @abstractmethod
    def meta_get(self, key: str) -> str | None: ...

    @abstractmethod
    def meta_set(self, key: str, value: str) -> None: ...

    @abstractmethod
    def stats(self) -> dict: ...

    @abstractmethod
    def close(self) -> None: ...


class SqliteRepository(Repository):
    def __init__(self, cfg: conn_mod.StorageConfig | None = None):
        self.cfg = cfg or conn_mod.load_config()
        if self.cfg.backend != "sqlite":
            conn_mod.connect(self.cfg)       # 外部后端在这里显式报错，不静默降级
            raise conn_mod.ExternalBackendNotReady(self.cfg.backend)
        self.connection = conn_mod.connect(self.cfg)
        self._lock = threading.RLock()
        self.init_schema()

    # ------------------------------------------------------------ 基础
    def init_schema(self) -> str:
        with self._lock:
            return conn_mod.init_schema(self.connection)

    @staticmethod
    def _spec(table: str) -> TableSpec:
        if table not in TABLES:
            raise KeyError(f"未知表 {table!r}，可用：{sorted(TABLES)}")
        return TABLES[table]

    @staticmethod
    def _check_key(spec: TableSpec, key: dict) -> None:
        missing = [k for k in spec.pk if k not in key]
        if missing:
            raise ValueError(f"表 {spec.name} 的主键缺字段：{missing}")

    def _insert_sql(self, spec: TableSpec) -> str:
        idents = ("owner", *spec.pk, *spec.columns, "payload", "updated_at")
        marks = ",".join("?" for _ in idents)
        sets = [f"{c}=excluded.{c}" for c in (*spec.columns, "payload", "updated_at")]
        conflict = ",".join(("owner", *spec.pk))
        return (f"INSERT INTO {spec.name} ({','.join(idents)}) VALUES ({marks}) "
                f"ON CONFLICT({conflict}) DO UPDATE SET {','.join(sets)}")

    def _put_unlocked(self, table: str, key: dict, payload: dict,
                      ts: str | None) -> None:
        spec = self._spec(table)
        self._check_key(spec, key)
        if not isinstance(payload, dict):
            raise TypeError(f"表 {table} 的 payload 必须是 dict，收到 {type(payload).__name__}")
        projected = spec.project(payload, key) or {}
        values = [self.cfg.owner]
        values += [key[k] for k in spec.pk]
        values += [projected.get(c) for c in spec.columns]
        values += [dumps(payload), ts or _now()]
        self.connection.execute(self._insert_sql(spec), values)

    def put_row(self, table: str, key: dict, payload: dict,
                ts: str | None = None) -> None:
        with self._lock:
            self._put_unlocked(table, key, payload, ts)

    # ------------------------------------------------------------ 读
    def _where(self, spec: TableSpec, key: dict) -> tuple[str, list]:
        allowed = {"owner", *spec.pk, *spec.columns}
        parts, values = [], []
        for k, v in key.items():
            if k not in allowed:
                raise ValueError(f"表 {spec.name} 不支持按 {k!r} 过滤")
            parts.append(f"{k}=?")
            values.append(v)
        return (" AND ".join(parts) or "1=1"), values

    def get_row(self, table: str, key: dict) -> dict | None:
        spec = self._spec(table)
        with self._lock:
            where, values = self._where(spec, {"owner": self.cfg.owner, **key})
            row = self.connection.execute(
                f"SELECT payload FROM {spec.name} WHERE {where} LIMIT 1", values).fetchone()
        return json.loads(row[0]) if row else None

    def list_rows(self, table: str, **filters) -> list[dict]:
        spec = self._spec(table)
        with self._lock:
            where, values = self._where(spec, {"owner": self.cfg.owner, **filters})
            order = ",".join(spec.pk)
            rows = self.connection.execute(
                f"SELECT payload FROM {spec.name} WHERE {where} ORDER BY {order}",
                values).fetchall()
        return [json.loads(r[0]) for r in rows]

    def list_rows_insertion(self, table: str, **filters) -> list[dict]:
        """按写入顺序（rowid）返回，而不是按主键排序。

        holdings.json / watchlist.json 是**有序清单**：用户增删的先后就是前端
        列表的顺序，store.load() / watchstore.load() 原样返回文件顺序。若 DB
        读回按 code 排序，/api/holdings 与 /api/watchlist 的响应顺序会变，
        逐字段 diff 立刻暴露。rowid 在 replace_all 时按文件顺序递增，
        UPSERT 已有行不改变 rowid、新行追加到末尾，与文件的 append 语义一致。
        """
        spec = self._spec(table)
        with self._lock:
            where, values = self._where(spec, {"owner": self.cfg.owner, **filters})
            rows = self.connection.execute(
                f"SELECT payload FROM {spec.name} WHERE {where} ORDER BY rowid",
                values).fetchall()
        return [json.loads(r[0]) for r in rows]

    def list_entries(self, table: str, **filters) -> list[tuple[dict, dict]]:
        """返回 (主键列, payload)，主键来自真实的列而不是从 payload 反推。

        有些源文件的键只体现在路径上（例如 tracking/<code>/periods/<date>.json
        的 payload 里没有 code 字段），对账必须走这个方法才不会漏行。
        """
        spec = self._spec(table)
        with self._lock:
            where, values = self._where(spec, {"owner": self.cfg.owner, **filters})
            cols = ",".join(spec.pk)
            rows = self.connection.execute(
                f"SELECT {cols}, payload FROM {spec.name} WHERE {where} ORDER BY {cols}",
                values).fetchall()
        return [({k: r[k] for k in spec.pk}, json.loads(r["payload"])) for r in rows]

    def count(self, table: str) -> int:
        spec = self._spec(table)
        with self._lock:
            return int(self.connection.execute(
                f"SELECT COUNT(*) FROM {spec.name} WHERE owner=?",
                (self.cfg.owner,)).fetchone()[0])

    def delete_row(self, table: str, key: dict) -> bool:
        spec = self._spec(table)
        self._check_key(spec, key)
        with self._lock:
            where, values = self._where(spec, {"owner": self.cfg.owner, **key})
            cur = self.connection.execute(f"DELETE FROM {spec.name} WHERE {where}", values)
            return cur.rowcount > 0

    # ------------------------------------------------------------ 事务 / 批量
    @contextmanager
    def tx(self) -> Iterator["SqliteRepository"]:
        """显式事务。嵌套时复用外层事务，避免 BEGIN within a transaction。"""
        with self._lock:
            nested = self.connection.in_transaction
            if not nested:
                self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self
                if not nested:
                    self.connection.execute("COMMIT")
            except Exception:
                if not nested:
                    self.connection.execute("ROLLBACK")
                raise

    def replace_all(self, table: str,
                    items: Iterable[tuple[dict, dict, str]]) -> int:
        """用一批行整体替换该 owner 的这张表（在一个事务里）。

        与「只 UPSERT」不同，源里删掉的行也会在库里删掉，保证库 === 文件。
        items 元素为 (key, payload, ts)。
        """
        spec = self._spec(table)
        rows = list(items)
        with self.tx():
            self.connection.execute(f"DELETE FROM {spec.name} WHERE owner=?",
                                    (self.cfg.owner,))
            for key, payload, ts in rows:
                self._put_unlocked(table, key, payload, ts)
        return len(rows)

    # ------------------------------------------------------------ 元数据
    def meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT INTO schema_meta(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at", (key, value, _now()))

    def stats(self) -> dict:
        return {"config": self.cfg.describe(),
                "schema_version": conn_mod.schema_version(self.connection),
                "tables": {t: self.count(t) for t in ASSET_TABLES},
                "job_runs": int(self.connection.execute(
                    "SELECT COUNT(*) FROM job_runs").fetchone()[0])}

    def close(self) -> None:
        with self._lock:
            self.connection.close()


_singleton: SqliteRepository | None = None
_singleton_lock = threading.RLock()


def get_repository(cfg: conn_mod.StorageConfig | None = None,
                   fresh: bool = False) -> Repository:
    """进程内单例。验证脚本用 fresh=True 拿独立实例，避免互相污染。"""
    global _singleton
    if fresh:
        return SqliteRepository(cfg)
    with _singleton_lock:
        if _singleton is None:
            _singleton = SqliteRepository(cfg)
        return _singleton


__all__ = ["Repository", "SqliteRepository", "get_repository", "dumps", "fingerprint"]
