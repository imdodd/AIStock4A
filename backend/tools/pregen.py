"""盘后预生成：把异动股证据包导出成给模型看的小摘要，再把生成结果写回 pregen

用法：
    python -m tools.pregen export            # 导出当日异动池证据摘要（默认量比前 30 + 涨停 + 自选持仓）
    python -m tools.pregen export --top 60
    python -m tools.pregen stat              # 查看当日已预生成多少只

流程：
    1. export 产出 data/cases/{date}/digest.json（体积小，可直接喂给模型）
    2. 模型按 digest 写解读，用 save_brief() 写回 data/cases/{date}/ai/{code}.json
    3. /api/ai/brief 检测到 pregen 后自动改用该叙述（mode=pregen），前端秒开

为什么不让服务端自己调模型：WorkBuddy 的免密钥模型走浏览器 SDK + Origin 精确匹配，
本地 127.0.0.1 服务过不了校验，而且官方禁止后端做代理。所以改成「离线生成、在线读取」。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from datasource import evidence
import reasons

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(DATA_DIR, "cases")


def _universe(day: str, top: int) -> list[dict]:
    """异动池：量比前 N + 所有涨停 + 自选/持仓（若有当日异动）"""
    try:
        sc = json.load(open(os.path.join(DATA_DIR, "screener", f"{day}.json"),
                            encoding="utf-8"))
    except Exception:
        return []
    items = sc.get("items") or []
    items.sort(key=lambda x: -(x.get("vol_ratio") or 0))
    picked = {x["code"]: x for x in items[:top]}
    for x in items:
        if "涨停" in (x.get("extra") or []):
            picked[x["code"]] = x
    try:
        import config.universe as uni
        focus = {c for c, _ in uni.WATCHLIST} | set(uni.hold_codes())
        for x in items:
            if x["code"] in focus:
                picked[x["code"]] = x
    except Exception:
        pass
    return list(picked.values())


def digest_row(case: dict, rule: dict) -> dict:
    q = case.get("quote") or {}
    return {
        "code": case["code"], "name": case["name"],
        "chg": q.get("chg"), "vol_ratio": q.get("vol_ratio"),
        "turnover": q.get("turnover"), "amount_yi": round((q.get("amount") or 0) / 1e4, 2),
        "tag": case.get("tag"), "industry": case.get("industry"),
        "sector_chg": (case.get("sector") or {}).get("chg"),
        "peer_total": case.get("peer_total"), "peer_limit": case.get("peer_limit"),
        "stage": (case.get("tech") or {}).get("sub"),
        "rs60": (case.get("tech") or {}).get("rs60"),
        "dist52": (case.get("tech") or {}).get("dist52"),
        "hist": {k: (case.get("hist") or {}).get(k)
                 for k in ("ret5", "ret20", "vol_vs_ma5", "is_high20", "limit_up_20d")},
        "limit_up": case.get("limit_up") or {},
        "flow": {k: (case.get("flow") or {}).get(k)
                 for k in ("main_net", "super_net", "big_net", "main_net_5d")},
        "dragon": case.get("dragon") or {},
        "intraday": {k: (case.get("intraday") or {}).get(k)
                     for k in ("open_chg", "peak_time", "peak_chg", "close_chg",
                               "vol_first60", "close_pos")},
        "notices": [f"{a['date']} {a['title']}" for a in (case.get("notices") or [])[:4]],
        "news": [f"{n['date']} {n['title']}" for n in (case.get("news") or []) if n.get("hit")][:3],
        "rule_causes": [f"{c['type']}({c['weight']}): {c['text']}" for c in rule.get("causes", [])],
        "rule_risks": rule.get("risks"),
    }


def export(day: str | None = None, top: int = 30, refresh: bool = False) -> str:
    day = day or datetime.now().strftime("%Y-%m-%d")
    rows = _universe(day, top)
    if not rows:
        raise SystemExit(f"{day} 没有异动数据，先跑 screener")
    out = []
    for it in rows:
        try:
            case = evidence.build_case(it["code"], day, refresh=refresh)
            out.append(digest_row(case, reasons.brief(case)))
        except Exception as e:
            print(f"  ! {it['code']} 取证失败：{e}")
    p = os.path.join(CASES, day, "digest.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({"date": day, "count": len(out), "items": out},
              open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"导出 {len(out)} 只 → {p}")
    return p


def export_holdings(day: str | None = None, refresh: bool = False) -> str:
    """持仓证据摘要：evidence 取证 + 持仓上下文（成本/浮盈/动态止损/触发状态）。

    产出 data/cases/{date}/holdings_digest.json，供自动化任务逐只写「持仓每日分析」。
    """
    import store
    import stops
    day = day or datetime.now().strftime("%Y-%m-%d")
    hs = store.load()
    try:
        sd = stops.load()
    except Exception:
        sd = {"items": {}}
    out = []
    for it in hs:
        code = it.get("code")
        if not code:
            continue
        try:
            case = evidence.build_case(code, day, refresh=refresh)
            row = digest_row(case, reasons.brief(case))
        except Exception as e:
            print(f"  ! {code} 取证失败：{e}")
            row = {"code": code, "name": it.get("name") or code}
        price = (case.get("quote") or {}).get("price") if case else None
        pnl = round((price / it["cost"] - 1) * 100, 2) if price and it.get("cost") else None
        row["holding"] = {
            "cost": it.get("cost"), "quantity": it.get("quantity"),
            "added_at": str(it.get("added_at") or "")[:10], "pnl_pct": pnl,
            "price": price,
        }
        s = (sd.get("items") or {}).get(code) or {}
        row["stop"] = {k: s.get(k) for k in (
            "stop", "rule", "status", "distance_pct", "cost_stop", "trail_stop",
            "pivot_stop", "peak_close", "peak_date", "pivot", "pivot_date",
            "pivot_kind", "triggered", "rationale", "held_days")}
        out.append(row)
    p = os.path.join(CASES, day, "holdings_digest.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({"date": day, "count": len(out), "items": out},
              open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"导出 {len(out)} 只持仓 → {p}")
    return p


def save_brief(day: str, code: str, brief: dict) -> str:
    p = os.path.join(CASES, day, "ai", f"{code}.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    brief = dict(brief, code=code, date=day, mode="pregen",
                 generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    json.dump(brief, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return p


def stat(day: str | None = None) -> dict:
    day = day or datetime.now().strftime("%Y-%m-%d")
    d = os.path.join(CASES, day, "ai")
    files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    return {"date": day, "pregen": len(files), "codes": [f[:-5] for f in files]}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "export"
    if cmd == "stat":
        print(json.dumps(stat(), ensure_ascii=False))
    elif cmd == "export-holdings":
        export_holdings(refresh="--refresh" in sys.argv)
    else:
        top = 30
        if "--top" in sys.argv:
            top = int(sys.argv[sys.argv.index("--top") + 1])
        export(top=top, refresh="--refresh" in sys.argv)
