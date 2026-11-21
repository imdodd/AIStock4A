#!/usr/bin/env python3
"""历史资讯日文件一次性迁移：把「长文本当枚举」的旧格式收敛回契约。

背景（生产实测 2026-11-15）：AI 没校验就落盘，把 118 字分析长文塞进 `impact`
与 `sectors[].dir`，导致 counts 的 key 变成整句话、每条计数恒为 1，利多/利空
统计卡全 0，颜色类失配，sector_heat.net / watch_hits.net 大面积为 0。

本工具只做三件事：枚举抽前缀、原文移进 note / impact_note、按 news.save() 的
同口径重算 counts / sector_heat / watch_hits。**默认 dry-run，不写任何文件**；
`--apply` 才原子替换 `--src` 里的原文件（不调 news.save()，它固定写
backend/data/news，传别的目录会静默写错位置）。条目数不变是硬断言。
影子期内 `--apply` 只允许写本工程内目录，外部目录需显式 `--allow-external`。

用法：
  python tools/normalize_news.py --src backend/data/news --json
  python tools/normalize_news.py --src backend/data/news --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)

from contracts.models import CATEGORY_VALUES, IMPACT_VALUES  # noqa: E402

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
PREFIX_RE = re.compile(r"^\s*(利多|利空|中性|分化)")


def split_impact(value) -> tuple[str, str | None]:
    """返回 (四值枚举, 需要挪走的原文)。合规短值不产生 note。"""
    s = str(value or "").strip()
    if s in IMPACT_VALUES:
        return s, None
    m = PREFIX_RE.match(s)
    return (m.group(1) if m else "中性"), (s or None)


def normalize_payload(payload: dict) -> tuple[dict, dict]:
    """纯函数：不改入参，返回 (归一化后的完整日文件, 修正统计)。"""
    out = json.loads(json.dumps(payload, ensure_ascii=False))
    stats = {"items": len(out.get("items") or []), "impact_fixed": 0,
             "dir_fixed": 0, "category_fixed": 0, "logic_added": 0}
    for it in out.get("items") or []:
        impact, note = split_impact(it.get("impact"))
        if note is not None:
            it["impact"] = impact
            if not it.get("impact_note"):
                it["impact_note"] = note
            stats["impact_fixed"] += 1
        elif it.get("impact") != impact:
            it["impact"] = impact
        cat = it.get("category")
        if cat not in CATEGORY_VALUES:
            it["category"] = "other"
            stats["category_fixed"] += 1
        if not str(it.get("logic") or "").strip():
            it["logic"] = "历史数据未记录传导逻辑；仅保留事实摘要，不补推断。"
            stats["logic_added"] += 1
        for group in ("sectors", "stocks"):
            for ref in it.get(group) or []:
                enum, long_text = split_impact(ref.get("dir"))
                if long_text is not None:
                    ref["dir"] = enum
                    if not ref.get("note"):
                        ref["note"] = long_text
                    stats["dir_fixed"] += 1
                elif ref.get("dir") != enum:
                    ref["dir"] = enum
                # 旧格式的 dir 缺省跟随条目 impact
                if not ref.get("dir"):
                    ref["dir"] = it["impact"]

    # ---- 与 news.save() 完全同口径的聚合 ----
    items = out.get("items") or []
    counts: dict[str, int] = {}
    heat: dict[str, dict] = {}
    hits: dict[str, dict] = {}
    for it in items:
        counts[it["impact"]] = counts.get(it["impact"], 0) + 1
        for s in it.get("sectors") or []:
            k = s.get("code") or s.get("name") or "?"
            h = heat.setdefault(k, {"code": s.get("code"),
                                    "name": s.get("name") or k, "count": 0,
                                    "net": 0, "linked": bool(s.get("linked"))})
            h["count"] += 1
            h["net"] += 1 if s.get("dir") == "利多" else (-1 if s.get("dir") == "利空" else 0)
        for st in it.get("stocks") or []:
            if not st.get("in_pool"):
                continue
            k = st["code"]
            h = hits.setdefault(k, {"code": k, "name": st.get("name") or k,
                                    "count": 0, "net": 0, "titles": []})
            h["count"] += 1
            h["net"] += 1 if st.get("dir") == "利多" else (-1 if st.get("dir") == "利空" else 0)
            if len(h["titles"]) < 4:
                h["titles"].append(it.get("title") or "")
    out["counts"] = counts
    out["sector_heat"] = sorted(heat.values(), key=lambda x: (-x["count"], x["name"]))
    out["watch_hits"] = sorted(hits.values(), key=lambda x: (-x["count"], x["code"]))
    stats["counts_before"] = dict(payload.get("counts") or {})
    stats["counts_after"] = counts
    stats["bull_after"] = counts.get("利多", 0)
    stats["bear_after"] = counts.get("利空", 0)
    stats["heat_net_nonzero"] = sum(1 for x in out["sector_heat"] if x["net"])
    return out, stats


def run(src: str, date: str | None = None, apply: bool = False,
        allow_external: bool = False) -> dict:
    src = os.path.abspath(src)
    if apply and not allow_external:
        try:
            rel = os.path.relpath(src, ROOT)
            inside = rel != os.pardir and not rel.startswith(os.pardir + os.sep)
        except ValueError:            # 跨盘符，relpath 无法表示相对路径
            inside = False
        if not inside:
            raise ValueError(
                "--apply 只允许写本工程内的目录；外部目录须显式 --allow-external，"
                "且影子期内不应指向生产数据目录")
    files = sorted(f for f in os.listdir(src) if DAY_RE.match(f)) if os.path.isdir(src) else []
    if date:
        files = [f for f in files if f == f"{date}.json"]
    report = {"src": os.path.abspath(src), "apply": apply, "files": [], "ok": True}
    for fn in files:
        p = os.path.join(src, fn)
        with open(p, encoding="utf-8") as f:
            payload = json.load(f)
        clean, stats = normalize_payload(payload)
        same_items = len(clean.get("items") or []) == len(payload.get("items") or [])
        entry = {"file": fn, "items": stats["items"],
                 "items_unchanged": same_items, **stats, "written": False}
        if not same_items:
            report["ok"] = False
            entry["error"] = "条目数变化，拒绝落盘"
        elif apply:
            # 写回 --src 里的那一个文件：先 .tmp 再原子替换，与 news.save() 同手法。
            # 聚合口径已在 normalize_payload 里复刻，源文件的其它字段原样保留。
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
            entry["written"] = True
        report["files"].append(entry)
    report["total_files"] = len(files)
    report["total_items"] = sum(e["items"] for e in report["files"])
    report["total_fixed"] = sum(e["impact_fixed"] + e["dir_fixed"] for e in report["files"])
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="历史资讯旧格式归一化（默认只读）")
    ap.add_argument("--src", required=True, help="news 日文件目录")
    ap.add_argument("--date", default=None)
    ap.add_argument("--apply", action="store_true", help="原子替换 --src 里的日文件")
    ap.add_argument("--allow-external", action="store_true",
                    help="允许 --apply 写本工程之外的目录（影子期请勿使用）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        res = run(a.src, a.date, a.apply, a.allow_external)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 1
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print(f"[normalize] {res['total_files']} 个日文件 / {res['total_items']} 条，"
              f"修正 {res['total_fixed']} 处，apply={res['apply']}")
        for e in res["files"]:
            print(f"  {e['file']}: impact {e['impact_fixed']} / dir {e['dir_fixed']} / "
                  f"category {e['category_fixed']} / logic {e['logic_added']}，"
                  f"利多 {e['bull_after']} 利空 {e['bear_after']}，"
                  f"条目不变 {e['items_unchanged']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
