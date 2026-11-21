"""统一数据目录解析（阶段 6 · 数据目录分家）。

默认行为完全不变：``ASTOCK_DATA_DIR`` 未设置时仍读 ``backend/data``，
也就是影子差分用的冻结夹具，8100 / 8101 的零差异基线不受影响。

设置 ``ASTOCK_DATA_DIR`` 后，服务进程和它拉起的所有 CLI 子任务（EOD、
pregen、资讯等）都改用同一份目录。于是一套代码可以同时跑两套实例：

* 8100：不设变量 → 冻结夹具 → 继续做逐字段差分；
* 8103：设变量 → 真实数据目录 → 自己抓数、自己长数据。

路径只在导入期解析一次；子进程继承环境变量后重新导入，天然一致。
"""
from __future__ import annotations

import os
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(HERE, "data")


def data_dir() -> str:
    """当前生效的数据目录绝对路径。"""
    raw = (os.environ.get("ASTOCK_DATA_DIR") or "").strip()
    if not raw:
        return DEFAULT_DATA_DIR
    return os.path.abspath(raw if os.path.isabs(raw) else os.path.join(HERE, raw))


DATA_DIR = data_dir()
DATA_PATH = Path(DATA_DIR)


def sub(*parts: str) -> str:
    """拼一个当前数据目录下的子路径。"""
    return os.path.join(DATA_DIR, *parts)


def describe() -> dict:
    """给运维页 / 健康检查展示，不泄露任何密钥内容。"""
    return {
        "dir": DATA_DIR,
        "is_default": os.path.normcase(DATA_DIR) == os.path.normcase(DEFAULT_DATA_DIR),
        "env": (os.environ.get("ASTOCK_DATA_DIR") or "").strip() or None,
    }


__all__ = ["DATA_DIR", "DATA_PATH", "data_dir", "sub", "describe", "DEFAULT_DATA_DIR"]
