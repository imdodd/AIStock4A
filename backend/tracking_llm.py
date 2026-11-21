"""跟踪期的 LLM 产出层：分层取证 → 调配置的 LLM → 契约校验 → 合并成一期。

为什么分三层，而不是把 13 个维度塞进一个 prompt 一次问完：

1. 方案里的维度数据节奏本来就不同——日频看量能、季频看财报、行业政策要联网。
   混在一起时模型会把「上一季的营收同比」当成「本期变化」，这是最容易出的错。
2. 上下文越长越容易漏维度，实测一次问 13 个维度会稳定丢 2~3 个。
3. 分层后每层的证据同源，判断依据能对上号，出错时也定位得到是哪一层。

三层与角度的对应关系见 ``LAYERS``。没有列入任何一层的角度会落到 quant 层兜底，
宁可多问一次，也不要让维度静默消失。

本模块只做「取证 → 问 → 校验 → 合并」，落盘由 ``jobs.tracking_review`` 负责，
这样手工补跑和定时跑走的是同一条代码路径。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

import ai
import profile
import tracking
from datasource import evidence as ev

# 层 id / 中文名 / 覆盖的维度角度（angle）
#
# 分层依据不是「周期长短」，而是**这一层的数字从哪来**：
# quant 全是本地行情接口能给的（价格/形态/资金流），fundamental 是财报与估值锚，
# search 与 dep 都要联网或查公告才能判。同一层里证据同源，模型才不容易张冠李戴。
LAYERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("quant", "行情·资金·量能", ("capital", "market")),
    ("fundamental", "业绩与估值锚", ("earnings", "valuation")),
    ("search", "行业·政策·事件", ("industry", "policy", "disclosure")),
    ("dep", "业绩依赖链", ("dependency",)),
)

STATUS_MAP = {
    "g": "g", "绿": "g", "达标": "g", "改善": "g", "good": "g",
    "y": "y", "黄": "y", "中性": "y", "留意": "y", "warn": "y",
    "r": "r", "红": "r", "恶化": "r", "触发": "r", "bad": "r",
    "n": "n", "灰": "n", "无内容": "n", "无增量": "n", "none": "n",
}

SYSTEM = """你是 A 股持仓跟踪助手，负责按固定维度给一只股票做本期的状态判断。

硬性规则（违反即整份结果作废）：
1. 只能用【本期证据】里出现的事实。证据里没有的信息，status 一律给 "n"，value 写「本期无增量」，
   并在 note 里说明缺什么。绝不用常识、记忆或推测补数字。
