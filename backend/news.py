"""每日重要资讯与 A 股影响简报的本地存储。

数据由 WorkBuddy 每日自动化任务产出（WebSearch 检索 + 影响分析），
落盘到 data/news/YYYY-MM-DD.json；本模块负责读写、索引、统计聚合与结构校验。
服务端只读，页面不做联网抓取，避免前端等外部接口。

单日文件结构（前端按此契约渲染）：
{
  "date": "YYYY-MM-DD",
  "generated_at": "...",
  "window": "最近 24 小时",
  "summary": "一句话总览",
  "source_of_truth": "公开报道 + 本地行业/个股清单",
  "items": [
    {
      "id": "us-stock-1",
      "category": "us_stock|fed|us_gov|cn_policy|commodity|other",
      "category_name": "美股",
      "title": "...",
      "time": "2026-11-08 08:00",
      "source": "第一财经",
      "url": "https://...",
      "summary": "事实摘要，不掺观点",
      "impact": "利多|利空|中性|分化",
      "horizon": "短期|中期",
      "logic": "传到 A 股的路径（分析推断）",
      "sectors": [{"code":"pt01801081","name":"半导体","dir":"利空","note":"..."}],
      "stocks":  [{"code":"sh688629","name":"示例股份","dir":"利空","note":"..."}],
      "confidence": "高|中|低",
      "keywords": ["AI监管","数据中心"]
    }
  ],
  "counts": {...}, "sector_heat": [...], "watch_hits": [...]
}
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(DATA_DIR, "news")
SECTORS_JSON = os.path.join(DATA_DIR, "snapshot", "sectors.json")
PURGE_LOG = os.path.join(DIR, "_purge.log")

# 资讯保留期：超过 30 天的日文件（json + 人读 md）由增量任务每次运行时顺带清理。
RETENTION_DAYS = 30
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(json|md|json\.tmp|md\.tmp)$")

CATEGORY_NAME = {
    "us_stock": "美股",
    "fed": "美联储",
    "us_gov": "美国政府",
    "cn_policy": "国内政策",
    "commodity": "商品/汇率",
    "other": "其他",
}


def _sector_index() -> dict[str, str]:
    """本地申万二级行业的 code → name，用来校验影响板块能不能跳转。"""
    try:
        with open(SECTORS_JSON, encoding="utf-8") as f:
            loc = json.load(f)
        items = loc if isinstance(loc, list) else loc.get("items", [])
        return {s["code"]: s.get("name") or s["code"] for s in items if s.get("code")}
    except Exception:
        return {}


def _universe() -> dict[str, str]:
    """自选 + 持仓的代码 → 名称（用于标记「命中我的池子」）。"""
    out: dict[str, str] = {}
    try:
        import watchstore
        for c, n in watchstore.items():
            out[c] = n
    except Exception:
        pass
    try:
        import store as portfolio_store
        for r in portfolio_store.load():
            out[r["code"]] = r.get("name") or r["code"]
    except Exception:
        pass
    return out


def list_dates() -> list[str]:
    if not os.path.isdir(DIR):
        return []
    return sorted((f[:-5] for f in os.listdir(DIR)
                   if f.endswith(".json") and len(f) == 15), reverse=True)


def load(date: str | None = None) -> dict | None:
    dates = list_dates()
    day = date or (dates[0] if dates else None)
    if not day:
        return None
    # 防目录穿越：只接受纯日期
    if not (len(day) == 10 and day[4] == "-" and day[7] == "-" and day.replace("-", "").isdigit()):
        return None
    p = os.path.join(DIR, day + ".json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def index(limit: int = 30) -> dict:
    dates = list_dates()
    rows = []
    for day in dates[:limit]:
        d = load(day) or {}
        rows.append(dict(date=day, items=len(d.get("items") or []),
                         summary=d.get("summary") or "",
                         generated_at=d.get("generated_at") or "",
                         bull=(d.get("counts") or {}).get("利多", 0),
                         bear=(d.get("counts") or {}).get("利空", 0)))
    return {"dates": dates[:limit], "latest": dates[0] if dates else None,
            "rows": rows, "total": len(dates)}


def save(payload: dict) -> str:
    """校验 + 自动聚合统计 + 原子落盘。自动化任务只需给出 date / summary / items。"""
    day = str(payload.get("date") or "").strip()
    if not (len(day) == 10 and day[4] == "-" and day[7] == "-"):
        raise ValueError("date 必须是 YYYY-MM-DD")
    items = payload.get("items") or []
    if not isinstance(items, list):
        raise ValueError("items 必须是数组")
    sectors = _sector_index()
    universe = _universe()

    # 逐条补全默认值 + 校验板块代码，避免前端点了跳不过去
    for i, it in enumerate(items):
        it.setdefault("id", f"n{i + 1}")
        cat = it.get("category") or "other"
        it["category"] = cat
        it.setdefault("category_name", CATEGORY_NAME.get(cat, "其他"))
        it.setdefault("impact", "中性")
        it.setdefault("horizon", "短期")
        it.setdefault("confidence", "中")
        it.setdefault("keywords", [])
        for s in it.get("sectors") or []:
            code = (s.get("code") or "").strip()
            if code and code in sectors:
                s["name"] = sectors.get(code, s.get("name"))
                s["linked"] = True
            else:
                s["linked"] = False
            s.setdefault("dir", it["impact"])
        for st in it.get("stocks") or []:
            code = (st.get("code") or "").strip()
            st["in_pool"] = code in universe
            if st["in_pool"] and not st.get("name"):
                st["name"] = universe[code]
            st.setdefault("dir", it["impact"])

    counts: dict[str, int] = {}
    heat: dict[str, dict] = {}
    hits: dict[str, dict] = {}
    for it in items:
        counts[it["impact"]] = counts.get(it["impact"], 0) + 1
        for s in it.get("sectors") or []:
            k = s.get("code") or s.get("name") or "?"
            h = heat.setdefault(k, {"code": s.get("code"), "name": s.get("name") or k,
                                    "count": 0, "net": 0, "linked": bool(s.get("linked"))})
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

    out = dict(payload)
    out["items"] = items
    out["generated_at"] = payload.get("generated_at") or datetime.now().isoformat(timespec="seconds")
    out["window"] = payload.get("window") or "最近 24 小时"
    out["counts"] = counts
    out["sector_heat"] = sorted(heat.values(), key=lambda x: (-x["count"], x["name"]))
    out["watch_hits"] = sorted(hits.values(), key=lambda x: (-x["count"], x["code"]))
    out.setdefault("disclaimer",
                   "资讯为公开报道摘录，影响分析为模型推断，不是事实陈述也不预示股价表现；不构成买卖建议。")

    os.makedirs(DIR, exist_ok=True)
    p = os.path.join(DIR, day + ".json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p


# ---------------------------------------------------------------- 保留期 / 增量上下文

def status(date: str | None = None) -> dict:
    """给 30 分钟增量任务看的上下文：今天有没有文件、最近已收录哪些标题。

    recent_titles / recent_urls 用来避免 AI 把同一条新闻反复检索、反复写进来。
    """
    day = date or datetime.now().strftime("%Y-%m-%d")
    d = load(day)
    items = (d or {}).get("items") or []
    return {
        "date": day,
        "exists": bool(d),
        "first_run": not bool(d),          # 当天第一次跑 → 做 24 小时完整回顾
        "items": len(items),
        "generated_at": (d or {}).get("generated_at"),
        "window": (d or {}).get("window"),
        "summary": (d or {}).get("summary"),
        "recent_titles": [it.get("title") or "" for it in items[:12]],
        "recent_urls": [it.get("url") or "" for it in items[:12]],
        "stored_dates": list_dates(),
        "retention_days": RETENTION_DAYS,
        "purge_cutoff": (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d"),
    }


def purge(days: int = RETENTION_DAYS, dry_run: bool = False) -> dict:
    """删除日期早于 (今天 - days) 的日文件。

    只认严格 YYYY-MM-DD.json / .md / .tmp，且必须能被 strptime 解析；
    其它文件（含本日志）一律不动。删除明细追加到 _purge.log，便于追溯。
    """
    cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    removed: list[str] = []
    kept: list[str] = []
    skipped: list[str] = []
    if os.path.isdir(DIR):
        for fn in sorted(os.listdir(DIR)):
            m = _DAY_FILE.match(fn)
            if not m:
                if fn != os.path.basename(PURGE_LOG):
                    skipped.append(fn)
                continue
            day = m.group(1)
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                skipped.append(fn)
                continue
            if day >= cutoff:
                kept.append(fn)
                continue
            if dry_run:
                removed.append(fn)
                continue
            try:
                os.remove(os.path.join(DIR, fn))
                removed.append(fn)
            except OSError as e:
                skipped.append(f"{fn} ({e})")
    result = {"cutoff": cutoff, "days": int(days), "dry_run": dry_run,
              "removed": removed, "kept": kept, "skipped": skipped,
              "remaining_dates": list_dates()}
    if removed and not dry_run:
        try:
            os.makedirs(DIR, exist_ok=True)
            with open(PURGE_LOG, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')} cutoff={cutoff} "
                        f"removed={','.join(removed)}\n")
        except OSError:
            pass
    return result
