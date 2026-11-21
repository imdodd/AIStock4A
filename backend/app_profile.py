"""运行档位：单仓库、一份代码，靠 APP_PROFILE 切换私有/开源实现。

设计动机：旧做法是「生产目录 + 开源镜像」两份物理副本，结果 20 个 py 差 98 行、
8 个前端函数差 18 行，全靠人肉比对，同步脚本还可能把敏感文件带出去。
这里改成一份代码 + 私有实现放在 contrib/private/ 覆盖层（已被 .gitignore 排除）。

本模块只负责「读档位 + 给默认值」，不 import 任何业务模块，也不联网。
后续阶段 2/5 的 datasource、producer、search provider 从这里取默认值即可。
"""
from __future__ import annotations

import os

# 安全默认：什么都不配时按开源档跑，不会误用私有实现或外部 agent。
_PROFILES: dict[str, dict[str, object]] = {
    "oss": {
        "data_source": "tencent",
        "search_provider": "none",
        "producer": "llm_direct",
        "db_dsn": "",
        "private_layer": False,
    },
    "private": {
        "data_source": "tencent",
        "search_provider": "search_api",
        "producer": "llm_direct",
        "db_dsn": "",
        "private_layer": True,
    },
}

# 环境变量优先级高于档位默认值（便于临时覆盖，不必改代码）
_ENV_KEYS = {
    "data_source": "DATA_SOURCE",
    "search_provider": "SEARCH_PROVIDER",
    "producer": "PRODUCER",
    "db_dsn": "DB_DSN",
}


def current() -> str:
    name = (os.environ.get("APP_PROFILE") or "oss").strip().lower()
    return name if name in _PROFILES else "oss"


def config() -> dict[str, object]:
    cfg = dict(_PROFILES[current()])
    cfg["profile"] = current()
    for key, env in _ENV_KEYS.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    return cfg


def is_private() -> bool:
    return current() == "private"


__all__ = ["current", "config", "is_private"]
