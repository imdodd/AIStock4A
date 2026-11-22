"""联网检索 producer：用 LLM 服务端 web_search 直接产出过契约的资讯 patch。

与事实层 + 判断层两段式（fetch_news + news_judge）并列，是第三条检索档：

    SEARCH_PROVIDER=llm_websearch

区别：查询词、联网检索、增量判断、结构化标注全部由 DeepSeek 服务端工具
``deepseek_web_search`` 在一次调用里完成，最接近生产 WorkBuddy 任务里
「Agent 用 WebSearch 多次搜索 → 判断增量 → 写 patch」的工作方式。

硬约束（实测踩坑后写死，别删）：
  * 模型名强制 deepseek-flash（联网工具只在 flash 上开放），不跟随 LLM_MODEL；
  * 数字必须逐字来自 sources[].snippet，否则契约会判编数并整条拒绝；
  * sectors / stocks 必须是对象数组（含 code/name/dir/note），不是字符串数组；
  * sectors[].code 只能从 snapshot/sectors.json 全量板块里选（标注范围 = 全部板块）；
  * 无 Key / 调用失败 / 全被契约拒 → 本轮不落盘（宁缺毋滥，与判断层一致）。

落盘仍走 news_update.apply，绝不直接写 data/news/ 日文件。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import env_loader  # noqa: E402
env_loader.load_env()

from data_paths import DATA_DIR  # noqa: E402

import ai  # noqa: E402
import news  # noqa: E402
from config import universe  # noqa: E402
from contracts.validate import validate_patch  # noqa: E402
from jobs import news_judge, news_update  # noqa: E402

CN_TZ = timezone(timedelta(hours=8))
INBOX = os.path.join(DATA_DIR, "inbox", "news_websearch")


def _parse_item_time(value: str):
    """把模型给出的发布时间解析成北京时间；解析不了就按不可信处理。"""
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                pass
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return dt.astimezone(CN_TZ)


def _freshness_floor(day: str):
    """消息板只收采集日当天及前一天发布的稿件，更早的按旧闻拒绝。

    原来放宽到 2 天，是为了让周末能兜住周五的存量；实测发现周末检索词若
    锚定到旧日期，整批 2 天前的旧闻会被当成「新鲜」入库，污染当日简报。
    收紧到 1 天后：周末两点之间的真新闻（政策/海外/公告）仍能进，跨了两天
    以上的旧行情解读会被拒。窗口可用 NEWS_FRESH_DAYS 覆盖。
    """
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=CN_TZ)
    days = max(0, int(os.environ.get("NEWS_FRESH_DAYS", "1") or 1))
    return base - timedelta(days=days)
# 联网工具只在 deepseek-flash 上开放；允许用环境变量显式覆盖以适配后续模型名变更。
SEARCH_MODEL = (os.environ.get("LLM_WEBSEARCH_MODEL") or "deepseek-flash").strip()
TIMEOUT = 180

# 查询词模板必须把「今天/今日/最新」锚定到采集日的具体日期——LLM 直接看到
# 「今日」会自行脑补成搜索结果里出现的旧日期（实测把 22 日采集锚成了 20 日），
# 导致整批旧闻回填当日简报。非交易日照样有政策/海外/公告增量，单独给一套词。
SEARCH_RECIPE = (
    "每条查询都必须带上采集日的具体日期，禁止用搜索结果里看到的其它日期替换。"
    "交易日：先搜「A股 {day_cn} 板块 异动 原因」，再搜「世界经济 美联储 货币政策 {day_cn}」"
    "这类宏观主题；再对查询范围里每只个股搜「<个股名> {day_cn} 公告」；"
    "再对 Stage1/Stage2 重点行业搜「<行业名> {day_cn} 最新消息」。"
    "美国权威财经源必须单独搜：用英文搜「US stocks Fed inflation {day_en} Reuters AP CNBC」、"
    "「Wall Street {day_en} Bloomberg WSJ MarketWatch」这类词，覆盖美股大盘、美联储、"
    "通胀就业、美元美债、中美贸易与半导体出口管制等主题。"
)
WEEKEND_RECIPE = (
    "每条查询都必须带上采集日的具体日期，禁止用搜索结果里看到的其它日期替换。"
    "非交易日同样有真新闻要采：先搜「{day_cn} 政策发布 国务院 证监会 央行」与"
    "「{day_cn} 海外市场 美股 美联储 最新」；再搜「<个股名> {day_cn} 公告」与"
    "「下周 A股 展望 {day_cn}」；对重点行业搜「<行业名> {day_cn} 消息」。"
    "不要搜「板块异动原因」这类交易日模板——周末没有盘中异动。"
    "美国权威财经源必须单独搜：用英文搜「Fed inflation US stocks {day_en} Reuters AP」、"
    "「Wall Street week ahead {day_en} Bloomberg CNBC WSJ」这类词，周末海外政策、"
    "经济数据与下周展望照常收录。"
)
# 美国权威财经源白名单：给模型明确的媒体清单，避免搜到营销号 / 聚合站的低质内容。
US_SOURCES_HINT = (
    "美国权威财经源优先：Reuters 路透社、AP 美联社、Bloomberg 彭博、CNBC、"
    "Wall Street Journal 华尔街日报、Financial Times 金融时报、MarketWatch、"
    "Yahoo Finance、Nasdaq、Investopedia、Federal Reserve 美联储官网、"
    "BLS 美国劳工统计局、BEA 美国经济分析局、白宫 / 美国财政部 / USTR 官方发布。"
    "这些源的报道即使不点名 A 股也要收——美联储利率、美国通胀就业、美元指数、"
    "美债收益率、美股科技巨头、中美贸易与出口管制，都是本系统判断大盘与板块的关键输入。"
)


def _cfg() -> dict | None:
    """密钥来自 ai.config()（文件优先），模型名强制 flash，不暴露密钥。"""
    cfg = ai.config()
    if not cfg:
        return None
    return {**cfg, "model": SEARCH_MODEL}


def _build_prompt(day: str, sectors: list[dict], stocks: list[dict],
                  stock_focus: list[dict], us_rss_block: str = "") -> str:
    stock_hint = "、".join(f"{s['code']}={s['name']}" for s in stock_focus) or "（无）"
    # 查询范围用 S1/S2，标注范围用全部板块——与 news_judge 的分离原则一致
    query_names = "、".join(s["name"] for s in sectors[:16]) or "（无）"
    sector_hint = "、".join(f"{s['code']}={s['name']}" for s in sectors) or "（无）"
    floor = _freshness_floor(day).strftime("%Y-%m-%d %H:%M")
    dt = datetime.strptime(day, "%Y-%m-%d")
    day_cn = f"{dt.year}年{dt.month}月{dt.day}日"
    day_en = dt.strftime("%B %d %Y").replace(" 0", " ")  # e.g. November 22 2026
    is_weekend = dt.weekday() >= 5
    recipe = ((WEEKEND_RECIPE if is_weekend else SEARCH_RECIPE)
              .replace("{day_cn}", day_cn).replace("{day_en}", day_en))
    return f"""你是 A 股资讯的增量判断器。采集日 {day_cn}（交易日与非交易日一视同仁）。
