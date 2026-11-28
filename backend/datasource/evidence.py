"""异动归因证据层：把「为什么异动」拆成可核验的五类证据

背景：东财涨停池并不提供「涨停原因」字段（只有连板数 / 首封时间 / 炸板次数 /
封单金额 / 所属行业），任何号称能直接读到原因的口径都是编的。所以这里改成
攒证据链，让归因建立在事实上：

    公告      np-anotice-stock.eastmoney.com     近 N 日公告标题
    新闻      search-api-web.eastmoney.com       近 N 日相关新闻标题
    龙虎榜    datacenter-web.eastmoney.com       RPT_DAILYBILLBOARD_DETAILSNEW
    资金流    push2.eastmoney.com                fflow/daykline（主力/散户净额）
    分时      push2his.eastmoney.com             trends2（拉升时点、尾盘强弱）
    涨停池    push2ex.eastmoney.com              getTopicZTPool（连板/封板/炸板）

再叠加本地已有的盘口（screener）、行业与阶段（members）、板块（sectors），
合成一份 data/cases/{date}/{code}.json，当日复用。

所有源都允许失败：单源挂掉只把 source_status 标 false，不影响整份证据包。
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

import screener as scr

from data_paths import DATA_DIR
from datasource import nethttp, ttlcache
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASE_DIR = os.path.join(DATA_DIR, "cases")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

# 2026-11-27：缓存粒度从「整包按日落盘」下沉到「每个子调用」。
# 整包缓存（cases/{day}/{code}.json）只作为快照存档与 60s 去抖，不再决定数据新鲜度。
CASE_DEBOUNCE = 60.0      # 整包组装结果的内存复用窗口（秒）；超过即重新组装
CLOSE_HM = "15:05"        # 收盘判定（含集合竞价后的撮合与回写延迟）


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _closed_now() -> bool:
    return time.strftime("%H:%M") >= CLOSE_HM


def _secs_to_close() -> float:
    """距 15:05 还有多少秒（下限 60）。盘中使用：磁盘缓存活到收盘即失效。"""
    now = datetime.now()
    close = now.replace(hour=15, minute=5, second=0, microsecond=0)
    return max(60.0, (close - now).total_seconds())


def _ttl_for(day: str, live: float) -> tuple[float, float]:
    """按「这只票的日期」决定 (内存 TTL, 磁盘 TTL)。

    历史日 / 当日已收盘 → 永久（数据不再变）；
    当日盘中 → 内存 live 秒（分时 60s、资金流 120s），磁盘活到收盘（跨进程复用当天抓取）。
    """
    day = day or _today()
    if day < _today() or (day == _today() and _closed_now()):
        return ttlcache.INF, ttlcache.INF
    return live, min(6 * 3600.0, _secs_to_close())

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}
ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
DT_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
FLOW_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
TREND_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
ZT_URL = "https://push2ex.eastmoney.com/getTopicZTPool"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get(url: str, timeout: int = 12, retry: int = 0) -> str:
    """取东财原始文本：统一走 nethttp（动态代理 + host 熔断 + 连接层不重试）。

    2026-11-27 P0 改造：retry 默认从 2 降到 0。一次完整档案要打 8 类接口，
    每个接口最多重试 3 遍意味着最坏 24 次请求；而 TCP 层被掐断时（rc=56 /
    RemoteDisconnected）这些重试全打在同一个封禁窗口上，只会延长封禁。
    现在连接层错误由 nethttp 直接止损并累计 host streak，普通错误也默认不重试。
    """
    return nethttp.http_text(url, timeout=timeout, retry=retry, headers=UA)


def secid(code: str) -> str:
    """腾讯风格代码 → 东财 secid（sh→1.，sz/bj→0.）"""
    return ("1." if code.startswith("sh") else "0.") + code[2:]


def _dump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ------------------------------------------------------------------ 涨停池
def zt_pool(day: str, use_cache: bool = True) -> dict:
    """当日涨停池（整池一次取回，多只复用）：code → {lbc,fbt,lbt,zbc,fund,hybk}"""
    path = os.path.join(CACHE_DIR, f"ztpool-{day}.json")
    if use_cache and os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    d8 = day.replace("-", "")
    url = (f"{ZT_URL}?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
           f"&Pageindex=0&pagesize=300&sort=fbt%3Aasc&date={d8}")
    out: dict[str, dict] = {}
    try:
        d = json.loads(_get(url))
        for it in ((d.get("data") or {}).get("pool") or []):
            mkt = "sh" if it.get("m") == 1 else "sz"
            code = mkt + str(it.get("c")).zfill(6)
            out[code] = {
                "lbc": it.get("lbc"), "fbt": it.get("fbt"), "lbt": it.get("lbt"),
                "zbc": it.get("zbc"), "fund": it.get("fund"), "hybk": it.get("hybk"),
                "days": (it.get("zttj") or {}).get("days"),
                "ct": (it.get("zttj") or {}).get("ct"),
            }
        _dump(path, out)
    except Exception:
        pass
    return out


def _hhmmss(v) -> str | None:
    """93000 → 09:30:00"""
    if v in (None, "", 0):
        return None
    s = str(int(v)).zfill(6)
    return f"{s[:2]}:{s[2:4]}:{s[4:]}"


# ------------------------------------------------------------------ 公告
def _announcements_raw(code: str, days: int = 10, limit: int = 12) -> list[dict]:
    q = urllib.parse.urlencode({
        "sr": -1, "page_size": limit, "page_index": 1, "ann_type": "A",
        "client_source": "web", "stock_list": code[2:], "f_node": 0, "s_node": 0,
    })
    d = json.loads(_get(f"{ANN_URL}?{q}"))
    out = []
    for x in ((d.get("data") or {}).get("list") or []):
        dt = (x.get("notice_date") or "")[:10]
        if not dt:
            continue
        out.append({"date": dt, "title": (x.get("title") or "").strip(),
                    "url": f"https://data.eastmoney.com/notices/detail/{code[2:]}/{x.get('art_code')}.html"
                    if x.get("art_code") else None})
        if len(out) >= limit:
            break
    return out


def announcements(code: str, days: int = 10, limit: int = 12) -> list[dict]:
    """近 N 日公告：6 小时 TTL（落盘跨进程复用）。

    公告一天只变几条，远低于行情频率。原来每次点开证据包都重打一次，
    85 只巡检每天就是 85 次；现在同一窗口内只真打一次。
    """
    return ttlcache.cached(f"ann:{code}:{days}:{limit}", 6 * 3600,
                           lambda: _announcements_raw(code, days, limit),
                           disk="ann", disk_ttl=6 * 3600)


# ------------------------------------------------------------------ 新闻
def _news_raw(code: str, name: str, limit: int = 8) -> list[dict]:
    param = {
        "uid": "", "keyword": name, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                       "pageIndex": 1, "pageSize": limit,
                                       "preTag": "", "postTag": ""}},
    }
    url = f"{NEWS_URL}?cb=cb&param={urllib.parse.quote(json.dumps(param, ensure_ascii=False))}"
    raw = _get(url)
    body = raw[raw.index("(") + 1: raw.rindex(")")]
    d = json.loads(body)
    out = []
    for a in (((d.get("result") or {}).get("cmsArticleWebOld")) or []):
        title = (a.get("title") or "").replace("\n", " ").strip()
        if not title:
            continue
        # 搜索按名字匹配，容易带出同名公司的新闻；标题里没出现名字或代码的降权保留
        out.append({"date": (a.get("date") or "")[:16], "title": title,
                    "url": a.get("url"), "hit": name in title or code[2:] in title})
    return out


def news(code: str, name: str, limit: int = 8) -> list[dict]:
    """相关新闻：30 分钟 TTL（只放内存）。新闻是「归因旁证」，不需要秒级新鲜。"""
    return ttlcache.cached(f"news:{code}:{limit}", 1800,
                           lambda: _news_raw(code, name, limit))


# ------------------------------------------------------------------ 龙虎榜
def _dragon_tiger_raw(code: str, day: str) -> dict:
    flt = f"(TRADE_DATE%3D%27{day}%27)(SECURITY_CODE%3D%22{code[2:]}%22)"
    q = (f"?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&filter={flt}"
         f"&pageNumber=1&pageSize=10&source=WEB&client=WEB")
    d = json.loads(_get(DT_URL + q))
    rows = ((d.get("result") or {}).get("data")) or []
    if not rows:
        return {"on_board": False}
    r = rows[0]
    return {
        "on_board": True,
        "explain": r.get("EXPLAIN"),
        "net_buy": _f(r.get("BILLBOARD_NET_AMT")),
        "buy_amt": _f(r.get("BILLBOARD_BUY_AMT")),
        "sell_amt": _f(r.get("BILLBOARD_SELL_AMT")),
        "deal_amt": _f(r.get("BILLBOARD_DEAL_AMT")),
        "reason": r.get("EXPLANATION"),
        "turnover_ratio": _f(r.get("TURNOVERRATE")),
    }


def dragon_tiger(code: str, day: str) -> dict:
    """龙虎榜：盘中 30 分钟（盘后 ~18:00 才出数据），历史日永久。"""
    mem, disk = _ttl_for(day, 1800)
    return ttlcache.cached(f"lhb:{code}:{day}", mem,
                           lambda: _dragon_tiger_raw(code, day),
                           disk="lhb", disk_ttl=disk)


# ------------------------------------------------------------------ 资金流
def _fund_flow_raw(code: str, days: int = 5) -> dict:
    q = (f"?lmt={days}&klt=101&secid={secid(code)}&fields1=f1,f2,f3,f7"
         f"&fields2=f51,f52,f53,f54,f55,f56,f57")
    d = json.loads(_get(FLOW_URL + q))
    rows = []
    for line in ((d.get("data") or {}).get("klines") or []):
        p = line.split(",")
        if len(p) < 7:
            continue
        rows.append({"date": p[0], "main": _f(p[1]), "small": _f(p[2]),
                     "mid": _f(p[3]), "big": _f(p[4]), "super": _f(p[5])})
    if not rows:
        return {}
    last = rows[-1]
    return {
        "date": last["date"],
        "main_net": last["main"],
        "small_net": last["small"],
        "mid_net": last["mid"],
        "big_net": last["big"],
        "super_net": last["super"],
        "main_net_5d": sum((r["main"] or 0) for r in rows),
        "rows": rows,
    }


def fund_flow(code: str, days: int = 5, day: str | None = None) -> dict:
    """近 N 日主力资金：盘中 120 秒，历史日/收盘后永久。"""
    day = day or _today()
    mem, disk = _ttl_for(day, 120)
    return ttlcache.cached(f"flow:{code}:{day}:{days}", mem,
                           lambda: _fund_flow_raw(code, days),
                           disk="flow", disk_ttl=disk)


# ------------------------------------------------------------------ 分时
#
# 2026-11-20：本机直连 push2his.eastmoney.com 会被掐连接（分时和日 K 都报
# RemoteDisconnected），腾讯 gtimg 正常，所以分时改成「东财优先 + 腾讯兜底」。
# 东财连续失败就熔断 5 分钟，避免每个图位都先等一次 12 秒超时。
TX_MINUTE = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
TX_M5 = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"

_EM_TREND_DOWN_UNTIL = 0.0        # 东财分时熔断截止时间戳


def _em_trend_down() -> bool:
    return time.time() < _EM_TREND_DOWN_UNTIL


def _em_trend_trip(sec: float = 300.0) -> None:
    global _EM_TREND_DOWN_UNTIL
    _EM_TREND_DOWN_UNTIL = time.time() + sec


def _tx_json(url: str, timeout: int = 8) -> dict:
    """腾讯 gtimg 取 JSON（失败只重试一次：它对短时连发也比较敏感）"""
    return json.loads(_get(url, timeout=timeout, retry=1))


def _hm(t: str) -> str:
    """0935 → 09:35；已是 09:35 就原样返回"""
    t = str(t)[-4:]
    return f"{t[:2]}:{t[2:]}" if t.isdigit() else str(t)


def tx_minute(code: str) -> dict:
    """腾讯当日分时。原始行是「HHMM 价格 累计量(手) 累计额(元)」，

    量要差分回每分钟，均价用 累计额 ÷ (累计量 × 100) 反推 —— 和东财 f58 等价。
    """
    d = _tx_json(f"{TX_MINUTE}?code={code}")
    node = (d.get("data") or {}).get(code) or {}
    qt = (node.get("qt") or {}).get(code) or []
    prev = _f(qt[4]) if len(qt) > 4 else None          # qt[3]=现价 4=昨收 5=今开
    inner = node.get("data") or {}
    date8 = str(inner.get("date") or "")
    iso = f"{date8[:4]}-{date8[4:6]}-{date8[6:]}" if len(date8) == 8 else None
    pts, last_v = [], 0.0
    for line in (inner.get("data") or []):
        p = str(line).split()
        if len(p) < 3:
            continue
        hm, price = _hm(p[0]), _f(p[1])
        if price is None or price <= 0:
            continue
        if not ("09:30" <= hm <= "15:00"):              # 剔掉集合竞价与盘后固定价交易
            continue
        cv = _f(p[2]) or 0.0                            # 累计量（手）
        ca = _f(p[3]) if len(p) > 3 else None           # 累计额（元）
        vol = cv - last_v if cv >= last_v else 0.0      # 差分回每分钟
        last_v = cv
        avg = round(ca / (cv * 100), 3) if (ca and cv) else None
        pts.append({"t": f"{iso} {hm}" if iso else hm, "d": iso, "hm": hm,
                    "p": price, "avg": avg, "v": vol})
    if not pts:
        return {"code": code, "days": 1, "prev_close": prev, "date": iso,
                "points": [], "source": "tencent"}
    return {"code": code, "days": 1, "prev_close": prev or pts[0]["p"],
            "date": iso or pts[-1]["d"], "points": pts, "source": "tencent"}


def tx_m5_day(code: str, iso: str) -> dict:
    """腾讯 5 分钟线取某个交易日的历史分时（48 个点），均价用「量加权收盘」近似。

    比东财的 1 分钟粗，但胜在还能用；东财恢复后 trend_on 会优先走 1 分钟。
    注意腾讯 m5 的第 8 位不是成交额（试过，算出来的均价离谱），
    所以这里用 Σ(收盘×量)/Σ量 近似均价，与交易所均价误差在千分之几。
    """
    key = iso.replace("-", "")
    d = _tx_json(f"{TX_M5}?param={code},m5,,320")
    node = (d.get("data") or {}).get(code) or {}
    bars = node.get("m5") or []
    prev, pts, cum_v, cum_pv = None, [], 0.0, 0.0
    started = False
    for b in bars:                                     # 腾讯按时间升序给
        if len(b) < 6:
            continue
        bt = str(b[0])
        if bt.startswith(key):
            started = True
            close = _f(b[2])
            if close is None:
                continue
            vol = _f(b[5]) or 0.0
            cum_v += vol
            cum_pv += close * vol
            avg = round(cum_pv / cum_v, 3) if cum_v else None
            pts.append({"hm": _hm(bt[-4:]), "p": close, "avg": avg, "v": vol})
        elif not started:                              # 目标日之前的交易日：只记收盘当昨收
            prev = _f(b[2]) or prev
        else:                                          # 已经过了目标日，别再往后覆盖昨收
            break
    if not prev:
        prev = _f(node.get("prec")) or (pts[0]["p"] if pts else None)
    return {"code": code, "date": iso, "granularity": "5m",
            "prev_close": prev, "points": pts, "source": "tencent"}


def _em_trend_points(code: str, days: int = 1) -> dict:
    """东财 trends2：f51 时间, f53 收, f56 量, f58 均价；preClose 为昨收。"""
    q = (f"?secid={secid(code)}&fields1=f1,f2,f3,f7,f8"
         f"&fields2=f51,f53,f56,f58&ndays={days}")
    d = json.loads(_get(TREND_URL + q, timeout=6, retry=1))
    data = d.get("data") or {}
    prev = _f(data.get("preClose"))
    pts = []
    for line in (data.get("trends") or []):
        p = line.split(",")
        if len(p) < 3:
            continue
        ts, price = p[0], _f(p[1])
        if price is None or price <= 0:
            continue
        pts.append({
            "t": ts, "d": ts[:10], "hm": ts[-5:], "p": price,
            "avg": _f(p[3]) if len(p) > 3 else None,
            "v": _f(p[2]) or 0,
        })
    return {"code": code, "days": days, "prev_close": prev,
            "date": pts[-1]["d"] if pts else None, "points": pts, "source": "eastmoney"}


def trend_points(code: str, days: int = 1) -> dict:
    """分时原始点（画图用）：时间 / 价格 / 均价 / 分钟量

    days>1 时东财能返回多日；腾讯只有当日，所以兜底时多日请求会降级成当日一天。
    东财一旦失败就熔断 5 分钟，直接走腾讯，不再挨个图位等超时。
    """
    n = max(1, min(5, int(days or 1)))
    if not _em_trend_down():
        try:
            d = _em_trend_points(code, n)
            if d.get("points"):
                return d
            _em_trend_trip()
        except Exception:
            _em_trend_trip()
    d = tx_minute(code)
    d["days"] = 1
    return d


def _norm_day(day: str) -> tuple[str, str]:
    """2026-11-04 / 20261104 → ('20261104', '2026-11-04')"""
    d8 = "".join(ch for ch in str(day) if ch.isdigit())
    if len(d8) != 8:
        raise ValueError(f"日期格式不对：{day}")
    return d8, f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"


def trend_on(code: str, day: str) -> dict:
    """指定交易日的历史分时，用于日K下方联动子图。

    两级取数：
      1) trends2 ndays=5 —— 近 5 个交易日有 1 分钟粒度 + 交易所均价（f58），最准；
      2) 5 分钟 K 线（klt=5, beg=end=该日）—— 更早的日期，48 个点，均价用累计成交额/累计量推算。

    历史日的数据不会再变，所以落盘到 data/intraday/{code}/{date}.json，只算一次。
    """
    d8, iso = _norm_day(day)
    # 当天的分时还在变，不能落盘；历史日才缓存
    frozen = iso < datetime.now().strftime("%Y-%m-%d")
    cache = os.path.join(DATA_DIR, "intraday", code, f"{d8}.json")
    if frozen and os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                c = json.load(f)
            if c.get("points"):
                return c
        except Exception:
            pass

    out = None

    # ---- 1) 近 5 日：1 分钟 + 均价 ----
    try:
        multi = trend_points(code, 5)
        buckets: dict[str, list] = {}
        order: list[str] = []
        for p in multi.get("points") or []:
            d = p.get("d")
            if d not in buckets:
                buckets[d] = []
                order.append(d)
            buckets[d].append(p)
        if iso in buckets:
            i = order.index(iso)
            prev = multi.get("prev_close") if i == 0 else buckets[order[i - 1]][-1]["p"]
            out = {
                "code": code, "date": iso, "granularity": "1m",
                "prev_close": prev, "source": multi.get("source"),
                "points": [{"hm": p["hm"], "p": p["p"], "avg": p.get("avg"), "v": p.get("v") or 0}
                           for p in buckets[iso]],
            }
    except Exception:
        out = None

    # ---- 2) 更早：5 分钟 K 线（东财）----
    if not out and not _em_trend_down():
        try:
            q = (f"?secid={secid(code)}&klt=5&fqt=1&beg={d8}&end={d8}&lmt=60"
                 f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
            d = json.loads(_get(KLINE_URL + q, timeout=6, retry=1))
            data = d.get("data") or {}
            prev = _f(data.get("preKPrice"))
            pts = []
            cum_v = 0.0
            cum_a = 0.0
            for line in (data.get("klines") or []):
                p = line.split(",")
                if len(p) < 6 or not p[0].startswith(iso):
                    continue
                close = _f(p[2])
                vol = _f(p[5]) or 0.0
                amt = _f(p[6]) if len(p) > 6 else None
                cum_v += vol
                cum_a += amt or 0.0
                avg = round(cum_a / (cum_v * 100), 3) if cum_v > 0 and cum_a > 0 else None
                pts.append({"hm": p[0][-5:], "p": close, "avg": avg, "v": vol})
            if pts:
                out = {"code": code, "date": iso, "granularity": "5m",
                       "prev_close": prev, "points": pts, "source": "eastmoney"}
            else:
                _em_trend_trip()
        except Exception:
            _em_trend_trip()
            out = None

    # ---- 3) 腾讯 5 分钟线兜底（东财掐连接时的备用源）----
    if not out:
        try:
            t = tx_m5_day(code, iso)
            if t.get("points"):
                out = t
        except Exception:
            out = None

    if not out:
        return {"code": code, "date": iso, "prev_close": None,
                "granularity": None, "points": []}

    # 历史日的数据不会再变，落盘缓存
    if frozen:
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
        except Exception:
            pass
    return out


def _intraday_raw(code: str, prev_close: float | None) -> dict:
    # 统一走 trend_points，东财挂了会自动落到腾讯，不用在这里再写一遍取数
    try:
        tp = trend_points(code, 1)
    except Exception:
        tp = {}
    pts = [{"t": p.get("t"), "hm": p["hm"], "price": p["p"], "vol": p.get("v") or 0}
           for p in (tp.get("points") or [])]
    if prev_close is None:
        prev_close = tp.get("prev_close")
    if len(pts) < 10:
        return {}
    prices = [p["price"] for p in pts]
    total_vol = sum(p["vol"] for p in pts) or 1
    peak_i = max(range(len(pts)), key=lambda i: prices[i])
    low_i = min(range(len(pts)), key=lambda i: prices[i])
    ref = prev_close or prices[0]

    def pct(x):
        return round((x / ref - 1) * 100, 2) if ref else None

    def at(hm: str):
        for p in pts:
            if p["hm"] >= hm:
                return p
        return pts[-1]

    first60 = sum(p["vol"] for p in pts if p["hm"] <= "10:30")
    last30 = sum(p["vol"] for p in pts if p["hm"] >= "14:30")
    o, t10, t1030, t1130, t14, close = at("09:30"), at("10:00"), at("10:30"), at("11:30"), at("14:00"), pts[-1]
    return {
        "prev_close": ref,
        "open_chg": pct(o["price"]),
        "chg_10": pct(t10["price"]),
        "chg_1030": pct(t1030["price"]),
        "chg_1130": pct(t1130["price"]),
        "chg_14": pct(t14["price"]),
        "close_chg": pct(close["price"]),
        "peak_time": pts[peak_i]["hm"], "peak_chg": pct(prices[peak_i]),
        "low_time": pts[low_i]["hm"], "low_chg": pct(prices[low_i]),
        "close_pos": round((close["price"] - prices[low_i]) /
                           (prices[peak_i] - prices[low_i]) * 100, 1)
        if prices[peak_i] > prices[low_i] else None,
        "vol_first60": round(first60 / total_vol * 100, 1),
        "vol_last30": round(last30 / total_vol * 100, 1),
        "points": len(pts),
    }


def intraday(code: str, prev_close: float | None, day: str | None = None) -> dict:
    """当日分时摘要：盘中 60 秒、收盘后永久。

    盘中分时是「必须重取」的那一类，所以 TTL 只给 60s；但同一分钟内
    反复点开同一只票不该重复打上游。
    """
    day = day or _today()
    mem, _ = _ttl_for(day, 60)
    return ttlcache.cached(f"intraday:{code}:{day}", mem,
                           lambda: _intraday_raw(code, prev_close))


# ------------------------------------------------------------------ 日线历史
def _history_raw(code: str, day: str, bars: int = 40) -> dict:
    """近 N 个交易日日线 + 衍生位（近 20 日新高/放量倍数/连涨/区间涨幅）"""
    q = (f"?secid={secid(code)}&klt=101&fqt=1&lmt={bars}&end=20500101"
         f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57")
    d = json.loads(_get("https://push2his.eastmoney.com/api/qt/stock/kline/get" + q))
    rows = []
    for line in ((d.get("data") or {}).get("klines") or []):
        p = line.split(",")
        if len(p) < 6:
            continue
        rows.append({"date": p[0], "open": _f(p[1]), "close": _f(p[2]),
                     "high": _f(p[3]), "low": _f(p[4]), "volume": _f(p[5])})
    rows = [r for r in rows if r["date"] <= day]
    if len(rows) < 6:
        return {}
    today, prev = rows[-1], rows[:-1]
    v5 = sum(r["volume"] or 0 for r in prev[-5:]) / max(1, len(prev[-5:]))
    c20 = prev[-19:]
    closes = [r["close"] for r in rows]
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] and closes[i - 1] and closes[i] > closes[i - 1]:
            streak += 1
        else:
            break
    daily = [round((closes[i] / closes[i - 1] - 1) * 100, 2)
             for i in range(1, len(closes)) if closes[i] and closes[i - 1]]
    return {
        "bars": len(rows),
        "ret5": round((today["close"] / prev[-5]["close"] - 1) * 100, 2) if len(prev) >= 5 else None,
        "ret20": round((today["close"] / prev[-20]["close"] - 1) * 100, 2) if len(prev) >= 20 else None,
        "vol_vs_ma5": round((today["volume"] or 0) / v5, 2) if v5 else None,
        "is_high20": bool(c20) and (today["close"] or 0) >= max((r["close"] or 0) for r in c20),
        "is_low20": bool(c20) and (today["close"] or 0) <= min((r["close"] or 0) for r in c20),
        "max_chg20": max(daily[-20:]) if daily else None,
        "min_chg20": min(daily[-20:]) if daily else None,
        "prev_chg": daily[-1] if daily else None,
        "limit_up_20d": sum(1 for x in daily[-20:] if x >= 9.8),
        "streak_up": streak,
    }


def history(code: str, day: str, bars: int = 40) -> dict:
    """近 N 日日线 + 衍生位：盘中 60 秒（最后一根在变），收盘后永久。

    键里带 day，所以「看历史某天」和「看今天」是两份缓存，互不覆盖。
    """
    mem, disk = _ttl_for(day, 60)
    return ttlcache.cached(f"hist:{code}:{day}:{bars}", mem,
                           lambda: _history_raw(code, day, bars),
                           disk="kline", disk_ttl=disk)


# ------------------------------------------------------------------ 组装
def _screener_row(code: str, day: str) -> dict:
    d = scr.load(day)
    if not d:
        return {}
    for it in d.get("items") or []:
        if it.get("code") == code:
            return it
    return {}


def _sector_snapshot(ind_pt: str | None) -> dict:
    if not ind_pt:
        return {}
    try:
        sectors = json.load(open(os.path.join(DATA_DIR, "snapshot", "sectors.json"),
                                 encoding="utf-8"))
    except Exception:
        return {}
    rows = sectors if isinstance(sectors, list) else sectors.get("items") or []
    for r in rows:
        if r.get("code") == ind_pt or r.get("pt") == ind_pt:
            return {"name": r.get("name"), "chg": r.get("chg"), "stage": r.get("stage"),
                    "rs60": r.get("rs60"), "rank": None}
    return {}


def _live_quote_raw(code: str) -> dict:
    """腾讯实时快照兜底（不在当日异动池时用），字段对齐 screener 行"""
    try:
        import profile
        b = profile.basic(code) or {}
    except Exception:
        return {}
    if not b.get("price"):
        return {}
    prev, hi, lo = b.get("prev_close"), b.get("high"), b.get("low")
    amp = round((hi - lo) / prev * 100, 2) if prev and hi and lo else None
    return {
        "code": code, "name": b.get("name"), "price": b.get("price"), "prev": prev,
        "chg": b.get("chg_pct"), "open": b.get("open"), "high": hi, "low": lo,
        "vol_ratio": b.get("vol_ratio"), "turnover": b.get("turnover"),
        "amplitude": amp, "amount": b.get("amount"),
        "high52": b.get("high52"), "low52": b.get("low52"),
    }


def _live_quote(code: str) -> dict:
    """实时快照 60 秒 TTL：盘口兜底不需要秒级，避免每次「刷新」都白打一次腾讯。"""
    return ttlcache.cached(f"quote:{code}", 60, lambda: _live_quote_raw(code))


def build_case(code: str, day: str | None = None, refresh: bool = False,
               hard: bool = False) -> dict:
    """组装单只个股的当日证据包（含各源可用性标记）。

    2026-11-27：缓存粒度下沉到子调用（见 datasource/ttlcache.py）。

    · 整包文件退化为「快照存档 + 60 秒去抖」，不再决定数据新鲜度——
      以前 09:40 建的包，下午 15:00 打开还是早上的数据。
    · refresh=True 只清「盘中会变」的三类键（intraday / hist / flow），
      公告 6h、新闻 30min、龙虎榜当日一次、盘口 60s 照旧命中；
      于是强制重建从 7 次外呼降到 2~3 次，缓存不再形同虚设。
    · hard=True 才是「真的全部重打」，给排查与人工兜底用（管理端点显式传 hard=1）。
    """
    im = scr.industry_map()
    day = day or (scr.dates() or [datetime.now().strftime("%Y-%m-%d")])[0]
    path = os.path.join(CASE_DIR, day, f"{code}.json")

    if hard:
        # 全清：只在「怀疑缓存本身有问题」时用，正常前端刷新不要走这里
        for p in ("intraday:", "hist:", "flow:", "ann:", "news:", "lhb:", "quote:"):
            ttlcache.drop(p)
    elif refresh:
        # 只清可变键。公告/新闻/龙虎榜刻意不清：它们一天只变几次，
        # 清掉就等于把「刷新」退化成「全量重打」。
        for p in ("intraday:", "hist:", "flow:"):
            ttlcache.drop(p)
    elif os.path.exists(path):
        # 60 秒内重复打开同一只票：复用刚组装好的整包，避免高频点开时反复拼装。
        # 超过 60 秒就重新组装——各子调用自己会命中缓存，只重算分数，不打上游。
        try:
            if time.time() - os.path.getmtime(path) < CASE_DEBOUNCE:
                cached = json.load(open(path, encoding="utf-8"))
                if cached.get("schema") == 1:
                    cached["cache"] = "debounce"
                    return cached
        except Exception:
            pass

    row = _screener_row(code, day)
    info = im.get(code) or {}
    # 不在当日异动池（例如从自选/持仓点进来）时，盘口退回实时快照
    quote_source = "screener"
    if not row:
        row = _live_quote(code)
        quote_source = "live" if day == datetime.now().strftime("%Y-%m-%d") else "live_stale"
    name = row.get("name") or info.get("name") or code
    prev = row.get("prev")
    status: dict[str, bool] = {}
    case: dict = {
        "schema": 1, "code": code, "name": name, "date": day,
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "quote_source": quote_source,
    }

    # 盘口（本地 screener，缺则留空）
    case["quote"] = {k: row.get(k) for k in
                     ("price", "prev", "chg", "open", "high", "low", "vol_ratio",
                      "turnover", "amplitude", "amount", "high52", "low52")}
    case["tag"] = row.get("tag")
    case["extra"] = row.get("extra") or []

    # 涨停池
    try:
        zt = zt_pool(day).get(code) or {}
        status["zt"] = bool(zt)
        if zt:
            zt = dict(zt, fbt=_hhmmss(zt.get("fbt")), lbt=_hhmmss(zt.get("lbt")))
        case["limit_up"] = zt
    except Exception:
        status["zt"] = False
        case["limit_up"] = {}

    # 技术位置（来自行业成分股快照）
    case["tech"] = {k: info.get(k) for k in
                    ("stage", "sub", "rs60", "dist52", "pivot", "stop", "sig", "sig_date")}
    case["industry"] = info.get("industry") or row.get("industry")
    case["ind_pt"] = info.get("ind_pt") or row.get("ind_pt")
    case["sector"] = _sector_snapshot(case["ind_pt"])

    # 分时
    try:
        case["intraday"] = intraday(code, prev, day)
        status["intraday"] = bool(case["intraday"])
    except Exception:
        case["intraday"], status["intraday"] = {}, False

    # 日线历史（近 20 日新高/放量倍数/连涨）
    try:
        case["hist"] = history(code, day)
        status["hist"] = bool(case["hist"])
    except Exception:
        case["hist"], status["hist"] = {}, False

    # 资金流
    try:
        case["flow"] = fund_flow(code, day=day)
        status["flow"] = bool(case["flow"])
    except Exception:
        case["flow"], status["flow"] = {}, False

    # 龙虎榜
    try:
        case["dragon"] = dragon_tiger(code, day)
        status["dragon"] = bool(case["dragon"].get("on_board"))
    except Exception:
        case["dragon"], status["dragon"] = {}, False

    # 公告（只保留近 N 日）
    try:
        anns = announcements(code)
        case["notices"] = [a for a in anns if a["date"] >= _shift(day, -12)]
        status["notice"] = bool(case["notices"])
    except Exception:
        case["notices"], status["notice"] = [], False

    # 新闻
    try:
        nw = news(code, name)
        case["news"] = [n for n in nw if n["date"][:10] >= _shift(day, -5)]
        status["news"] = bool(case["news"])
    except Exception:
        case["news"], status["news"] = [], False

    # 板块内同向异动（同日 screener 里同行业其它票）
    try:
        sc = scr.load(day) or {}
        peers = [x for x in (sc.get("items") or [])
                 if x.get("ind_pt") == case["ind_pt"] and x.get("code") != code]
        peers.sort(key=lambda x: -(x.get("chg") or 0))
        case["peers"] = [{"code": x["code"], "name": x.get("name"), "chg": x.get("chg"),
                          "vol_ratio": x.get("vol_ratio"), "tag": x.get("tag"),
                          "limit": "涨停" in (x.get("extra") or [])} for x in peers[:12]]
        case["peer_total"] = len(peers) + 1
        case["peer_limit"] = sum(1 for x in peers if "涨停" in (x.get("extra") or [])) \
            + (1 if "涨停" in case["extra"] else 0)
    except Exception:
        case["peers"], case["peer_total"], case["peer_limit"] = [], 0, 0

    case["source_status"] = status
    _dump(path, case)
    return case


def _shift(day: str, delta_days: int) -> str:
    from datetime import timedelta
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=delta_days)).strftime("%Y-%m-%d")


def case_dates() -> list[str]:
    if not os.path.isdir(CASE_DIR):
        return []
    return sorted((d for d in os.listdir(CASE_DIR)
                   if os.path.isdir(os.path.join(CASE_DIR, d))), reverse=True)


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "sz000995"
    c = build_case(code, refresh=True)
    print(json.dumps({k: v for k, v in c.items() if k not in ("peers", "news", "notices")},
                     ensure_ascii=False, indent=1)[:3000])
    print("公告:", [a["title"] for a in c.get("notices", [])][:3])
    print("新闻:", [a["title"][:40] for a in c.get("news", [])][:3])
