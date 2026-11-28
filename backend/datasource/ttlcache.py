"""子调用级 TTL 缓存：把缓存粒度从「整包」降到「每个请求」。

## 为什么需要它

个股证据包原来是「整包按日落盘」：``data/cases/{day}/{code}.json``，命中就整份返回，
未命中就把 7 个源全部重打。这带来两个后果：

    09:40 建的包，下午 15:00 打开还是 09:40 的数据（整包 TTL 实际 = 一天）；
    一旦 refresh=1，7 个子调用全部重打，缓存等于不存在。

## 做法

每个取数函数自带「键 + TTL + 落盘标签」，命中就返回，不命中才打上游。
键里带日期，于是「今天 vs 历史」天然分开：历史日的数据永不过期，当日的按秒级 TTL。

    cached("ann:sh600887", 6 * 3600, lambda: fetch_ann(code), disk="ann")
    cached("hist:sh600887:2026-11-27", 60, lambda: fetch_kline(code), disk="kline")
    cached("lhb:sh600887:2026-11-27", INF, lambda: fetch_lhb(code, day), disk="lhb")

## 参数语义

    ttl       内存有效期（秒）。0 表示不缓存（每次都打）
    disk      落盘标签（None 表示只放内存）。文件落在 ``<DATA_DIR>/cache/ttl/<标签>/``
    disk_ttl  落盘有效期（秒）。None 表示与 ttl 相同；``INF`` 表示永久

## 为什么内存和磁盘用两套 TTL

盘中同一个进程里反复点开同一只票，走内存 60s 就够；而「下午 3 点打开，
应该看到 9:40 之后的最新数据」要求内存过期后能落到磁盘再看一眼——
磁盘那份是同一天内其它进程（EOD / 补数脚本）抓的，比重新打上游便宜。
历史日则两边都永久，因为收盘后数据不再变。

## 刷新语义

``drop(prefix)`` 只清指定前缀，不碰其它。于是 ``refresh=1`` 可以只清
``intraday:`` / ``hist:`` / ``flow:`` 这三个「盘中会变」的键，
公告、龙虎榜、新闻、涨停池照旧命中缓存——7 次外呼降到 2~3 次。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from typing import Any, Callable

from data_paths import DATA_DIR

INF = math.inf
DISK_ROOT = os.path.join(DATA_DIR, "cache", "ttl")

_MEM: dict[str, tuple[float, Any]] = {}
_LOCK = threading.Lock()
_HITS = {"mem": 0, "disk": 0, "miss": 0, "put": 0}


def _slug(key: str) -> str:
    """键 → 文件名安全串。保留前缀可读性，尾部加 hash 防截断碰撞。"""
    base = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(key))[:80].strip("_") or "k"
    return f"{base}.{hashlib.sha1(str(key).encode('utf-8')).hexdigest()[:8]}"


def _path(key: str, tag: str) -> str:
    return os.path.join(DISK_ROOT, tag, _slug(key) + ".json")


def _disk_get(key: str, tag: str, ttl: float) -> tuple[bool, Any]:
    """返回 (是否命中, 值)。文件损坏/过期都按未命中处理，绝不让缓存错误冒泡。"""
    try:
        with open(_path(key, tag), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001
        return False, None
    at = float(d.get("at") or 0)
    if ttl is not INF and time.time() - at > ttl:
        return False, None
    return True, d.get("v")


def _disk_put(key: str, tag: str, value: Any) -> None:
    try:
        p = _path(key, tag)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"key": key, "at": time.time(), "v": value},
                      f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


def get(key: str, ttl: float, disk: str | None = None,
        disk_ttl: float | None = None) -> tuple[bool, Any]:
    """两级读：内存 → 磁盘。返回 (是否命中, 值)。"""
    if ttl <= 0:
        return False, None
    with _LOCK:
        hit = _MEM.get(key)
    if hit and time.time() - hit[0] <= ttl:
        _HITS["mem"] += 1
        return True, hit[1]
    if disk:
        ok, v = _disk_get(key, disk, ttl if disk_ttl is None else disk_ttl)
        if ok:
            _HITS["disk"] += 1
            with _LOCK:
                _MEM[key] = (time.time(), v)
            return True, v
    _HITS["miss"] += 1
    return False, None


def put(key: str, value: Any, disk: str | None = None) -> None:
    with _LOCK:
        _MEM[key] = (time.time(), value)
        _HITS["put"] += 1
    if disk:
        _disk_put(key, disk, value)


def cached(key: str, ttl: float, fn: Callable[[], Any], *,
           disk: str | None = None, disk_ttl: float | None = None) -> Any:
    ok, v = get(key, ttl, disk, disk_ttl)
    if ok:
        return v
    v = fn()
    put(key, v, disk)
    return v


def drop(prefix: str = "") -> int:
    """清掉指定前缀的键（内存 + 磁盘）。prefix 为空则全清。返回清掉的键数。

    只用于「盘中可变」的键。公告/龙虎榜这类一天只变几次的不要清，
    否则 refresh 又退化成全量重打。
    """
    n = 0
    with _LOCK:
        for k in [k for k in _MEM if k.startswith(prefix)]:
            _MEM.pop(k, None)
            n += 1
    if os.path.isdir(DISK_ROOT):
        pat = _slug(prefix)[:-9] if prefix else ""      # 去掉尾部 ".xxxxxxxx"
        for tag in os.listdir(DISK_ROOT):
            d = os.path.join(DISK_ROOT, tag)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if not fn.endswith(".json"):
                    continue
                if pat and not fn.startswith(pat):
                    continue
                try:
                    os.remove(os.path.join(d, fn))
                    n += 1
                except OSError:
                    pass
    return n


def stats() -> dict:
    with _LOCK:
        return {**_HITS, "mem_keys": len(_MEM), "disk_root": DISK_ROOT}