2. status 只能是 g / y / r / n 四个之一，必须严格按【判断阈值】裁决，不许自己调阈值。
3. delta 写与上期相比的方向和幅度，格式「↑ +1.2pct」「↓ -8.10pct」「→ 持平」；没有上期数据就留空字符串。
4. note 说清依据，带上关键数字与它的来源字段名，例如「主力净流出 0.38 亿（flow.main_net）」。
5. 不给买卖建议：不出现买入/卖出/加仓/减仓/目标价/止损位这类表述。
6. 中文，句子短，不写「综上所述」。
7. 只输出一个 JSON 对象，不要 Markdown 代码块，不要任何解释文字。结构固定：
   {"verdict":"偏多|中性|偏空","summary":"一句话说清本期最重要的变化",
    "values":[{"key":"维度key","value":"本期取值","status":"g|y|r|n","delta":"↑/↓/→ 幅度","note":"依据"}],
    "highlights":["本期最值得注意的两三件事"],
    "sources":[{"title":"来源标题","date":"YYYY-MM-DD","url":"链接",
                "quote":"原文摘录，含你引用的那个数字，不超过 120 字"}]}
   values 必须覆盖给你的每一个维度 key，一个都不能少。
   sources 只在本期用过联网检索时才需要；每条都必须带 quote 原文摘录，
   平台会拿 quote 核对你 note 里每个数字有没有出处，没摘录的数字会被标黄。"""


# ---------------------------------------------------------------- 分层
def layer_of(dim: dict) -> str:
    ang = dim.get("angle") or ""
    for lid, _label, angles in LAYERS:
        if ang in angles:
            return lid
    return "quant"


def split_layers(plan: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {lid: [] for lid, _l, _a in LAYERS}
    for d in plan.get("dimensions") or []:
        if d.get("key"):
            out[layer_of(d)].append(d)
    return out


def layer_label(lid: str) -> str:
    for l, label, _a in LAYERS:
        if l == lid:
            return label
    return lid


# ---------------------------------------------------------------- 取证
_CASE_CACHE: dict = {}


def _case(code: str, day: str | None = None) -> dict:
    """证据包一次取齐、多层复用：build_case 要连打几个接口，
    每层都重取一遍既慢又容易把自己打到限流。"""
    key = (code, day or "")
    if key not in _CASE_CACHE:
        _CASE_CACHE[key] = ev.build_case(code, day) if day else ev.build_case(code)
    return _CASE_CACHE[key]


def _num(v, nd: int = 2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd)


def _yi(v):
    """元 → 亿元，资金流数字用得上"""
    n = _num(v, 0)
    return None if n is None else round(n / 1e8, 2)


def evidence_quant(code: str, day: str | None = None) -> dict:
    """行情 / 技术形态 / 资金流 / 估值锚 —— 全部本地可得，不走检索。"""
    case = _case(code, day)
    q = case.get("quote") or {}
    t = case.get("tech") or {}
    f = case.get("flow") or {}
    out = {
        "日期": case.get("date"),
        "现价": q.get("price"), "昨收": q.get("prev"), "涨跌幅%": q.get("chg"),
        "量比": q.get("vol_ratio"), "换手率%": q.get("turnover"), "振幅%": q.get("amplitude"),
        "阶段": t.get("sub") or (f"Stage {t.get('stage')}" if t.get("stage") else None),
        "RS60": t.get("rs60"), "距52周高%": t.get("dist52"),
        "关键点pivot": t.get("pivot"), "动态止损": t.get("stop"),
        "最近信号": t.get("sig"),
        "主力净额_亿": _yi(f.get("main_net")),
        "超大单净额_亿": _yi(f.get("super_net")),
        "主力净额_5日_亿": _yi(f.get("main_net_5d")),
    }
    try:
        b = profile.basic(code)
        out.update({
            "52周位置%": b.get("pos52"),
            "52周高": b.get("high52"), "52周低": b.get("low52"),
        })
    except Exception as e:                                    # 52 周区间取不到不影响趋势判断
        out["区间取数失败"] = f"{type(e).__name__}"
    return {k: v for k, v in out.items() if v is not None}


def evidence_fundamental(code: str) -> dict:
    """财报 + 一致预期。注意 profile.finance 的 revenue/profit 单位已经是亿元，
    再除 1e8 会变成 0.0——模型会很老实地报「疑为缺失」，然后这一层全判无内容。"""
    out: dict = {}
    try:
        fin = profile.finance(code, limit=8)
        latest = fin.get("latest") if isinstance(fin.get("latest"), dict) else {}
        if latest:
            out.update({
                "最新报告期": latest.get("period"),
                "营收_亿": _num(latest.get("revenue")),
                "净利_亿": _num(latest.get("profit")),
                "扣非_亿": _num(latest.get("deducted")),
                "营收同比%": _num(latest.get("revenue_yoy")),
                "净利同比%": _num(latest.get("profit_yoy")),
                "ROE%": _num(latest.get("roe")),
                "EPS": _num(latest.get("eps"), 4),
                "每股净资产": _num(latest.get("bps"), 4),
                "毛利率%": _num(latest.get("margin")),
                "每股经营现金流": _num(latest.get("ocf_ps"), 4),
            })
        out["营收趋势"] = fin.get("revenue_trend")
        out["净利趋势"] = fin.get("profit_trend")
        rows = []
        for r in (fin.get("periods") or [])[1:5]:      # 最近几期，供模型看趋势而不是单点
            if isinstance(r, dict) and r.get("period"):
                rows.append({"期": r.get("period"), "营收_亿": _num(r.get("revenue")),
                             "营收同比%": _num(r.get("revenue_yoy")),
                             "净利_亿": _num(r.get("profit")),
                             "净利同比%": _num(r.get("profit_yoy"))})
        if rows:
            out["前几期"] = rows
    except Exception as e:
        out["财报取数失败"] = f"{type(e).__name__}"
    try:
        b = profile.basic(code)
        out.update({"PE_TTM": _num(b.get("pe_ttm")), "PB": _num(b.get("pb")),
                    "股息率%": _num(b.get("dividend_yield")),
                    "52周位置%": _num(b.get("pos52")),
                    "总市值_亿": _num(b.get("total_mktcap"))})
    except Exception as e:
        out["估值取数失败"] = f"{type(e).__name__}"
    try:
        c = profile.consensus(code)
        out.update({"研报数": c.get("target_n"),
                    "一致目标均价": _num(c.get("target_avg")),
                    "一致EPS": _num(c.get("consensus_eps"), 4)})
    except Exception as e:
        out["研报取数失败"] = f"{type(e).__name__}"
    return {k: v for k, v in out.items() if v is not None}


def evidence_search(code: str, day: str | None = None) -> dict:
    """行业与政策类维度靠检索：本地已有公告/资讯作为「已知事实」，
    缺口由模型自己用 deepseek_web_search 补，并要求把来源写进 note。"""
    case = _case(code, day)
    notices = [{"日期": n.get("date"), "标题": n.get("title")}
               for n in (case.get("notices") or [])][:8]
    news = [{"日期": n.get("date"), "标题": n.get("title")}
            for n in (case.get("news") or [])][:8]
    sector = case.get("sector") or {}
    return {
        "行业": sector.get("name") or sector.get("industry") or None,
        "最新公告": notices,
        "最新资讯": news,
        "检索截止日": case.get("date"),
    }


def build_evidence(plan: dict, lid: str, day: str | None) -> dict:
    code = plan["code"]
    if lid == "fundamental":
        return evidence_fundamental(code)
    if lid == "search":
        return evidence_search(code, day)
    return evidence_quant(code, day)


# ---------------------------------------------------------------- 提问
def _dim_block(dims: list[dict]) -> str:
    lines = []
    for d in dims:
        th = d.get("thresholds") or {}
        lines.append(
            f"- key={d['key']}｜{d.get('name')}｜指标：{d.get('metric')}\n"
            f"  阈值 绿={th.get('g')}；黄={th.get('y')}；红={th.get('r')}\n"
            f"  为什么这么判：{d.get('why')}"
            + (f"\n  证伪条件：{d.get('invalidation')}" if d.get("invalidation") else "")
        )
    return "\n".join(lines)


def _prev_block(prev: dict | None, dims: list[dict]) -> str:
    if not prev:
        return "（没有上一期数据，delta 留空）"
    keys = {d["key"] for d in dims}
    rows = [v for v in (prev.get("values") or []) if v.get("key") in keys]
    if not rows:
        return "（上一期没有这些维度的记录，delta 留空）"
    return "\n".join(
        f"- {v.get('key')}：{v.get('value')}（{v.get('status')}）" for v in rows)


def build_messages(plan: dict, lid: str, dims: list[dict],
                   evidence: dict, prev: dict | None) -> list[dict]:
    name, code = plan.get("name"), plan["code"]
    date = datetime.now().strftime("%Y-%m-%d")
    head = (f"标的：{name}（{code}）｜行业：{plan.get('sector')}"
            f"（{plan.get('sub') or '阶段未知'}）｜本期日期：{date}\n"
            f"本层主题：{layer_label(lid)}\n"
            f"跟踪口径：{len(plan.get('dimensions') or [])} 个维度固定，本层只判 {len(dims)} 个\n")
    chain = ""
    if lid in ("search", "dep") and plan.get("dependency_chain"):
        chain = "\n【业绩依赖链 · 判断主线索】\n" + str(plan["dependency_chain"]) + "\n"
    user = (head + chain
            + "\n【本期证据】（只能引用这里和检索来源里的事实）\n"
            + json.dumps(evidence, ensure_ascii=False, indent=1)
            + "\n\n【要判的维度】\n" + _dim_block(dims)
            + "\n\n【上一期同维度取值】\n" + _prev_block(prev, dims))
    if lid in ("search", "dep"):
        user += ("\n\n【检索要求】本地公告与资讯只是已知事实的起点。"
                 "对每个维度先用 deepseek_web_search 搜一次近 7 天的增量信息，"
                 "把搜到的关键事实（含日期与媒体名）写进 note；搜不到就 status=n、"
                 "value 写「本期无增量」并注明下次更新时间。不许拿旧闻充数。"
                 "本期引用过的来源必须逐条列在输出的 sources 里，每条带 quote 原文摘录——"
                 "平台会拿 quote 去核对你 note 里每个数字有没有出处，摘录对不上的会被标黄。")
    user += ("\n\n按系统约定输出 JSON，values 必须覆盖上面每一个 key，"
             "顺序与本清单一致。")
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]


def call_llm(messages: list[dict], lid: str, cfg: dict | None = None,
             timeout: int = 180) -> tuple[dict | None, dict]:
    """调一次配置里的模型。search 层带 deepseek_web_search 工具。"""
    cfg = cfg or ai.config()
    if not cfg:
        return None, {"error": "no_llm_config", "hint": "data-live/.secrets/llm.json 或 LLM_API_KEY 未配置"}
    body = {
        "model": cfg["model"], "messages": messages, "stream": False,
        "temperature": 0.15 if lid == "quant" else (0.3 if lid in ("search", "dep") else 0.2),
        "max_tokens": int(cfg.get("max_tokens", 2400)),
    }
    if lid in ("search", "dep"):
        body["tools"] = [{"type": "deepseek_web_search"}]
    req = _req(cfg, body)
    import urllib.request
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}
    msg = ((doc.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content") or ""
    usage = doc.get("usage") or {}
    info = {"model": cfg.get("model"), "usage": usage,
            "tool_calls": len(msg.get("tool_calls") or [])}
    draft = parse_json(text)
    if not draft:
        info["raw_tail"] = text[-400:]
    return draft, info


def _req(cfg: dict, body: dict):
    import urllib.request
    return urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST")


def parse_json(text: str) -> dict | None:
    """模型偶尔裹代码块或前后加话，逐级退让地找第一个完整对象。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    for cand in (s, s[s.find("{"):s.rfind("}") + 1] if "{" in s else ""):
        if not cand:
            continue
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- 校验
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _nums(text: str) -> set[str]:
    return set(_NUM.findall(str(text or "")))


