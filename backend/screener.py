"""全市场量能异动扫描。

口径（用户已确认：只看量能异动、全市场、先手动触发）：
  量比 >= 2 为前提（腾讯实时快照字段 49，盘后 = 当日总量 / 前 5 日均量）：
    - 放量上攻     量比>=2 且 涨幅 >= 3%
    - 放量下跌     量比>=2 且 涨幅 <= -3%
    - 高位放量滞涨 量比>=2 且 |涨幅| < 1.5% 且 距52周高 >= -10%
    - 附加标签：涨停（10cm>=9.7 / 20cm>=19.7）、ST、次新（快照无上市日期则不打）
输出：data/screener/{行情日期}.json，保留最近 5 个文件。

扫描池（2026-11-08 修正）：
  以前用 jobs/fetch_members 产出的行业成分股快照当股票池，那份快照来自腾讯 pt
  板块，只有 4480 只且完全没有科创板/北交所，界面却写着「全市场扫描」。
  现在改为 data/snapshot/stock_names.json 全市场 A 股（EOD 每 7 天刷新），
  行业成分股快照只作为 LEFT JOIN 的标签源：行业名/阶段/细分态/RS60/dist52，
  取不到标签不影响入池。距52周高在标签缺失时用实时快照 high52 近似补齐。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from datasource.tencent import TencentSource

from data_paths import DATA_PATH
BASE = Path(__file__).resolve().parent
DATA = DATA_PATH /"screener"
MEMBERS_DIR = DATA_PATH /"snapshot" / "members"
NAMES_FILE = DATA_PATH /"snapshot" / "stock_names.json"
KEEP_DAYS = 5

VR_MIN = 2.0


def universe() -> dict[str, str]:
    """全市场 A 股 code → 名称（离线表，EOD 每 7 天刷新一次）。

    这是异动扫描唯一合法的股票池来源；行业成分股快照只用于挂标签，
    不能再拿它当池子，否则科创板/北交所会被整批漏掉。
    """
    try:
        with open(NAMES_FILE, encoding="utf-8") as f:
            d = json.load(f)
        items = d.get("items", []) if isinstance(d, dict) else d
        return {str(x["code"]): str(x.get("name") or x["code"])
                for x in (items or []) if isinstance(x, dict) and x.get("code")}
    except Exception:
        return {}


def industry_map() -> dict[str, dict]:
    """code → 行业/阶段标签（来自行业成分股快照），仅作 LEFT JOIN 用。"""
    try:
        with open(MEMBERS_DIR / "_index.json", encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for pt, info in idx.items():
        f = MEMBERS_DIR / f"{pt}.json"
        if not f.exists():
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                d = json.load(fp)
        except Exception:
            continue
        for m in d.get("members") or []:
            out[m.get("code")] = {
                "ind_pt": pt, "industry": info.get("name"),
                "stage": m.get("stage"), "sub": m.get("sub"),
                "dist52": m.get("dist52"), "rs60": m.get("rs60"),
                "grade": m.get("grade"), "quality": m.get("quality"),
                "pivot": m.get("pivot"), "stop": m.get("stop"),
                "sig": m.get("sig"), "sig_date": m.get("sig_date"),
            }
    return out


def _classify(r: dict, im: dict | None) -> tuple[str | None, list[str]]:
    """返回 (主标签, 附加标签列表)

    距52周高优先用行业快照里按收盘序列算的 dist52；快照没有该股时，
    用实时快照的 high52（盘中最高价口径）近似补齐并打 dist52_approx 标记，
    这样新上市/科创板/北交所也能参与「高位放量滞涨」判定。
    """
    chg, vr = r.get("chg"), r.get("vol_ratio")
    if chg is None or vr is None or vr < VR_MIN:
        return None, []
    dist52 = (im or {}).get("dist52")
    if dist52 is None:
        p, hi = r.get("price"), r.get("high52")
        if p and hi and hi > 0:
            dist52 = (p / hi - 1) * 100
            r["dist52"] = round(dist52, 2)
            r["dist52_approx"] = True
    tags: list[str] = []
    if chg <= -3:
        tag = "放量下跌"
    elif chg >= 3:
        tag = "放量上攻"
    elif abs(chg) < 1.5 and dist52 is not None and dist52 >= -10:
        tag = "高位放量滞涨"
    else:
        tag = None
    if tag is None:
        return None, []
    # 涨停判定（10cm / 20cm 板）
    code = r.get("code", "")
    if (chg >= 9.7 and not code.startswith(("sh68", "sz30"))) or \
       (chg >= 19.7 and code.startswith(("sh68", "sz30"))):
        tags.append("涨停")
    if "ST" in r.get("name", "").upper():
        tags.append("ST")
    return tag, tags


def scan(verbose: bool = False) -> dict:
    t0 = time.time()
    uni = universe()
    im = industry_map()
    codes = sorted(uni) if uni else sorted(im)
    if not codes:
        raise RuntimeError("全市场股票名称表为空，先跑 tools/build_stock_names.py")

    src = TencentSource(verbose=verbose)
    snaps = src.snapshots(codes)
    if verbose:
        print(f"[异动] 快照取回 {len(snaps)}/{len(codes)} 只 · "
              f"带行业标签 {sum(1 for c in codes if c in im)} 只")

    items: list[dict] = []
    quote_dates = []
    for code, s in snaps.items():
        if not s.get("price") or s["price"] <= 0 or not s.get("volume"):
            continue                                   # 停牌/未上市
        info = im.get(code) or {}
        row = {"code": code, **s, **{k: v for k, v in info.items() if v is not None}}
        row["name"] = s.get("name") or uni.get(code) or code
        tag, extra = _classify(row, info)
        if tag is None:
            continue
        row["tag"] = tag
        row["extra"] = extra
        if s.get("quote_date"):
            quote_dates.append(s["quote_date"])
        items.append(row)

    items.sort(key=lambda x: (-(x.get("vol_ratio") or 0), -(x.get("chg") or 0)))
    qdate = max(set(quote_dates), key=quote_dates.count) if quote_dates else \
        datetime.now().strftime("%Y-%m-%d")

    stats = {
        "total": len(items),
        "up": sum(1 for x in items if x["tag"] == "放量上攻"),
        "down": sum(1 for x in items if x["tag"] == "放量下跌"),
        "stall": sum(1 for x in items if x["tag"] == "高位放量滞涨"),
        "limit": sum(1 for x in items if "涨停" in x["extra"]),
        "star": sum(1 for x in items if x["code"].startswith("sh688")),
        "bse": sum(1 for x in items if x["code"].startswith("bj")),
    }
    out = {
        "date": qdate, "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "universe": len(codes), "labeled": sum(1 for c in codes if c in im),
        "quotes": len(snaps),
        "rules": f"量比>={VR_MIN} 且（涨跌≥3% 或 高位滞涨）",
        "source": "stock_names.json 全市场 A 股 · 行业标签来自 members 快照 LEFT JOIN",
        "stats": stats, "items": items,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    f = DATA / f"{qdate}.json"
    json.dump(out, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    _cleanup(verbose)
    if verbose:
        print(f"[异动] {qdate}: {stats}，耗时 {time.time() - t0:.1f}s → {f.name}")
    return out


def _cleanup(verbose=False):
    files = sorted(DATA.glob("*.json"))
    for f in files[:-KEEP_DAYS]:
        f.unlink()
        if verbose:
            print("[异动] 清理过期", f.name)


def dates() -> list[str]:
    return sorted((f.stem for f in DATA.glob("*.json")), reverse=True)[:KEEP_DAYS]


def load(date: str | None = None) -> dict | None:
    if date is None:
        ds = dates()
        if not ds:
            return None
        date = ds[0]
    f = DATA / f"{date}.json"
    if not f.exists():
        return None
    return json.load(open(f, encoding="utf-8"))


if __name__ == "__main__":
    r = scan(verbose=True)
    print(json.dumps(r["stats"], ensure_ascii=False))