周末与节假日同样要采集：政策发布、海外市场、公司公告常在非交易日出现，周一开盘前必须已在库里。
用联网搜索采集确有增量的资讯。中美两边的财经资讯都要有，美国权威源的英文报道翻译成中文输出。

查询范围：个股（{stock_hint}）；行业（{query_names} 等 Stage1/Stage2 行业）；
大局（国内外宏观经济、世界经济、货币政策、海外市场——官方媒体这类报道即使不点名 A 股
也对本系统判断大盘有价值，必须保留，不许当「与 A 股无关」剔除）。
{US_SOURCES_HINT}
搜索方式：{recipe}
{us_rss_block}
只保留对大局 / 板块 / 个股确有增量的新闻，最多 6 条；其中美国权威源至少 2 条（有真增量时）。
只输出一个 JSON 对象，不要解释文字。

输出格式：
{{"date":"{day}","summary":"本轮大局一句话","window":"检索时间窗","source_of_truth":"事实摘自所列公开报道；板块/个股影响为模型推断，不构成买卖建议。","items":[
{{"title":"客观标题","url":"原文链接","source":"媒体名","time":"YYYY-MM-DD HH:MM",
"summary":"事实摘要","category":"other","impact":"利多|利空|中性|分化",
"impact_note":"影响说明","horizon":"短期|中期","confidence":"高|中|低","logic":"传导路径",
"keywords":["关键词"],
"sectors":[{{"code":"pt...","name":"板块名","dir":"利多|利空|中性|分化","note":"影响说明"}}],
"stocks":[{{"code":"sh600887","name":"伊利股份","dir":"利多","note":"影响说明"}}],
"sources":[{{"title":"同标题","url":"同原文链接","time":"同time","snippet":"从搜索结果逐字摘录的完整摘要"}}]
}}]}}

