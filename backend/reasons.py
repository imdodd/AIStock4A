"""规则归因引擎：不依赖模型，先把「为什么异动」讲成有证据的因果链

设计原则：
  1. 每条归因必须挂具体数字，不允许出现「受利好消息刺激」这类没有出处的套话；
  2. 同时输出反证（为什么不是某个原因），避免单边叙事；
  3. 权重决定排序，权重来自证据强度而非措辞力度；
  4. 模型（若配置）只负责把这些事实讲顺，不允许新增事实。

输出结构：
    headline  一句话结论
    causes    归因列表（type / weight / text / evidence[]）
    risks     风险点
    watch     明日观察点
    facts     关键事实清单（供模型引用与前端折叠展示）
    counters  反证
    confidence  high | medium | low（取决于证据源覆盖度）
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
SENTI = os.path.join(DATA_DIR, "raw", "sentiment.json")

NOTICE_KEYS = ("预增", "预盈", "扭亏", "中标", "签订", "合同", "收购", "重组", "增持",
               "回购", "定增", "合作", "战略", "投产", "扩产", "获批", "专利", "涨价",
               "立案", "减持", "问询", "关注函", "停牌", "复牌", "股权激励")
RISK_NOTICE_KEYS = ("减持", "立案", "问询", "关注函", "亏损", "预亏", "诉讼", "质押")


def _n(v, unit="", digits=2, sign=False):
    if v is None:
        return "—"
    s = f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"
    return s + unit


def _yi(v):
    """元 → 亿元文本"""
    if v is None:
        return "—"
    return f"{v / 1e8:.2f} 亿"


def _wan(v):
    if v is None:
        return "—"
    return f"{v / 1e4:.0f} 万"


def market_env(day: str) -> dict:
    try:
        d = json.load(open(SENTI, encoding="utf-8"))
        return d if d.get("date") == day else {}
    except Exception:
        return {}


# ------------------------------------------------------------------ 各项规则
def _limit_up_text(c: dict) -> str | None:
    zt = c.get("limit_up") or {}
    if not zt:
        return None
    parts = []
    if zt.get("lbc") and zt["lbc"] > 1:
        parts.append(f"{zt['lbc']} 连板")
    elif zt.get("ct") and zt.get("days"):
        parts.append(f"{zt['days']} 天 {zt['ct']} 板")
    if zt.get("fbt"):
        parts.append(f"首封 {zt['fbt']}")
    if zt.get("zbc"):
        parts.append(f"日内炸板 {zt['zbc']} 次（末次回封 {zt.get('lbt') or '—'}）")
    else:
        parts.append("全天未炸板")
    if zt.get("fund"):
        parts.append(f"封单 {_yi(zt['fund'])}")
    return "，".join(parts)


def _r_sector(c: dict) -> dict | None:
    sec = c.get("sector") or {}
    total = c.get("peer_total") or 0
    limit = c.get("peer_limit") or 0
    chg = sec.get("chg")
    if not sec:
        return None
    peers = c.get("peers") or []
    top = "、".join(f"{p['name']}({_n(p.get('chg'), '%', 1, True)})" for p in peers[:3])
    if total >= 3 and (limit >= 1 or (chg or 0) >= 2):
        w = 0.55 + min(0.2, total * 0.02) + min(0.15, limit * 0.05)
        return {
            "type": "板块共振", "weight": round(w, 2),
            "text": (f"{sec.get('name')} 今日整体 {_n(chg, '%', 2, True)}，"
                     f"同行业另有 {total - 1} 只进入异动池"
                     + (f"、{limit} 只涨停" if limit else "")
                     + "，属于板块层面的资金整体进攻，个股是跟随而非独立行情。"),
            "evidence": [f"板块涨幅 {_n(chg, '%', 2, True)}（{sec.get('name')}，阶段 S{sec.get('stage')}）",
                         f"同行业异动 {total} 只 / 涨停 {limit} 只"]
                        + ([f"同板块领涨：{top}"] if top else []),
        }
    if total <= 1 and abs(chg or 0) < 1.5:
        return {
            "type": "非板块驱动", "weight": 0.35, "kind": "counter",
            "text": (f"{sec.get('name')} 板块今日仅 {_n(chg, '%', 2, True)}，"
                     f"同行业没有其它个股进入异动池，说明这是个股自身的事情，不是行业行情。"),
            "evidence": [f"板块涨幅 {_n(chg, '%', 2, True)}", "同行业异动家数 1 只（仅本股）"],
        }
    if (chg or 0) >= 2:
        return {
            "type": "板块跟涨", "weight": 0.4,
            "text": f"{sec.get('name')} 今日 {_n(chg, '%', 2, True)}，板块有整体表现，但个股为独立放量。",
            "evidence": [f"板块涨幅 {_n(chg, '%', 2, True)}"],
        }
    return None


def _r_notice(c: dict) -> dict | None:
    notices = c.get("notices") or []
    if not notices:
        return None
    day = c.get("date") or ""
    # 只认最近 5 个自然日内、且带实质关键词的公告；「异常波动公告」是结果不是原因
    substance, echo = [], []
    for a in notices:
        if not a.get("date"):
            continue
        gap = (datetime.strptime(day, "%Y-%m-%d") -
               datetime.strptime(a["date"], "%Y-%m-%d")).days if day else 99
        if gap < 0 or gap > 5:
            continue
        if "异常波动" in a["title"]:
            echo.append(a)
        elif any(k in a["title"] for k in NOTICE_KEYS):
            substance.append(a)
    if substance:
        w = 0.6 + min(0.25, len(substance) * 0.08)
        bear = [a for a in substance
                if any(k in a["title"] for k in ("立案", "问询", "关注函", "减持", "预亏", "诉讼"))]
        kind_txt = "利空" if bear else "催化"
        chg = (c.get("quote") or {}).get("chg") or 0
        tone = ""
        if bear and chg < 0:
            tone = "，与当日下跌方向一致，构成直接的负面驱动"
        elif bear and chg > 0:
            tone = "，但股价当日仍上涨，说明市场未按利空定价，需留意后续发酵"
        return {
            "type": f"公告{kind_txt}", "weight": round(w, 2),
            "text": f"近 5 个交易日内有实质公告落地{tone}：" +
                    "；".join(a["title"].split(":", 1)[-1] for a in substance[:3]),
            "evidence": [f"{a['date']} {a['title']}" for a in substance[:3]],
        }
    if echo:
        return {
            "type": "异动公告（结果而非原因）", "weight": 0.25, "kind": "counter",
            "text": (f"公司在 {echo[0]['date']} 发布过股票交易异常波动公告，"
                     "这是股价已经异动后的被动披露，不构成上涨原因。"),
            "evidence": [f"{a['date']} {a['title']}" for a in echo[:2]],
        }
    return None


def _r_flow(c: dict) -> dict | None:
    f = c.get("flow") or {}
    d = c.get("dragon") or {}
    out = []
    if f.get("main_net") is not None:
        main, sup, big = f.get("main_net"), f.get("super_net"), f.get("big_net")
        if main > 0 and (sup or 0) > 0 and (big or 0) < 0:
            out.append({
                "type": "资金分歧", "weight": 0.62,
                "text": (f"主力合计净流入 {_yi(main)}，但结构是超大单 +{_yi(sup)}、"
                         f"大单 {_yi(big)}，说明大资金内部对倒分歧明显，"
                         "不是单边一致的加仓。"),
                "evidence": [f"主力净额 {_yi(main)}", f"超大单 {_yi(sup)}", f"大单 {_yi(big)}",
                             f"小单 {_yi(f.get('small_net'))} / 中单 {_yi(f.get('mid_net'))}"],
            })
        elif main > 0:
            out.append({
                "type": "资金流入", "weight": 0.55,
                "text": f"当日主力净流入 {_yi(main)}，近 5 日累计 {_yi(f.get('main_net_5d'))}。",
                "evidence": [f"主力净额 {_yi(main)}", f"5 日累计 {_yi(f.get('main_net_5d'))}"],
            })
        elif main < 0 and (c.get("quote") or {}).get("chg", 0) and (c["quote"]["chg"] or 0) > 0:
            out.append({
                "type": "价涨资金流出", "weight": 0.6, "kind": "counter",
                "text": (f"股价上涨但主力净流出 {_yi(abs(main))}，"
                         "说明拉升靠的是散户与中小单接力，筹码在向散户转移。"),
                "evidence": [f"主力净额 {_yi(main)}", f"中单 {_yi(f.get('mid_net'))}"],
            })
    if d.get("on_board"):
        out.append({
            "type": "龙虎榜席位", "weight": 0.5,
            "text": (f"登上龙虎榜（{d.get('reason') or '—'}），榜单买入 {_yi(d.get('buy_amt'))}、"
                     f"卖出 {_yi(d.get('sell_amt'))}，净额 {_yi(d.get('net_buy'))}；"
                     f"东财席位解读：「{d.get('explain') or '无'}」。"),
            "evidence": [f"买入 {_yi(d.get('buy_amt'))} / 卖出 {_yi(d.get('sell_amt'))}",
                         f"榜单成交 {_yi(d.get('deal_amt'))}",
                         f"席位解读：{d.get('explain') or '—'}"],
        })
    if not out:
        return None
    out.sort(key=lambda x: -x["weight"])
    return out[0]


def _r_tech(c: dict) -> dict | None:
    h = c.get("hist") or {}
    t = c.get("tech") or {}
    q = c.get("quote") or {}
    chg = q.get("chg") or 0
    ev, txt = [], []
    if h.get("is_high20") and chg > 0:
        txt.append("收盘创 20 日新高")
        ev.append("收盘价 ≥ 前 19 日最高收盘")
    if h.get("limit_up_20d"):
        txt.append(f"近 20 日已有 {h['limit_up_20d']} 个涨停")
        ev.append(f"20 日涨停次数 {h['limit_up_20d']}")
    if h.get("ret5") is not None and abs(h["ret5"]) >= 15:
        txt.append(f"5 日累计 {_n(h['ret5'], '%', 1, True)}")
        ev.append(f"5 日涨幅 {_n(h['ret5'], '%', 2, True)} / 20 日 {_n(h.get('ret20'), '%', 2, True)}")
    if h.get("vol_vs_ma5") is not None and chg >= 9 and h["vol_vs_ma5"] < 1.3:
        txt.append(f"成交量为前 5 日均量 {h['vol_vs_ma5']:.2f} 倍，属缩量涨停（惜售）")
        ev.append(f"量能倍数 {h['vol_vs_ma5']:.2f}x（缩量）")
    elif h.get("vol_vs_ma5") is not None:
        txt.append(f"成交量为前 5 日均量 {h['vol_vs_ma5']:.2f} 倍")
        ev.append(f"量能倍数 {h['vol_vs_ma5']:.2f}x")
    if h.get("streak_up", 0) >= 3:
        txt.append(f"已连续 {h['streak_up']} 日收阳")
        ev.append(f"连涨 {h['streak_up']} 日")
    if not txt:
        return None
    w = 0.45 + (0.1 if h.get("is_high20") else 0) + (0.1 if (t.get("stage") == 2) else 0)
    return {
        "type": "技术形态", "weight": round(min(0.68, w), 2),
        "text": "；".join(txt) + f"。当前 {t.get('sub') or '—'}，RS60 {_n(t.get('rs60'), '', 1)}，"
                                f"距 52 周高 {_n(t.get('dist52'), '%', 1)}。",
        "evidence": ev + [f"阶段 {t.get('sub') or '—'} / RS60 {_n(t.get('rs60'), '', 1)}",
                          f"距 52 周高 {_n(t.get('dist52'), '%', 1)}"],
    }


def _r_game(c: dict) -> dict | None:
    """高换手 + 大幅拉升 + 席位双向放量 → 资金博弈型行情（妖股/游资特征）"""
    q, f, d, zt = c.get("quote") or {}, c.get("flow") or {}, c.get("dragon") or {}, c.get("limit_up") or {}
    turnover, chg = q.get("turnover") or 0, q.get("chg") or 0
    if turnover < 20 or (chg < 7 and not zt):
        return None
    net = f.get("main_net")
    two_way = bool(d.get("on_board")) and (d.get("buy_amt") or 0) > 0 and (d.get("sell_amt") or 0) > 0
    divergent = net is not None and (f.get("super_net") or 0) * (f.get("big_net") or 0) < 0
    if not (two_way or divergent or (d.get("net_buy") or 0) < 0):
        return None
    ev = [f"换手率 {_n(turnover, '%', 1)}", f"涨幅 {_n(chg, '%', 2, True)}"]
    if two_way:
        ev.append(f"龙虎榜买 {_yi(d.get('buy_amt'))} / 卖 {_yi(d.get('sell_amt'))}"
                  f"，净额 {_yi(d.get('net_buy'))}")
    if divergent:
        ev.append(f"超大单 {_yi(f.get('super_net'))} vs 大单 {_yi(f.get('big_net'))}")
    txt = (f"换手 {turnover:.1f}%、单日 {_n(chg, '%', 2, True)}，"
           + ("登上龙虎榜且买卖双向巨额成交，" if two_way else "")
           + ("主力内部超大单与大单方向相反，" if divergent else "")
           + "筹码在日内大规模易手，属于资金博弈驱动的行情，"
             "不是基本面重估，走势取决于次日承接而非估值。")
    return {"type": "资金博弈", "weight": 0.8, "text": txt, "evidence": ev}


def _r_intraday(c: dict) -> dict | None:
    it = c.get("intraday") or {}
    if not it or it.get("points", 0) < 30:
        return None
    peak_t, peak_c = it.get("peak_time"), it.get("peak_chg")
    close_c, open_c = it.get("close_chg"), it.get("open_chg")
    vol60, vol30 = it.get("vol_first60"), it.get("vol_last30")
    ev = [f"开盘 {_n(open_c, '%', 2, True)} / 最高 {peak_t} {_n(peak_c, '%', 2, True)}"
          f" / 收盘 {_n(close_c, '%', 2, True)}"]
    if vol60 is not None:
        ev.append(f"前 1 小时成交占全天 {vol60:.0f}%")
    txt = None
    if peak_t and peak_t <= "10:30" and close_c is not None and peak_c is not None \
            and peak_c - close_c >= 2.5:
        txt = f"早盘 {peak_t} 冲到 {_n(peak_c, '%', 2, True)} 后回落，收盘 {_n(close_c, '%', 2, True)}，是典型的早盘一波流后承接不足。"
    elif peak_t and peak_t <= "10:30" and close_c is not None and abs(peak_c - close_c) < 0.6:
        zt = c.get("limit_up") or {}
        txt = (f"早盘 {peak_t} 即拉到 {_n(peak_c, '%', 2, True)} 并维持到收盘，全天在高位横盘。"
               if not zt.get("zbc") else
               f"早盘 {peak_t} 即拉到 {_n(peak_c, '%', 2, True)}，但盘中炸板 {zt['zbc']} 次"
               f"（末次回封 {zt.get('lbt') or '—'}），封板并不牢固。")
    elif peak_t and peak_t >= "14:00" and close_c is not None and peak_c - open_c > 3:
        txt = f"主升在午后，{peak_t} 才打出 {_n(peak_c, '%', 2, True)} 的高点，属尾盘突袭型。"
    elif (it.get("open_chg") or 0) <= -2 and (close_c or 0) >= 3:
        txt = f"低开 {_n(open_c, '%', 2, True)} 后一路走高到 {_n(close_c, '%', 2, True)}，日内完成反转。"
    if not txt:
        return None
    return {"type": "分时节奏", "weight": 0.42, "text": txt, "evidence": ev}


def _r_rebound(c: dict) -> dict | None:
    t, h, q = c.get("tech") or {}, c.get("hist") or {}, c.get("quote") or {}
    if (t.get("dist52") or 0) > -40 or (q.get("chg") or 0) < 3:
        return None
    if t.get("stage") != 4:
        return None
    return {
        "type": "超跌反弹", "weight": 0.5,
        "text": (f"股价距 52 周高仍有 {_n(t.get('dist52'), '%', 1)}，处于 S4 下跌阶段，"
                 f"今日 {_n(q.get('chg'), '%', 2, True)} 更接近超跌后的技术性反弹，而非趋势反转。"
                 + (f"前一日 {_n(h.get('prev_chg'), '%', 2, True)}。" if h.get("prev_chg") is not None else "")),
        "evidence": [f"距 52 周高 {_n(t.get('dist52'), '%', 1)}", "阶段 S4",
                     f"20 日区间涨跌 {_n(h.get('min_chg20'), '%', 1)} ~ {_n(h.get('max_chg20'), '%', 1)}"],
    }


def _r_distribution(c: dict) -> dict | None:
    q, f, it = c.get("quote") or {}, c.get("flow") or {}, c.get("intraday") or {}
    chg = q.get("chg") or 0
    if chg > -3:
        return None
    tags = [f"跌幅 {_n(chg, '%', 2, True)}", f"量比 {_n(q.get('vol_ratio'), '', 2)}",
            f"换手 {_n(q.get('turnover'), '%', 1)}"]
    txt = f"放量下跌 {_n(abs(chg), '%', 2)}，量比 {_n(q.get('vol_ratio'), '', 2)}，属资金主动出逃。"
    if (f.get("main_net") or 0) < 0:
        txt += f"主力净流出 {_yi(abs(f['main_net']))}，印证抛压来自大资金。"
        tags.append(f"主力净流出 {_yi(abs(f['main_net']))}")
    if it.get("peak_time") and it["peak_time"] <= "10:30":
        txt += f"分时上 {it['peak_time']} 见高点后单边下行，无有效反抽。"
    return {"type": "放量派发", "weight": 0.75, "text": txt, "evidence": tags}


def _r_market(c: dict, mk: dict) -> dict | None:
    if not mk:
        return None
    ratio = mk.get("updown_ratio")
    up_ratio = mk.get("up_ratio")
    chg = (c.get("quote") or {}).get("chg") or 0
    if up_ratio is None:
        return None
    if up_ratio >= 70 and chg > 0:
        return {
            "type": "市场环境", "weight": 0.3, "kind": "context",
            "text": (f"当日全市场 {_n(up_ratio, '%', 1)} 个股上涨、涨停 {mk.get('limit_up')} 家、"
                     f"跌停 {mk.get('limit_down')} 家（涨跌比 {_n(ratio, '', 2)}），"
                     "普涨环境本身就容易放大个股涨幅，需要扣除这层 beta。"),
            "evidence": [f"上涨 {mk.get('up')} / 下跌 {mk.get('down')}",
                         f"涨停 {mk.get('limit_up')} / 跌停 {mk.get('limit_down')}"],
        }
    if up_ratio <= 30 and chg > 3:
        return {
            "type": "逆势独立", "weight": 0.55,
            "text": (f"全市场仅 {_n(up_ratio, '%', 1)} 个股上涨（涨停 {mk.get('limit_up')} 家），"
                     "个股逆势走强，独立性较强。"),
            "evidence": [f"上涨占比 {_n(up_ratio, '%', 1)}", f"涨停 {mk.get('limit_up')}"],
        }
    return None


# ------------------------------------------------------------------ 汇总
def attribute(case: dict) -> dict:
    mk = market_env(case.get("date") or "")
    cands = [x for x in (
        _r_distribution(case),
        _r_sector(case),
        _r_notice(case),
        _r_game(case),
        _r_flow(case),
        _r_tech(case),
        _r_intraday(case),
        _r_rebound(case),
        _r_market(case, mk),
    ) if x]

    causes = sorted([x for x in cands if x.get("kind") not in ("counter", "context")],
                    key=lambda x: -x["weight"])
    counters = [x for x in cands if x.get("kind") == "counter"]
    contexts = [x for x in cands if x.get("kind") == "context"]

    q = case.get("quote") or {}
    t = case.get("tech") or {}
    name, chg = case.get("name"), q.get("chg") or 0
    direction = "上涨" if chg > 0 else "下跌"
    lead = causes[0]["type"] if causes else "暂无明确主导因素"

    # 一句话结论：方向 + 幅度 + 主因 + 一句限定
    quiet = abs(chg) < 2 and (q.get("vol_ratio") or 0) < 1.5
    bits = [f"{name} 今日{direction} {abs(chg):.2f}%"]
    if q.get("price"):
        bits.append(f"收 {q['price']:.2f} 元")
    if q.get("vol_ratio"):
        bits.append(f"量比 {q['vol_ratio']:.2f}")
    if q.get("turnover"):
        bits.append(f"换手 {q['turnover']:.1f}%")
    if quiet:
        head = ("，".join(bits) + f"，量价上不构成异动；当前 {t.get('sub') or '—'}，"
                "以下按位置与结构给参考，不是异动归因。")
    else:
        head = "，".join(bits) + f"；主导因素是{lead}。"
        if t.get("sub"):
            head += f"当前 {t['sub']}。"

    risks = []
    if "ST" in (case.get("name") or "").upper():
        risks.append("ST 标的，退市与流动性风险显著高于普通股。")
    if (q.get("turnover") or 0) >= 25:
        risks.append(f"换手 {q['turnover']:.1f}%，筹码高度不稳定，次日承接不确定性大。")
    if (case.get("limit_up") or {}).get("zbc"):
        risks.append(f"日内炸板 {(case['limit_up'])['zbc']} 次，封板意愿不坚决。")
    if (case.get("dragon") or {}).get("on_board") and ((case["dragon"].get("net_buy") or 0) < 0):
        risks.append(f"龙虎榜净卖出 {_yi(abs(case['dragon']['net_buy']))}，上榜资金整体在减仓。")
    if (case.get("tech") or {}).get("stage") == 4:
        risks.append("仍处 S4 下跌阶段，反弹不等于反转。")
    if (case.get("hist") or {}).get("ret5") and abs(case["hist"]["ret5"]) >= 25:
        risks.append(f"5 日已累计 {_n(case['hist']['ret5'], '%', 1, True)}，短期涨幅透支。")
    for a in (case.get("notices") or [])[:6]:
        if any(k in a["title"] for k in RISK_NOTICE_KEYS):
            risks.append(f"{a['date']} 公告：{a['title'].split(':', 1)[-1]}")
            break

    watch = []
    zt = case.get("limit_up") or {}
    if zt:
        watch.append(f"次日能否站稳今日封板价，以及封单是否继续放大（今日封单 {_yi(zt.get('fund'))}）。")
    if (q.get("vol_ratio") or 0) >= 2:
        watch.append("量能能否维持：缩量上涨可持有逻辑，放量滞涨需警惕派发。")
    if (case.get("sector") or {}).get("chg") is not None:
        watch.append(f"{(case['sector'] or {}).get('name')} 板块能否跟上，个股独立行情通常难持续。")
    pivot = (case.get("tech") or {}).get("pivot")
    if pivot:
        watch.append(f"关键点 {pivot}，跌破则视为假突破（参考止损 {(case.get('tech') or {}).get('stop') or '—'}）。")

    status = case.get("source_status") or {}
    got = sum(1 for v in status.values() if v)
    confidence = "high" if got >= 6 else ("medium" if got >= 4 else "low")

    facts = [
        f"盘口：{_n(q.get('chg'), '%', 2, True)}，量比 {_n(q.get('vol_ratio'), '', 2)}，"
        f"换手 {_n(q.get('turnover'), '%', 1)}，振幅 {_n(q.get('amplitude'), '%', 1)}，"
        f"成交额 {_n((q.get('amount') or 0) / 1e4, ' 亿', 2)}",
    ]
    lt = _limit_up_text(case)
    if lt:
        facts.append(f"涨停结构：{lt}")
    if case.get("hist"):
        h = case["hist"]
        facts.append(f"位置：5 日 {_n(h.get('ret5'), '%', 1, True)} / 20 日 {_n(h.get('ret20'), '%', 1, True)}，"
                     f"20 日内涨停 {h.get('limit_up_20d')} 次"
                     + ("，创 20 日新高" if h.get("is_high20") else ""))
    if case.get("sector"):
        facts.append(f"板块：{(case['sector'] or {}).get('name')} "
                     f"{_n((case['sector'] or {}).get('chg'), '%', 2, True)}，"
                     f"同行业异动 {case.get('peer_total')} 只 / 涨停 {case.get('peer_limit')} 只")

    return {
        "headline": head,
        "causes": causes[:4],
        "counters": counters,
        "contexts": contexts,
        "risks": risks[:4],
        "watch": watch[:3],
        "facts": facts,
        "confidence": confidence,
        "sources": {k: bool(v) for k, v in status.items()},
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "rule",
    }


def brief(case: dict) -> dict:
    return attribute(case)


if __name__ == "__main__":
    import sys
    from datasource import evidence as ev
    code = sys.argv[1] if len(sys.argv) > 1 else "sz000995"
    c = ev.build_case(code)
    print(json.dumps(brief(c), ensure_ascii=False, indent=1))
