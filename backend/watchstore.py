"""用户自选观察池的本地存储（唯一可写来源）。

分工（与持仓 store.py 完全对称）：
    data/portfolio/watchlist.json   用户维护的自选名单（本模块读写，前端增删）
    data/snapshot/watchlist.json    每天重算的行情视图（build_snapshot 产出，勿手改）

以前自选写死在 config/universe.py，改一次得动代码；现在改成文件，前端就能直接增删。
首次运行若文件不存在，自动用 universe.WATCHLIST 播种，老数据不丢。
删除会同步从当日快照里剔除，前端立刻生效，不必等下一次收盘快照。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(DATA_DIR, "portfolio")
PATH = os.path.join(DIR, "watchlist.json")
SNAPSHOT = os.path.join(DATA_DIR, "snapshot", "watchlist.json")

_lock = threading.RLock()


def _seed() -> list[dict]:
    from config.universe import WATCHLIST
    now = datetime.now().isoformat(timespec="seconds")
    return [dict(code=c, name=n, added_at=now, note=None) for c, n in WATCHLIST]


def _write(rows: list[dict]) -> None:
    os.makedirs(DIR, exist_ok=True)
    try:
        bdir = os.path.join(DIR, "backup")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(bdir, f"watchlist-{stamp}.json"), "w", encoding="utf-8") as bf:
            json.dump(rows, bf, ensure_ascii=False, indent=1)
        olds = sorted(f for f in os.listdir(bdir) if f.startswith("watchlist-"))
        for f in olds[:-30]:
            os.remove(os.path.join(bdir, f))
    except Exception:
        pass
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)
    try:
        with open(os.path.join(DIR, "watchlist.log"), "a", encoding="utf-8") as lf:
            lf.write(f"{datetime.now().isoformat(timespec='seconds')} write {len(rows)} rows: "
                     f"{','.join(r.get('code', '?') for r in rows)}\n")
    except Exception:
        pass


def load() -> list[dict]:
    """读取自选清单；文件缺失时用 universe 播种并落盘。"""
    with _lock:
        if not os.path.exists(PATH):
            rows = _seed()
            _write(rows)
            return rows
        try:
            with open(PATH, encoding="utf-8") as f:
                rows = json.load(f)
        except Exception:
            return _seed()
        if not isinstance(rows, list):
            return _seed()
        return [r for r in rows if isinstance(r, dict) and r.get("code")]


def codes() -> list[str]:
    return [r["code"] for r in load()]


def items() -> list[tuple[str, str]]:
    return [(r["code"], r.get("name") or r["code"]) for r in load()]


def add(code: str, name: str | None = None) -> tuple[dict, bool]:
    """新增自选。返回 (记录, 是否新建)。代码规范化复用持仓 store.norm_code。"""
    import store as portfolio_store
    code = portfolio_store.norm_code(code)
    if not code:
        raise ValueError("代码格式不对，应为 6 位数字或在前面加 sh/sz/bj")
    with _lock:
        rows = load()
        for r in rows:
            if r["code"] == code:
                if name:
                    r["name"] = name
                    _write(rows)
                return r, False
        row = dict(code=code, name=name or code,
                   added_at=datetime.now().isoformat(timespec="seconds"), note=None)
        rows.append(row)
        _write(rows)
        return row, True


def remove(code: str) -> bool:
    """从自选清单删除；同时把当日行情快照里的该行剔掉，让页面立刻生效。"""
    import store as portfolio_store
    code = portfolio_store.norm_code(code) or code
    with _lock:
        rows = load()
        keep = [r for r in rows if r["code"] != code]
        if len(keep) == len(rows):
            return False
        _write(keep)
    try:
        if os.path.exists(SNAPSHOT):
            with open(SNAPSHOT, encoding="utf-8") as f:
                snap = json.load(f)
            if isinstance(snap, list):
                snap = [r for r in snap if r.get("code") != code]
                tmp = SNAPSHOT + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, SNAPSHOT)
    except Exception:
        pass                      # 快照写失败不影响清单本身，下次 EOD 会重建
    return True