def _is_dayish(n: str) -> bool:
    """1~31 的纯整数当日期/天数看，不参与「编数」判定：
    「11月18日」里的 18 是日期不是数据，每期都把它标黄只会让人忽略这个标记。"""
    return bool(re.fullmatch(r"\d{1,2}", n)) and 1 <= int(n) <= 31


def _allowed_nums(evidence: dict, prev: dict | None, dims: list[dict],
                  extra_text: str = "") -> set[str]:
    """允许出现的数字：证据包、上一期取值、维度阈值、本期检索来源。
    别的都算「没出处」——这是防模型编数唯一跑得动的办法。"""
    pool = _nums(json.dumps(evidence, ensure_ascii=False))
    if prev:
        pool |= _nums(json.dumps(prev.get("values") or [], ensure_ascii=False))
    for d in dims:
        pool |= _nums(json.dumps(d.get("thresholds") or {}, ensure_ascii=False))
        pool |= _nums(str(d.get("metric") or ""))
    if extra_text:
        pool |= _nums(extra_text)
    return pool


def validate_cells(raw_values, dims: list[dict], evidence: dict,
                   prev: dict | None, extra_text: str = "") -> tuple[list[dict], dict]:
    """把模型给的 values 收敛成契约允许的形状。

    三条硬约束：key 必须在方案里、status 必须能映射成 g/y/r/n、每个维度都要有格子。
    数字另算一档：找不到出处的**不删不改**，只打 unverified 标记并计数——
    删掉等于谎报「没数据」，改掉等于替模型编数，两者都比标记一份可疑更糟。
    检索层把 sources 全文丢进 extra_text，检索来的真数字就不会被误判成编造。
    """
    want = {d["key"]: d for d in dims}
    allowed = _allowed_nums(evidence, prev, dims, extra_text)
    by_key: dict[str, dict] = {}
    issues: list[dict] = []
    for v in raw_values or []:
        if not isinstance(v, dict):
            continue
        k = str(v.get("key") or "").strip()
        if k not in want:
            issues.append({"key": k, "why": "维度不在本层清单"})
            continue
        if k in by_key:
            issues.append({"key": k, "why": "重复，保留第一条"})
            continue
        st = STATUS_MAP.get(str(v.get("status") or "").strip().lower()) \
            or STATUS_MAP.get(str(v.get("status") or "").strip())
        if not st:
            issues.append({"key": k, "why": f"状态无法识别：{v.get('status')}"})
            st = "n"
        cell = {
            "key": k,
            "value": str(v.get("value") or ("本期无增量" if st == "n" else "")).strip(),
            "status": st,
            "delta": str(v.get("delta") or "").strip(),
            "note": str(v.get("note") or "").strip(),
        }
        if not cell["value"]:
            cell["value"] = "本期无增量"
        stray = sorted(n for n in _nums(cell["value"] + " " + cell["note"])
                       if n not in allowed and not _is_dayish(n))
        if stray:
            cell["unverified"] = 1
            cell["note"] = (cell["note"] + f"（未能在证据或来源摘录中定位：{'、'.join(stray[:6])}）").strip()
        by_key[k] = cell
    for k, d in want.items():
        if k in by_key:
            continue
        by_key[k] = {"key": k, "value": "本期无增量", "status": "n", "delta": "",
                     "note": f"本层未返回（{d.get('name')}），已按无内容处理"}
        issues.append({"key": k, "why": "模型漏了该维度，补空"})
    cells = [by_key[k] for k in want]
    stat = {"n_cells": len(cells),
            "n_unverified": sum(1 for c in cells if c.get("unverified")),
            "n_gap": sum(1 for c in cells if c["status"] == "n"),
            "issues": issues[:12]}
    return cells, stat


