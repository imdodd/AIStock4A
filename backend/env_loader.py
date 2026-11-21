"""零依赖 .env 加载器。

为什么需要：全仓只读 ``os.environ``，没有引 python-dotenv，也没有任何地方
自动读 ``.env``。结果是「把 .env.example 复制成 .env 填好」根本不生效。
本模块在服务入口（main.py）和批处理 CLI（jobs/build_cases.py）的导入期
最前面调用一次，把仓库根 ``.env`` 合进进程环境。

规则：
  1. 真实环境变量优先 —— 已存在的键**绝不覆盖**（方便临时用 set 覆盖文件）；
  2. 只认 KEY=VALUE，支持 ``#`` 注释、``export `` 前缀、单双引号与转义；
  3. 值里含 ``#`` 的 URL / Key 不会被误截断，只有「空白 + #」才算行内注释；
  4. 函数返回值里**只有键名，绝不带任何值**，避免日志/接口泄露密钥。

定位顺序：``ASTOCK_ENV_FILE`` 指定 > 仓库根 ``.env`` > ``backend/.env``。
"""
from __future__ import annotations

import os
import re
from typing import Any

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _strip_inline_comment(value: str, quoted: bool) -> str:
    if quoted:
        return value
    m = re.search(r"\s+#", value)
    return value[: m.start()].rstrip() if m else value


def _unquote(value: str) -> tuple[str, bool]:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        body = value[1:-1]
        if value[0] == '"':
            body = body.replace(r"\n", "\n").replace(r"\t", "\t").replace(r'\"', '"')
        return body, True
    return value, False


def parse(text: str) -> dict[str, str]:
    """解析 .env 文本；纯函数，方便单测。"""
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        value, quoted = _unquote(value.strip())
        value = _strip_inline_comment(value, quoted).strip()
        if not quoted:
            value = value.strip("'\"")
        out[key] = value
    return out


def find_env_file(explicit: str | None = None) -> str:
    """返回第一个存在的 .env 路径；都不存在时返回期望的仓库根路径。"""
    backend = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(backend)
    candidates = [p for p in (explicit, os.path.join(root, ".env"),
                              os.path.join(backend, ".env")) if p]
    for p in candidates:
        if os.path.isfile(p):
            return os.path.abspath(p)
    return os.path.abspath(candidates[1])


def load_env(path: str | None = None, override: bool = False) -> dict[str, Any]:
    """把 .env 合进 os.environ，返回不含任何值的状态字典。"""
    p = find_env_file(path or os.environ.get("ASTOCK_ENV_FILE"))
    result: dict[str, Any] = {"found": os.path.isfile(p), "path": p,
                              "applied": [], "skipped": [], "error": None}
    if not result["found"]:
        return result                      # 没有 .env 完全正常：默认值就能跑
    try:
        with open(p, encoding="utf-8-sig") as f:
            pairs = parse(f.read())
    except Exception as e:                 # 坏文件不许拖垮服务
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    for key, value in pairs.items():
        if not override and key in os.environ:
            result["skipped"].append(key)
            continue
        os.environ[key] = value
        result["applied"].append(key)
    return result


def describe() -> dict[str, Any]:
    """给运维页/自检用的状态：只列键名与档位，不含敏感值。"""
    backend = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(backend)
    p = find_env_file()
    return {
        "path": p,
        "exists": os.path.isfile(p),
        "tracked_by_git": False,           # .env 在 .gitignore 中
        "hint": "改完重启服务生效；临时覆盖用真实环境变量（优先级更高）",
    }


__all__ = ["load_env", "parse", "find_env_file", "describe"]