硬约束 0 · 最重要：summary / impact_note / logic / note 里的每个数字，都必须能在该条
sources[].snippet 里逐字找到。snippet 从搜索结果摘要里完整摘录，不要改写数字。
找不到带数字的摘要就把数字改成定性描述，绝不许自己补数字。这是硬校验，编数会被整条丢弃。

硬约束 0b · 英文报道翻译成中文（用户明确要求）：美国源的英文稿，title / summary /
impact_note / logic / keywords / 各 note 全部翻成通顺中文；source 写「Reuters 路透社」
这种中英对照，url 保留原文链接。但 sources[].snippet 必须【逐字保留英文原文】，
绝不能把 snippet 翻译成中文——数字校验是拿中文正文里的数字去 snippet 里逐字比对，
snippet 一翻译英文数字（如 9.8 billion）就对不上，整条会被当编数丢弃。

硬约束 1：sectors 和 stocks 必须是【对象数组】，每个元素含 code/name/dir/note 四个字段，
绝不能写成字符串数组。没有相关标的就写 []。

硬约束 2：sectors[].code 只能从下面的板块清单里原样复制，不许编造：
{sector_hint}

硬约束 3：stocks[].code 必须从查询范围的个股里选，不许编造。

  硬约束 4：time 必须是原文发布时间，统一换算成北京时间（UTC+8，格式 YYYY-MM-DD HH:MM），
  且不得早于 {floor}。美国源常见美东时间 ET / EDT，比北京时间慢 12–13 小时，
  务必 +12~13 小时换算后再填——照抄美东时间会被判成旧闻整条丢弃。
  旧公告、旧研报、旧处罚、旧行情解读即使第一次搜到也不算增量；宁可返回 items: []，
  不要把旧闻翻炒成新消息。特别注意：你的检索词里若出现不是 {day_cn} 的日期
  （例如从搜索结果里抄来的旧日期），立刻改成 {day_cn} 重新搜。
  日期锚点只能是采集日 {day_cn}，一天都不许前移。

  硬约束 5：没有实质增量就返回 items: []。非交易日的政策/海外/公告若有增量照常收录，
  但如果翻来覆去只有 {floor[:10]} 之前的旧稿，就返回 items: []，不要拿旧闻填空。"""


def _extract_json(text: str) -> dict | None:
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


def _call(cfg: dict, prompt: str, day: str) -> tuple[dict | None, dict]:
    """一次服务端联网搜索调用；返回 (patch|None, info)。info 含查询词与原始来源，供审计。"""
    url = str(cfg["base_url"]).rstrip("/") + "/chat/completions"
    body = {
        "model": SEARCH_MODEL,
        "messages": [
            {"role": "system", "content": "你只输出合法 JSON，不要代码块，不要解释文字。"},
            {"role": "user", "content": prompt},
        ],
        "tools": [{"type": "deepseek_web_search"}],
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + str(cfg["api_key"])},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            doc = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        return None, {"ok": False, "reason": f"联网搜索 HTTP {e.code}: {detail}"}
    except Exception as e:
        return None, {"ok": False, "reason": f"联网搜索失败：{type(e).__name__}: {str(e)[:200]}"}

    ch = (doc.get("choices") or [{}])[0]
    content = str((ch.get("message") or {}).get("content") or "")
    tool_calls = (ch.get("message") or {}).get("server_tool_calls") or []
    queries: list[str] = []
    raw_sources: list[dict] = []
    for block in tool_calls:
        for call in block.get("calls") or []:
            act = call.get("action") or {}
            queries.extend([str(q) for q in (act.get("queries") or [])])
            raw_sources.extend([s for s in (call.get("sources") or []) if isinstance(s, dict)])
    usage = (doc.get("usage") or {}).get("server_tool_use") or {}

    patch = _extract_json(content)
    if patch is None:
        return None, {"ok": False, "reason": "模型输出不是合法 JSON，本轮不写入",
                      "queries": queries, "raw_sources": raw_sources, "usage": usage}
    info = {"ok": True, "queries": queries, "raw_sources": raw_sources,
            "usage": usage, "finish_reason": ch.get("finish_reason")}
    return patch, info


def write_raw(day: str, patch: dict | None, info: dict) -> str | None:
    """审计副产物：记下本轮模型实际搜了哪些词、用了哪些来源、最终 patch。"""
    try:
        os.makedirs(INBOX, exist_ok=True)
        stamp = datetime.now(CN_TZ).strftime("%H%M")
        path = os.path.join(INBOX, f"{day}-{stamp}.json")
        payload = {"date": day, "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                   "model": SEARCH_MODEL, "queries": info.get("queries") or [],
                   "raw_sources": info.get("raw_sources") or [],
                   "usage": info.get("usage") or {}, "patch": patch,
                   "us_rss_fetched": info.get("us_rss_fetched") or 0,
                   "us_rss_errors": info.get("us_rss_errors") or []}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def _fallback_facts(day: str, dry_run: bool = False) -> dict:
    """无 LLM Key 时静默降级：用零 Key 固定源抓客观新闻，事实流直出。

    免费用户开箱即用——没有判断层就没有影响方向和板块标注，但新闻本身能看到。
    复用 fetch_news 的 none provider 与 build_patch，不重复实现抓取逻辑。
    """
    from jobs import fetch_news  # 延迟导入，避免模块级循环
    items, errors = fetch_news.get_provider("none").fetch(list(fetch_news.DEFAULT_QUERIES), 20)
    fresh, dup, invalid = fetch_news.dedupe(items, day)
    fresh = fresh[:20]
    patch = fetch_news.build_patch(day, fresh, "none-fallback")
    patch["source_of_truth"] = ("公开报道摘录，来源见每条 sources；未配置 AI Key，"
                                "本批次为事实流直出（无影响判断与标注），不构成买卖建议。")
    return {"ok": True, "skip": False, "degraded": "no_llm_key",
            "reason": "未配置 LLM Key，已降级为零 Key 事实流（固定源）",
            "queries": list(fetch_news.DEFAULT_QUERIES), "fetched": len(items),
            "fresh": len(fresh), "duplicates": dup, "invalid": invalid,
            "errors": errors, "patch": patch}


def search(day: str, cfg: dict | None = None, dry_run: bool = False) -> dict:
    """主入口：联网检索 → 契约校验 → 返回可直接交给 news_update.apply 的 patch。

    无 Key 时静默降级到零 Key 固定源事实流，保证免费用户也有资讯可看。
    """
    cfg = cfg or _cfg()
    if not cfg:
        return _fallback_facts(day, dry_run)
    sectors = news_judge.all_sectors()
    stocks = [{"code": s["code"], "name": s["name"]} for s in sectors]
    stock_focus = news_judge.focus_stocks()

    # 美国权威源 RSS：直抓英文原文喂给 LLM 翻译（联网搜索实测只返中文站，
    # 靠英文查询词拿不到 Reuters/美联储/NPR）。失败不影响主检索链。
    from jobs import us_rss
    us_items: list[dict] = []
    us_errors: list[str] = []
    if (os.environ.get("US_RSS_ENABLED", "1") or "1").strip().lower() in ("1", "true", "yes", "on"):
        try:
            got = us_rss.fetch(day)
            us_items = got.get("items") or []
            us_errors = got.get("errors") or []
        except Exception as e:
            us_errors = [f"{type(e).__name__}: {str(e)[:100]}"]
    rss_block = us_rss.as_prompt_block(us_items)
    prompt = _build_prompt(day, sectors, stocks, stock_focus, us_rss_block=rss_block)

    patch, info = _call(cfg, prompt, day)
    info["us_rss_fetched"] = len(us_items)
    info["us_rss_errors"] = us_errors
    raw_path = write_raw(day, patch, info) if not dry_run else None
    if patch is None:
        return {"ok": False, "skip": False, "raw_file": raw_path,
                "reason": info.get("reason") or "联网检索未产出 patch",
                "queries": info.get("queries") or [], "patch": None}

    patch["date"] = day
    sector_index = {s["code"]: s["name"] for s in sectors}
    # 数字白名单纳入本轮检索语料全文：搜索真实返回的数字不算编造，
    # 只认 sources[].snippet 摘录会把摘片段漏掉的真实数字误杀（实测拒绝率过高）。
    num_ctx = [{"title": str(s.get("title") or ""),
                "snippet": str(s.get("snippet") or "")}
               for s in info.get("raw_sources") or [] if isinstance(s, dict)]
    # 美国源 RSS 英文原文也进白名单：数字是真实抓取的（如 NPR 的 "S&P 500 sinks 3.7%"），
    # 中文翻译后要逐字核对，不放进白名单会被整条当编数拒。
    num_ctx += [{"title": str(s.get("title") or ""),
                 "snippet": str(s.get("summary") or "")}
                for s in us_items if isinstance(s, dict)]
    # 当日已入库新闻（全部过过契约）的标题与摘要也进白名单：指数点位、涨跌幅、
    # 成交额这类市场数字会在多轮间重复出现，多轮互相印证，不算编数。
    try:
        import news as news_store
        stored = news_store.load(day) or {}
        num_ctx += [{"title": str(x.get("title") or ""),
                     "snippet": str(x.get("summary") or "")}
                    for x in (stored.get("items") or []) if isinstance(x, dict)]
    except Exception:
        pass
    try:
        kept_patch, rejected = validate_patch(patch, sector_index,
                                              require_sources=True,
                                              number_context=num_ctx)
    except Exception as e:
        return {"ok": False, "skip": False, "raw_file": raw_path,
                "reason": f"契约校验失败：{type(e).__name__}: {str(e)[:160]}",
                "queries": info.get("queries") or [], "rejected": [], "patch": None}

    floor = _freshness_floor(day)
    fresh_items, stale_count = [], 0
    for it in kept_patch.get("items") or []:
        pub = _parse_item_time(it.get("time"))
        if pub is None or pub < floor:
            stale_count += 1
            rejected.append({"title": it.get("title") or "",
                             "rejected_reason":
                             f"发布时间 {it.get('time') or '空'} 早于时效窗口 "
                             f"{floor.strftime('%Y-%m-%d')}，按旧闻拒绝"})
            continue
        it["codes"] = [s["code"] for s in (it.get("stocks") or []) if s.get("code")]
        it["fetched_by"] = "news_websearch"
        fresh_items.append(it)
    kept_patch["items"] = fresh_items

    queries = info.get("queries") or []
    if not kept_patch.get("items"):
        reason = ("联网检索判断本轮无实质增量"
                  if not stale_count else
                  f"联网检索本轮无可用增量：{stale_count} 条因发布时间早于 "
                  f"{floor.strftime('%Y-%m-%d')} 被拒为旧闻")
        return {"ok": True, "skip": True, "raw_file": raw_path,
                "reason": reason, "queries": queries,
                "rejected": rejected, "kept": 0, "patch": None}
    return {"ok": True, "skip": False, "raw_file": raw_path, "queries": queries,
            "rejected": rejected, "kept": len(kept_patch["items"]),
            "usage": info.get("usage") or {}, "patch": kept_patch}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(description="LLM 服务端联网检索资讯（对齐生产 WebSearch 方式）")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    ap.add_argument("--dry-run", action="store_true", help="不写审计副本、不落盘")
    ap.add_argument("--apply", action="store_true", help="把过契约的 patch 交给 news_update 合并")
    ap.add_argument("--emit-patch", default=None, help="额外输出 patch 到指定路径")
    a = ap.parse_args()

    day = a.date or datetime.now(CN_TZ).strftime("%Y-%m-%d")
    res = search(day, dry_run=a.dry_run)
    patch = res.get("patch")
    merged = None
    if a.apply and patch is not None and not res.get("skip"):
        try:
            merged = news_update.apply(day, patch, a.dry_run, no_purge=True)
        except Exception as e:
            merged = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            res["ok"] = False
    patch_path = None
    if a.emit_patch and patch is not None and not a.dry_run:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(a.emit_patch)), exist_ok=True)
            with open(a.emit_patch, "w", encoding="utf-8") as f:
                json.dump(patch, f, ensure_ascii=False, indent=1)
            patch_path = a.emit_patch
        except Exception as e:
            res["ok"] = False
            res["reason"] = f"patch 落盘失败：{type(e).__name__}: {e}"

    out = {k: v for k, v in res.items() if k != "patch"}
    out.update({"date": day, "provider": "llm_websearch", "model": SEARCH_MODEL,
                "patch_file": patch_path, "merged": merged,
                "patch_items": len((patch or {}).get("items") or [])})
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
