"""用户持仓清单的本地存储（唯一可写来源）。

两份 holdings 的分工：
    data/portfolio/holdings.json   用户自己维护的持仓名单（本模块读写，前端增删）
    data/snapshot/holdings.json    每天重算的行情视图（build_snapshot 产出，勿手改）

以前持仓名单写死在 config/universe.py，改一次得动代码；现在改成文件，前端就能直接增删。
首次运行若文件不存在，自动用 universe.HOLDINGS 播种，老数据不丢。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(DATA_DIR, "portfolio")
PATH = os.path.join(DIR, "holdings.json")

_lock = threading.RLock()


# ---------------------------------------------------------------- 代码规范化
def norm_code(raw: str) -> str:
    """把用户输入规范化成 sh/sz/bj 前缀的六位代码

    接受 002897 / sz002897 / SZ.002897 / 002897.SZ 等写法。
    """
    s = (raw or "").strip().lower().replace(".", "").replace(" ", "")
    if not s:
        return ""
    for pre in ("sh", "sz", "bj"):
        if s.startswith(pre):
            s = s[len(pre):]
            break
    if s.endswith("sh") or s.endswith("sz") or s.endswith("bj"):
        s = s[:-2]
    if not (len(s) == 6 and s.isdigit()):
        return ""
    if s[0] in ("6", "5", "9"):
        return "sh" + s
    if s[0] in ("8", "4"):
        return "bj" + s
    return "sz" + s


# ---------------------------------------------------------------- 读写
def _seed() -> list[dict]:
    from config.universe import HOLDINGS
    now = datetime.now().isoformat(timespec="seconds")
    return [dict(code=c, name=n, cost=None, quantity=None, added_at=now, note=None)
            for c, n in HOLDINGS]


def _write(rows: list[dict]) -> None:
    os.makedirs(DIR, exist_ok=True)
    # 每次写入留一份带时间戳的备份（保留最近 30 份），任何一次误删都能回滚
    try:
        bdir = os.path.join(DIR, "backup")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(bdir, f"holdings-{stamp}.json"), "w", encoding="utf-8") as bf:
            json.dump(rows, bf, ensure_ascii=False, indent=1)
        olds = sorted(f for f in os.listdir(bdir) if f.startswith("holdings-"))
        for f in olds[:-30]:
            os.remove(os.path.join(bdir, f))
    except Exception:
        pass
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)          # 原子替换，避免写一半断电把清单写坏
    # 审计日志：谁在什么时候把清单改成了几条
    try:
        with open(os.path.join(DIR, "holdings.log"), "a", encoding="utf-8") as lf:
            lf.write(f"{datetime.now().isoformat(timespec='seconds')} write {len(rows)} rows: "
                     f"{','.join(r.get('code', '?') for r in rows)}\n")
    except Exception:
        pass


def load() -> list[dict]:
    """读取持仓清单；文件缺失时用 universe 播种并落盘"""
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


def add(code: str, name: str | None = None, cost: float | None = None,
        quantity: float | None = None) -> tuple[dict, bool]:
    """新增或更新一条持仓。返回 (记录, 是否新建)"""
    code = norm_code(code)
    if not code:
        raise ValueError("代码格式不对，应为 6 位数字或在前面加 sh/sz/bj")
    with _lock:
        rows = load()
        for r in rows:
            if r["code"] == code:
                if name:
                    r["name"] = name
                if cost is not None:
                    r["cost"] = cost
                if quantity is not None:
                    r["quantity"] = quantity
                _write(rows)
                return r, False
        row = dict(code=code, name=name or code, cost=cost, quantity=quantity,
                   added_at=datetime.now().isoformat(timespec="seconds"), note=None)
        rows.append(row)
        _write(rows)
        return row, True


def update(code: str, **fields) -> dict | None:
    code = norm_code(code)
    with _lock:
        rows = load()
        for r in rows:
            if r["code"] == code:
                for k in ("name", "cost", "quantity", "note"):
                    if k in fields and fields[k] is not None:
                        r[k] = fields[k]
                _write(rows)
                return r
    return None


def remove(code: str) -> bool:
    code = norm_code(code)
    with _lock:
        rows = load()
        keep = [r for r in rows if r["code"] != code]
        if len(keep) == len(rows):
            return False
        _write(keep)
        return True
