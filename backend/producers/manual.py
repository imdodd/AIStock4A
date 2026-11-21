"""manual 档：离线 / 调试 / 全链路回滚，零成本、零联网。

导出一份「证据 + 规则结论 + 空产出栏」的模板，人工（或任意外部工具）填完放到
`data/inbox/manual/{date}/{code}.json`，producer 读回来仍要过同一套契约校验 ——
手工通道不享受豁免，否则它就会变成绕过防幻觉的后门。
"""
from __future__ import annotations

import os
from datetime import datetime

from contracts.validate import validate_brief
from producers.base import (DEFAULT_INBOX, Producer, atomic_write_json, fallback,
                            inbox_path, read_json, scan_inbox)

FILL_HINT = ("只复述左侧证据与规则结论，不许新增数字/事件；每个判断后加 [依据:字段]；"
             "不写买卖建议；填完删掉本说明字段。")


def template(row: dict, rule: dict) -> dict:
    return {
        "code": row.get("code"), "date": row.get("date"),
        "_how_to_fill": FILL_HINT,
        "digest": row,
        "rule": {k: rule.get(k) for k in
                 ("headline", "causes", "counters", "risks", "watch", "facts",
                  "confidence")},
        "headline": "", "summary": "", "causes": [], "counters": [],
        "risks": [], "watch": [], "confidence": rule.get("confidence") or "medium",
        "mode": "llm",
    }


class ManualProducer(Producer):
    name = "manual"

    def __init__(self, root: str | None = None):
        self.root = root or DEFAULT_INBOX

    def generate(self, row: dict, rule: dict) -> dict:
        day = str(row.get("date") or datetime.now().strftime("%Y-%m-%d"))
        code = str(row.get("code") or "")
        path = inbox_path("manual", day, code, self.root)
        filled = read_json(path)
        if not filled:
            return fallback(rule, f"manual_pending：未回填 {os.path.relpath(path, self.root)}")
        filled.pop("_how_to_fill", None)
        filled.setdefault("code", code)
        filled.setdefault("date", day)
        res = validate_brief(filled, row)
        if not res.ok:
            return fallback(rule, "contract_rejected：" + res.reason)
        return res.normalized

    def export(self, pack: dict, out_dir: str | None = None) -> list[str]:
        day = str(pack.get("date") or datetime.now().strftime("%Y-%m-%d"))
        base = out_dir or os.path.join(self.root, "manual", day)
        paths = []
        for row in pack.get("items") or []:
            rule = {"headline": "", "causes": [], "counters": [], "risks": [],
                    "watch": [], "facts": [], "confidence": "medium"}
            row = dict(row, date=day)
            paths.append(atomic_write_json(
                os.path.join(base, f"{row.get('code')}.json"), template(row, rule)))
        return paths

    def collect(self, day: str) -> list[str]:
        return scan_inbox(os.path.join(self.root, "manual", day))


__all__ = ["ManualProducer", "template"]
