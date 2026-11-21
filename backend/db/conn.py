"""存储层连接、档位解析与 schema 初始化。

零新增依赖：SQLite 走标准库 sqlite3。MySQL / PostgreSQL 只做 DSN 识别并给出
明确的「需自行安装驱动并实现方言」提示，不装驱动、不静默降级 —— 这是已确认的
决策 1（默认内置 SQLite，其他数据库通过 repository 层接入）。

环境变量：
  STORAGE_BACKEND   sqlite | mysql | postgres（不填则按 DB_DSN 推断）
  DB_DSN            mysql://... / postgresql://...；sqlite 档可留空
  ASTOCK_DB_PATH    SQLite 文件路径；设 :memory: 可完全不落盘
  ASTOCK_OWNER      多用户预留，默认 'default'
  STORAGE_READ      files（默认）| db —— 只影响 bridge，不接主链路时无副作用
  STORAGE_DUAL_WRITE 1 = 后续接线时同时写文件与库，默认关
  DB_JOURNAL        wal（默认）| memory —— 只读环境可用 memory 避免创建 -wal 文件
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.abspath(os.path.join(DATA_DIR, "astock.db"))
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
SCHEMA_VERSION = "1.1"   # 1.1: 阶段 4 加入 job_defs（CREATE TABLE IF NOT EXISTS，老库自动升级）

_VALID_BACKENDS = {"sqlite", "mysql", "postgres"}


@dataclass(frozen=True)
class StorageConfig:
    backend: str
    path: str
    dsn: str
    owner: str
    read_from: str
    dual_write: bool
    journal: str

    @property
    def memory(self) -> bool:
        return self.path == ":memory:"

    def describe(self) -> dict:
        return {"backend": self.backend, "path": "<memory>" if self.memory else self.path,
                "owner": self.owner, "read_from": self.read_from,
                "dual_write": self.dual_write, "journal": self.journal,
                "has_dsn": bool(self.dsn)}


def _infer_backend(dsn: str) -> str:
    d = dsn.lower()
    if d.startswith(("mysql://", "mysql+")):
        return "mysql"
    if d.startswith(("postgres://", "postgresql://", "postgres+")):
        return "postgres"
    return "sqlite"


def load_config(env: dict[str, str] | None = None) -> StorageConfig:
    e = os.environ if env is None else env
    dsn = (e.get("DB_DSN") or "").strip()
    backend = (e.get("STORAGE_BACKEND") or "").strip().lower() or _infer_backend(dsn)
    if backend not in _VALID_BACKENDS:
        raise ValueError(f"STORAGE_BACKEND 只能是 {sorted(_VALID_BACKENDS)}，收到 {backend!r}")

    path = (e.get("ASTOCK_DB_PATH") or "").strip()
    if not path and backend == "sqlite" and dsn.startswith("sqlite:///"):
        path = dsn[len("sqlite:///"):]
    if not path:
        path = DEFAULT_DB_PATH

    cfg = StorageConfig(
        backend=backend,
        path=path,
        dsn=dsn,
        owner=(e.get("ASTOCK_OWNER") or "default").strip() or "default",
        read_from=(e.get("STORAGE_READ") or "files").strip().lower(),
        dual_write=(e.get("STORAGE_DUAL_WRITE") or "").strip() in ("1", "true", "yes"),
        journal=(e.get("DB_JOURNAL") or "wal").strip().lower(),
    )
    if cfg.read_from not in ("files", "db"):
        raise ValueError("STORAGE_READ 只能是 files 或 db")
    return cfg


class ExternalBackendNotReady(NotImplementedError):
    """MySQL / PostgreSQL 只留接口，驱动与方言由部署方自行实现。"""


def connect(cfg: StorageConfig | None = None) -> sqlite3.Connection:
    """打开一个 SQLite 连接。外部后端在这里显式报错，不静默退回 SQLite。"""
    cfg = cfg or load_config()
    if cfg.backend != "sqlite":
        from db import mysql, postgres
        adapter = mysql if cfg.backend == "mysql" else postgres
        return adapter.connect(cfg)          # 必然抛 ExternalBackendNotReady

    if not cfg.memory:
        parent = os.path.dirname(os.path.abspath(cfg.path))
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise PermissionError(
                f"无法创建数据库目录 {parent}：{exc}。"
                "可设 ASTOCK_DB_PATH=:memory: 做不落盘验证，或在可写目录指定路径。"
            ) from exc
    try:
        conn = sqlite3.connect(cfg.path, check_same_thread=False, isolation_level=None,
                               timeout=10.0)
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"无法打开 SQLite 数据库 {cfg.path!r}：{exc}。"
            "当前进程若为只读沙箱，请改用 ASTOCK_DB_PATH=:memory:。"
        ) from exc

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute(f"PRAGMA journal_mode={cfg.journal.upper()}")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass                                   # 只读/内存库不支持时忽略，不影响功能
    return conn


def init_schema(conn: sqlite3.Connection, version: str = SCHEMA_VERSION) -> str:
    """执行 DDL 并写入版本号。可重复调用。"""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        ddl = f.read()
    conn.executescript(ddl)
    conn.execute(
        "INSERT INTO schema_meta(key, value, updated_at) VALUES('schema_version', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (version, datetime.now().isoformat(timespec="seconds")),
    )
    return version


def schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


_conn_lock = threading.RLock()

__all__ = ["StorageConfig", "load_config", "connect", "init_schema", "schema_version",
           "ExternalBackendNotReady", "SCHEMA_VERSION", "DEFAULT_DB_PATH", "SCHEMA_PATH"]
