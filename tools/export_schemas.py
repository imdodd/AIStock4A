#!/usr/bin/env python3
"""导出三份产出契约的 JSON Schema（模型即文档，schema 由 pydantic 生成）。

  python tools/export_schemas.py ai_brief > docs/contracts/ai_brief.schema.json
  python tools/export_schemas.py --list

只往 stdout 打印，不写盘 —— 沙箱里 python 子进程对 D 盘只读，由 shell 重定向落文件。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, BACKEND)

from contracts import AiBrief, DigestPack, NewsPatch  # noqa: E402

MODELS = {"digest": DigestPack, "ai_brief": AiBrief, "news_patch": NewsPatch}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?", default=None)
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list or not a.model:
        print(json.dumps(sorted(MODELS), ensure_ascii=False))
        return 0
    if a.model not in MODELS:
        print(f"未知契约 {a.model}，可选 {sorted(MODELS)}", file=sys.stderr)
        return 2
    print(json.dumps(MODELS[a.model].model_json_schema(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