# ---------------------------------------------------------------- 合并
def merge_layers(plan: dict, parts: list[dict], day: str) -> dict:
    """三层结果合成一期：状态按维度顺序拼回去，verdict 用投票，summary 拼接。"""
    order = [d["key"] for d in (plan.get("dimensions") or []) if d.get("key")]
    cells: dict[str, dict] = {}
    for p in parts:
        for c in p.get("cells") or []:
            cells[c["key"]] = c
    values = [cells[k] for k in order if k in cells]
    g = sum(1 for v in values if v["status"] == "g")
    r = sum(1 for v in values if v["status"] == "r")
    verdict = "偏多" if g > r else ("偏空" if r > g else "中性")
    summaries, highlights, sources = [], [], []
    for p in parts:
        s = str(p.get("summary") or "").strip().rstrip("。；;，, ")
        if s:
            summaries.append(s)
        for h in (p.get("highlights") or [])[:3]:
            h = str(h).strip()
            if h and h not in highlights:
                highlights.append(h)
        for src in (p.get("sources") or []):
            if isinstance(src, dict) and src.get("title") and src not in sources:
                sources.append(src)
    summary = "；".join(summaries)[:300] or "本期无显著变化"
    return {
        "date": day,
        "verdict": verdict,
        "summary": summary,
        "values": values,
        "highlights": highlights[:5],
        "sources": sources[:12],
        "source": "llm",
        "model": (parts[0].get("model") if parts else None),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "layers": [{"layer": p["layer"], "verdict": p.get("verdict"),
                    "calls": p.get("calls", 1), "stats": p.get("stats")} for p in parts],
    }


