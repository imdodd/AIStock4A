"""个股基本面研究档案：证据包 → AI 生成 markdown → 前端只读渲染。

设计要点
--------
1. 离线生成、在线读取。页面点开股票只读落盘结果，绝不在请求里同步调模型。
2. 事实与解释分层：可核查数字（分部营收占比/毛利率/PE/EPS/目标价）只来自证据包，
   缺就写「未获取到」；产品场景与功能原理属产业常识，允许写但必须标注
   【常识性说明·非公司披露】，且不得夹带具体份额/客户名/金额。
3. 三档情景估值由本模块**先算好**再交给模型复述，避免模型自己编目标价：
   优先用多家机构研报目标价分布（悲观=最低/中性=中位/乐观=最高），
   机构样本不足时降级为「一致 EPS 分布 × 动态 PE」的平台测算并明确标注。
4. 重建触发：证据 hash 变化（财报期/分部数据/机构目标价）、重大公告指纹变化、
   30 天 TTL、手动强制。日常股价波动不重建。
5. 一份档案两处用：md 的「业绩依赖因素」章 + 结构化 dep.json，
   持仓跟踪 tracking.build_plan() 直接读 dep.json 生成跟踪维度。

目录：
    data/research/{code}.md            人读档案（六章 + 口径尾注）
    data/research/{code}.meta.json     版本 / hash / 来源 / 生成信息
    data/research/{code}.dep.json      业绩依赖因素结构化（跟踪方案复用）
    data/research/versions/{code}/     旧版归档（最多 3 份）
    data/research/_queue.json          当日生成计数（限流）
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import statistics
import threading
import time
from datetime import datetime, timedelta

import ai
import profile as prof

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(DATA_DIR, "research")
VER = os.path.join(DIR, "versions")
QUEUE = os.path.join(DIR, "_queue.json")

TTL_DAYS = 30
KEEP_VERSIONS = 3
DAILY_RESIDENT = 40           # 常驻上限：持仓全量 + 关注池 Top20 + 当日重建余量
DAILY_ONDEMAND = 15           # 其余 60 只在页面点开时按需生成，每日上限

SECTIONS = [
    ("business", "主营业务与产品原理"),
    ("valuation", "当前估值合理性与三档目标价"),
    ("growth", "业绩与成长性（营收占比 / 毛利率趋势）"),
    ("peers", "竞争对手分析"),
    ("risks", "主要风险"),
    ("dependencies", "业绩依赖因素"),
]
SECTION_TITLE = dict(SECTIONS)

_LOCK = threading.Lock()
_RUNNING: set[str] = set()
_JOBS: dict[str, dict] = {}


# ---------------------------------------------------------------- 证据包
def _num(x, nd=2):
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def _merge_targets(p: dict) -> list[dict]:
    """合并两个来源的机构目标价：reportapi 研报列表 + F10 ybzy 研报摘要"""
    bag: dict[tuple, dict] = {}
    for it in (p.get("reports") or {}).get("items") or []:
        t = it.get("target")
        if not t or t <= 0:
            continue
        org = (it.get("org") or "未署名机构").strip()
        row = dict(org=org, date=it.get("date") or "", price=_num(t),
                   rating=it.get("rating") or "", title=(it.get("title") or "")[:100],
                   source="东财研报")
        bag[(org, row["date"], row["price"])] = row
    for t in (p.get("consensus") or {}).get("targets") or []:
        row = dict(org=t.get("org"), date=t.get("date"), price=t.get("price"),
                   rating=t.get("rating") or "", title=t.get("title") or "",
                   source="F10 研报摘要")
        bag[(row["org"], row["date"], row["price"])] = row
    return sorted(bag.values(), key=lambda x: (x["date"] or "", x["price"] or 0))


def valuation_scenarios(p: dict) -> dict:
    """悲观 / 中性 / 乐观三档目标价。数值先算好，模型只负责复述与解释。

    主口径：按机构去重取最新一条目标价，分布的最低/中位/最高 = 悲观/中性/乐观。
    样本不足 2 家时：用多家机构 EPS 预测分布 × 当前动态 PE × 情景系数，
    明确标注为平台测算，不冒充机构目标价。
    """
    basic = p.get("basic") or {}
    price = basic.get("price")
    all_t = _merge_targets(p)
    latest_by_org: dict[str, dict] = {}
    for t in all_t:
        k = t["org"]
        if k not in latest_by_org or (t["date"] or "") > (latest_by_org[k]["date"] or ""):
            latest_by_org[k] = t
    org_rows = sorted(latest_by_org.values(), key=lambda x: x["price"] or 0)

    cons = p.get("consensus") or {}
    forecasts = [f for f in (cons.get("forecasts") or []) if (f.get("eps_y2") or 0) > 0]
    eps_vals = sorted(f["eps_y2"] for f in forecasts)
    avg = cons.get("consensus_eps") or {}
    eps_ref = avg.get("eps_y2") or (p.get("reports") or {}).get("eps_consensus")
    fwd_pe = _num(price / eps_ref, 1) if price and eps_ref and eps_ref > 0 else None

    scenarios = []
    if len(org_rows) >= 2:
        vals = [r["price"] for r in org_rows]
        low, mid, high = vals[0], _num(statistics.median(vals)), vals[-1]
        basis = f"{len(org_rows)} 家机构最新目标价（按机构去重，取最新研报）"
        for key, label, v, ref in (("bear", "悲观", low, org_rows[0]),
                                   ("base", "中性", mid, None),
                                   ("bull", "乐观", high, org_rows[-1])):
            scenarios.append(dict(
                key=key, label=label, price=v,
                space_pct=_num((v / price - 1) * 100, 1) if price and v else None,
                org=ref["org"] if ref else "机构目标价中位数",
                org_date=ref["date"] if ref else "",
                implied_pe=_num(v / eps_ref, 1) if v and eps_ref and eps_ref > 0 else None,
                note=("取给出最低目标价的机构观点" if key == "bear" else
                      "取给出最高目标价的机构观点" if key == "bull" else
                      "取机构目标价中位数，代表主流预期")))
        method = "机构目标价分布"
    else:
        if eps_vals and fwd_pe:
            q = statistics.quantiles(eps_vals, n=4) if len(eps_vals) >= 4 else None
            eps_low = _num(q[0], 3) if q else eps_vals[0]
            eps_mid = _num(statistics.median(eps_vals), 3)
            eps_high = _num(q[2], 3) if q else eps_vals[-1]
            for key, label, eps, k, note in (
                    ("bear", "悲观", eps_low, 0.9, "机构 EPS 预测下四分位 × 动态 PE × 0.9（估值收缩）"),
                    ("base", "中性", eps_mid, 1.0, "机构 EPS 预测中位数 × 当前动态 PE"),
                    ("bull", "乐观", eps_high, 1.1, "机构 EPS 预测上四分位 × 动态 PE × 1.1（估值扩张）")):
                v = _num(eps * fwd_pe * k)
                scenarios.append(dict(key=key, label=label, price=v,
                                      space_pct=_num((v / price - 1) * 100, 1) if price and v else None,
                                      org="平台测算（非机构目标价）", org_date="",
                                      implied_pe=_num(fwd_pe * k, 1), note=note))
            basis = (f"仅 {len(org_rows)} 家机构给出目标价，样本不足；"
                     f"改用 {len(eps_vals)} 家机构次年 EPS 预测 × 动态 PE {fwd_pe} 倍测算")
            method = "平台测算（EPS 分布）"
        else:
            # 第三级降级：机构目标价与盈利预测都没有时，用同行业真实 PE 分位
            # × 本股 EPS(TTM) 给一个相对估值参考区间，仍然明确标注为平台测算。
            pe_vals = sorted(x["pe_ttm"] for x in (p.get("peers") or {}).get("items") or []
                             if (x.get("pe_ttm") or 0) > 0 and (x.get("pe_ttm") or 0) < 150)
            pe_now = basic.get("pe_ttm")
            eps_ttm = _num(price / pe_now, 3) if price and pe_now and pe_now > 0 else None
            if len(pe_vals) >= 2 and eps_ttm:
                q = statistics.quantiles(pe_vals, n=4) if len(pe_vals) >= 4 else None
                lo = q[0] if q else pe_vals[0]
                mid = statistics.median(pe_vals)
                hi = q[2] if q else pe_vals[-1]
                for key, label, pe in (("bear", "悲观", lo), ("base", "中性", mid),
                                       ("bull", "乐观", hi)):
                    v = _num(eps_ttm * pe)
                    scenarios.append(dict(
                        key=key, label=label, price=v,
                        space_pct=_num((v / price - 1) * 100, 1) if v and price else None,
                        org="平台测算（同业 PE 分位，非机构目标价）", org_date="",
                        implied_pe=_num(pe, 1),
                        note=f"本股 EPS(TTM) {eps_ttm} 元 × 同业 PE {label}分位 {_num(pe, 1)} 倍"))
                method = "平台测算（同业 PE 分位）"
                basis = (f"未获取到机构目标价与盈利预测；改用 {len(pe_vals)} 家同行业公司 PE 分位 "
                         f"× 本股 EPS(TTM) {eps_ttm} 元测算。同业业务未必完全可比，仅作相对估值参考")
            else:
                # 第四级降级：亏损股（PE 为负 / EPS 为负）盈利法失效，
                # 改用同业 PB 分位 × 每股净资产，仍是纯真实数据的相对估值。
                bps = _num(((p.get("finance") or {}).get("latest") or {}).get("bps"), 3)
                if not bps and price and (basic.get("pb") or 0) > 0:
                    bps = _num(price / basic["pb"], 3)
                pb_now = basic.get("pb") or 0
                pb_cap = min(10, pb_now * 3) if pb_now > 0 else 10
                pb_vals = sorted(x["pb"] for x in (p.get("peers") or {}).get("items") or []
                                 if 0 < (x.get("pb") or 0) < pb_cap)
                if len(pb_vals) >= 2 and bps and bps > 0:
                    q = statistics.quantiles(pb_vals, n=4) if len(pb_vals) >= 4 else None
                    for key, label, pb in (("bear", "悲观", q[0] if q else pb_vals[0]),
                                           ("base", "中性", statistics.median(pb_vals)),
                                           ("bull", "乐观", q[2] if q else pb_vals[-1])):
                        v = _num(bps * pb)
                        scenarios.append(dict(
                            key=key, label=label, price=v,
                            space_pct=_num((v / price - 1) * 100, 1) if v and price else None,
                            org="平台测算（同业 PB 分位，非机构目标价）", org_date="",
                            implied_pe=None,
                            note=f"每股净资产 {bps} 元 × 同业 PB {label}分位 {_num(pb, 2)} 倍"))
                    method = "平台测算（同业 PB 分位）"
                    basis = (f"本股 PE(TTM) 为负且无机构盈利预测，盈利法不适用；"
                             f"改用 {len(pb_vals)} 家同行业公司 PB 分位 × 每股净资产 {bps} 元测算。"
                             "这是资产端相对估值，不反映成长性，也不代表机构观点")
                else:
                    method = "数据不足"
                    basis = "机构目标价、盈利预测、同业正 PE 与 PB 样本均不足，无法给出三档目标价"

    # 平台测算才封顶：机构原文目标价必须原样保留，不能替机构改数。
    if method.startswith("平台测算") and price:
        for s in scenarios:
            sp = s.get("space_pct")
            if sp is not None and abs(sp) > 100:
                lim = 100 if sp > 0 else -100
                s["price"] = _num(price * (1 + lim / 100))
                s["space_pct"] = lim
                s["note"] = (s.get("note") or "") + "；平台按相对现价 ±100% 封顶，避免同业极端值外推"

    return dict(
        price=price, as_of=basic.get("time") or "",
        method=method, basis=basis,
        target_count=len(org_rows), target_rows=org_rows[:12],
        all_targets=all_t[:20], forecast_count=len(forecasts),
        eps_ref=_num(eps_ref, 3), forward_pe=fwd_pe,
        pe_ttm=basic.get("pe_ttm"), pb=basic.get("pb"),
        dividend_yield=basic.get("dividend_yield"), pos52=basic.get("pos52"),
        eps_consensus=(p.get("reports") or {}).get("eps_consensus"),
        rating_main=(p.get("reports") or {}).get("rating_main"),
        scenarios=scenarios,
        caveat=("目标价为多家投研机构公开观点汇总或平台情景测算，代表假设下的价格区间，"
                "不是对未来价格的承诺，也不是本平台的买卖建议；研报观点可能滞后或被后续修正。"),
    )


def evidence(code: str, refresh: bool = False) -> dict:
    """给模型看的结构化证据包：裁掉大段原文，只留可核查事实。"""
    p = prof.profile(code, refresh=refresh)
    b = p.get("basic") or {}
    f = p.get("finance") or {}
    biz = p.get("business") or {}
    comp = p.get("company") or {}
    rep = p.get("reports") or {}
    cons = p.get("consensus") or {}
    nt = p.get("notices") or {}
    peers = p.get("peers") or {}

    periods = []
    for x in (biz.get("periods") or [])[:6]:
        periods.append(dict(report_date=x.get("report_date"),
                            product=(x.get("product") or [])[:6],
                            region=(x.get("region") or [])[:4]))
    fin_periods = []
    for x in (f.get("periods") or [])[:6]:
        fin_periods.append({k: x.get(k) for k in
                            ("report_date", "period", "revenue", "revenue_yoy",
                             "profit", "profit_yoy", "margin", "roe", "eps", "ocf_ps")})
    ev = dict(
        code=code, name=p.get("name") or code,
        fetched_at=p.get("fetched_at"),
        basic={k: b.get(k) for k in ("price", "chg_pct", "pe_ttm", "pb",
                                     "total_mktcap", "float_mktcap", "dividend_yield",
                                     "high52", "low52", "pos52", "turnover", "time")},
        finance=dict(periods=fin_periods, revenue_trend=f.get("revenue_trend"),
                     profit_trend=f.get("profit_trend")),
        business=dict(scope=(comp.get("business_scope") or biz.get("scope") or "")[:900],
                      review=(biz.get("review") or "")[:1600],
                      review_date=biz.get("review_date"),
                      periods=periods, latest_report=biz.get("latest_report"),
                      concentration=biz.get("concentration")),
        company={k: comp.get(k) for k in ("org_name", "em2016", "industrycsrc1",
                                         "listing_date", "province")},
        company_profile=(comp.get("org_profile") or "")[:1200],
        reports=dict(total=rep.get("total"), rating_dist=rep.get("rating_dist"),
                     rating_main=rep.get("rating_main"), eps_consensus=rep.get("eps_consensus"),
                     target_avg=rep.get("target_avg"), target_n=rep.get("target_n"),
                     items=[{k: x.get(k) for k in ("org", "date", "rating", "target",
                                                   "eps_this_year", "title")}
                            for x in (rep.get("items") or [])[:8]]),
        consensus=dict(targets=(cons.get("targets") or [])[:12],
                       target_n=cons.get("target_n"),
                       forecasts=[{k: x.get(k) for k in ("org", "date", "eps_y2", "eps_y3")}
                                  for x in (cons.get("forecasts") or [])[:12]],
                       consensus_eps=cons.get("consensus_eps"), note=cons.get("note")),
        peers=dict(sector=peers.get("sector"), items=(peers.get("items") or [])[:5]),
        notices=dict(major=[x for x in (nt.get("items") or []) if x.get("major")][:6],
                     major_hash=nt.get("major_hash"), latest_date=nt.get("latest_date")),
        errors=p.get("errors") or {},
    )
    ev["valuation"] = valuation_scenarios(p)
    return ev


def _stable_hash(ev: dict) -> str:
    """只由结构性事实决定，日内行情波动不会触发重写。"""
    payload = {
        "latest_report": ev["business"].get("latest_report"),
        "periods": ev["business"].get("periods"),
        "finance_periods": ev["finance"].get("periods"),
        "company_profile": ev.get("company_profile"),
        "targets": ev["consensus"].get("targets"),
        "eps": ev["consensus"].get("consensus_eps"),
        "reports": ev["reports"].get("items"),
        "peers": [x.get("code") for x in (ev["peers"].get("items") or [])],
        "major_hash": ev["notices"].get("major_hash"),
    }
    return hashlib.md5(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                  default=str).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 状态 / 落盘
def _safe(code: str) -> str:
    """code 会拼进档案文件名且可能来自 HTTP 请求，拒绝 ..、斜杠等穿越输入。"""
    from data_paths import safe_slug
    return safe_slug(code, "股票代码")


def _meta_path(code: str) -> str:
    return os.path.join(DIR, f"{_safe(code)}.meta.json")


def _md_path(code: str) -> str:
    return os.path.join(DIR, f"{_safe(code)}.md")


def _dep_path(code: str) -> str:
    return os.path.join(DIR, f"{_safe(code)}.dep.json")


def _atomic(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _load_meta(code: str) -> dict:
    try:
        return json.load(open(_meta_path(code), encoding="utf-8"))
    except Exception:
        return {}


def needs_refresh(code: str, ev: dict | None = None) -> tuple[bool, str]:
    """返回 (是否需要重建, 原因)。ev 为 None 时只读 meta，不联网。"""
    meta = _load_meta(code)
    if not os.path.exists(_md_path(code)) or not meta:
        return True, "尚无档案"
    h = _stable_hash(ev) if ev else None
    if h and meta.get("hash") != h:
        return True, "财报 / 机构目标价 / 重大公告有更新"
    try:
        t = datetime.fromisoformat(meta.get("generated_at") or "")
        if datetime.now() - t > timedelta(days=TTL_DAYS):
            return True, "超过 30 天未刷新"
    except Exception:
        return True, "生成时间缺失"
    return False, ""


def existing_codes() -> list[str]:
    return sorted(f[:-3] for f in (os.listdir(DIR) if os.path.isdir(DIR) else [])
                  if f.endswith(".md") and not f.startswith("_"))


def major_changed(code: str) -> bool:
    """只打一次 F10 公告接口，判断重大公告指纹是否变化（EOD 每日巡检用）。"""
    meta = _load_meta(code)
    old = meta.get("major_hash")
    if not old:
        return False
    try:
        cur = prof.notices(code).get("major_hash")
    except Exception:
        return False
    return bool(cur and cur != old)


def status() -> dict:
    """档案总览：数量、当日生成、常驻/按需限额、正在生成的代码。"""
    mds = [f[:-3] for f in (os.listdir(DIR) if os.path.isdir(DIR) else [])
           if f.endswith(".md") and not f.startswith("_")]
    try:
        q = json.load(open(QUEUE, encoding="utf-8"))
    except Exception:
        q = {}
    today = datetime.now().strftime("%Y-%m-%d")
    if q.get("date") != today:
        q = {"date": today, "resident": 0, "ondemand": 0}
    return dict(total=len(mds), date=today,
                resident=q.get("resident", 0), ondemand=q.get("ondemand", 0),
                limits=dict(resident=DAILY_RESIDENT, ondemand=DAILY_ONDEMAND),
                running=sorted(_RUNNING), jobs=_JOBS)


def _bump(kind: str):
    with _LOCK:
        try:
            q = json.load(open(QUEUE, encoding="utf-8"))
        except Exception:
            q = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if q.get("date") != today:
            q = {"date": today, "resident": 0, "ondemand": 0}
        q[kind] = q.get(kind, 0) + 1
        _atomic(QUEUE, json.dumps(q, ensure_ascii=False, indent=1))


def _quota(kind: str) -> bool:
    s = status()
    return s.get(kind, 0) < s["limits"][kind]


# ---------------------------------------------------------------- Markdown → HTML
def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', s)
    return s


def md_to_html(md: str) -> tuple[str, list[dict]]:
    """轻量 markdown 渲染。先转义再解析，禁止注入；不支持内嵌 HTML。"""
    sections: list[dict] = []
    cur_key, cur_title, buf = None, "", []

    def flush():
        if cur_key is None:
            return
        sections.append(dict(key=cur_key, title=cur_title, html=_render_lines(buf)))

    for line in md.splitlines():
        m = re.match(r"^##\s+(.+)$", line.strip())
        if m:
            flush()
            title = m.group(1).strip()
            cur_title = title
            cur_key = next((k for k, t in SECTIONS if t in title or title in t), None)
            if not cur_key:
                cur_key = "extra_" + hashlib.md5(title.encode()).hexdigest()[:6]
            buf = []
            continue
        if cur_key is None:          # 头部前言（标题/口径）单独丢弃，前端有卡片头
            continue
        buf.append(line.rstrip())
    flush()
    return "\n".join(s["html"] for s in sections), sections


def _render_lines(lines: list[str]) -> str:
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        s = ln.strip()
        if not s:
            i += 1
            continue
        if s.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            head = [c.strip() for c in s.strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{_inline(x)}</th>" for x in head)
            tb = "".join("<tr>" + "".join(f"<td>{_inline(x)}</td>" for x in r) + "</tr>"
                         for r in rows)
            out.append(f'<div class="md-table"><table><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table></div>')
            continue
        if s.startswith("### "):
            out.append(f"<h4>{_inline(s[4:])}</h4>")
        elif s.startswith("> "):
            out.append(f"<blockquote>{_inline(s[2:])}</blockquote>")
        elif re.match(r"^[-*]\s+", s):
            items = []
            while i < n and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append("<li>" + _inline(re.sub(r"^[-*]\s+", "", lines[i].strip())) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        elif re.match(r"^\d+[.)]\s+", s):
            items = []
            while i < n and re.match(r"^\d+[.)]\s+", lines[i].strip()):
                items.append("<li>" + _inline(re.sub(r"^\d+[.)]\s+", "", lines[i].strip())) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        elif s.startswith("---"):
            out.append("<hr>")
        elif s.startswith("【") or s.startswith("*"):
            out.append(f'<p class="md-note">{_inline(s.strip("*"))}</p>')
        else:
            out.append(f"<p>{_inline(s)}</p>")
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------- 提示词
SYSTEM = """你是 A 股基本面研究助理，任务是按固定六章写一份个股研究档案。用户是活跃投资者，要的是能直接用来做功课的事实与逻辑，不要营销腔。

