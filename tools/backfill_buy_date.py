"""给 realized.json 的历史清仓记录回填买入日期（buy_date）。

背景：早期版本的 record_realized() 只记了清仓日、成本、数量，没记买入日期；
而持仓的 added_at 在删除那一刻就被一起删掉了。好在 store 每次写盘都会在
data/portfolio/backup/ 下留一份 holdings-YYYYMMDD-HHMMSS.json 快照，
快照里的每只股票都带 added_at —— 用它来反推。

匹配策略（逐级降级，找不到就留空，绝不猜）：
  1) 清仓日当天及之前、时间上最后一个包含该股票的快照 → 取 added_at
  2) 若 1) 没有，退到「最早包含该股票」的快照（适用于快照留档前就已在仓的票）
  3) 都没有 → buy_date 保持 None，前端显示「—」

用法（在仓库根目录执行）：
    .venv\\Scripts\\python.exe tools\\backfill_buy_date.py --data-dir backend/data-live --dry-run
    .venv\\Scripts\\python.exe tools\\backfill_buy_date.py --data-dir backend/data-live
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime

STAMP = re.compile(r"holdings-(\d{8})-(\d{6})\.json$")


def sync_db(rows: list[dict], backend_dir: str) -> str:
    """把台账同步进 SQLite。

    STORAGE_READ=db 时服务读的是库、不是文件 —— 只改 JSON 会白忙一场。
    另外 realized 是「只增台账」且主键是 payload 指纹，单条 UPSERT 会因为
    指纹变化变成多插一行，所以必须用 replace_all 整表替换（库 === 文件）。
    """
    sys.path.insert(0, os.path.abspath(backend_dir))
    # 脚本是独立进程，不会自动继承服务的存储配置；用与 main.py 同一个加载器读仓库根 .env，
    # 否则 STORAGE_READ 默认 files，会误判成「不需要同步库」而白白跳过。
    import env_loader                                       # noqa: E402
    env_loader.load_env()
    from db import conn as db_conn                          # noqa: E402
    from db.repository import get_repository, fingerprint   # noqa: E402

    cfg = db_conn.load_config()
    if cfg.read_from != "db":
        return "跳过：STORAGE_READ != db，服务直接读文件"
    bak = None
    if not cfg.memory and os.path.exists(cfg.path):
        bak = cfg.path + f".bak-{datetime.now():%Y%m%d-%H%M%S}"
        src = sqlite3.connect(cfg.path, timeout=15)
        dst = sqlite3.connect(bak)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    repo = get_repository()
    items = [({"fingerprint": fingerprint(r)}, r,
              r.get("recorded_at") or datetime.now().isoformat(timespec="seconds"))
             for r in rows]
    n = repo.replace_all("realized", items)
    return f"{n} 行写入 {cfg.path}" + (f"（旧库备份 {os.path.basename(bak)}）" if bak else "")


def load_snapshots(backup_dir: str) -> list[tuple[datetime, str]]:
    """返回 [(快照时间, 文件路径)]，按时间升序。"""
    out = []
    for path in glob.glob(os.path.join(backup_dir, "holdings-*.json")):
        m = STAMP.search(os.path.basename(path))
        if not m:
            continue
        d, t = m.group(1), m.group(2)
        try:
            ts = datetime.strptime(d + t, "%Y%m%d%H%M%S")
        except ValueError:
            continue
        out.append((ts, path))
    out.sort(key=lambda x: x[0])
    return out


def snapshot_rows(path: str) -> dict[str, str | None]:
    """快照内 code → added_at。"""
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        return {}
    if not isinstance(rows, list):
        return {}
    return {r.get("code"): r.get("added_at") for r in rows if r.get("code")}


def pick_buy_date(code: str, sell_date: str, snaps) -> tuple[str | None, str]:
    """返回 (buy_date, 依据说明)。"""
    cut = datetime.fromisoformat(sell_date + "T23:59:59")
    before = [(ts, p) for ts, p in snaps if ts <= cut]
    # 1) 清仓日及之前，时间上最后一个包含该股的快照
    for ts, path in reversed(before):
        added = snapshot_rows(path).get(code)
        if added:
            return str(added)[:10], f"清仓前最后快照 {ts:%m-%d %H:%M}"
    # 2) 退到最早包含该股的快照
    for ts, path in snaps:
        added = snapshot_rows(path).get(code)
        if added:
            return str(added)[:10], f"最早快照 {ts:%m-%d %H:%M}（清仓日无快照）"
    return None, "快照里没出现过该股"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, help="live 数据目录，如 backend/data-live")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写回")
    ap.add_argument("--no-sync-db", action="store_true",
                    help="只改 JSON、不同步 SQLite（服务读库时会看不到结果）")
    args = ap.parse_args()

    pf = os.path.join(os.path.abspath(args.data_dir), "portfolio")
    realized_path = os.path.join(pf, "realized.json")
    if not os.path.exists(realized_path):
        print(f"[x] 找不到台账：{realized_path}")
        return 1
    snaps = load_snapshots(os.path.join(pf, "backup"))
    print(f"快照 {len(snaps)} 份，时间范围 "
          f"{snaps[0][0]:%Y-%m-%d %H:%M} ~ {snaps[-1][0]:%Y-%m-%d %H:%M}" if snaps else "没有可用快照")

    with open(realized_path, encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        print("[x] realized.json 不是数组")
        return 1

    filled = skipped = missed = 0
    for r in rows:
        code, sell = r.get("code"), str(r.get("date") or "")[:10]
        if not code or not sell:
            continue
        if r.get("buy_date"):
            skipped += 1
            print(f"  = {sell} {r.get('name') or code:<8} 已有买入日期 {r['buy_date']}")
            continue
        buy, how = pick_buy_date(code, sell, snaps)
        if buy:
            r["buy_date"] = buy
            filled += 1
            print(f"  + {sell} {r.get('name') or code:<8} 买入 {buy}  ({how})")
        else:
            missed += 1
            print(f"  ! {sell} {r.get('name') or code:<8} 无法回填：{how}")

    print(f"\n合计：回填 {filled} 条 · 已有 {skipped} 条 · 无法回填 {missed} 条")
    if args.dry_run:
        print("（--dry-run，未写入）")
        return 0

    if filled:
        bak = realized_path + f".bak-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(realized_path, bak)
        tmp = realized_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        os.replace(tmp, realized_path)
        print(f"已写入 {realized_path}\n旧文件备份：{bak}")
    else:
        print("JSON 里没有待补的买入日期（已全部就位），仍会同步一次数据库")

    # JSON 只是文件版；服务读库时以 SQLite 为准，必须一起同步
    if not args.no_sync_db:
        try:
            backend_dir = os.path.dirname(os.path.abspath(args.data_dir))
            print("DB 同步：" + sync_db(rows, backend_dir))
        except Exception as e:      # 同步失败不影响文件，但必须说清楚
            print(f"[!] DB 同步失败：{type(e).__name__}: {e}")
            print("    JSON 已改好，但服务若读库（STORAGE_READ=db）仍会看不到买入日期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
