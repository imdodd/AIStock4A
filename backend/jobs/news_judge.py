"""资讯判断层：把事实层抓到的公开报道，对齐生产 WorkBuddy 任务的检索口径。

生产那几个「A股消息增量·整点/半点轮」任务，质量不来自搜索引擎，而来自四条约束：

  1. 范围固定：持仓 + 自选个股，以及 sectors.json 里 Stage1/Stage2 的行业；
  2. 查询词固定：「A股 今日 板块 异动 原因」/「<行业名> 最新消息」/「<个股名> 公告」；
  3. 增量判断：Agent 判断"确有增量"才写，没增量就跳过；
  4. 结构化标注：sectors / stocks / codes / impact / horizon / confidence / logic。

本模块把 2/3/4 做成代码 + LLM，让 V2 内置调度器接管后不会机械抓一堆无关新闻。
事实层（fetch_news.py）仍只负责抓，本模块只负责判，落盘仍走 news_update 去重合并。

开关：NEWS_JUDGE=1 或 fetch_news --judge。无 LLM Key 时**不落盘**（宁缺毋滥），
因为没判断层的机械抓取正是要避免的跑偏源。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from data_paths import DATA_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import sys  # noqa: E402
sys.path.insert(0, ROOT)

import ai  # noqa: E402
import news  # noqa: E402
from config import universe  # noqa: E402
from contracts.validate import validate_patch  # noqa: E402
from producers.llm_direct import chat  # noqa: E402

CN_TZ = timezone(timedelta(hours=8))
SECTORS_JSON = os.path.join(DATA_DIR, "snapshot", "sectors.json")

# 与生产任务一致的三类查询词模板
Q_MARKET = "A股 今日 板块 异动 收盘 原因"
Q_SECTOR = "A股 {name} 板块 最新消息"
# 只写「名字+公告」会被 Tavily 搜成同名 npm 包 / 外国公司（实测某科技股「XX 公告」
# 返回 Yarn / 同名科技公司），必须带 A股 + 代码前缀消歧。
Q_STOCK = "A股 {name} {code} 公告"

# 单轮候选上限：只取有增量的，宁可少。生产每轮也是"多次搜索但只写确有增量的"。
MAX_FETCH = 12
# 证据正文给足：Tavily content 上限 500 字，截断到 400 会砍掉后面的数字，
# 模型凭标题印象补写就被防幻觉校验全拒（实测一轮 kept=0）。
SNIPPET_LEN = 500


def _load_sectors() -> list[dict]:
    try:
        with open(SECTORS_JSON, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []
    rows = d.get("items") if isinstance(d, dict) else d
    return [r for r in (rows or []) if isinstance(r, dict)]


def all_sectors() -> list[dict]:
    """全部板块，供 LLM 标注用。查询范围只取 S1/S2，但标注范围要覆盖全板块——
    否则「券商股拉升」这类新闻无法标到证券Ⅱ（可能处于 Stage4），产出就成了空标注。"""
    return [{"code": str(r.get("code") or ""), "name": str(r.get("name") or "")}
            for r in _load_sectors() if r.get("code") and r.get("name")]


def focus_sectors() -> list[dict]:
    """生产口径：Stage1（筑底）+ Stage2（上升）的行业。stage 是数字 1/2。"""
    out = []
    for r in _load_sectors():
        try:
            stage = int(r.get("stage") or 0)
        except (TypeError, ValueError):
            continue
        if stage in (1, 2) and r.get("name"):
            out.append({"code": str(r.get("code") or ""), "name": str(r["name"])})
    return out


def focus_stocks() -> list[dict]:
    """生产口径：持仓（hold_codes）+ 自选（watch_items），带中文名。"""
    names = universe.names()
    out: list[dict] = []
    seen: set[str] = set()
    for code in list(universe.hold_codes() or []) + [c for c, _ in (universe.watch_items() or [])]:
        code = str(code or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": str(names.get(code) or code)})
    return out


def build_queries(max_queries: int = 4) -> list[str]:
    """动态查询词：全局 + 持仓/自选 + S1/S2 行业，按轮次轮转覆盖，不重复刷同一主题。

    生产任务每轮显式搜三类词；这里受搜索 API 预算约束（默认 4 次），
    用「当前半小时槽位」做偏移，整点/半点轮自然轮换到不同个股与行业。
    """
    budget = max(1, int(max_queries or 4))
    sector_q = [Q_SECTOR.format(name=s["name"]) for s in focus_sectors()]
    stock_q = [Q_STOCK.format(name=s["name"], code=s["code"]) for s in focus_stocks()]
    if budget == 1:
        return [Q_MARKET]

    # 大局那条固定保留；剩下额度按「个股 6 : 板块 4」分，各自按半小时槽位轮转，
    # 避免纯轮转时连续几条全是板块、把大盘异动和持仓公告挤掉。
    now = datetime.now(CN_TZ)
    slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
    rest = budget - 1
    n_stock = min(len(stock_q), max(1, round(rest * 0.6)))
    n_sector = rest - n_stock

    picked: list[str] = [Q_MARKET]
    if stock_q:
        start = slot % len(stock_q)
        picked += [stock_q[(start + i) % len(stock_q)] for i in range(min(n_stock, len(stock_q)))]
    if sector_q and n_sector > 0:
        start = (slot * 3) % len(sector_q)
        picked += [sector_q[(start + i) % len(sector_q)]
                   for i in range(min(n_sector, len(sector_q)))]
    # 个股或板块不足时用另一类补齐，保证预算用满
    if len(picked) < budget and sector_q:
        need = budget - len(picked)
        start = (slot * 5) % len(sector_q)
        picked += [sector_q[(start + i) % len(sector_q)] for i in range(need)]
    return picked[:budget]


def _extract_json(text: str) -> dict | None:
    """容忍模型把 JSON 包在 ```json 里，或前后带一句解释。"""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S | re.I)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        d = json.loads(t)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _prompt(day: str, items: list[dict], sectors: list[dict], stocks: list[dict],
            label_sectors: list[dict] | None = None) -> str:
    """只给模型候选清单与事实片段，逼它从清单里选 code，不许自造新闻和数字。

    sectors 是检索范围（S1/S2），label_sectors 是标注范围（全部板块）——两者不同，
    详见 all_sectors() 的说明。
    """
    candidates = [{
        "title": str(it.get("title") or "")[:120],
        "url": str(it.get("url") or ""),
        "source": str(it.get("source") or ""),
        "time": str(it.get("time") or ""),
        "snippet": str(it.get("raw_summary") or it.get("summary") or "")[:SNIPPET_LEN],
    } for it in items[:MAX_FETCH]]
    label_pool = label_sectors if label_sectors is not None else sectors
    sector_hint = "、".join(f"{s['code']}={s['name']}" for s in label_pool) or "（无）"
    stock_hint = "、".join(f"{s['code']}={s['name']}" for s in stocks) or "（无）"
    return f"""你是 A 股资讯的增量判断器。交易日 {day}。下面是从公开报道抓到的候选新闻。

任务：只保留对【大局判断 / 行业板块 / 持仓自选个股】确有增量的新闻。

【大局判断的范围要放宽】大局不只是 A 股盘面，还包括国内外宏观经济、货币政策、
世界经济与海外市场（如美联储政策、全球经济形势、汇率与大宗商品）——这类由官方媒体
发布的报道即使没有直接点名 A 股，也对判断大局有价值，应当保留，不要当成「与 A 股
无关」剔除。它对持仓的影响写进 logic 与相关板块/个股标注即可。

必须剔除的只有：纯生活娱乐、量化技术教程、旧闻翻炒、纯观点博眼球、软文广告。
拿不准的一律保留——宁可多留一条宏观新闻，也不要漏掉影响大局的关键事实。

候选新闻（JSON）：
{json.dumps(candidates, ensure_ascii=False)}

可选板块 code（只能从这里选，不许编造）：{sector_hint}
可选个股 code（只能从这里选，不许编造）：{stock_hint}

只输出一个 JSON 对象，不要任何解释文字：
{{
  "summary": "本轮大局一句话（只用上面事实，不编数字）",
  "window": "检索时间窗描述",
  "source_of_truth": "事实摘自所列公开报道；板块/个股影响为模型推断，不构成买卖建议。",
  "items": [
    {{
      "title": "客观标题，不加观点",
      "url": "必须与候选里的 url 完全一致",
      "source": "必须与候选里的 source 完全一致",
      "time": "必须与候选里的 time 完全一致",
      "summary": "事实摘要，只用候选 snippet 里的事实，不编数字",
      "category": "other 或 cn_policy 或 commodity 或 us_stock",
      "impact": "利多|利空|中性|分化（四选一，不许写长句）",
      "impact_note": "影响方向说明",
      "horizon": "短期 或 中期",
      "confidence": "高 或 中 或 低",
      "logic": "传导路径：这条事实如何影响大局/相关板块/相关个股",
      "keywords": ["关键词"],
      "sectors": [{{"code": "pt...", "name": "...", "dir": "利多|利空|中性|分化", "note": "影响说明"}}],
      "stocks": [{{"code": "sh/sz/bj+6位", "name": "...", "dir": "利多|利空|中性|分化", "note": "影响说明"}}],
      "sources": [{{"title": "同候选 title", "url": "同候选 url", "time": "同候选 time", "snippet": "从候选 snippet 摘录"}}]
    }}
  ]
}}

硬约束：
0. **最重要**：标题以外的 summary/impact_note/logic/note 里出现的每一个数字，
   都必须能在该条 sources[].snippet 里逐字找到。snippet 没写的数字绝对不许补，
   哪怕你在标题里"见过"或凭常识知道——宁可写"跌幅明显"也不许写"跌0.28%"。
   这是硬校验：snippet 外的数字会导致整条被判为编数而丢弃。
1. 每条必须带 sources[]，snippet 只能从候选里逐字摘录，不许改写数字；
2. sectors[].code 只能取上面的可选板块，stocks[].code 只能取上面的可选个股；
3. 若候选里没有值得写入的增量，直接返回 {{"summary":"本轮无实质增量","window":"...","source_of_truth":"...","items":[]}}；
4. 输出的 items 不要超过 {MAX_FETCH} 条。"""


def relevant(items: list[dict]) -> tuple[list[dict], int]:
    """检索层相关性过滤：先剔掉明显不是 A 股财经的结果。

    实测「XX科技 公告」这类简短词会被 Tavily 搜成 npm 的 keywords:a-share / Yarn /
    同名科技公司与斯里兰卡/柬埔寨银行 —— 这些不该占用判断层额度。
    只做保守剔除（命中即明显无关），拿不准的一律留给 LLM 判断，不误杀。
    """
    noise = ("npm", "yarn", "pypi", "github.com", "stackoverflow",
             "bank of ceylon", "unicredit", "commerzbank", "cimb bank",
             "macron", "canadia group", "am-ex:", "amex:")
    out, dropped = [], 0
    for it in items:
        title = str(it.get("title") or "")
        text = " ".join([title, str(it.get("url") or ""),
                         str(it.get("source") or "")]).lower()
        if any(w in text for w in noise):
            dropped += 1
            continue
        # 美股盘前/纳指这类纯英文标题对 A 股大局判断无用：目标系统只消费中文资讯。
        if not re.search(r"[\u4e00-\u9fff]", title):
            dropped += 1
            continue
        out.append(it)
    return out, dropped


def judge(day: str, items: list[dict], cfg: dict | None = None,
          max_tokens: int = 3000) -> tuple[dict | None, dict]:
    """对事实层候选做增量判断 + 契约校验。返回 (patch|None, info)。

    patch=None 且 skip=False 有两种：无 Key 降级（degraded，事实流仍写入）与调用失败。
    模型判断无增量 / 全被契约拒绝时 skip=True（本轮不写）。
    设计取舍（2026-11-20）：零 Key 的免费用户走事实流直出——没有 Key 就没有判断层，
    但客观新闻仍要能看到；这一档不标影响方向、不标板块个股，statistics 会显示中性。
    """
    cfg = cfg or ai.config()
    if not cfg:
        # 静默降级：无 Key 不是错误，事实层候选照常交给调用方按事实流落盘。
        return None, {"ok": True, "skip": False, "degraded": "no_llm_key",
                      "reason": "未配置 LLM Key，降级为事实流直出（不做影响判断与标注）"}
    items, noise_dropped = relevant(items)
    if not items:
        return None, {"ok": True, "skip": True, "kept": 0,
                      "noise_dropped": noise_dropped,
                      "reason": f"检索结果全部无关（剔除 {noise_dropped} 条），本轮无增量"}

    sectors = focus_sectors()
    stocks = focus_stocks()
    # 校验索引也要用全部板块，和 prompt 的标注范围一致；否则标了证券Ⅱ等
    # Stage3/4 板块会被契约误判为幻觉（实测这一条导致所有 sectors 被清空）。
    sector_index = {s["code"]: s["name"] for s in all_sectors()}
    message = [{"role": "user", "content": _prompt(day, items, sectors, stocks,
                                                   all_sectors())}]
    run_cfg = {**cfg, "max_tokens": int(cfg.get("max_tokens") or max_tokens)}
    try:
        reply = chat(run_cfg, message, timeout=60)
    except Exception as e:
        return None, {"ok": False, "skip": False,
                      "reason": f"LLM 判断调用失败：{type(e).__name__}: {str(e)[:160]}"}

    raw = _extract_json(str(reply.get("content") or ""))
    if raw is None:
        return None, {"ok": False, "skip": False, "reason": "LLM 输出不是合法 JSON，本轮不写入"}

    raw["date"] = day
    try:
        patch, rejected = validate_patch(raw, sector_index, require_sources=True)
    except Exception as e:
        return None, {"ok": False, "skip": False,
                      "reason": f"契约校验失败：{type(e).__name__}: {str(e)[:160]}"}

    # 补齐 codes：生产日文件把命中个股同时写进 codes 和 stocks，前端统计卡依赖它。
    for it in patch.get("items") or []:
        it["codes"] = [s["code"] for s in (it.get("stocks") or []) if s.get("code")]
        it["fetched_by"] = "news_judge"

    if not patch.get("items"):
        return None, {"ok": True, "skip": True, "reason": "LLM 判断本轮无实质增量",
                      "kept": 0, "rejected": rejected}
    return patch, {"ok": True, "skip": False, "noise_dropped": noise_dropped,
                   "kept": len(patch["items"]), "rejected": rejected}