硬性规则（违反即失败）：
1. 【可核查事实】数字、报告期、机构名、目标价、产品名、公告标题，只能来自【证据包】和【三档情景估值】。证据包没有的，写「未获取到」，绝不用常识、记忆或推测补齐数字。
2. 【常识性说明】产品使用场景、功能原理这类产业通识允许写，但必须在该段开头标注【常识性说明·非公司披露】，并且这一段里不准出现具体市占率、客户名、金额、份额等数字。
3. 三档目标价必须逐字使用【三档情景估值】给出的悲观/中性/乐观价格与空间，写清楚对应机构（或平台测算标注）、研报日期、隐含 PE 和假设；不准自己另算一个目标价。样本不足时必须照抄 scenarios 里的 org 字段（例如「平台测算（同业 PB 分位，非机构目标价）」）与 basis 口径。
4. 严格区分事实与推断：推断写成「倾向于……，因为……」，并说明证据强度。
5. 中文，大白话，句子短。每章 250~450 字，能用表格就用表格。禁止「综上所述」「值得注意的是」这类空话。
6. 不写「建议买入/卖出/加仓/减仓/持有」，不写平台自己的目标价。目标价是机构观点汇总，行文要说明这一点。

输出格式：只输出 markdown 正文，六个二级标题必须按下面顺序、逐字一致，不要加一级标题，不要写结尾免责声明（平台会自动追加）：

