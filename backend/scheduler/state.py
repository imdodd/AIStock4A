"""调度状态持久化：job_defs（可读定义）+ job_runs（去重 / 追补 / 审计）。

刻意不放进阶段 3 的资产 repository：那些表有 data/ 源文件、要参与 importer 对账，
而这两张表是运行时写入、没有源文件，混在一起会被 ``--allow-empty`` 误清空。

所有 SQL 参数化；表名固定，不接受外部标识符。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Any

from db.repository import dumps


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JobState:
    def __init__(self, repo):
        # repo 需提供 connection / cfg.owner（SqliteRepository）。
        # 与资产库共用同一连接与文件，避免两个连接互相抢写锁。
        self.repo = repo
        self.conn = repo.connection
        self.owner = repo.cfg.owner
        self._lock = threading.RLock()

    # ------------------------------------------------------------ job_defs
    def list_defs(self, only_enabled: bool = False) -> list[dict]:
        sql = "SELECT * FROM job_defs WHERE owner=?"
        if only_enabled:
            sql += " AND enabled=1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, (self.owner,)).fetchall()
        return [self._def_row(r) for r in rows]

    @staticmethod
    def _def_row(r) -> dict:
        try:
            params = json.loads(r["params"] or "{}")
        except ValueError:
            params = {}
        return {"id": r["id"], "job_type": r["job_type"], "cron": r["cron"],
                "params": params, "enabled": int(r["enabled"]),
                "note": r["note"], "updated_at": r["updated_at"]}

    def get_def(self, job_id: str) -> dict | None:
        with self._lock:
            r = self.conn.execute(
                "SELECT * FROM job_defs WHERE owner=? AND id=?",
                (self.owner, job_id)).fetchone()
        return self._def_row(r) if r else None

    def put_def(self, job_id: str, job_type: str, cron: str,
                params: dict | None = None, enabled: int = 1,
                note: str | None = None) -> dict:
        if not job_id or not job_type or not cron:
            raise ValueError("id / job_type / cron 均不能为空")
        if not isinstance(params or {}, dict):
            raise ValueError("params 必须是对象")
        with self._lock:
            self.conn.execute(
                "INSERT INTO job_defs(owner,id,job_type,cron,params,enabled,note,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(owner,id) DO UPDATE SET job_type=excluded.job_type, "
                "cron=excluded.cron, params=excluded.params, enabled=excluded.enabled, "
                "note=excluded.note, updated_at=excluded.updated_at",
                (self.owner, job_id, job_type, cron, dumps(params or {}),
                 int(bool(enabled)), note, _now()))
        return self.get_def(job_id)

    def delete_def(self, job_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM job_defs WHERE owner=? AND id=?", (self.owner, job_id))
            return cur.rowcount > 0

    # ------------------------------------------------------------ job_runs
    def try_start(self, job_type: str, trade_date: str) -> bool:
        """同一 (owner, job_type, trade_date) 只允许跑一次。

        INSERT OR IGNORE 是原子去重：哪怕两个进程同时触发，也只有一个返回 True。
        """
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO job_runs"
                "(owner,job_type,trade_date,status,started_at,finished_at,detail) "
                "VALUES(?,?,?,?,?,NULL,NULL)",
                (self.owner, job_type, trade_date, "running", _now()))
            return cur.rowcount > 0

    def finish(self, job_type: str, trade_date: str, status: str,
               detail: Any = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE job_runs SET status=?, finished_at=?, detail=? "
                "WHERE owner=? AND job_type=? AND trade_date=?",
                (status, _now(), dumps(detail) if detail is not None else None,
                 self.owner, job_type, trade_date))

    def get_run(self, job_type: str, trade_date: str) -> dict | None:
        with self._lock:
            r = self.conn.execute(
                "SELECT * FROM job_runs WHERE owner=? AND job_type=? AND trade_date=?",
                (self.owner, job_type, trade_date)).fetchone()
        return dict(r) if r else None

    def has_success(self, job_type: str, trade_date: str) -> bool:
        with self._lock:
            r = self.conn.execute(
                "SELECT 1 FROM job_runs WHERE owner=? AND job_type=? AND trade_date=? "
                "AND status='ok'", (self.owner, job_type, trade_date)).fetchone()
        return bool(r)

    def last_success(self, job_type: str) -> str | None:
        with self._lock:
            r = self.conn.execute(
                "SELECT MAX(trade_date) FROM job_runs WHERE owner=? AND job_type=? "
                "AND status='ok'", (self.owner, job_type)).fetchone()
        return r[0] if r else None

    def list_runs(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM job_runs WHERE owner=? "
                "ORDER BY trade_date DESC, job_type LIMIT ?",
                (self.owner, max(1, min(int(limit), 200)))).fetchall()
        return [dict(r) for r in rows]

    def reset_run(self, job_type: str, trade_date: str) -> bool:
        """手工重跑时清掉当日锁；仅供 /api/jobs/{id}/run 与运维使用。"""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM job_runs WHERE owner=? AND job_type=? AND trade_date=?",
                (self.owner, job_type, trade_date))
            return cur.rowcount > 0


__all__ = ["JobState"]
