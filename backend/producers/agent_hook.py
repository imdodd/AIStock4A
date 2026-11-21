"""agent 档：只定义「按契约投递」的入口，不绑定任何具体 agent。

服务端只做两件事：扫描 `data/inbox/briefs/{date}/{code}.json`、按 AiBrief 契约
校验后落位。对面是外部 agent、自建脚本还是别的投递方，代码一概不关心，
仓库里不绑定任何厂商实现。

投递的 news_patch 走同一个入口 `deliver_patch()`：校验不过就整份拒绝，
绝不直接写日文件。
"""
from __future__ import annotations

import os
from datetime import datetime

from contracts.validate import validate_brief, validate_patch
from producers.base import (DEFAULT_INBOX, Producer, atomic_write_json, fallback,
                            inbox_path, read_json, scan_inbox)


class AgentProducer(Producer):
    name = "agent"

    def __init__(self, root: str | None = None):
        self.root = root or DEFAULT_INBOX

    def generate(self, row: dict, rule: dict) -> dict:
        day = str(row.get("date") or datetime.now().strftime("%Y-%m-%d"))
        code = str(row.get("code") or "")
        path = inbox_path("briefs", day, code, self.root)
        delivered = read_json(path)
        if not delivered:
            return fallback(rule, f"agent_pending：未收到 {os.path.relpath(path, self.root)}")
        res = validate_brief(delivered, row)
        if not res.ok:
            return fallback(rule, "contract_rejected：" + res.reason)
        out = res.normalized
        out.setdefault("code", code)
        out.setdefault("date", day)
        out.setdefault("mode", "llm")
        return out

    def collect(self, day: str | None = None) -> list[str]:
        base = os.path.join(self.root, "briefs")
        if day:
            return scan_inbox(os.path.join(base, day))
        out = []
        for sub in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            out += scan_inbox(os.path.join(base, sub))
        return out


def deliver_brief(day: str, code: str, payload: dict, root: str | None = None) -> str:
    """给 agent 侧用的落盘工具：只写 inbox，不碰业务日文件。"""
    return atomic_write_json(inbox_path("briefs", day, code, root), payload)


def deliver_patch(day: str, patch: dict, root: str | None = None,
                  sectors_index: dict | None = None) -> dict:
    """校验 → 落 inbox；被拒的条目随结果返回，不写进投递文件。"""
    try:
        clean, rejected = validate_patch(patch, sectors_index)
    except Exception as exc:
        return {"ok": False, "path": None, "rejected": [],
                "reason": f"patch_rejected：{type(exc).__name__}: {exc}"}
    if not clean.get("items"):
        return {"ok": False, "path": None, "rejected": rejected,
                "reason": "全部条目被契约拒绝，不落盘"}
    path = atomic_write_json(
        os.path.join(root or DEFAULT_INBOX, "news", f"{day}-patch.json"), clean)
    return {"ok": True, "path": path, "rejected": rejected, "kept": len(clean["items"])}


__all__ = ["AgentProducer", "deliver_brief", "deliver_patch"]