## 主营业务与产品原理
## 当前估值合理性与三档目标价
## 业绩与成长性（营收占比 / 毛利率趋势）
## 竞争对手分析
## 主要风险
## 业绩依赖因素

各章要求：
- 主营业务：先写公司披露的经营范围/主营构成（引用证据包原文要点），再用【常识性说明·非公司披露】段落讲主要产品用在什么场景、靠什么原理实现功能；产品名必须来自证据包。
- 估值：先给现价、PE(TTM)、PB、股息率、52 周位置、机构一致 EPS 与评级分布；再给三档目标价表格（悲观/中性/乐观、价格、相对现价空间、对应机构、隐含 PE、核心假设）；最后用一段话说清「当前估值相对增速是否合理」，只能基于证据包里的增速与目标价分布判断。
- 业绩与成长性：用表格列最近 3~6 期分产品营收占比与分部毛利率，指出占比变化方向和毛利率升降；再结合整体营收/净利同比、ROE 说成长性。没有分部数据的期次写「未获取到」。
- 竞争对手：用证据包同业表列 3~5 家的 PE/PB/市值做可比估值；定性部分只能说业务定位差异，不准编市占率、产能、客户。
- 主要风险：至少覆盖产品/业务集中度（用证据包占比数字）、毛利率下滑、政策与公告事件（引用公告标题和日期）、估值回撤四类；每条写清「经由哪个变量传导到业绩」。
- 业绩依赖因素：按「终端需求 → 客户资本开支/订单 → 出货量×单价 → 营收结构 → 毛利率 → 净利 → 估值」链条，挑 3~5 个对这家公司最关键的依赖变量；每个变量写清可观测代理指标、领先业绩多久、以及「什么情况说明这条逻辑破了」的证伪条件。"""


def _user_prompt(ev: dict) -> str:
    val = ev["valuation"]
    block = {
        "code": ev["code"], "name": ev["name"], "fetched_at": ev["fetched_at"],
        "basic": ev["basic"], "finance": ev["finance"], "business": ev["business"],
        "company": ev["company"], "company_profile": ev["company_profile"],
        "reports": ev["reports"], "consensus": ev["consensus"],
        "peers": ev["peers"], "notices": ev["notices"], "errors": ev["errors"],
    }
    return ("【证据包】\n" + json.dumps(block, ensure_ascii=False, separators=(",", ":"))
            + "\n\n【三档情景估值 · 必须逐字引用】\n"
            + json.dumps(val, ensure_ascii=False, separators=(",", ":"))
            + "\n\n请严格按六个二级标题输出研究档案。")


DEP_SYSTEM = """你是业绩归因助手。基于【证据包】输出严格的 JSON 对象，不要 markdown、不要解释文字。

