"""盘后批处理：digest → producer → 契约校验 → 落 pregen。

为什么要有这一层：在线 `/api/ai/brief` 每次点开直连模型会把成本从「每天固定
N 次」变成「每次点击 1 次」，而且 pregen 与 llm 两条通道渲染割裂。这里把叙述
统一成 **producer 输出 AiBrief 全字段**，盘后一次算好落盘，在线只读盘。

  python -m jobs.build_cases --date 2026-11-10 --dry-run --json
  python -m jobs.build_cases --date 2026-11-10 --producer rule --apply

默认 dry-run（不写任何业务文件）；`--apply` 才调 ai.save_pregen。
证据包、规则结论、契约校验全在本地完成，不联网、不调模型（除非显式选 llm_direct
且配了 Key）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Any

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import env_loader  # noqa: E402
env_loader.load_env()   # CLI 不经 main.py，需自己合入 .env 里的 LLM / 检索配置

import ai  # noqa: E402
from contracts.validate import validate_brief  # noqa: E402
from producers.base import fallback, get_producer  # noqa: E402

CASES = os.path.join(DATA_DIR, "cases")
_CAUSE_RE = re.compile(r"^(.*?)\(([-\d.]+)\):\s*(.*)$")


def load_pack(path: str | None = None, day: str | None = None) -> dict:
    day = day or datetime.now().strftime("%Y-%m-%d")
    p = path or os.path.join(CASES, day, "digest.json")
    with open(p, encoding="utf-8") as f:
        pack = json.load(f)
    if not isinstance(pack, dict) or not isinstance(pack.get("items"), list):
        raise ValueError(f"digest 结构不对：{p}")
    pack.setdefault("date", day)
    return pack


def rule_from_row(row: dict, cases_dir: str | None = None) -> dict:
    """规则结论：优先复用完整证据包的 reasons.brief()，否则从 digest 的 rule_* 重建。

    digest 本身已经是裁剪后的证据包，用 `--use-cases` 才能拿到更完整的规则结果；
    默认路径完全离线、确定，适合影子期验收。
    """
    if cases_dir:
        p = os.path.join(cases_dir, f"{row.get('code')}.json")
        if os.path.isfile(p):
            try:
                import reasons
                with open(p, encoding="utf-8") as f:
                    return reasons.brief(json.load(f))
            except Exception:
                pass
    causes = []
    for line in row.get("rule_causes") or []:
        m = _CAUSE_RE.match(str(line))
        if m:
            causes.append({"type": m.group(1), "weight": float(m.group(2)),
                           "text": m.group(3), "evidence": [], "basis": ""})
        else:
            causes.append({"type": "规则归因", "weight": 0.0,
                           "text": str(line), "evidence": [], "basis": ""})
    chg = row.get("chg")
    head = f"{row.get('name') or row.get('code') or '该股'} 今日"
    head += f"涨跌 {chg:.2f}%" if isinstance(chg, (int, float)) else "异动"
    head += "，以下为规则归因，未做模型叙述。"
    return {
        "headline": head, "causes": causes[:4], "counters": [],
        "risks": list(row.get("rule_risks") or [])[:4], "watch": [], "facts": [],
        "confidence": "medium", "sources": {},
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "rule",
    }


def run(day: str | None = None, pack: dict | None = None,
        digest_path: str | None = None, producer: Any = None,
        cases_dir: str | None = None, apply: bool = False,
        limit: int | None = None, save_fn=None) -> dict:
    """返回逐只结果；apply=False 时完全不写盘（验收脚本直接传内存 pack）。"""
    pack = pack if pack is not None else load_pack(digest_path, day)
    day = str(pack.get("date") or day or datetime.now().strftime("%Y-%m-%d"))
    prod = producer if producer is not None and not isinstance(producer, str) \
        else get_producer(producer)
    rows = pack.get("items") or []
    if limit:
        rows = rows[:int(limit)]
    saver = save_fn or (ai.save_pregen if apply else None)

    results, n_ok, n_fallback = [], 0, 0
    for raw in rows:
        row = dict(raw or {}, date=day)
        code = str(row.get("code") or "")
        rule = rule_from_row(row, cases_dir)
        try:
            brief = prod.generate(row, rule)
        except Exception as exc:
            brief = fallback(rule, f"producer_error：{type(exc).__name__}: {exc}")
        res = validate_brief(brief, row)
        if res.ok:
            final = res.normalized
            n_ok += 1
        else:
            final = fallback(rule, "contract_rejected：" + res.reason)
            n_fallback += 1
        path = None
        if saver is not None:
            try:
                path = saver(day, code, final)
            except Exception as exc:
                final = dict(final, save_error=f"{type(exc).__name__}: {exc}")
        results.append({
            "code": code, "name": row.get("name"), "mode": final.get("mode"),
            "confidence": final.get("confidence"),
            "fallback_reason": final.get("fallback_reason"),
            "headline": str(final.get("headline") or "")[:80],
            "n_causes": len(final.get("causes") or []),
            "file": path,
        })
    return {
        "ok": True, "date": day, "producer": getattr(prod, "name", str(prod)),
        "total": len(rows), "generated": n_ok, "fallback": n_fallback,
        "apply": bool(saver is not None), "items": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="盘后批量生成契约化 AI 解读")
    ap.add_argument("--date", default=None)
    ap.add_argument("--digest", default=None, help="digest.json 路径")
    ap.add_argument("--producer", default=None,
                    help="rule | llm_direct | agent | manual")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--apply", action="store_true", help="真正写 pregen 文件")
    ap.add_argument("--use-cases", action="store_true",
                    help="用 cases/{date}/{code}.json 调 reasons.brief() 取完整规则结论")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        cases_dir = os.path.join(CASES, a.date) if a.use_cases else None
        res = run(day=a.date, digest_path=a.digest, producer=a.producer,
                  cases_dir=cases_dir, apply=a.apply, limit=a.limit)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 1
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(f"[build_cases] {res['date']} producer={res['producer']} "
              f"{res['total']} 只：生成 {res['generated']} / 规则兜底 {res['fallback']}，"
              f"apply={res['apply']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
