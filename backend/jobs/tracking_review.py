"""跟踪期产出任务：后端自己调配置的 LLM，不再依赖会话侧自动化。

链路：扫方案 → 按层取证 → 逐层问模型 → 契约校验 → 合并落盘（periods/*.json + *.md）

为什么要有这个 job：原来每只股票都要在会话里建一条定时自动化，产出依赖
WorkBuddy 在线、依赖 MCP 工具、提示词还有落盘路径漂移的隐患。这里改成
「方案是数据、调度是代码、模型是配置」——加一只股票只写 meta.json，
改频率只改 frequency，换模型只改 .secrets/llm.json。

用法：
    python -m jobs.tracking_review --cad weekly          # 该频率档下全部方案
    python -m jobs.tracking_review --code sh600886       # 单只
    python -m jobs.tracking_review --code sh600886 --force   # 覆盖今天已有的一期
    python -m jobs.tracking_review --code sh600886 --dry     # 只组 prompt 不调模型

stdout 最后一行是 JSON 汇总，会被 handlers 写进 job_runs.detail。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# CLI 手工跑和调度器子进程都要先吃 .env：data_paths 在导入期就把
# ASTOCK_DATA_DIR 定下来了，晚一步就会去读 backend/data 那份冻结夹具。
from env_loader import load_env                 # noqa: E402
load_env()

# STORAGE_READ=db 时主服务会把 tracking.load / period_list / save_period 换成库版。
# 子进程不装这套，就会出现「文件写了、界面看不到」的鬼打墙——库里还是上一次的状态。
# 所以这里必须跟 main.py 一样先把存储层装上，读写才和主服务同一条路径。
import storage_runtime                          # noqa: E402
storage_runtime.install_from_env()

import ai                                       # noqa: E402
import tracking                                 # noqa: E402
import tracking_llm as tl                       # noqa: E402

RUNS_DIRNAME = "runs"


def _today() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


def _has_period(code: str, day: str) -> bool:
    return os.path.exists(os.path.join(tracking.period_dir(code), f"{day}.json"))


def _save_run(code: str, rec: dict) -> str:
    d = os.path.join(tracking.DIR, code, RUNS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    from datetime import datetime
    name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    return os.path.basename(p)


def pick_codes(cad: str | None, code: str | None) -> list[str]:
    if code:
        return [code]
    out = []
    for c, meta in (tracking.list_all() or {}).items():
        m = tracking.load(c)
        if not m or (m.get("status") or "active") != "active":
            continue
        kind = (m.get("frequency") or {}).get("kind")
        if cad and cad != "all" and kind != cad:
            continue
        out.append(c)
    return sorted(out)


def run_one(code: str, day: str | None = None, force: bool = False,
            dry: bool = False, only_layer: str | None = None,
            cfg: dict | None = None, pause: float = 2.0) -> dict:
    day = day or _today()
    meta = tracking.load(code)
    if not meta:
        return {"code": code, "ok": False, "reason": "方案不存在"}
    if (meta.get("status") or "active") != "active":
        return {"code": code, "ok": False, "reason": f"方案状态 {meta.get('status')}"}
    if _has_period(code, day) and not force:
        return {"code": code, "ok": True, "skipped": True, "reason": f"{day} 已有本期"}

    periods = tracking.period_list(code)
    prev = periods[0] if periods else None
    if prev and prev.get("date") == day:
        prev = periods[1] if len(periods) > 1 else None   # 覆盖当天重跑时别拿自己当上期

    layers = tl.split_layers(meta)
    plan_layers = [(lid, layers[lid]) for lid, _l, _a in tl.LAYERS
                   if layers[lid] and (not only_layer or lid == only_layer)]
    if not plan_layers:
        return {"code": code, "ok": False, "reason": "方案没有可判的维度"}

    parts, diag = [], []
    for lid, dims in plan_layers:
        try:
            evd = tl.build_evidence(meta, lid, None)
        except Exception as e:
            diag.append({"layer": lid, "error": f"取证失败 {type(e).__name__}: {e}"})
            evd = {}
        messages = tl.build_messages(meta, lid, dims, evd, prev)
        if dry:
            diag.append({"layer": lid, "dims": len(dims),
                         "prompt_chars": sum(len(m["content"]) for m in messages)})
            continue
        draft, info = tl.call_llm(messages, lid, cfg)
        if draft is None:
            # 解析失败重试一次，温度降一档；再失败就把本层按无内容落，不阻断其他层
            draft, info2 = tl.call_llm(messages + [
                {"role": "user", "content": "上一次输出不是合法 JSON。这次只输出 JSON 对象，不要任何其他文字。"}
            ], lid, cfg)
            info = {"first": info, "retry": info2}
        if draft is None:
            diag.append({"layer": lid, "error": "模型两次都没给出可解析 JSON", "info": info})
            cells, stat = tl.validate_cells([], dims, evd, prev)
            parts.append({"layer": lid, "cells": cells, "stats": stat,
                          "verdict": "中性", "summary": "", "calls": 2})
            continue
        # 检索来的数字只有在 sources 的 quote 里能对上才算有出处：
        # 只给标题是核不动的，模型随便写个 337.00 亿也没人知道真假。
        src_text = "\n".join(
            f"{s.get('title') or ''} {s.get('quote') or ''}"
            for s in (draft.get("sources") or []) if isinstance(s, dict))
        cells, stat = tl.validate_cells(
            draft.get("values"), dims, evd, prev, extra_text=src_text)
        parts.append({
            "layer": lid, "cells": cells, "stats": stat,
            "verdict": draft.get("verdict") or "中性",
            "summary": draft.get("summary") or "",
            "highlights": draft.get("highlights") or [],
            "sources": draft.get("sources") or [],
            "model": (cfg or ai.config() or {}).get("model"),
            "calls": 1 + (1 if info.get("retry") else 0),
        })
        diag.append({"layer": lid, "dims": len(dims), "info": info, "stats": stat})
        if pause:
            time.sleep(pause)

    if dry:
        return {"code": code, "ok": True, "dry": True, "layers": diag}

    period = tl.merge_layers(meta, parts, day)
    tracking.save_period(code, period)
    md_path = os.path.join(tracking.period_dir(code), f"{day}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(tl.md_of(period, meta))
    rec = {"code": code, "day": day, "ok": True, "verdict": period["verdict"],
           "n_values": len(period["values"]),
           "n_gap": sum(1 for v in period["values"] if v["status"] == "n"),
           "n_unverified": sum(1 for v in period["values"] if v.get("unverified")),
           "model": period.get("model"), "layers": diag,
           "md": os.path.basename(md_path)}
    rec["run"] = _save_run(code, rec)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cad", default=None, help="daily|twice_weekly|weekly|quarterly|all")
    ap.add_argument("--code", default=None)
    ap.add_argument("--day", default=None, help="指定交易日，默认今天")
    ap.add_argument("--force", action="store_true", help="覆盖当天已有的一期")
    ap.add_argument("--dry", action="store_true", help="只组 prompt，不调模型")
    ap.add_argument("--layer", default=None, help="只跑某一层，调试用")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几只，0=不限")
    ap.add_argument("--pause", type=float, default=2.0, help="层间/股间停顿秒数")
    args = ap.parse_args()

    cfg = ai.config()
    if not cfg and not args.dry:
        print(json.dumps({"ok": False, "reason": "未配置 LLM（.secrets/llm.json 或 LLM_API_KEY）"},
                         ensure_ascii=False))
        return 2

    codes = pick_codes(args.cad, args.code)
    if args.limit:
        codes = codes[:args.limit]
    if not codes:
        print(json.dumps({"ok": True, "codes": 0, "note": f"cad={args.cad} 下没有启用的方案"},
                         ensure_ascii=False))
        return 0

    results, t0 = [], time.time()
    for c in codes:
        try:
            r = run_one(c, args.day, args.force, args.dry, args.layer, cfg, args.pause)
        except Exception as e:                     # 单只失败不许拖垮整轮
            r = {"code": c, "ok": False, "reason": f"{type(e).__name__}: {e}"}
        results.append(r)
        print(f"[tracking_review] {c} -> {r.get('verdict') or r.get('reason') or 'ok'}",
              file=sys.stderr)
        if args.pause:
            time.sleep(args.pause)

    summary = {
        "ok": all(r.get("ok") for r in results),
        "cad": args.cad, "model": cfg.get("model") if cfg else None,
        "total": len(results),
        "done": sum(1 for r in results if r.get("ok") and not r.get("skipped") and not r.get("dry")),
        "skipped": sum(1 for r in results if r.get("skipped")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "unverified_total": sum(r.get("n_unverified") or 0 for r in results),
        "elapsed_s": round(time.time() - t0, 1),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