def md_of(period: dict, plan: dict) -> str:
    """同目录的简报，供人读；JSON 才是给时间轴渲染的。"""
    st = {"g": "绿 · 达标/改善", "y": "黄 · 中性/需留意", "r": "红 · 恶化/触发", "n": "灰 · 无内容"}
    names = {d["key"]: d.get("name") or d["key"] for d in (plan.get("dimensions") or [])}
    lines = [f"# {plan.get('name')}（{plan['code']}）跟踪简报 · {period['date']}", "",
             f"**本期综合判断：{period['verdict']}**", "",
             period.get("summary") or "", ""]
    if period.get("highlights"):
        lines += ["## 最值得注意", ""] + [f"- {h}" for h in period["highlights"]] + [""]
    lines += ["## 逐维度状态", "", "| 维度 | 本期取值 | 状态 | 环比 | 依据 |",
              "| --- | --- | --- | --- | --- |"]
    for v in period.get("values") or []:
        nm = names.get(v["key"], v["key"])
        val = str(v.get("value") or "").replace("|", "／")
        note = str(v.get("note") or "").replace("|", "／")
        lines.append(f"| {nm} | {val} | {st.get(v['status'], '')} | {v.get('delta') or '—'} | {note} |")
    lines += ["", "---", "",
              f"生成方式：后端分层取数 + 配置模型（{period.get('model') or '未记录'}）逐层判断，"
              "数字经「能否在证据中找到出处」校验，找不到出处的已在依据里标注。",
              "", "以上为投研参考记录，不构成投资建议。", ""]
    return "\n".join(lines)


__all__ = ["LAYERS", "layer_of", "split_layers", "layer_label", "build_evidence",
           "build_messages", "call_llm", "parse_json", "validate_cells",
           "merge_layers", "md_of"]
