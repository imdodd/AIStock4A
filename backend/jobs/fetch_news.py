"""资讯事实层：按可插拔 provider 抓取公开报道，产出 RawItem 与「事实流 patch」。

定位（见 astock-refactor-plan.md 阶段 5）：

    事实层（本文件，不需要 AI、不需要 Key、断网也不报错）
        -> data/inbox/news_raw/{date}-{HHMM}.json   原始事实，可审计
        -> 判断层 producer 出 news_patch.json（本文件只产最小合规事实流）
        -> jobs/news_update.py --patch 负责去重/置顶/重算统计/原子落盘

本文件**绝不直接写 data/news/ 日文件**，这是既有红线。

用法（工作目录 server/ 或 backend/ 均可，路径全部相对推导）：

  python -m jobs.fetch_news --dry-run                      # 默认 none 档，只打印不写盘
  python -m jobs.fetch_news --provider none --limit 20
  python -m jobs.fetch_news --provider search_api --queries "央行,半导体"
  python -m jobs.fetch_news --emit-patch ../.tmp/news_patch.json
  python -m jobs.fetch_news --apply --dry-run            # 直接走合并器预演

环境变量：

  SEARCH_PROVIDER       none | search_api（默认 none）
  SEARCH_API_BACKEND    tavily | serper | generic（默认 tavily）
  SEARCH_API_KEY        搜索服务密钥；也可放 data/.secrets/search.json
  SEARCH_API_URL        generic 档的接口地址，{query} 为查询词占位符
  SEARCH_API_RESULTS_PATH / SEARCH_API_FIELDS  generic 档的结果路径与字段映射
  SEARCH_API_MAX_QUERIES / SEARCH_API_MAX_RESULTS  预算上限（默认 4 / 5）
  SEARCH_MAX_QUERIES       none 档每轮最多几个主题（默认 3，防止反复打上游）

零新增依赖：只用标准库 urllib / json / xml / datetime。
"""
from __future__ import annotations

import argparse
import html
import importlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import env_loader  # noqa: E402
env_loader.load_env()

from data_paths import DATA_DIR  # noqa: E402

import news  # noqa: E402
from jobs import news_update  # noqa: E402

