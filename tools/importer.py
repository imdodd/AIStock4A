"""文件体系 → SQLite 的幂等导入器（阶段 3）。

用法（在仓库根目录）：
  python tools/importer.py --src backend/data --db :memory:
  python tools/importer.py --src <生产 data> --db :memory: --json
  python tools/importer.py --src <dir> --core-only        # 只导确认的 7 张表
  python tools/importer.py --rollback                     # 归档库文件，不删除

也可用 ASTOCK_SRC / ASTOCK_DB_PATH 环境变量代替 --src / --db。
**源目录全程只读**；默认不写生产目录，也不会改动任何业务文件。

四道护栏：
  1) 幂等：整表 replace_all + 内容指纹主键，重复跑条数与内容都不变；
  2) 可校验：每表导入前后条数对齐，且库里 payload 与源 JSON 逐条相等；
  3) 可回滚：--rollback 只把库文件归档到 data/db_backup/<时间戳>/，绝不删除；
  4) 防误清空：源里某表为空而库里已有数据时，默认跳过该表（--allow-empty 才覆盖）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)

from db import (ASSET_TABLES, CORE_TABLES, TABLES, SqliteRepository,  # noqa: E402
                dumps, fingerprint, load_config)
from db.conn import ExternalBackendNotReady  # noqa: E402

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- 源文件读取
def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _ts(path: str) -> str:
    """用文件 mtime 当 updated_at：源文件不变，重复导入的库内容就完全不变。"""
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    except OSError:
        return "1970-01-01T00:00:00"


def _rows(path: str, key_field: str) -> list[tuple[dict, dict, str]]:
    if not os.path.isfile(path):
        return []
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    ts = _ts(path)
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        # realized 是只增台账，主键是内容指纹而不是文件里的某个字段
        key = fingerprint(row) if key_field == "fingerprint" else row.get(key_field)
        if not key:
            continue
        out.append(({key_field: str(key)}, row, ts))
    return out


def _single(path: str, key: dict) -> list[tuple[dict, dict, str]]:
    if not os.path.isfile(path):
        return []
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [(key, data, _ts(path))]


def _tracking_plans(src: str) -> list[tuple[dict, dict, str]]:
    base = os.path.join(src, "tracking")
    out = []
    if not os.path.isdir(base):
        return out
    for code in sorted(os.listdir(base)):
        p = os.path.join(base, code, "meta.json")
        if not os.path.isfile(p):
            continue
        try:
            meta = _read_json(p)
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict):
            meta.setdefault("code", code)
            out.append(({"code": str(meta.get("code") or code)}, meta, _ts(p)))
    return out


def _tracking_periods(src: str) -> list[tuple[dict, dict, str]]:
    base = os.path.join(src, "tracking")
    out = []
    if not os.path.isdir(base):
        return out
    for code in sorted(os.listdir(base)):
        d = os.path.join(base, code, "periods")
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(d, fn)
            try:
                period = _read_json(p)
            except (OSError, ValueError):
                continue
            if not isinstance(period, dict):
                continue
            day = str(period.get("date") or fn[:-5])
            if not DAY_RE.match(day):
                continue
            period.setdefault("date", day)
            out.append(({"code": code, "date": day}, period, _ts(p)))
    return out


def _news(src: str) -> list[tuple[dict, dict, str]]:
    d = os.path.join(src, "news")
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json") or not DAY_RE.match(fn[:-5]):
            continue
        p = os.path.join(d, fn)
        try:
            payload = _read_json(p)
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("date", fn[:-5])
            out.append(({"date": str(payload["date"])}, payload, _ts(p)))
    return out


def collect(src: str, tables: tuple[str, ...]) -> tuple[dict[str, list], list[str]]:
    """返回 {表名: [(key, payload, ts), ...]} 与警告列表。"""
    port = os.path.join(src, "portfolio")
    getters = {
        "holdings": lambda: _rows(os.path.join(port, "holdings.json"), "code"),
        "watchlist": lambda: _rows(os.path.join(port, "watchlist.json"), "code"),
        "stops": lambda: _single(os.path.join(port, "stops.json"), {"scope": "default"}),
        "realized": lambda: _rows(os.path.join(port, "realized.json"), "fingerprint"),
        "pnl_daily": lambda: _rows(os.path.join(port, "pnl_daily.json"), "date"),
        "tracking_plans": lambda: _tracking_plans(src),
        "tracking_periods": lambda: _tracking_periods(src),
        "news_daily": lambda: _news(src),
    }
    result, warnings = {}, []
    for t in tables:
        rows = getters[t]()
        result[t] = rows
        if not rows:
            warnings.append(f"{t}: 源里没有可导入的数据（{TABLES[t].source}）")
    return result, warnings


# ---------------------------------------------------------------- 比对 / 摘要
def _norm(rows: list[tuple[dict, dict, str]]) -> dict[str, str]:
    """按主键建 {key: payload指纹}，与顺序无关。"""
    return {dumps(k): fingerprint(p) for k, p, _ in rows}


def _digest(repo: SqliteRepository, tables: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for t in tables:
        payloads = [dumps(r) for r in repo.list_rows(t)]
        h.update(t.encode())
        h.update(dumps(payloads).encode("utf-8"))
    return h.hexdigest()[:32]


def verify(repo: SqliteRepository, collected: dict[str, list],
           tables: tuple[str, ...]) -> dict:
    detail = {}
    ok = True
    for t in tables:
        expected = _norm(collected[t])
        # 主键列由库直接给出，不从 payload 反推（period 文件的 code 只在目录名里）
        actual = {dumps({k: str(v) for k, v in key.items()}): fingerprint(payload)
                  for key, payload in repo.list_entries(t)}
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(k for k in set(expected) & set(actual)
                         if expected[k] != actual[k])
        good = not missing and not extra and not changed
        ok = ok and good
        detail[t] = {"expected": len(expected), "actual": len(actual),
                     "missing": len(missing), "extra": len(extra),
                     "changed": len(changed), "match": good}
    return {"ok": ok, "tables": detail}


# ---------------------------------------------------------------- 主流程
def apply_tables(repo: SqliteRepository, collected: dict[str, list],
                 tables: tuple[str, ...], allow_empty: bool = False) -> tuple[dict, dict, list]:
    """把一批源数据整表灌进库。抽出成函数，便于单独验证「防误清空」护栏。"""
    before = {t: repo.count(t) for t in tables}
    applied: dict[str, int] = {}
    skipped: list[str] = []
    with repo.tx():
        for t in tables:
            if not collected[t] and before[t] > 0 and not allow_empty:
                skipped.append(f"{t}: 源为空而库里有 {before[t]} 行，已跳过（防误清空）")
                continue
            applied[t] = repo.replace_all(t, collected[t])
    return before, applied, skipped


def do_rollback(cfg, dry_run: bool) -> dict:
    if cfg.memory:
        return {"ok": False, "reason": ":memory: 库没有文件可归档"}
    if cfg.backend != "sqlite":
        return {"ok": False, "reason": "只支持 SQLite 库文件归档"}
    path = cfg.path
    if not os.path.isfile(path):
        return {"ok": False, "reason": f"库文件不存在：{path}"}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(os.path.dirname(path), "db_backup", stamp)
    files = [path] + [path + s for s in ("-wal", "-shm")
                      if os.path.exists(path + s)]
    if dry_run:
        return {"ok": True, "dry_run": True, "from": path, "to": dest,
                "files": [os.path.basename(f) for f in files]}
    os.makedirs(dest, exist_ok=True)
    moved = []
    for f in files:
        shutil.move(f, os.path.join(dest, os.path.basename(f)))
        moved.append(os.path.basename(f))
    return {"ok": True, "from": path, "to": dest, "files": moved,
            "note": "旧库已归档，未删除；下次启动会重建空库"}


def run(a) -> dict:
    cfg = load_config()
    if a.db:
        from dataclasses import replace
        cfg = replace(cfg, path=a.db)
    if a.rollback:
        return {"action": "rollback", **do_rollback(cfg, a.dry_run)}

    src = a.src or os.environ.get("ASTOCK_SRC") or ""
    if not src:
        return {"ok": False, "error": "必须显式指定 --src（或 ASTOCK_SRC），工具不内置私有路径"}
    src = os.path.abspath(src)
    if not os.path.isdir(src):
        return {"ok": False, "error": f"源目录不存在：{src}"}

    tables = CORE_TABLES if a.core_only else ASSET_TABLES
    collected, warnings = collect(src, tables)
    out: dict[str, Any] = {
        "ok": True, "action": "import", "src": src, "db": "<memory>" if cfg.memory else cfg.path,
        "tables_requested": list(tables), "dry_run": a.dry_run,
        "source_counts": {t: len(v) for t, v in collected.items()},
        "warnings": warnings, "errors": [],
    }
    if a.dry_run:
        return out

    try:
        repo = SqliteRepository(cfg)
    except (ExternalBackendNotReady, PermissionError, RuntimeError, ValueError) as e:
        out["ok"] = False
        out["errors"].append(f"{type(e).__name__}: {e}")
        return out

    try:
        before, applied, skipped = apply_tables(repo, collected, tables, a.allow_empty)
        after = {t: repo.count(t) for t in tables}
        out["before"] = before
        out["applied"] = applied
        out["after"] = after
        out["skipped_guards"] = skipped

        checked = tuple(t for t in tables if t not in
                        {s.split(":")[0] for s in skipped})
        v = verify(repo, collected, checked)
        out["verify"] = v
        digest1 = _digest(repo, checked)

        # 幂等复跑：再来一遍完整导入，条数与内容摘要都不许变
        apply_tables(repo, collected, checked, a.allow_empty)
        after2 = {t: repo.count(t) for t in tables}
        digest2 = _digest(repo, checked)
        out["repeat"] = {"after": after2, "digest_1": digest1, "digest_2": digest2,
                         "idempotent": digest1 == digest2 and after == after2}
        out["stats"] = repo.stats()

        out["ok"] = (v["ok"] and out["repeat"]["idempotent"]
                     and all(after[t] == len(collected[t]) for t in checked))
        if not out["ok"]:
            out["errors"].append("校验或幂等复跑未通过，请检查 verify / repeat 明细")
        repo.meta_set("last_import", json.dumps(
            {"src": os.path.basename(src), "at": datetime.now().isoformat(timespec="seconds"),
             "tables": {t: len(collected[t]) for t in tables}}, ensure_ascii=False))
    except Exception as e:                                   # noqa: BLE001
        out["ok"] = False
        out["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        repo.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="文件体系 → SQLite 幂等导入器")
    ap.add_argument("--src", help="源 data/ 目录（必填，或设 ASTOCK_SRC）")
    ap.add_argument("--db", help="SQLite 路径；:memory: 表示不落盘（或设 ASTOCK_DB_PATH）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不建库不写入")
    ap.add_argument("--core-only", action="store_true", help="只导确认的 7 张核心资产表")
    ap.add_argument("--allow-empty", action="store_true",
                    help="允许用空源覆盖库里已有数据（默认禁止，防误清空）")
    ap.add_argument("--rollback", action="store_true", help="归档库文件到 db_backup/，不删除")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()
    res = run(a)
    print(json.dumps(res, ensure_ascii=False, indent=1 if a.json else None))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
