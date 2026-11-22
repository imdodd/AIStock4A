"""美国权威财经源 RSS 采集：抓取英文原文，交给 LLM 翻译成中文再入消息板。

背景（2026-11-22 用户要求「中美财经资讯都要有，英文翻中文」）：
    实测 DeepSeek 联网搜索即使给英文查询词（Fed/Reuters/Bloomberg），
    返回的 13 条来源仍全是中文站，美国权威源一条都没进。故改走直抓 RSS：
    美联储官网、MarketWatch、NPR 在本机实测可达（200），CNBC/Google News
    超时、BLS/Investing 403，不纳入默认源。

设计：
    * 只抓原文（title / link / description / pubDate），不翻译、不判断——
      翻译与 A 股影响判断统一交给 news_websearch 的 LLM，保证同一套契约校验。
    * 时间必须按 RFC822 / ISO 正确解析并换算北京时间：fetch_news._norm_time
      认不出 RFC822 会退回「当前时间」，会把旧闻伪装成当日稿，所以这里自己解析。
    * 零新增依赖：复用 fetch_news._http（含熔断/UA）与 xml.etree。
    * 任何一个源失败都只记 error、不影响其它源，更不能让整轮消息采集中断。
"""
from __future__ import annotations

import email.utils
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

from jobs import fetch_news

CN_TZ = timezone(timedelta(hours=8))

# 默认源：本机 2026-11-22 实测 http=200。可用 US_RSS_FEEDS 环境变量覆盖
# （JSON 数组 [{"name":..,"url":..}]），便于换线路/加源而不改代码。
DEFAULT_FEEDS = [
    {"name": "Federal Reserve 美联储",
     "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"name": "Federal Reserve 美联储",
     "url": "https://www.federalreserve.gov/feeds/press_monetary.xml"},
    {"name": "MarketWatch",
     "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    {"name": "NPR Business",
     "url": "https://feeds.npr.org/1006/rss.xml"},
]
MAX_PER_FEED = int(os.environ.get("US_RSS_PER_FEED", "6") or 6)
MAX_TOTAL = int(os.environ.get("US_RSS_MAX_TOTAL", "12") or 12)
_TAG = re.compile(r"<[^>]+>")

# MarketWatch 头条 RSS 混大量「我的退休/婚姻理财」个人故事，对 A 股无增量。
# 只放行宏观/市场/公司类；美联储与 NPR 源本身够聚焦，不过滤。
_MW_TOPIC = re.compile(
    r"\b(fed|inflation|interest rate|stock|market|s&p|nasdaq|dow|treasur|yield|dollar|"
    r"tariff|trade|china|semiconductor|chip|oil|gold|crypto|earnings|gdp|jobs|"
    r"unemploy|recession|federal reserve|wall street|econom|ipo|merger|acqui|"
    r"bubble|sell.?off|rally|bond|pce|cpi|powell)\b", re.I)


def _clean(v: Any, limit: int = 600) -> str:
    import html
    s = html.unescape(_TAG.sub(" ", str(v or "")))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _parse_time(v: str) -> str | None:
    """RFC822 / ISO → 北京时间 'YYYY-MM-DD HH:MM'；解析不了返回 None（不当当日稿）。"""
    s = str(v or "").strip()
    if not s:
        return None
    dt = None
    # RFC822：Sun, 22 Nov 2026 09:00:00 GMT / -0500
    try:
        parsed = email.utils.parsedate_to_datetime(s)
        if parsed is not None:
            dt = parsed
    except Exception:
        pass
    # ISO 8601：2026-11-22T09:00:00Z / +08:00
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)   # 没时区的按北京时间，不臆造偏移
    return dt.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M")


def _feeds() -> list[dict]:
    raw = os.environ.get("US_RSS_FEEDS", "").strip()
    if raw:
        try:
            import json
            arr = json.loads(raw)
            if isinstance(arr, list) and arr:
                return [x for x in arr
                        if isinstance(x, dict) and x.get("url")]
        except Exception:
            pass
    return DEFAULT_FEEDS


def fetch(day: str, *, verbose: bool = False) -> dict:
    """抓全部美国源，返回 {"items":[...], "errors":[...]}。

    每条 item：title/source/url/time/summary(英文原文)/raw_title(英文原题)/fetched_by。
    时间过滤用采集日往前 1 天（与 news_websearch 时效窗口一致），太旧的直接丢。
    """
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=CN_TZ)
    floor = (base - timedelta(days=max(0, int(os.environ.get("NEWS_FRESH_DAYS", "1") or 1)))
             ).strftime("%Y-%m-%d %H:%M")   # 与 _parse_time 返回值同类型，都是字符串
    items: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()

    for feed in _feeds():
        name, url = str(feed.get("name") or "US Source"), str(feed.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        try:
            raw = fetch_news._http(url, timeout=15)
            root = ET.fromstring(raw)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:80]}")
            continue

        count = 0
        for node in root.iter():
            if not node.tag.endswith(("item", "entry")):
                continue
            get = lambda k: next((c.text or "" for c in node if c.tag.endswith(k)), "")
            link = get("link")
            if not link:
                for c in node:
                    if c.tag.endswith("link") and c.attrib.get("href"):
                        link = c.attrib["href"]
                        break
            title = _clean(get("title"), 220)
            link = str(link or "").strip()
            pub = _parse_time(get("pubDate") or get("published") or get("updated"))
            if not title or not link.startswith(("http://", "https://")) or not pub:
                continue
            # MarketWatch 个人理财故事整段过滤（对 A 股无增量，白烧检索配额）
            if "marketwatch" in name.lower() and not _MW_TOPIC.search(title):
                continue
            if pub < floor:
                continue
            key = link or title
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "title": title,              # 英文原题，供 LLM 翻译
                "source": name,
                "url": link,
                "time": pub,                 # 北京时间，已换算
                "summary": _clean(get("description") or get("summary"), 900),
                "fetched_by": "us_rss",
            })
            count += 1
            if count >= MAX_PER_FEED or len(items) >= MAX_TOTAL:
                break
        if verbose:
            print(f"[us_rss] {name}: {count} 条")
        if len(items) >= MAX_TOTAL:
            break

    return {"items": items, "errors": errors}


def as_prompt_block(items: list[dict]) -> str:
    """把英文原文格式化进 prompt：明确标为「必处理语料」，并要求翻译。"""
    if not items:
        return ""
    lines = ["以下是本轮从美国权威源 RSS 抓到的英文原文（已换算北京时间），"
             "你必须逐条判断有无 A 股增量：有增量的翻译成中文收录，无增量的跳过："]
    for i, it in enumerate(items, 1):
        lines.append(
            f'{i}. [{it["source"]}] {it["time"]}\n'
            f'   原题: {it["title"]}\n'
            f'   摘要: {it["summary"][:500]}\n'
            f'   链接: {it["url"]}'
        )
    lines.append("翻译要求：title/summary/impact_note/logic/note 全部中文；"
                 "source 写「Reuters 路透社」这类中英对照；"
                 "sources[].snippet 逐字保留上面的英文原文（含数字），不许翻译成中文。")
    return "\n".join(lines)