INBOX = os.path.join(DATA_DIR, "inbox", "news_raw")
SECRET_FILE = os.path.join(DATA_DIR, ".secrets", "search.json")
CN_TZ = timezone(timedelta(hours=8))

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/xml, */*",
}
TIMEOUT = 10
MAX_SUMMARY = 500

# 事实层的检索主题。none 档逐个主题查一次固定源；search_api 档受预算上限约束。
DEFAULT_QUERIES = ["A股 政策", "央行 证监会", "半导体 光模块", "新能源 电池"]

# 固定源清单。enabled=False 的是「格式通用但未在本机验证」的源，默认不启用，
# 避免上线后静默拉一堆 404。新增源只需加一条配置 + 在 _FIXED_ADAPTERS 注册解析函数。
FIXED_SOURCES = [
    {"name": "eastmoney_search", "label": "东方财富资讯", "kind": "eastmoney", "enabled": True},
    {"name": "sina_roll", "label": "新浪财经滚动", "kind": "sina_roll", "enabled": True},
    {"name": "official_rss", "label": "官方 RSS（占位）", "kind": "rss", "enabled": False, "url": ""},
]

# 同一 host 本轮失败 2 次即熔断，不再继续打。东财风控越撞越严，这里刻意不重试。
_HOST_FAILS: dict[str, int] = {}
_HOST_FAIL_LIMIT = 2


# ---------------------------------------------------------------- HTTP（单次、不重试）

class CircuitOpen(Exception):
    pass


def _host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return url


def _http(url: str, *, headers: dict | None = None, body: dict | None = None,
          timeout: int = TIMEOUT) -> str:
    host = _host(url)
    if _HOST_FAILS.get(host, 0) >= _HOST_FAIL_LIMIT:
        raise CircuitOpen(f"{host} 本轮已熔断，跳过")
    h = dict(UA)
    if headers:
        h.update(headers)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception:
        _HOST_FAILS[host] = _HOST_FAILS.get(host, 0) + 1
        raise


def _json(url: str, **kw) -> dict:
    """容忍 JSONP 包裹（东财 search-api-web 返回 cb({...})）。"""
    raw = _http(url, **kw).strip()
    if not raw:
        raise ValueError("空响应")
    if not raw.startswith(("{", "[")):
        i, j = raw.find("("), raw.rfind(")")
        if i >= 0 and j > i:
            raw = raw[i + 1:j]
    d = json.loads(raw)
    if not isinstance(d, dict):
        raise ValueError("响应不是对象")
    return d


# ---------------------------------------------------------------- RawItem 契约

_TAG = re.compile(r"<[^>]+>")


def _clean(v, limit: int = 0) -> str:
    s = html.unescape(_TAG.sub(" ", str(v or "")))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] if limit else s


def _norm_time(v) -> str:
    """统一成 'YYYY-MM-DD HH:MM'；认不出就用当前时间，绝不伪造历史时间。"""
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        try:
            ts = float(v)
            if ts > 1e12:
                ts /= 1000
            return datetime.fromtimestamp(ts, CN_TZ).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    s = str(v or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", s):
        return s[:16].replace("T", " ")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s + " 00:00"
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M")


def make_item(*, title, url, source, time, raw_summary="", provider="", query="") -> dict | None:
    """唯一的事实条目构造入口，保证 news_update.valid_item() 的四条红线都能过。"""
    title = _clean(title, 180)
    url = str(url or "").strip()
    if not title or not url.startswith(("http://", "https://")):
        return None
    return {
        "title": title,
        "url": url,
        "source": _clean(source, 60) or provider or "公开来源",
        "time": _norm_time(time),
        "raw_summary": _clean(raw_summary, MAX_SUMMARY),
        "provider": provider,
        "query": query,
    }


# ---------------------------------------------------------------- Provider 接口

class SearchProvider(ABC):
    name = "base"

    @abstractmethod
    def fetch(self, queries: list[str], limit: int) -> tuple[list[dict], list[str]]:
        """返回 (RawItem 列表, 错误说明列表)。任何单源失败都只记错误，不抛给上层。"""


def register_provider(name: str, target: str):
    """登记 provider，格式 '模块路径:类名'。后续 searxng / agent 按此接入，不改本文件主逻辑。"""
    _REGISTRY[name.lower()] = target


_REGISTRY: dict[str, str] = {}


def get_provider(name: str | None = None) -> SearchProvider:
    name = (name or os.environ.get("SEARCH_PROVIDER") or "none").lower()
    if name not in _REGISTRY:
        raise ValueError(f"未知检索 provider {name}，可选：{', '.join(sorted(_REGISTRY))}")
    mod_path, cls_name = _REGISTRY[name].split(":")
    if mod_path in (__name__, "jobs.fetch_news"):
        cls = globals()[cls_name]
    else:
        cls = getattr(importlib.import_module(mod_path), cls_name)
    return cls()


# ---------------------------------------------------------------- none 档：固定源

def _eastmoney(query: str, limit: int, label: str) -> list[dict]:
    """东财 search-api-web（evidence.news() 同款），按主题取近期资讯标题。"""
    param = {
        "uid": "", "keyword": query, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                       "pageIndex": 1, "pageSize": limit,
                                       "preTag": "", "postTag": ""}},
    }
    url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
           + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
    d = _json(url, headers={"Referer": "https://quote.eastmoney.com/"})
    rows = ((d.get("result") or {}).get("cmsArticleWebOld")) or []
    # 该接口按时间排序且是模糊匹配，会混入完全不相关的稿件（实测按「光模块」
    # 能返回「特医食品」）。标题与摘要都不含主题词的一律丢掉，宁缺毋滥。
    words = [w for w in re.split(r"[\s,，]+", query) if len(w) >= 2]
    out = []
    for a in rows:
        title = _clean(a.get("title"), 180)
        summary = _clean(a.get("content") or a.get("summary"), MAX_SUMMARY)
        if words and not any(w in title or w in summary for w in words):
            continue
        it = make_item(title=title, url=a.get("url"),
                       source=a.get("mediaName") or a.get("source") or label,
                       time=a.get("date"), raw_summary=summary,
                       provider="none", query=query)
        if it:
            out.append(it)
        if len(out) >= limit:
            break
    return out


def _sina_roll(query: str, limit: int, label: str) -> list[dict]:
    """新浪财经滚动新闻。query 只用于本地关键词过滤，接口本身是全量滚动。"""
    url = ("https://feed.mix.sina.com.cn/api/roll/get?"
           "pageid=153&lid=2516&num=50&page=1")
    d = _json(url, headers={"Referer": "https://finance.sina.com.cn/"})
    rows = ((d.get("result") or {}).get("data")) or []
    words = [w for w in re.split(r"[\s,，]+", query) if w]
    out = []
    for a in rows:
        title = _clean(a.get("title"))
        if words and not any(w in title for w in words):
            continue
        it = make_item(title=title, url=a.get("url"),
                       source=a.get("media_name") or label,
                       time=a.get("ctime") or a.get("time"),
                       raw_summary=a.get("intro") or a.get("summary"),
                       provider="none", query=query)
        if it:
            out.append(it)
        if len(out) >= limit:
            break
    return out


def _rss(query: str, limit: int, label: str, url: str) -> list[dict]:
    """通用 RSS 2.0 / Atom 解析，供官方站点接入。"""
    if not url:
        return []
    raw = _http(url)
    root = ET.fromstring(raw)
    out = []
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
        it = make_item(title=get("title"), url=link, source=label,
                       time=get("pubDate") or get("published") or get("updated"),
                       raw_summary=get("description") or get("summary"),
                       provider="none", query=query)
        if it:
            out.append(it)
        if len(out) >= limit:
            break
    return out


_FIXED_ADAPTERS = {"eastmoney": _eastmoney, "sina_roll": _sina_roll, "rss": _rss}


class NoneProvider(SearchProvider):
    """脚本自足档：只打固定源，不需要任何 Key，断网时返回空列表而不报错。"""

    name = "none"

    def fetch(self, queries: list[str], limit: int) -> tuple[list[dict], list[str]]:
        items: list[dict] = []
        errors: list[str] = []
        enabled = [s for s in FIXED_SOURCES if s.get("enabled")]
        # 默认每轮最多 3 个主题：固定源无需鉴权，但也不该对已被风控的出口反复发请求。
        budget = int(os.environ.get("SEARCH_MAX_QUERIES") or 3)
        queries = queries[:max(1, budget)]
        for src in enabled:
            fn = _FIXED_ADAPTERS.get(src.get("kind"))
            if not fn:
                errors.append(f"{src['name']}: 未知适配器 {src.get('kind')}")
                continue
            for q in queries:
                try:
                    got = fn(q, limit, src.get("label") or src["name"],
                             **({"url": src.get("url", "")} if src.get("kind") == "rss" else {}))
                    items.extend(got)
                except Exception as e:
                    errors.append(f"{src['name']} / {q}: {type(e).__name__}: {e}")
                    break   # 该源本次失败就换下一个源，不重试
        return items, errors


# ---------------------------------------------------------------- search_api 档

def _cfg() -> dict:
    """密钥优先级：环境变量 > data/.secrets/search.json。密钥永不进入输出。"""
    d = {}
    try:
        with open(SECRET_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        pass
    return {
        "backend": (os.environ.get("SEARCH_API_BACKEND") or d.get("backend") or "tavily").lower(),
        "key": os.environ.get("SEARCH_API_KEY") or d.get("api_key") or "",
        "url": os.environ.get("SEARCH_API_URL") or d.get("url") or "",
        "results_path": os.environ.get("SEARCH_API_RESULTS_PATH") or d.get("results_path") or "results",
        "fields": os.environ.get("SEARCH_API_FIELDS") or d.get("fields") or {},
        "max_queries": int(os.environ.get("SEARCH_API_MAX_QUERIES") or d.get("max_queries") or 4),
        "max_results": int(os.environ.get("SEARCH_API_MAX_RESULTS") or d.get("max_results") or 5),
    }


def _dig(d: dict, path: str) -> list:
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return []
        if cur is None:
            return []
    return cur if isinstance(cur, list) else []


def _tavily_request(c: dict, q: str) -> tuple[list[dict], str]:
    url = c["url"] or "https://api.tavily.com/search"
    # 官方当前参数：time_range（day/week/month/year），旧的 days 已不在文档里；
    # language=zh-cn 只提升中文结果排名，要严格过滤再配 filter_by_language。
    body = {"api_key": c["key"], "query": q, "topic": "news",
            "time_range": "day", "language": "zh-cn",
            "max_results": c["max_results"], "include_raw_content": False}
    # Tavily 官方当前用 Authorization: Bearer；body 里的 api_key 是旧写法，
    # 两边都带，兼容新旧网关，且密钥不进 URL / 日志。
    headers = {"Content-Type": "application/json"}
    if c.get("key"):
        headers["Authorization"] = "Bearer " + c["key"]
    d = _json(url, body=body, headers=headers)
    rows = _dig(d, "results")
    out = []
    for a in rows:
        it = make_item(title=a.get("title"), url=a.get("url"),
                       source=a.get("source") or "Tavily",
                       time=a.get("published_date"), raw_summary=a.get("content"),
                       provider="search_api", query=q)
        if it:
            out.append(it)
    return out, url


def _serper_request(c: dict, q: str) -> tuple[list[dict], str]:
    url = c["url"] or "https://google.serper.dev/search"
    body = {"q": q, "num": c["max_results"], "tbs": "qdr:d"}
    d = _json(url, body=body,
              headers={"Content-Type": "application/json", "X-API-KEY": c["key"]})
    rows = _dig(d, "organic")
    out = []
    for a in rows:
        it = make_item(title=a.get("title"), url=a.get("link"),
                       source=a.get("source") or "Serper",
                       time=a.get("date"), raw_summary=a.get("snippet"),
                       provider="search_api", query=q)
        if it:
            out.append(it)
    return out, url


def _generic_request(c: dict, q: str) -> tuple[list[dict], str]:
    """通用 JSON 适配器：按官方文档配 URL / 结果路径 / 字段映射即可接任意搜索服务。

    DeepSeek Search API 这类新服务用它接，不在代码里硬编未核实的 endpoint。
    密钥走 Authorization 头，不拼进 URL，避免出现在日志里。
    """
    if not c["url"]:
        raise ValueError("generic 档必须设 SEARCH_API_URL（用 {query} 作查询词占位符）")
    url = c["url"].replace("{query}", urllib.parse.quote(q))
    fields = c["fields"]
    if isinstance(fields, str):
        fields = json.loads(fields or "{}")
    fmap = {"title": "title", "url": "url", "summary": "content",
            "time": "published_date", "source": "source"}
    fmap.update(fields or {})
    d = _json(url, headers={"Authorization": "Bearer " + c["key"]} if c["key"] else None)
    rows = _dig(d, c["results_path"])
    out = []
    for a in rows:
        it = make_item(title=a.get(fmap["title"]), url=a.get(fmap["url"]),
                       source=a.get(fmap["source"]) or "搜索服务",
                       time=a.get(fmap["time"]), raw_summary=a.get(fmap["summary"]),
                       provider="search_api", query=q)
        if it:
            out.append(it)
    return out, url


_SEARCH_BACKENDS = {"tavily": _tavily_request, "serper": _serper_request,
                    "generic": _generic_request}


class SearchApiProvider(SearchProvider):
    """搜索 API 档：结构化返回，直接产出可落盘的 sources[]。

    有硬性预算上限（max_queries × max_results），调用次数由脚本控制，不交给模型决定。
    """

    name = "search_api"

    def fetch(self, queries: list[str], limit: int) -> tuple[list[dict], list[str]]:
        c = _cfg()
        fn = _SEARCH_BACKENDS.get(c["backend"])
        errors: list[str] = []
        if not fn:
            return [], [f"未知 SEARCH_API_BACKEND {c['backend']}"]
        if c["backend"] != "generic" and not c["key"]:
            return [], ["缺少 SEARCH_API_KEY：本轮不检索，自动退回事实流（不报错）"]
        items: list[dict] = []
        # 硬预算：查询数由脚本切分决定，不交给模型；单主题结果数同样截断。
        for q in queries[:max(1, c["max_queries"])]:
            try:
                got, _ = fn(c, q)
                items.extend(got[:max(1, c["max_results"])])
            except Exception as e:
                errors.append(f"{c['backend']} / {q}: {type(e).__name__}: {e}")
                break
        return items, errors


# ---------------------------------------------------------------- 判重 / 落盘

def _existing(day: str) -> list[dict]:
    st = news.status(day)
    return [{"title": t, "url": u}
            for t, u in zip(st.get("recent_titles") or [], st.get("recent_urls") or [])]


def dedupe(items: list[dict], day: str) -> tuple[list[dict], int, int]:
    old = _existing(day)
    out: list[dict] = []
    dup = invalid = 0
    for it in items:
        if not news_update.valid_item(it):
            invalid += 1
            continue
        if news_update.is_dup(it, old) or news_update.is_dup(it, out):
            dup += 1
            continue
        out.append(it)
    out.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    return out, dup, invalid


def write_raw(day: str, items: list[dict], provider: str, errors: list[str]) -> str:
    stamp = datetime.now(CN_TZ).strftime("%H%M")
    path = os.path.join(INBOX, f"{day}-{stamp}.json")
    payload = {
        "date": day,
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "provider": provider,
        "count": len(items),
        "items": items,
        "errors": errors,
    }
    os.makedirs(INBOX, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def build_patch(day: str, items: list[dict], provider: str) -> dict:
    """事实流最小合规 patch：impact 固定四值枚举，长文本进 note，来源可审计。

    重要：这里绝不让模型/服务的自由文本进入 impact 或 sectors[].dir，
    否则会重演 11-15 日文件里「118 字长文当枚举 key、统计卡全 0」的问题。
    """
    rows = []
    for it in items:
        rows.append({
            "title": it["title"],
            "url": it["url"],
            "source": it["source"],
            "time": it["time"],
            "summary": it.get("raw_summary") or it["title"],
            "category": "other",
            "impact": "中性",
            "impact_note": "未做影响分析：事实层自动抓取，尚未经判断层 producer 处理。",
            "horizon": "短期",
            "confidence": "低",
            "logic": "未做影响分析",
            "keywords": [],
            "sectors": [],
            "stocks": [],
            "sources": [{"title": it["title"], "url": it["url"],
                         "time": it["time"], "snippet": it.get("raw_summary") or ""}],
            "fetched_by": provider,
        })
    return {
        "date": day,
        "summary": f"事实流共 {len(rows)} 条，未做影响分析；影响方向待判断层 producer 补齐。",
        "window": "最近 24 小时（事实层自动抓取）",
        "source_of_truth": "公开报道摘录，来源见每条 sources；本批次未做影响判断，不构成买卖建议。",
        "items": rows,
    }


# ---------------------------------------------------------------- CLI

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="资讯事实层抓取（不写日文件）")
    ap.add_argument("--provider", default=None, help="none | search_api，默认读 SEARCH_PROVIDER")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    ap.add_argument("--limit", type=int, default=20, help="单源单主题最多取几条")
    ap.add_argument("--queries", default=None, help="逗号分隔的检索主题")
    ap.add_argument("--dry-run", action="store_true", help="不写任何文件")
    ap.add_argument("--emit-patch", default=None, help="额外输出事实流 patch 到指定路径")
    ap.add_argument("--apply", action="store_true", help="把事实流 patch 交给 news_update 合并")
    ap.add_argument("--judge", action="store_true",
                    help="启用判断层：只对确有增量的新闻做结构化标注再落盘（或设 NEWS_JUDGE=1）")
    a = ap.parse_args()

    day = a.date or datetime.now(CN_TZ).strftime("%Y-%m-%d")
    judge_on = bool(a.judge) or (os.environ.get("NEWS_JUDGE") or "").strip().lower() in (
        "1", "true", "yes", "on")
    if a.queries:
        queries = [q.strip() for q in a.queries.split(",") if q.strip()]
    elif judge_on:
        # 对齐生产：持仓/自选 + S1/S2 行业的动态查询词，按半小时槽位轮转
        from jobs import news_judge  # 延迟导入，避免影响未启用判断层的既有路径
        queries = news_judge.build_queries(int(os.environ.get("SEARCH_API_MAX_QUERIES") or 4))
    else:
        queries = list(DEFAULT_QUERIES)

    try:
        provider = get_provider(a.provider)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2

    items, errors = provider.fetch(queries, a.limit)
    fresh, dup, invalid = dedupe(items, day)
    fresh = fresh[:max(0, a.limit)]

    # 落盘失败只记 error，不中断事实流：raw 是可审计副产物，patch 才是给下游的产出。
    raw_path = None
    if not a.dry_run:
        try:
            raw_path = write_raw(day, fresh, provider.name, errors)
        except Exception as e:
            errors.append(f"raw 落盘失败（不影响 patch）: {type(e).__name__}: {e}")
    patch = build_patch(day, fresh, provider.name)
    judge_info: dict | None = None
    skip_apply = False
    if judge_on:
        from jobs import news_judge  # 延迟导入
        judged, judge_info = news_judge.judge(day, fresh)
        if judged is not None:
            patch = judged  # 判断层富 patch：带 sectors / stocks / codes / logic
        elif judge_info.get("degraded"):
            # 无 Key 静默降级：patch 仍是 build_patch 的事实流（impact=中性、空标注），
            # 照常落盘，不报错也不当成失败。免费用户开箱即用，只是没有影响分析。
            pass
        else:
            # 无增量 / 无候选是正常跳过；调用失败要显式报错，不静默当成功
            skip_apply = bool(judge_info.get("skip"))
            if not skip_apply:
                errors.append(f"判断层未产出：{judge_info.get('reason') or '未知原因'}")
    patch_path = None
    if a.emit_patch and not a.dry_run:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(a.emit_patch)), exist_ok=True)
            with open(a.emit_patch, "w", encoding="utf-8") as f:
                json.dump(patch, f, ensure_ascii=False, indent=1)
            patch_path = a.emit_patch
        except Exception as e:
            errors.append(f"patch 落盘失败: {type(e).__name__}: {e}")

    merged = None
    if a.apply and not skip_apply:
        try:
            merged = news_update.apply(day, patch, a.dry_run, no_purge=True)
        except Exception as e:
            errors.append(f"合并失败: {type(e).__name__}: {e}")
            merged = {"ok": False, "error": str(e)}

    # 显式要求 patch 却没写出来时判失败，避免调度器把「空产出」当成成功。
    ok = not (a.emit_patch and not a.dry_run and not patch_path)
    if judge_on and judge_info and not judge_info.get("ok") and not skip_apply:
        ok = False  # 无 Key / 调用失败 / 契约拒绝：必须让调度器记为失败
    out = {
        "ok": ok, "date": day, "provider": provider.name, "dry_run": a.dry_run,
        "judge": bool(judge_on), "judge_info": judge_info, "skip_apply": skip_apply,
        "queries": queries, "fetched": len(items), "fresh": len(fresh),
        "duplicates": dup, "invalid": invalid,
        "raw_file": raw_path, "patch_file": patch_path,
        "errors": errors, "sample": [x["title"] for x in fresh[:3]],
        "merged": merged,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok else 1


register_provider("none", "jobs.fetch_news:NoneProvider")
register_provider("search_api", "jobs.fetch_news:SearchApiProvider")

if __name__ == "__main__":
    raise SystemExit(main())
