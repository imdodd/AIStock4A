"""Producer 抽象与三档注册表、降级工具、原子落盘。

Producer 的职责只有一件：**把 digest + 规则结论讲成人话，输出结构化 AiBrief**。
事实、权重、反证、置信度一律由 reasons.py 算好；producer 不许新增事实，
也不许直接写日文件 —— 落盘统一由 jobs/build_cases.py 校验后调用 ai.save_pregen。

档位：
  llm_direct  开源默认，OpenAI 兼容 API；无 Key / 断网 / 契约不过 → 规则兜底
  agent       可选投递通道，按契约把结果投到 data/inbox/briefs/
  manual      离线调试，导出模板人工回填
  rule        纯规则，零成本，永远可用
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
DEFAULT_INBOX = os.path.join(DATA_DIR, "inbox")

_REGISTRY: dict[str, str] = {
    "rule": "producers.rule:RuleProducer",
    "llm_direct": "producers.llm_direct:LlmDirectProducer",
    "agent": "producers.agent_hook:AgentProducer",
    "manual": "producers.manual:ManualProducer",
}


class Producer(ABC):
    name = "base"

    @abstractmethod
    def generate(self, row: dict, rule: dict) -> dict:
        """返回 AiBrief-shaped dict；失败时返回 fallback(rule, reason)。"""


def register(name: str, target: str) -> None:
    """'模块路径:类名'，私有实现可在 contrib/private/ 里运行时登记。"""
    _REGISTRY[name.lower()] = target


def get_producer(name: str | None = None) -> Producer:
    if not name:
        from app_profile import config
        name = str(config().get("producer") or "llm_direct")
    name = name.strip().lower()
    if name not in _REGISTRY:
        raise ValueError(f"未知 producer {name}，可选：{', '.join(sorted(_REGISTRY))}")
    mod_path, cls_name = _REGISTRY[name].split(":")
    import importlib
    cls: Callable = getattr(importlib.import_module(mod_path), cls_name)
    return cls()


def fallback(rule: dict, reason: str | None = None) -> dict:
    """规则结果即兜底：保留代码算出的全部字段，只补契约标记。

    reason 为 None 表示「主动选 rule 档」，不算降级；有值则是 llm/agent/manual
    失败后的降级原因，前端与批处理日志都能看到，绝不静默。
    """
    out = dict(rule or {})
    out["mode"] = "rule"
    if reason:
        out["fallback_reason"] = reason
    else:
        out.setdefault("fallback_reason", None)
    out.setdefault("confidence", "low")
    out.setdefault("generated_at",
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return out


def atomic_write_json(path: str, obj) -> str:
    """先写 .tmp 再原子替换，避免写一半断电留下半份 JSON。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def scan_inbox(directory: str, suffix: str = ".json") -> list[str]:
    if not os.path.isdir(directory):
        return []
    return sorted(os.path.join(directory, f) for f in os.listdir(directory)
                  if f.endswith(suffix) and not f.endswith(".tmp"))


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def inbox_path(kind: str, day: str, code: str, root: str | None = None) -> str:
    return os.path.join(root or DEFAULT_INBOX, kind, day, f"{code}.json")


__all__ = ["Producer", "get_producer", "register", "fallback", "atomic_write_json",
           "scan_inbox", "read_json", "inbox_path", "DEFAULT_INBOX", "_REGISTRY"]