格式：
{"chain":"一句话描述这家公司从终端需求到利润的传导链",
 "factors":[{"name":"依赖变量","link":"处于链条哪一环","proxy":"可观测代理指标","source":"数据源","cadence":"daily|weekly|monthly|quarterly|event","lead":"领先业绩多久","threshold":{"g":"绿档条件","y":"黄档条件","r":"红档条件"},"invalidation":"证伪条件：什么情况说明逻辑破了","basis":"依据的证据字段"}]}

规则：
1. 3~5 个因素，必须是这家公司真实业务的关键变量，不准套通用模板。
2. proxy 要可量化、可定期获取；threshold 要用证据包里的真实数值锚定，没有数值时写「需人工确认」，不准编数字。
3. cadence 按数据发布节奏选：行情/资金=daily，产业价格/订单=weekly，排产/月度数据=monthly，财报分部数据=quarterly，公告/政策=event。
4. 只输出 JSON。"""


# ---------------------------------------------------------------- 降级 / 依赖兜底
def _fallback_md(ev: dict) -> str:
    """模型不可用时的结构化降级版：只有真实数字，没有 AI 叙述，六章齐全。"""
    v = ev["valuation"]
    biz, fin = ev["business"], ev["finance"]
    rows = ["| 情景 | 价格 | 空间 | 对应机构 | 隐含 PE | 说明 |",
            "|---|---|---|---|---|---|"]
    for s in v["scenarios"]:
        rows.append(f"| {s['label']} | {s['price']} | {s['space_pct']}% | {s['org']} | "
                    f"{s['implied_pe']} | {s['note']} |")
    p = biz.get("periods") or []
    prows = ["| 报告期 | 产品 | 营收占比 | 分部毛利率 |", "|---|---|---|---|"]
    for x in p[:4]:
        for it in (x.get("product") or [])[:3]:
            prows.append(f"| {x['report_date']} | {it['name']} | {it['ratio_pct']}% | "
                         f"{it['gross_margin_pct']}% |")
    peers = ["| 公司 | PE(TTM) | PB | 总市值(亿) |", "|---|---|---|---|"]
    for x in (ev["peers"].get("items") or []):
        peers.append(f"| {x['name']} | {x['pe_ttm']} | {x['pb']} | {x['total_mktcap']} |")
    return "\n\n".join([
        "## 主营业务与产品原理",
        (ev["business"].get("scope") or "未获取到") + "\n\n未获取到（模型通道不可用，本档案为数据摘要版，常识性说明未生成）。",
        "## 当前估值合理性与三档目标价",
        f"现价 {v['price']} · PE(TTM) {v['pe_ttm']} · PB {v['pb']}。口径：{v['basis']}",
        "\n".join(rows) if v["scenarios"] else "未获取到机构目标价与盈利预测，无法给出三档目标价。",
        "## 业绩与成长性（营收占比 / 毛利率趋势）",
        "\n".join(prows) if len(prows) > 2 else "未获取到分产品营收占比数据。",
        "\n".join(f"- {x.get('period')}：营收 {x.get('revenue')} 亿（同比 {x.get('revenue_yoy')}%），"
                 f"整体毛利率 {x.get('margin')}%，ROE {x.get('roe')}%"
                 for x in (fin.get("periods") or [])[:4]),
        "## 竞争对手分析",
        "\n".join(peers) if len(peers) > 2 else "未获取到同行业可比公司数据。",
        "## 主要风险",
        "\n".join(["- 业务集中度：第一大产品营收占比 "
                   f"{biz.get('concentration')}%，单一产品波动会直接放大业绩弹性。",
                   "- 毛利率：分部毛利率变化见上表，持续下行会压缩净利。",
                   "- 公告事件：" + "；".join(x["title"][:40] for x in
                                       (ev["notices"].get("major") or [])[:3]),
                   "- 估值回撤：三档情景价差见估值表，估值中枢下移会放大股价波动。"]),
        "## 业绩依赖因素",
        "未获取到（模型通道不可用）。跟踪方案将使用行业通用维度，建议稍后重新生成。",
    ])


def _fallback_deps(ev: dict) -> dict:
    biz = ev["business"]
    ps = biz.get("periods") or []
    top = ((ps[0].get("product") or [{}])[0] if ps else {})
    return dict(
        chain="模型通道不可用，以下为按披露结构生成的通用依赖链，需人工复核",
        factors=[dict(
            name=f"{top.get('name') or '主营'}收入规模", link="营收结构",
            proxy="分产品营收占比与分部毛利率", source="东财 F10 主营构成",
            cadence="quarterly", lead="与财报同步",
            threshold=dict(g="占比或毛利率改善", y="持平", r="占比与毛利率同时下滑"),
            invalidation="连续两个报告期占比下滑且毛利率走低",
            basis=f"最新一期第一大产品占比 {biz.get('concentration')}%")])


# ---------------------------------------------------------------- 生成
def _call_llm(cfg: dict, system: str, user: str, temperature: float = 0.3,
              timeout: int = 300) -> str:
    """研究档案是长文本，不用 ai.py 的流式通道（其 120s 超时对六章输出偏紧）。

    直接走 OpenAI 兼容 /chat/completions 非流式，超时 5 分钟；密钥只从本地
    data/.secrets/llm.json 读取，不写入任何产物、不下发到前端。
    """
    import urllib.request
    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False, "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read().decode("utf-8", "ignore"))
    return (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


def _archive(code: str):
    old = _md_path(code)
    if not os.path.exists(old):
        return
    d = os.path.join(VER, _safe(code))
    os.makedirs(d, exist_ok=True)
    day = (_load_meta(code).get("generated_at") or datetime.now().isoformat())[:10]
    _atomic(os.path.join(d, f"{day}.md"), open(old, encoding="utf-8").read())
    files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    for f in files[:-KEEP_VERSIONS]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass


FOOTER = """

