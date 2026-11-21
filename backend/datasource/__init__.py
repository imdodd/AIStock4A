"""数据源工厂：换源只改环境变量 DATA_SOURCE，不动引擎与任务代码

默认走腾讯公开接口（免鉴权）。要接别的源，实现 datasource.base.DataSource
并在 _REGISTRY 里登记即可。
"""
from __future__ import annotations

import os

from .base import DataSource

_REGISTRY: dict[str, str] = {
    "tencent": "datasource.tencent:TencentSource",
    "tushare": "datasource.tushare:TushareSource",
}


def get_source(name: str | None = None, **kw) -> DataSource:
    name = (name or os.environ.get("DATA_SOURCE") or "tencent").lower()
    if name not in _REGISTRY:
        raise ValueError(f"未知数据源 {name}，可选：{', '.join(_REGISTRY)}")
    mod_path, cls_name = _REGISTRY[name].split(":")
    import importlib
    mod = importlib.import_module(mod_path)
    return getattr(mod, cls_name)(**kw)


__all__ = ["DataSource", "get_source"]
