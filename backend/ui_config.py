"""界面风格配置：深色 / 浅色，落在数据目录的 ui.json。

为什么不放 localStorage：配置页是全局设置的入口，换浏览器/换设备应该一致；
localStorage 只用来在首屏提前上色、避免闪一下白，真正以服务端为准。

文件刻意不叫 theme.json 也不进 .secrets/——它不是密钥，运维页和备份都可能看到。
"""
from __future__ import annotations

import json
import os
import tempfile

from data_paths import DATA_DIR

FILE = os.path.join(DATA_DIR, "ui.json")
CHOICES = ("dark", "light")
DEFAULT = "dark"


def get_theme() -> str:
    try:
        d = json.load(open(FILE, encoding="utf-8"))
        t = str(d.get("theme") or DEFAULT).strip().lower()
        return t if t in CHOICES else DEFAULT
    except Exception:
        return DEFAULT


def set_theme(theme: str) -> str:
    t = str(theme or "").strip().lower()
    if t not in CHOICES:
        raise ValueError(f"theme 必须是 {' / '.join(CHOICES)} 之一")
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(FILE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"theme": t}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return t


def describe() -> dict:
    return {"theme": get_theme(), "choices": list(CHOICES), "file":
            os.path.relpath(FILE, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            .replace(os.sep, "/")}


__all__ = ["FILE", "CHOICES", "DEFAULT", "get_theme", "set_theme", "describe"]