---

**数据口径与免责**：本档案的可核查事实来自东方财富 F10（主营构成 / 公司简况 / 机构预测 / 研报 / 公告）、腾讯行情快照与本地行业成分股快照，数据截止日见文件头。三档目标价为多家投研机构公开观点汇总，或机构样本不足时用一致 EPS / 同业 PE / 同业 PB 分位做的平台情景测算，代表假设下的价格区间，不是对未来价格的承诺。带【常识性说明·非公司披露】标记的内容为产业通识，不是公司披露信息。本档案仅供研究参考，**不构成买卖建议**。
"""


def generate(code: str, force: bool = False, kind: str = "ondemand",
             skip_llm: bool = False) -> dict:
    """生成 / 重建一只股票的研究档案。返回摘要 dict。

    kind=resident 由 EOD 批量调用；kind=ondemand 由页面点开缺失档案时调用。
    """
    if code in _RUNNING:
        return {"code": code, "skipped": "已在生成中"}
    with _LOCK:
        _RUNNING.add(code)
        _JOBS[code] = {"state": "running", "started_at": datetime.now().isoformat(timespec="seconds")}
    try:
        ev = evidence(code, refresh=True)
        need, why = needs_refresh(code, ev)
        if not force and not need:
            _JOBS[code] = {"state": "done", "reason": "档案为最新，跳过"}
            return {"code": code, "skipped": why or "档案为最新"}
        if kind in ("resident", "ondemand") and not _quota(kind):
            _JOBS[code] = {"state": "done", "reason": f"{kind} 日配额已满"}
            return {"code": code, "skipped": f"{kind} 日配额已满，明天再试或手动执行批量任务"}
        if kind == "maintenance" and not os.path.exists(_md_path(code)):
            _JOBS[code] = {"state": "done", "reason": "维护模式不新建档案"}
            return {"code": code, "skipped": "维护模式不新建档案"}

        cfg = ai.config()
        mode, model, err = "fallback", None, None
        text = ""
        if cfg and not skip_llm:
            try:
                text = _call_llm(cfg, SYSTEM, _user_prompt(ev))
                if not text or text.count("## ") < 6:
                    raise ValueError(f"章节不完整（{text.count('## ')} 个二级标题）")
                mode, model = "llm", cfg.get("model")
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        if not cfg and not skip_llm:
            err = "未配置模型通道（data/.secrets/llm.json）"
        if mode == "fallback":
            text = _fallback_md(ev)

        # 六章标题对齐（模型偶尔改写标题，统一成契约标题）
        for key, title in SECTIONS:
            pattern = re.compile(r"^##\s+.*" + re.escape(title[:4]) + r".*$", re.M)
            text = pattern.sub("## " + title, text, count=1)
        missing = [t for _, t in SECTIONS if ("## " + t) not in text]
        for t in missing:
            text += f"\n\n## {t}\n\n未获取到（生成结果缺该章节，请重新生成）。"

        header = (f"# {ev['name']}（{code}）研究档案\n\n"
                  f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"- 数据截止：{ev['fetched_at']} · 最新财报期：{ev['business'].get('latest_report') or '未获取到'}\n"
                  f"- 生成方式：{mode}{'（' + model + '）' if model else ''} · 三档口径：{ev['valuation']['method']}\n"
                  f"- 证据版本：{_stable_hash(ev)}\n\n")
        _archive(code)
        _atomic(_md_path(code), header + text.strip() + FOOTER)

        # 依赖因素结构化：独立短调用，失败则用规则兜底
        dep = None
        if mode == "llm" and cfg:
            try:
                raw = _call_llm(cfg, DEP_SYSTEM,
                                "【证据包】\n" + json.dumps(
                                    {"business": ev["business"], "finance": ev["finance"],
                                     "consensus": ev["consensus"], "peers": ev["peers"],
                                     "valuation": {k: ev["valuation"][k] for k in
                                                   ("method", "basis", "scenarios")}},
                                    ensure_ascii=False, separators=(",", ":")),
                                temperature=0.2)
                raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
                dep = json.loads(raw)
                if not isinstance(dep, dict) or not dep.get("factors"):
                    raise ValueError("factors 为空")
            except Exception as e:
                dep = None
                _JOBS[code]["dep_error"] = f"{type(e).__name__}: {e}"
        dep = dep or _fallback_deps(ev)
        dep = dict(dep, code=code, name=ev["name"],
                   generated_at=datetime.now().isoformat(timespec="seconds"),
                   valuation_method=ev["valuation"]["method"])
        _atomic(_dep_path(code), json.dumps(dep, ensure_ascii=False, indent=1))

        meta = dict(code=code, name=ev["name"], mode=mode, model=model, error=err,
                    hash=_stable_hash(ev),
                    major_hash=ev["notices"].get("major_hash"),
                    latest_report=ev["business"].get("latest_report"),
                    valuation=ev["valuation"],
                    generated_at=datetime.now().isoformat(timespec="seconds"),
                    fetched_at=ev["fetched_at"],
                    sections=[t for _, t in SECTIONS],
                    sources=["东财 F10 主营构成 / 公司简况 / 机构预测 / 公告",
                             "东财研报列表", "腾讯行情快照", "本地行业成分股快照"])
        _atomic(_meta_path(code), json.dumps(meta, ensure_ascii=False, indent=1))
        _bump(kind)
        _JOBS[code] = {"state": "done", "mode": mode,
                       "at": meta["generated_at"], "error": err}
        return dict(code=code, mode=mode, model=model, error=err,
                    sections=len(SECTIONS), valuation=ev["valuation"]["method"],
                    generated_at=meta["generated_at"], md=_md_path(code))
    except Exception as e:
        _JOBS[code] = {"state": "error", "error": f"{type(e).__name__}: {e}"}
        return {"code": code, "error": f"{type(e).__name__}: {e}"}
    finally:
        with _LOCK:
            _RUNNING.discard(code)


def read(code: str) -> dict:
    """页面读取契约：不联网、不生成，只读落盘结果。"""
    meta = _load_meta(code)
    if not os.path.exists(_md_path(code)):
        return dict(code=code, exists=False, hint="尚未生成研究档案")
    md = open(_md_path(code), encoding="utf-8").read()
    disclaimer = ""
    if "**数据口径与免责**" in md:
        md, disclaimer = md.split("**数据口径与免责**", 1)
        disclaimer = disclaimer.strip().lstrip("：:").strip("*- \n")
    _, sections = md_to_html(md)
    stale = False
    try:
        stale = datetime.now() - datetime.fromisoformat(meta.get("generated_at") or "") > timedelta(days=TTL_DAYS)
    except Exception:
        stale = True
    dep = {}
    try:
        dep = json.load(open(_dep_path(code), encoding="utf-8"))
    except Exception:
        pass
    return dict(code=code, exists=True, name=meta.get("name") or code,
                generated_at=meta.get("generated_at"), fetched_at=meta.get("fetched_at"),
                mode=meta.get("mode"), model=meta.get("model"), stale=stale,
                latest_report=meta.get("latest_report"),
                valuation=meta.get("valuation") or {},
                sections=[s for s in sections if s["key"] in SECTION_TITLE],
                dependencies=dep, sources=meta.get("sources") or [],
                disclaimer=html.escape(disclaimer),
                job=_JOBS.get(code) or {},
                chars=len(md))


def generate_async(code: str, force: bool = False, kind: str = "ondemand") -> dict:
    """页面触发的按需生成：后台线程执行，前端轮询 /api/research/status。"""
    if code in _RUNNING:
        return {"queued": False, "state": "running"}
    if not force:
        m = _load_meta(code)
        if m and os.path.exists(_md_path(code)):
            return {"queued": False, "state": "exists", "hint": "档案已存在，可点强制刷新"}
    t = threading.Thread(target=generate, args=(code, force, kind), daemon=True,
                         name=f"research-{code}")
    t.start()
    return {"queued": True, "state": "queued"}


def dependencies(code: str) -> dict:
    try:
        return json.load(open(_dep_path(code), encoding="utf-8"))
    except Exception:
        return {}


if __name__ == "__main__":
    import sys
    c = sys.argv[1] if len(sys.argv) > 1 else "sz002371"
    r = generate(c, force="--force" in sys.argv, kind="resident")
    print(json.dumps({k: v for k, v in r.items() if k != "md"},
                     ensure_ascii=False, indent=1))
