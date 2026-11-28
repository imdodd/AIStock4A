"""擒龙引擎：双闸门 + 四支柱打分。

理论来源（用户 2026-11-25 确认）：
  如果按「胜率优先」在盘后静态筛龙，会系统性地错过真龙——因为真龙首板
  往往看着最弱。所以改成两道闸门：
    闸门一（宽进）：当日涨停 / 触板 / 首板 / 炸板全收进潜龙池，只挂标签，
                   不设胜率门槛、不淘汰。宁可错收一千，不可漏掉一条。
    闸门二（严出）：用**次日真实走势**当裁判——是否晋级、封板质量、题材
                   带动、资金承接，四支柱打分确认谁是真龙；晋级但分数不够的挂
                  「晋级未确认」单独观察，真掉队的才降级为「退位龙」保留观察
                  （反包二波还在池子里），不删。

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

    # 监管异动额度：连板能走多高，一半看情绪一半看交易所的异动条款。
    # 这里在建池时算一次并落盘（约 3~6 秒，只补缺失的票），API 直接读，不联网。
    try:
        from engines import regwatch
        regwatch.annotate(items, date, verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"[擒龙·闸门一] 监管异动计算跳过：{e}")

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
        from engines import regwatch as _rw
        st = _rw.stats(items)
        print(f"[擒龙·闸门一] {date}: 潜龙 {len(items)} 只 · 情绪 {sent['regime']} "
              f"· 涨停池 {len(zt)} / 异动涨停 {sum(1 for i in items if i['source'] == 'screener_limit')}"
              f" · 监管 严重{st['severe']}/异动{st['alert'] + st['warn']}/临界{st['near']}")
    return out


# ---------------------------------------------------------------- 四支柱
def score_one(item: dict, *, zt_today: dict | None = None,
              theme_limits: dict | None = None,
              sector_rank: list | None = None,
              dragon: dict | None = None,
              flow: dict | None = None,
              effective_lbc: int | None = None) -> dict:
    """四支柱打分，每项 0-25，总分 100。所有输入缺失都给中性分、不臆造。

    effective_lbc：闸门二验证时传入「验证日高度」——晋级了就按晋级后的连板数
    算高度分，没晋级则保留池子日高度。此前固定用池子日 lbc，导致晋级股
    高度被系统性低估（如 3→4 板仍按 3 板给 18 分，差 4 分卡在 68 门槛）。
    """
    raw_lbc = int(_num(item.get("lbc")) or 0)
    lbc = int(effective_lbc) if effective_lbc is not None else raw_lbc
    zbc = int(_num(item.get("zbc")) or 0)
    fbt = _fbt_min(item.get("fbt"))
    quote = item.get("quote") or {}
    fund = _num(item.get("fund"))
    amount = _num(quote.get("amount"))          # 万元
    turnover = _num(quote.get("turnover"))
    reasons: list[str] = []
    # 逐项计分明细：每根柱子由哪几个小项加出来、各得几分、该项上限多少。
    # 前端鼠标停在柱子上就按这个渲染公式（"连板 3 板 → 18 分 / 题材空间板 +3"），
    # 不用再让用户拿一句笼统的规则自己套。max 是「该项在天花板里占多少分」，
    # 不是「本项可得上限」——两者不一致时（如驱动项封顶 22）前端会单独标出。
    detail: dict[str, list[dict]] = {"height": [], "strength": [], "drive": [], "money": []}

    # --- 高度（连板数 + 是否题材空间板）---
    height_base = {0: 3, 1: 7, 2: 13, 3: 18, 4: 22}.get(lbc, 25 if lbc >= 5 else 3)
    height = height_base
    detail["height"].append({"name": f"连板 {lbc} 板", "pts": height_base, "max": 25})
    if lbc >= 3:
        reasons.append(f"{lbc} 连板，进入高位梯队")
    theme = item.get("hybk") or item.get("industry")
    tl = theme_limits or {}
    if theme and lbc and lbc >= tl.get(theme, 0) and tl.get(theme, 0) >= 2:
        before = height
        height = min(25, height + 3)
        detail["height"].append({
            "name": f"「{theme}」题材空间板（题材内最高 {tl.get(theme, 0)} 板）",
            "pts": height - before, "max": 3,
            "note": "已满 25 封顶" if height == before else ""})
        reasons.append(f"「{theme}」题材空间板")
    elif lbc >= 5:
        detail["height"].append({"name": "≥5 板已封顶", "pts": 0, "max": 3})

    # --- 强度（首封早 / 炸板少 / 封单占成交比）---
    strength = 0
    if fbt is not None:
        s_pts = 9 if fbt <= 9.58 else (6 if fbt <= 10.0 else (4 if fbt <= 10.5 else 2))
        strength += s_pts
        if fbt <= 10.0:
            # fbt 是东财 HHMMSS 整数（如 92500 = 09:25:00，集合竞价一字板；
            # 93000 = 09:30:00 开盘即封）。直接把原始数字塞进文案用户看不懂，
            # 这里翻译成时分秒，并补一句口径说明。
            # 注意：上面的 fbt 已被 _fbt_min 转成小时小数，这里必须取原始字段。
            s = str(int(_num(item.get("fbt")) or 0)).zfill(6)
            hhmmss = f"{s[0:2]}:{s[2:4]}:{s[4:6]}"
            note = "（集合竞价即封）" if s <= "093000" else "（开盘后快速封板）"
            reasons.append(f"首封 {hhmmss} 较早{note}")
            detail["strength"].append({"name": f"首封 {hhmmss}{note}",
                                       "pts": s_pts, "max": 9})
        else:
            detail["strength"].append({"name": f"首封 {fbt:.2f} 时（晚于 10:00）",
                                       "pts": s_pts, "max": 9})
    else:
        strength += 4
        detail["strength"].append({"name": "无首封时间（给中性分）", "pts": 4, "max": 9})
    z_pts = 9 if zbc == 0 else (5 if zbc == 1 else (2 if zbc == 2 else 0))
    strength += z_pts
    detail["strength"].append({"name": f"炸板 {zbc} 次", "pts": z_pts, "max": 9})
    if zbc == 0:
        reasons.append("全天零炸板，封板质量高")
    elif zbc >= 3:
        reasons.append(f"炸板 {zbc} 次，封板不稳")
    seal_ratio = (fund / 10000 / amount) if (fund and amount and amount > 0) else None
    if seal_ratio is None:
        strength += 3
        detail["strength"].append({"name": "无成交额（封单占比给中性分）", "pts": 3, "max": 7})
    else:
        seal_pts = (7 if seal_ratio >= 0.10 else (5 if seal_ratio >= 0.05 else
                    (3 if seal_ratio >= 0.02 else 1)))
        strength += seal_pts
        detail["strength"].append({"name": f"封单占成交 {seal_ratio * 100:.1f}%",
                                   "pts": seal_pts, "max": 7})

    # --- 带动（同题材涨停家数 + 板块资金排名）---
    drive = 0
    n_limit = int(tl.get(theme) or 0) if theme else 0
    d_pts = (12 if n_limit >= 5 else (9 if n_limit >= 3 else (6 if n_limit >= 2 else
             (3 if n_limit >= 1 else 2))))
    drive += d_pts
    detail["drive"].append({"name": f"同题材「{theme or '未分类'}」涨停 {n_limit} 只",
                            "pts": d_pts, "max": 12})
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
        detail["drive"].append({"name": "板块主力资金查无（给中性分）", "pts": 3, "max": 6})
    elif main_net > 0:
        drive += 6
        detail["drive"].append({"name": f"板块主力净流入 {main_net / 1e8:.2f} 亿",
                                "pts": 6, "max": 6})
        reasons.append("所属板块主力净流入为正")
    else:
        drive += 1
        detail["drive"].append({"name": f"板块主力净流出 {abs(main_net) / 1e8:.2f} 亿",
                                "pts": 1, "max": 6})
    # 板块资金排名靠前加分
    if rank and isinstance(rank.get("_rank"), int) and rank["_rank"] <= 10:
        drive += 4
        detail["drive"].append({"name": f"板块资金排名 第 {rank['_rank']} 名（前 10）",
                                "pts": 4, "max": 4})

    # --- 资金（龙虎榜净买 + 主力净流入 + 换手健康度）---
    money = 0
    net_buy = _num((dragon or {}).get("net_buy"))
    if (dragon or {}).get("on_board"):
        if (net_buy or 0) > 0:
            money += 10
            detail["money"].append({"name": f"龙虎榜净买 {net_buy / 1e4:.0f} 万",
                                    "pts": 10, "max": 10})
            reasons.append(f"龙虎榜净买 {(net_buy / 1e4):.0f} 万")
        else:
            money += 3
            detail["money"].append({"name": f"龙虎榜上榜但净卖 {abs(net_buy or 0) / 1e4:.0f} 万",
                                    "pts": 3, "max": 10})
    else:
        money += 4
        detail["money"].append({"name": "未上龙虎榜", "pts": 4, "max": 10})
    mnet = _num((flow or {}).get("main_net"))
    if mnet is None:
        money += 3
        detail["money"].append({"name": "个股主力资金查无（给中性分）", "pts": 3, "max": 8})
    elif mnet > 0:
        money += 8
        detail["money"].append({"name": f"个股主力净流入 {mnet / 1e8:.2f} 亿",
                                "pts": 8, "max": 8})
    else:
        money += 2
        detail["money"].append({"name": f"个股主力净流出 {abs(mnet) / 1e8:.2f} 亿",
                                "pts": 2, "max": 8})
    if turnover is None:
        money += 3
        detail["money"].append({"name": "换手率查无（给中性分）", "pts": 3, "max": 7})
    elif 3 <= turnover <= 25:
        money += 7
        detail["money"].append({"name": f"换手 {turnover:.1f}%（3–25% 健康）",
                                "pts": 7, "max": 7})
    elif turnover > 35:
        money += 3
        detail["money"].append({"name": f"换手 {turnover:.1f}%（>35% 偏高）",
                                "pts": 3, "max": 7})
        reasons.append(f"换手 {turnover:.0f}% 偏高，接力盘不稳")
    else:
        money += 4
        detail["money"].append({"name": f"换手 {turnover:.1f}%（3–25% 之外）",
                                "pts": 4, "max": 7})

    parts = {"height": height, "strength": strength,
             "drive": drive, "money": money}
    total = sum(parts.values())
    return {"parts": parts, "total": total, "reasons": reasons[:6],
            "detail": detail,
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
      pending   晋级未确认 —— 晋级了但总分不足（还在板上，只是四支柱不够硬）
      retired   退位龙 —— 未晋级 / 掉队（保留观察，不删）
      observe   只观察 —— 情绪冰点，一律不进确认提示区
    """
    import sys
    HERE = str(Path(__file__).resolve().parent.parent)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from datasource import evidence

    pool = load_pool(prev_date) or {"items": []}
    zt_now = evidence.zt_pool(trade_date) or {}
    # 池子里的 quote 来自 screener 行；纯涨停池入池的个股多数没有 screener 行，
    # 于是换手/成交额为空，strength 的「封单占成交比」和 money 的「换手健康度」
    # 只能拿中性分（两项各少 4 分）。晋级股最高只到 64，全部够不到
    # CONFIRM_MIN=68 —— 2026-11-26 池 26 只晋级股 confirmed=0 就是这么来的。
    # 验证前用腾讯批量补一次（1 次请求，零东财），只认验证当日行情。
    _attach_quotes(pool.get("items") or [], only_date=trade_date)
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

    # 验证日高度：晋级按晋级后的连板数，未晋级保留池子日高度。
    # 打分用这个口径，避免晋级股被旧高度压分（如 3→4 板仍按 3 板给 18 分）。
    def eff_lbc(it: dict) -> int:
        prev = int(_num(it.get("lbc")) or 0)
        now = int(_num((zt_now.get(it.get("code")) or {}).get("lbc")) or 0)
        return max(prev, now)

    # 只对打分 Top10 拉龙虎榜/资金流，控制东财请求量
    pre: list[tuple[int, dict, dict]] = []
    for it in pool.get("items") or []:
        s = score_one(it, zt_today=zt_now, theme_limits=theme_limits,
                      sector_rank=rank, effective_lbc=eff_lbc(it))
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
                flow = evidence.fund_flow(code, day=trade_date) or {}
            except Exception:
                flow = {}
            s = score_one(it, zt_today=zt_now, theme_limits=theme_limits,
                          sector_rank=rank, dragon=dragon, flow=flow,
                          effective_lbc=eff_lbc(it))
            total = s["total"]
        if sent["ice"]:
            status = "observe"
        elif advanced and total >= CONFIRM_MIN:
            status = "confirmed"
        elif advanced:
            # 晋级了但四支柱没过线：既不是确认龙，也不是「掉队退位」。
            # 两者混在一档时，26 只晋级股里 22 只被打成「退位」，用户会误以为
            # 它们当天走弱了。拆成独立一档，标签只说事实：晋级、未确认。
            status = "pending"
        else:
            status = "retired"
        items.append({
            "code": code, "name": it.get("name"),
            "prev_lbc": prev_lbc, "now_lbc": now_lbc,
            "advanced": advanced, "on_limit": on_limit,
            "status": status, "score": s["total"], "score_detail": s["parts"],
            # 逐项计分明细（每根柱由哪几个小项加出来），前端悬停直接显示公式
            "score_items": s.get("detail"),
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
              for k in ("confirmed", "pending", "retired", "observe")}
    # 监管异动按「验证日」的K线算：连板走到第几板触发异动，是次日的事
    try:
        from engines import regwatch
        regwatch.annotate(items, trade_date, verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"[擒龙·闸门二] 监管异动计算跳过：{e}")
    out = {
        "pool_date": prev_date,
        "verified_on": trade_date,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sentiment": sent,
        "confirm_min": CONFIRM_MIN,
        "rule": "晋级+四支柱≥68 且非冰点 → 确认龙；晋级但<68 → 晋级未确认；"
                "未晋级 → 退位龙保留观察；冰点 → 只观察",
        "counts": counts,
        "items": items,
    }
    _dump(_path("verdict", prev_date), out)
    if verbose:
        from engines import regwatch as _rw
        st = _rw.stats(items)
        print(f"[擒龙·闸门二] {prev_date}→{trade_date}: 确认 {counts['confirmed']} / "
              f"晋级未确认 {counts['pending']} / 退位 {counts['retired']} / "
              f"观察 {counts['observe']} · 情绪 {sent['regime']}"
              f" · 监管 严重{st['severe']}/异动{st['alert'] + st['warn']}/临界{st['near']}")
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


def _attach_quotes(items: list[dict], only_date: str | None = None) -> None:
    """用腾讯快照批量补现价/涨幅（一次请求，零东财）。

    验证文件落盘时没有存实时行情，历史回看时三档行的现价/涨幅会是「—」。
    这里在 API 汇总时按代码批量补一次；接口失败就保持原值，不影响主流程。

    only_date：只接受行情日期等于该交易日的快照。闸门二打分要用换手率/成交额，
    但它只对「验证当日」有意义——回测历史日时快照给的是今天的价，补进去会
    把过去的验证结论算错。日期不符就跳过，保持原值。
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
        if s.get("price") is None:
            continue
        if only_date and str(s.get("quote_date") or "")[:10] != only_date:
            continue
        q.update({k: s.get(k) for k in
                  ("price", "prev", "open", "high", "low", "chg",
                   "turnover", "amount", "amplitude", "vol_ratio", "quote_date")})
        it["quote"] = q


# ---------------------------------------------------------------- API 汇总
def lane_status(it: dict) -> str:
    """把落盘 status 归一到档位，兼容拆档前写的旧 verdict。

    旧版闸门二只有「晋级+≥68 / 其它」两道，晋级但分数不够的会被写成 retired，
    于是「晋级未确认」和「掉队退位」混在一档。读的时候按 advanced 自动纠正，
    不必等重跑验证：晋级 = 还没掉队，标签就不该叫退位。
    """
    st = it.get("status")
    if st == "retired" and it.get("advanced"):
        return "pending"
    return st


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
        return {"date": None, "confirmed": [], "pending": [], "retired": [],
                "observing": [], "pool": None, "intraday": None, "empty": True,
                "reg_stats": {},
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

    confirmed, pending, retired, observing = [], [], [], []
    for it in (verdict or {}).get("items") or []:
        st = lane_status(it)
        if st != it.get("status"):
            it = dict(it, status=st)   # 旧 verdict 的档位就地纠正，不改落盘文件
        if st == "confirmed":
            confirmed.append(it)
        elif st == "pending":
            pending.append(it)
        elif st == "observe":
            observing.append(it)
        else:
            retired.append(it)
    # 尚无验证结果时：整池进 observing（闸门一出池即观察，不当天提示）
    if not verdict:
        observing = [dict(i, status="observe", score=i.get("pre_score"),
                          score_detail=None, reasons=[])
                     for i in (pool.get("items") or [])]
    confirmed.sort(key=lambda x: -(x.get("score") or 0))
    pending.sort(key=lambda x: -(x.get("score") or 0))
    retired.sort(key=lambda x: -(x.get("score") or 0))
    _attach_quotes(confirmed + pending + retired + observing)
    # 监管异动：正常情况建池/验证时已落盘；旧文件（拆档前写的）现场用本地
    # 日K缓存补算一次，allow_fetch=False 保证接口不为几只缺缓存的票去联网。
    all_items = confirmed + pending + retired + observing
    if any(not it.get("reg") for it in all_items):
        try:
            from engines import regwatch
            regwatch.annotate(all_items, verdict.get("verified_on") if verdict else date,
                              allow_fetch=False)
        except Exception:
            pass
    try:
        from engines import regwatch as _rw
        reg_stats = _rw.stats(all_items)
    except Exception:
        reg_stats = {}
    return {
        "date": date,
        "sentiment": sent,
        "confirm_min": CONFIRM_MIN,
        "reg_stats": reg_stats,
        "counts": {"confirmed": len(confirmed), "pending": len(pending),
                   "retired": len(retired), "observing": len(observing)},
        "confirmed": confirmed,
        "pending": pending,
        "retired": retired,
        "observing": observing,
        "pool": {"date": pool.get("date"), "count": pool.get("count"),
                 "rule": pool.get("rule"), "source": pool.get("source")},
        "verified_on": verdict.get("verified_on") if verdict else None,
        "awaiting_verification": not bool(verdict),
        "latest_pool_date": latest_pool,
        "intraday": intraday.get("slots") or {},
        "intraday_date": intraday.get("trade_date"),
        "empty": not (confirmed or pending or retired or observing),
    }


# ---------------------------------------------------------------- 历史回溯 / 接力矩阵
def history_dates(n: int = 20) -> list[dict]:
    """列出可回溯的池子日（含已验证 + 最新待验证），供前端日期切换器。

    只读落盘文件，零网络。已验证的按 lane_status 重算四档计数（兼容旧 verdict），
    最新未验证的池标 awaiting_verification=true（点击后整池进「只观察」）。
    """
    out: list[dict] = []
    for d in dates("verdict", n):
        v = load_verdict(d) or {}
        # 档位计数按 lane_status 重算：旧 verdict 只有三档，直接把 counts 拿来用
        # 会把「晋级未确认」算进退位（日期按钮提示里数字对不上）。
        items = v.get("items") or []
        counts = {}
        for it in items:
            k = lane_status(it)
            k = "observing" if k == "observe" else k
            counts[k] = counts.get(k, 0) + 1
        out.append({
            "date": d,
            "verified_on": v.get("verified_on"),
            "counts": counts or (v.get("counts") or {}),
            "regime": (v.get("sentiment") or {}).get("regime"),
            "awaiting_verification": False,
        })
    for d in dates("pool", 1):
        if not load_verdict(d):
            out.append({"date": d, "verified_on": None, "counts": {},
                        "regime": None, "awaiting_verification": True})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:n]


def evolution(days: int = 10) -> dict:
    """跨日龙头接力矩阵 + 每日交接摘要。只读落盘 verdict，零网络。

    返回：
      dates: 正序的池子日列表（左旧右新）
      stocks: [{code,name,industry,confirmed_days,max_score,cells:{date:{status,score,now_lbc}}}]
      summary: 最近一个验证日的交接摘要（续任/反包/新晋/退位）
    只保留至少确认过一次（confirmed 或 rebound）的标的，按确认天数降序，
    避免把每天几十只退位杂毛堆进矩阵。
    """
    vdates = sorted(dates("verdict", days))
    if not vdates:
        return {"dates": [], "stocks": [], "summary": "", "empty": True}

    meta: dict[str, dict] = {}
    cells: dict[str, dict[str, dict]] = {}
    for d in vdates:
        for it in (load_verdict(d) or {}).get("items") or []:
            code = it.get("code")
            st = lane_status(it)
            if not code or st not in ("confirmed", "pending", "retired"):
                continue  # 冰点 observe 不进矩阵，避免噪音
            if code not in meta:
                meta[code] = {"code": code, "name": it.get("name") or code,
                              "industry": it.get("industry") or it.get("hybk") or "—"}
            cells.setdefault(code, {})[d] = {
                "status": st, "score": it.get("score"),
                "now_lbc": it.get("now_lbc"), "advanced": bool(it.get("advanced")),
            }

    # 反包标记：前一日 retired、当日 confirmed → rebound
    for i, d in enumerate(vdates):
        if i == 0:
            continue
        prev_d = vdates[i - 1]
        for code, m in cells.items():
            cur, prev = m.get(d), m.get(prev_d)
            if cur and cur["status"] == "confirmed" and prev and prev["status"] == "retired":
                cur["status"] = "rebound"

    rows = []
    for code, m in meta.items():
        cm = cells.get(code, {})
        n_conf = sum(1 for c in cm.values() if c["status"] in ("confirmed", "rebound"))
        if not n_conf:
            continue
        rows.append(dict(m, confirmed_days=n_conf,
                         max_score=max((c.get("score") or 0) for c in cm.values()),
                         cells=cm))
    rows.sort(key=lambda r: (-r["confirmed_days"], -r["max_score"], r["code"]))
    rows = rows[:20]

    # 交接摘要：比较最近两个验证日
    summary = ""
    if len(vdates) >= 2:
        last, prev = vdates[-1], vdates[-2]
        stay, rebound, fresh, pend, retired = [], [], [], [], []
        for r in rows:
            cur, before = r["cells"].get(last), r["cells"].get(prev)
            if cur and cur["status"] in ("confirmed", "rebound"):
                if before and before["status"] == "confirmed":
                    stay.append(f"{r['name']}({cur.get('now_lbc') or 0}板)续任")
                elif cur["status"] == "rebound":
                    rebound.append(f"{r['name']}反包回归")
                else:
                    fresh.append(f"{r['name']}新晋确认")
            elif cur and cur["status"] == "pending" and before and before["status"] in ("confirmed", "rebound"):
                # 还在板上但分数掉线：不是退位，标「晋级未确认」提醒复核
                pend.append(f"{r['name']}晋级未确认({cur.get('score') or 0}分)")
            elif cur and cur["status"] == "retired" and before and before["status"] in ("confirmed", "rebound"):
                retired.append(f"{r['name']}退位")
        parts = [x for x in [stay[0] if stay else "", "、".join(rebound[:3]),
                             "、".join(fresh[:3]), "、".join(pend[:3]),
                             "、".join(retired[:3])] if x]
        summary = f"{last} 交接：" + " · ".join(parts) if parts else f"{last} 交接：龙头格局未变"
    return {"dates": vdates, "stocks": rows, "summary": summary, "empty": not rows}


__all__ = ["build_pool", "verify_day", "intraday_check", "current",
           "history_dates", "evolution", "lane_status",
           "load_pool", "load_verdict", "load_intraday", "dates",
           "sentiment_state", "limit_price", "CONFIRM_MIN"]
