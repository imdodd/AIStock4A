"""擒龙引擎：双闸门 + 四支柱打分。

理论来源（用户 2026-11-25 确认）：
  如果按「胜率优先」在盘后静态筛龙，会系统性地错过真龙——因为真龙首板
  往往看着最弱。所以改成两道闸门：
    闸门一（宽进）：当日涨停 / 触板 / 首板 / 炸板全收进潜龙池，只挂标签，
                   不设胜率门槛、不淘汰。宁可错收一千，不可漏掉一条。
    闸门二（严出）：用**次日真实走势**当裁判——是否晋级、封板质量、题材
                   带动、资金承接，四支柱打分确认谁是真龙；掉队的降级为
                   「退位龙」保留观察（反包二波还在池子里），不删。

  这样「首板看着弱」的真龙不会因为闸门一被筛掉，而是在闸门二被重新捞出来。

情绪冰点（用户确认：只观察不提示）：
  情绪冰点或退潮期，闸门二只做记录、把 status 标成 observe，前端不进
  「确认龙」提示区——避免冰点打板送人头。数据照样留，方便复盘。

限流约束（关键，勿改）：
  * 涨停池走 datasource.evidence.zt_pool()：整池一次请求 + 落盘缓存，
    一天最多 1 次东财请求。
  * 盘中验证只走腾讯 TencentSource.snapshots()（一次 60 只），零东财请求。
  * 板块资金流读 sectorflow 的本地缓存，不再打东财。
  盘中三个时点（09:35 / 10:00 / 14:45）绝不调 evidence 的任何东财函数。

落盘：
  data/dragon/pool-{date}.json       闸门一潜龙池
  data/dragon/verdict-{date}.json    闸门二验证结果（date 是被验证的池子日）
  data/dragon/intraday-{date}.json   盘中验证（date 是当天）
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from data_paths import DATA_DIR, safe_slug

DRAGON_DIR = Path(DATA_DIR) / "dragon"
RAW_DIR = Path(DATA_DIR) / "raw"
SENTIMENT_FILE = RAW_DIR / "sentiment.json"

# 四支柱每项满分 25，总分 100。确认龙门槛 68 且必须晋级。
CONFIRM_MIN = 68
# 情绪档阈值：updown_ratio 涨跌家数比、limit_up 涨停家数、limit_down 跌停家数
ICE_UPDOWN = 0.35
ICE_LIMIT_UP = 12
HOT_LIMIT_UP = 60
HOT_UPDOWN = 1.5


# ---------------------------------------------------------------- 落盘读写
def _path(kind: str, date: str) -> Path:
    d = safe_slug(date, "日期")
    return DRAGON_DIR / f"{kind}-{d}.json"


def _dump(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def load_pool(date: str) -> dict | None:
    return _load(_path("pool", date))


def load_verdict(date: str) -> dict | None:
    return _load(_path("verdict", date))


def load_intraday(date: str) -> dict | None:
    return _load(_path("intraday", date))


def dates(kind: str = "pool", n: int = 20) -> list[str]:
    if not DRAGON_DIR.exists():
        return []
    prefix = kind + "-"
    names = [p.stem[len(prefix):] for p in DRAGON_DIR.glob(f"{prefix}*.json")
             if p.stem.startswith(prefix)]
    return sorted(names, reverse=True)[:n]


# ---------------------------------------------------------------- 情绪档位
def sentiment_state() -> dict:
    """读 raw/sentiment.json，给情绪档位。

    文件缺失（比如 --no-sentiment 跑的 EOD）时返回 unknown，**不默认成冰点**
    —— Ice 判定要有证据，不能用缺数据冒充冰点把真龙全藏了。
    """
    d = _load(SENTIMENT_FILE) or {}
    up, down = d.get("up"), d.get("down")
    lu, ld = d.get("limit_up"), d.get("limit_down")
    if up is None or down is None:
        return {"regime": "unknown", "ice": False, "updown_ratio": None,
                "limit_up": lu, "limit_down": ld, "source": "sentiment.json 缺失"}
    ratio = round(up / down, 3) if down else None
    lu, ld = int(lu or 0), int(ld or 0)
    ice = ((ratio is not None and ratio <= ICE_UPDOWN and ld >= lu) or lu <= ICE_LIMIT_UP)
    hot = (lu >= HOT_LIMIT_UP and ratio is not None and ratio >= HOT_UPDOWN)
    regime = "ice" if ice else ("hot" if hot else "normal")
    return {"regime": regime, "ice": ice, "hot": hot,
            "updown_ratio": ratio, "limit_up": lu, "limit_down": ld,
            "source": "raw/sentiment.json"}


# ---------------------------------------------------------------- 辅助
def limit_pct(code: str) -> float:
    """涨停幅度：科创/创业 20cm，北交所 30cm，其余 10cm。ST 一律按 10cm 粗判。"""
    if code.startswith(("sh68", "sz30")):
        return 0.20
    if code.startswith("bj"):
        return 0.30
    return 0.10


def limit_price(code: str, prev: float | None) -> float | None:
    if not prev or prev <= 0:
        return None
    return round(prev * (1 + limit_pct(code)), 2)


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fbt_min(fbt) -> float | None:
    """东财首封时间 93000 → 9.5（小时）。取不到返回 None。"""
    n = _num(fbt)
    if n is None or n <= 0:
        return None
    s = str(int(n)).zfill(6)
    h, m = int(s[:2]), int(s[2:4])
    return h + m / 60 if 9 <= h <= 15 else None


# ---------------------------------------------------------------- 闸门一
def build_pool(date: str, verbose: bool = False) -> dict:
    """闸门一：当日涨停 + 触板 + 首板 + 炸板全收，宽进不淘汰。

    数据来源：
      evidence.zt_pool(date)  涨停池（连板/封单/炸板/首封/行业）— 单次东财请求+缓存
      screener.load(date)     全市场异动（涨停标签、量比、换手、行业阶段标签）
    两者并集，涨停池为准补齐 lbc 等字段。
    """
    import sys
    HERE = str(Path(__file__).resolve().parent.parent)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from datasource import evidence
    import screener as scr

    zt = evidence.zt_pool(date) or {}
    sc = scr.load(date) or {}
    sc_rows = {r.get("code"): r for r in (sc.get("items") or []) if r.get("code")}
    im = scr.industry_map()

    # 名称兜底：涨停池/异动行可能没有 name，用全市场名称表补（4559 只，EOD 周更）
    all_names: dict[str, str] = {}
    try:
        with open(Path(DATA_DIR) / "snapshot" / "stock_names.json", encoding="utf-8") as fh:
            nd = json.load(fh)
        for x in (nd.get("items", []) if isinstance(nd, dict) else nd):
            if isinstance(x, dict) and x.get("code"):
                all_names[str(x["code"])] = str(x.get("name") or "")
    except Exception:
        pass

    sent = sentiment_state()
    items: list[dict] = []
    for code in sorted(set(zt) | set(sc_rows)):
        z = zt.get(code) or {}
        s = sc_rows.get(code) or {}
        info = im.get(code) or {}
        # 入池条件：在涨停池里（涨停/炸板触板），或异动里被判定涨停
        in_zt = bool(z)
        tagged_limit = "涨停" in (s.get("extra") or [])
        if not in_zt and not tagged_limit:
            continue
        lbc = int(_num(z.get("lbc")) or 0)
        zbc = int(_num(z.get("zbc")) or 0)
        if in_zt:
            source = "zt_pool"
        else:
            source = "screener_limit"
        items.append({
            "code": code,
            "name": s.get("name") or info.get("name") or all_names.get(code) or code,
            "source": source,
            "gate": "wide",
            "lbc": lbc or (1 if tagged_limit else 0),
            "zbc": zbc,
            "fbt": z.get("fbt"), "lbt": z.get("lbt"),
            "fund": _num(z.get("fund")),
            "hybk": z.get("hybk") or s.get("industry") or info.get("industry"),
            "days": z.get("days"), "ct": z.get("ct"),
            "quote": {k: s.get(k) for k in
                      ("price", "prev", "open", "chg", "high", "low",
                       "vol_ratio", "turnover", "amount", "amplitude")},
            "industry": info.get("industry") or s.get("industry"),
            "ind_pt": info.get("ind_pt"),
            "stage": info.get("stage"), "rs60": info.get("rs60"),
            "dist52": info.get("dist52"),
            "tag": s.get("tag"), "extra": s.get("extra") or [],
            "pre_score": None,           # 闸门一不打分，避免胜率优先漏龙
        })

    out = {
        "date": date,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "gate": "wide",
        "rule": "涨停/触板/首板/炸板全收；不设胜率门槛、不淘汰",
        "sentiment": sent,
        "source": "evidence.zt_pool(单次缓存) + screener 全市场异动",
        "count": len(items),
        "items": items,
    }
    _dump(_path("pool", date), out)
    if verbose:
        print(f"[擒龙·闸门一] {date}: 潜龙 {len(items)} 只 · 情绪 {sent['regime']} "
              f"· 涨停池 {len(zt)} / 异动涨停 {sum(1 for i in items if i['source'] == 'screener_limit')}")
    return out


# ---------------------------------------------------------------- 四支柱
def score_one(item: dict, *, zt_today: dict | None = None,
              theme_limits: dict | None = None,
              sector_rank: list | None = None,
              dragon: dict | None = None,
              flow: dict | None = None) -> dict:
    """四支柱打分，每项 0-25，总分 100。所有输入缺失都给中性分、不臆造。"""
    lbc = int(_num(item.get("lbc")) or 0)
    zbc = int(_num(item.get("zbc")) or 0)
    fbt = _fbt_min(item.get("fbt"))
    quote = item.get("quote") or {}
    fund = _num(item.get("fund"))
    amount = _num(quote.get("amount"))          # 万元
    turnover = _num(quote.get("turnover"))
    reasons: list[str] = []

    # --- 高度（连板数 + 是否题材空间板）---
    height = {0: 3, 1: 7, 2: 13, 3: 18, 4: 22}.get(lbc, 25 if lbc >= 5 else 3)
    if lbc >= 3:
        reasons.append(f"{lbc} 连板，进入高位梯队")
    theme = item.get("hybk") or item.get("industry")
    tl = theme_limits or {}
    if theme and lbc and lbc >= tl.get(theme, 0) and tl.get(theme, 0) >= 2:
        height = min(25, height + 3)
        reasons.append(f"「{theme}」题材空间板")

    # --- 强度（首封早 / 炸板少 / 封单占成交比）---
    strength = 0
    if fbt is not None:
        strength += 9 if fbt <= 9.58 else (6 if fbt <= 10.0 else (4 if fbt <= 10.5 else 2))
        if fbt <= 10.0:
            # fbt 是东财 HHMMSS 整数（如 92500 = 09:25:00，集合竞价一字板；
            # 93000 = 09:30:00 开盘即封）。直接把原始数字塞进文案用户看不懂，
            # 这里翻译成时分秒，并补一句口径说明。
            # 注意：上面的 fbt 已被 _fbt_min 转成小时小数，这里必须取原始字段。
            s = str(int(_num(item.get("fbt")) or 0)).zfill(6)
            hhmmss = f"{s[0:2]}:{s[2:4]}:{s[4:6]}"
            note = "（集合竞价即封）" if s <= "093000" else "（开盘后快速封板）"
            reasons.append(f"首封 {hhmmss} 较早{note}")
    else:
        strength += 4
    strength += 9 if zbc == 0 else (5 if zbc == 1 else (2 if zbc == 2 else 0))
    if zbc == 0:
        reasons.append("全天零炸板，封板质量高")
    elif zbc >= 3:
        reasons.append(f"炸板 {zbc} 次，封板不稳")
    seal_ratio = (fund / 10000 / amount) if (fund and amount and amount > 0) else None
    if seal_ratio is None:
        strength += 3
    else:
        strength += 7 if seal_ratio >= 0.10 else (5 if seal_ratio >= 0.05 else
                     (3 if seal_ratio >= 0.02 else 1))

    # --- 带动（同题材涨停家数 + 板块资金排名）---
    drive = 0
    n_limit = int(tl.get(theme) or 0) if theme else 0
    drive += 12 if n_limit >= 5 else (9 if n_limit >= 3 else (6 if n_limit >= 2 else
              (3 if n_limit >= 1 else 2)))
    if n_limit >= 3:
        # 名单由 verify_day 通过 theme_peers 注入，前端会把同题材个股渲染成可点击列表。
        # 这里只写结论句，避免把整串股票名塞进文字理由里。
        reasons.append(f"同题材 {n_limit} 只涨停，板块联动（下方列出个股，可点击看 K 线）")
    rank = next((r for r in (sector_rank or [])
                 if str(r.get("name") or "").replace("Ⅱ", "").strip()
                 == str(theme or "").replace("Ⅱ", "").strip()), None)
    main_net = _num(rank.get("zljlr")) if rank else None
    if main_net is None:
        drive += 3
    elif main_net > 0:
        drive += 6
        reasons.append("所属板块主力净流入为正")
    else:
        drive += 1
    # 板块资金排名靠前加分
    if rank and isinstance(rank.get("_rank"), int) and rank["_rank"] <= 10:
        drive += 4

    # --- 资金（龙虎榜净买 + 主力净流入 + 换手健康度）---
    money = 0
    net_buy = _num((dragon or {}).get("net_buy"))
    if (dragon or {}).get("on_board"):
        money += 10 if (net_buy or 0) > 0 else 3
        if (net_buy or 0) > 0:
            reasons.append(f"龙虎榜净买 {(net_buy / 1e4):.0f} 万")
    else:
        money += 4
    mnet = _num((flow or {}).get("main_net"))
    if mnet is None:
        money += 3
    elif mnet > 0:
        money += 8
    else:
        money += 2
    if turnover is None:
        money += 3
    elif 3 <= turnover <= 25:
        money += 7
    elif turnover > 35:
        money += 3
        reasons.append(f"换手 {turnover:.0f}% 偏高，接力盘不稳")
    else:
        money += 4

    parts = {"height": height, "strength": strength,
             "drive": drive, "money": money}
    total = sum(parts.values())
    return {"parts": parts, "total": total, "reasons": reasons[:6],
            "metrics": {"lbc": lbc, "zbc": zbc, "fbt": item.get("fbt"),
                        "seal_ratio": round(seal_ratio, 3) if seal_ratio else None,
                        "theme_limits": n_limit,
                        "main_net": main_net, "net_buy": net_buy,
                        "turnover": turnover}}


def _theme_limit_map(pool: dict) -> dict[str, int]:
    """从潜龙池统计每个题材的涨停只数（闸门一已含涨停池 lbc）。"""
    out: dict[str, int] = {}
    for it in pool.get("items") or []:
        theme = it.get("hybk") or it.get("industry")
        if not theme:
            continue
        lbc = _num(it.get("lbc")) or 0
        if lbc >= 1:
            out[theme] = out.get(theme, 0) + 1
    return out


def _sector_rank() -> list:
    """读 sectorflow 本地缓存的板块资金排名（零东财请求）。"""
    files = [Path(DATA_DIR) / "cache" / "sectorflow_rank.json"]
    for f in files:
        d = _load(f)
        if not d:
            continue
        rows = d.get("items") if isinstance(d, dict) else d
        if isinstance(rows, list):
            for i, r in enumerate(rows):
                if isinstance(r, dict):
                    r["_rank"] = i + 1
            return rows
    return []


# ---------------------------------------------------------------- 闸门二
def verify_day(prev_date: str, trade_date: str, verbose: bool = False) -> dict:
    """闸门二（盘后）：验证 prev_date 潜龙在 trade_date 的晋级结果并打分分类。

    只在盘后调用：此时可用 trade_date 涨停池（evidence 缓存，1 次请求）。
    status:
      confirmed 确认龙 —— 晋级 + 总分达标 + 非冰点
      retired   退位龙 —— 掉队 / 未晋级 / 分数不足（保留观察，不删）
      observe   只观察 —— 情绪冰点，一律不进确认提示区
    """
    import sys
    HERE = str(Path(__file__).resolve().parent.parent)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from datasource import evidence

    pool = load_pool(prev_date) or {"items": []}
    zt_now = evidence.zt_pool(trade_date) or {}
    sent = sentiment_state()
    theme_limits = _theme_limit_map(pool)
    rank = _sector_rank()
    # 同题材涨停名单：按题材分组，供前端「板块联动」渲染成可点击列表。
    # 只取涨停（lbc>=1），与 _theme_limit_map 的口径一致；排除自己由前端处理。
    theme_peers_map: dict[str, list[dict]] = {}
    for it in pool.get("items") or []:
        theme = it.get("hybk") or it.get("industry")
        if not theme or (_num(it.get("lbc")) or 0) < 1:
            continue
        theme_peers_map.setdefault(theme, []).append({
            "code": it.get("code"), "name": it.get("name") or it.get("code"),
            "lbc": int(_num(it.get("lbc")) or 0),
        })
    for peers in theme_peers_map.values():
        peers.sort(key=lambda x: -x["lbc"])

    # 只对打分 Top10 拉龙虎榜/资金流，控制东财请求量
    pre: list[tuple[int, dict, dict]] = []
    for it in pool.get("items") or []:
        s = score_one(it, zt_today=zt_now, theme_limits=theme_limits, sector_rank=rank)
        pre.append((s["total"], it, s))
    pre.sort(key=lambda x: -x[0])

    items: list[dict] = []
    for rank_i, (total, it, s) in enumerate(pre):
        code = it.get("code")
        z = zt_now.get(code) or {}
        prev_lbc = int(_num(it.get("lbc")) or 0)
        now_lbc = int(_num(z.get("lbc")) or 0)
        advanced = bool(z) and now_lbc > prev_lbc
        on_limit = bool(z)
        dragon, flow = {}, {}
        if rank_i < 10:
            try:
                dragon = evidence.dragon_tiger(code, trade_date) or {}
            except Exception:
                dragon = {}
            try:
                flow = evidence.fund_flow(code) or {}
            except Exception:
                flow = {}
            s = score_one(it, zt_today=zt_now, theme_limits=theme_limits,
                          sector_rank=rank, dragon=dragon, flow=flow)
            total = s["total"]
        if sent["ice"]:
            status = "observe"
        elif advanced and total >= CONFIRM_MIN:
            status = "confirmed"
        else:
            status = "retired"
        items.append({
            "code": code, "name": it.get("name"),
            "prev_lbc": prev_lbc, "now_lbc": now_lbc,
            "advanced": advanced, "on_limit": on_limit,
            "status": status, "score": s["total"], "score_detail": s["parts"],
            "reasons": s["reasons"], "metrics": s["metrics"],
            "theme_peers": theme_peers_map.get(
                it.get("hybk") or it.get("industry") or "", []),
            "industry": it.get("industry") or it.get("hybk"),
            "stage": it.get("stage"),
            "quote": it.get("quote"),
            "dragon": {"on_board": dragon.get("on_board"),
                       "net_buy": dragon.get("net_buy")},
            "flow": {"main_net": flow.get("main_net")},
        })

    counts = {k: sum(1 for i in items if i["status"] == k)
              for k in ("confirmed", "retired", "observe")}
    out = {
        "pool_date": prev_date,
        "verified_on": trade_date,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sentiment": sent,
        "confirm_min": CONFIRM_MIN,
        "rule": "晋级+四支柱≥68 且非冰点 → 确认龙；掉队 → 退位龙保留观察；冰点 → 只观察",
        "counts": counts,
        "items": items,
    }
    _dump(_path("verdict", prev_date), out)
    if verbose:
        print(f"[擒龙·闸门二] {prev_date}→{trade_date}: 确认 {counts['confirmed']} / "
              f"退位 {counts['retired']} / 观察 {counts['observe']} · 情绪 {sent['regime']}")
    return out


# ---------------------------------------------------------------- 盘中验证
def intraday_check(slot: str, trade_date: str, pool_date: str | None = None,
                   verbose: bool = False) -> dict:
    """盘中验证：腾讯快照批量查潜龙当日晋级/破板，零东财请求。

    slot: open(09:35) / mid(10:00) / close(14:45)
    pool_date 缺省取最近一期潜龙池（即上一交易日池子）。
    """
    from datasource.tencent import TencentSource

    if pool_date is None:
        ds = dates("pool", 3)
        pool_date = ds[0] if ds else trade_date
    pool = load_pool(pool_date) or {"items": []}
    codes = [i.get("code") for i in (pool.get("items") or []) if i.get("code")]
    sent = sentiment_state()

    snaps = TencentSource().snapshots(codes) if codes else {}
    items = []
    for it in pool.get("items") or []:
        code = it.get("code")
        s = snaps.get(code) or {}
        price, prev = _num(s.get("price")), _num(s.get("prev"))
        high = _num(s.get("high"))
        lp = limit_price(code, prev)
        touched = bool(lp and high and high >= lp * 0.998)
        sealed = bool(lp and price and price >= lp * 0.998)
        if sealed:
            state = "sealed"
        elif touched:
            state = "broken"          # 曾涨停但当前不在涨停价 → 炸板
        elif price and prev and price < prev * 0.97:
            state = "weak"
        else:
            state = "pending"
        items.append({
            "code": code, "name": it.get("name"),
            "price": price, "chg": s.get("chg"), "vol_ratio": s.get("vol_ratio"),
            "limit_price": lp, "touched": touched, "sealed": sealed,
            "state": state, "prev_lbc": it.get("lbc"),
            "observe_only": sent["ice"],
        })

    out = {
        "trade_date": trade_date, "pool_date": pool_date, "slot": slot,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "tencent.snapshots(零东财请求)",
        "sentiment": sent,
        "counts": {k: sum(1 for i in items if i["state"] == k)
                   for k in ("sealed", "broken", "weak", "pending")},
        "items": items,
    }
    path = _path("intraday", trade_date)
    # 三个时点写同一天文件，按 slot 分槽保留
    prev_day = _load(path) or {"trade_date": trade_date, "slots": {}}
    prev_day.setdefault("slots", {})[slot] = out
    prev_day["updated_at"] = out["checked_at"]
    _dump(path, prev_day)
    if verbose:
        print(f"[擒龙·盘中 {slot}] {trade_date}: 封板 {out['counts']['sealed']} / "
              f"炸板 {out['counts']['broken']} / 走弱 {out['counts']['weak']}")
    return out


def _attach_quotes(items: list[dict]) -> None:
    """用腾讯快照批量补现价/涨幅（一次请求，零东财）。

    验证文件落盘时没有存实时行情，历史回看时三档行的现价/涨幅会是「—」。
    这里在 API 汇总时按代码批量补一次；接口失败就保持原值，不影响主流程。
    """
    codes = [i.get("code") for i in items if i.get("code")]
    if not codes:
        return
    try:
        from datasource.tencent import TencentSource
        qs = TencentSource().snapshots(list(dict.fromkeys(codes))) or {}
    except Exception:
        return
    for it in items:
        q = dict(it.get("quote") or {})
        s = qs.get(it.get("code")) or {}
        if s.get("price") is not None:
            q.update({k: s.get(k) for k in
                      ("price", "prev", "open", "high", "low", "chg",
                       "turnover", "amount", "amplitude", "vol_ratio", "quote_date")})
        it["quote"] = q


# ---------------------------------------------------------------- API 汇总
def current(date: str | None = None) -> dict:
    """给 /api/dragon 用：最近一份「已验证」的潜龙池 + 盘中状态，三档归类。

    当天新建的池子要等次日走势才能验证，若直接取最新池会把上一交易日验证出的
    确认龙顶掉。所以默认优先取最近一份带 verdict 的池子；只有一份 verdict 都
    没有时（首次上线）才回退到最新池，并标记 awaiting_verification。
    """
    pool_dates = dates("pool", 10)
    latest_pool = pool_dates[0] if pool_dates else None
    if not date:
        date = next((d for d in pool_dates if load_verdict(d)), None) or latest_pool
    if not date:
        return {"date": None, "confirmed": [], "retired": [], "observing": [],
                "pool": None, "intraday": None, "empty": True,
                "awaiting_verification": True, "latest_pool_date": None}
    pool = load_pool(date) or {}
    verdict = load_verdict(date)
    # 盘中验证写在「验证当天」的文件里（如 intraday-2026-11-25.json），
    # 但验证的是「上一交易日池子」（pool_date=2026-11-24）。所以按 pool_date
    # 去最近几天的 intraday 文件里找，而不是直接读 intraday-{date}。
    intraday: dict = {}
    for d in dates("intraday", 5):
        cand = load_intraday(d) or {}
        slots = cand.get("slots") or {}
        # pool_date 记在每个 slot 里；任一 slot 指向本池，或文件名就是本池日
        slot_pool = {(v.get("pool_date") if isinstance(v, dict) else None)
                     for v in slots.values()}
        if date in slot_pool or d == date:
            intraday = cand
            break
    sent = verdict.get("sentiment") if verdict else (pool.get("sentiment") or sentiment_state())

    confirmed, retired, observing = [], [], []
    for it in (verdict or {}).get("items") or []:
        if it.get("status") == "confirmed":
            confirmed.append(it)
        elif it.get("status") == "observe":
            observing.append(it)
        else:
            retired.append(it)
    # 尚无验证结果时：整池进 observing（闸门一出池即观察，不当天提示）
    if not verdict:
        observing = [dict(i, status="observe", score=i.get("pre_score"),
                          score_detail=None, reasons=[])
                     for i in (pool.get("items") or [])]
    confirmed.sort(key=lambda x: -(x.get("score") or 0))
    retired.sort(key=lambda x: -(x.get("score") or 0))
    _attach_quotes(confirmed + retired + observing)
    return {
        "date": date,
        "sentiment": sent,
        "confirm_min": CONFIRM_MIN,
        "counts": {"confirmed": len(confirmed), "retired": len(retired),
                   "observing": len(observing)},
        "confirmed": confirmed,
        "retired": retired,
        "observing": observing,
        "pool": {"date": pool.get("date"), "count": pool.get("count"),
                 "rule": pool.get("rule"), "source": pool.get("source")},
        "verified_on": verdict.get("verified_on") if verdict else None,
        "awaiting_verification": not bool(verdict),
        "latest_pool_date": latest_pool,
        "intraday": intraday.get("slots") or {},
        "intraday_date": intraday.get("trade_date"),
        "empty": not (confirmed or retired or observing),
    }


__all__ = ["build_pool", "verify_day", "intraday_check", "current",
           "load_pool", "load_verdict", "load_intraday", "dates",
           "sentiment_state", "limit_price", "CONFIRM_MIN"]
