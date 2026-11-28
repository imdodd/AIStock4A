"""调度状态持久化：job_defs（可读定义）+ job_runs（去重 / 追补 / 审计）。

刻意不放进阶段 3 的资产 repository：那些表有 data/ 源文件、要参与 importer 对账，
而这两张表是运行时写入、没有源文件，混在一起会被 ``--allow-empty`` 误清空。

所有 SQL 参数化；表名固定，不接受外部标识符。
"""
from __future__ import annotations

import json
import os
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
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """老库补 ``pid`` 列。

        ``schema.sql`` 是 CREATE TABLE IF NOT EXISTS，已存在的表不会长出新列，
        这里做一次幂等 ALTER，避免升级后 try_start 写 pid 直接报错。
        """
        with self._lock:
            try:
                cols = {r[1] for r in self.conn.execute(
                    "PRAGMA table_info(job_runs)").fetchall()}
            except Exception:                       # noqa: BLE001
                return
            if cols and "pid" not in cols:
                try:
                    self.conn.execute("ALTER TABLE job_runs ADD COLUMN pid INTEGER")
                except Exception:                   # noqa: BLE001
                    pass

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
        """同一 (owner, job_type, trade_date) 同一时刻只允许一个执行者。

        语义是「占位」，不只是「插入」：
          * 已是 ``ok``  → 当天已完成，不重跑（省配额，这是原语义）；
          * 已是 ``running`` → 有人占着，不抢；
          * ``error`` / 被回收的孤儿 → **允许重占**，否则一次失败就把当天
            锁死到收盘，重启也救不回来。
        用 UPSERT 的 WHERE 过滤表达这套规则，写锁由 SQLite 序列化，并发下
        仍只有一个返回 True。
        同时记下占用者 pid，供重启后辨别孤儿占位。
        """
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO job_runs"
                "(owner,job_type,trade_date,status,started_at,finished_at,detail,pid) "
                "VALUES(?,?,?,?,?,NULL,NULL,?) "
                "ON CONFLICT(owner,job_type,trade_date) DO UPDATE SET "
                "status='running', started_at=excluded.started_at, "
                "finished_at=NULL, detail=NULL, pid=excluded.pid "
                "WHERE job_runs.status NOT IN ('running','ok')",
                (self.owner, job_type, trade_date, "running", _now(), os.getpid()))
            return cur.rowcount > 0

    def reclaim_orphans(self) -> list[dict]:
        """回收上个进程遗留的 running 占位，返回被回收的行。

        进程重启（含被手动重启、崩溃、任务管理器结束）时，正在跑的任务
        来不及走 ``finish``，数据库里就留下一条 ``status='running'``。
        这条行会让后续所有 ``try_start`` 都返回 False —— 当天该任务既不会
        正常触发、也不会被追补，前端还一直显示「运行中」，且毫无告警。

        判据用 pid 而不是时间：EOD 的成分股分析单次就可能跑 40 分钟以上，
        按超时猜会把真在跑的任务误杀；pid 不是当前进程则一定不是我在跑。
        服务为单进程部署（nssm 未开 --reload），pid 复用也只会指向另一个
        不会去 finish 这行的进程，回收依然安全。
        """
        me = os.getpid()
        where = ("owner=? AND status='running' AND (pid IS NULL OR pid<>?)")
        with self._lock:
            rows = self.conn.execute(
                f"SELECT job_type, trade_date, started_at, pid FROM job_runs WHERE {where}",
                (self.owner, me)).fetchall()
            if not rows:
                return []
            self.conn.execute(
                f"UPDATE job_runs SET status='error', finished_at=?, detail=? WHERE {where}",
                (_now(),
                 dumps({"error": "进程重启中断，已回收孤儿占位",
                        "reason": "orphan_running_reclaimed"}),
                 self.owner, me))
        return [dict(r) for r in rows]

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
