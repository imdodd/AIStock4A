"""个股档案：基本信息 + 财务摘要 + 券商研报概述

三类数据全部免鉴权，服务端缓存 24 小时：

    基本信息  https://qt.gtimg.cn/q=sh603198           （GBK 文本，88 字段）
    财务摘要  datacenter-web.eastmoney.com  RPT_F10_FINANCE_MAINFINADATA
    券商研报  reportapi.eastmoney.com/report/list

注意：东财的 push2his 行情域名会按 TLS 指纹拦截 Python，而这两个数据中心域名
用普通 Request 就能取（已实测），所以这里不做 curl 兜底。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(DATA_DIR, "profile")
TTL = 24 * 3600
SCHEMA = 2                     # 档案结构版本；加字段时 +1，旧缓存自动失效

# F10 页面接口（免鉴权，实测 200）。注意 REPORT 用的是 datacenter 域名，
# F10 用的是 emweb 域名，两者不能混。
F10_BASE = "https://emweb.securities.eastmoney.com/PC_HSF10/{page}/PageAjax?code={code}"

# 重大公告关键词：命中即认为需要重建研究档案（业绩、资本运作、监管、股权等）
MAJOR_KW = re.compile(
    "业绩预告|业绩快报|年度报告|半年度报告|季度报告|三季报|年报|中报|季报|"
    "重大合同|重大项目|中标|减持|增持|回购|定增|向特定对象|募集资金|可转债|"
    "并购|重组|收购|出售资产|资产注入|诉讼|仲裁|处罚|监管|立案|问询|关注函|"
    "停牌|复牌|股权激励|权益变动|控制权|实际控制人|解禁|对外投资|关联交易|"
    "风险提示|澄清|终止|变更会计|商誉减值|计提减值|退市|分红|利润分配"
)

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://emweb.securities.eastmoney.com/",
}
QUOTE_URL = "https://qt.gtimg.cn/q="
FIN_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_URL = "https://reportapi.eastmoney.com/report/list"


def _f(v):
    try:
        n = float(v)
        return n
    except (TypeError, ValueError):
        return None


def _get(url: str, gbk: bool = False, retry: int = 1):
    last = None
    for i in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=18) as r:
                raw = r.read()
            return raw.decode("gbk", "ignore") if gbk else raw.decode("utf-8", "ignore")
        except Exception as e:
            last = e
            if i < retry:
                time.sleep(0.6)
    raise last  # type: ignore[misc]


# ------------------------------------------------------------------ 基本信息
def basic(code: str) -> dict:
    """实时快照：价格 / PE / PB / 总市值 / 流通市值 / 52 周高低 / 换手"""
    text = _get(QUOTE_URL + code, gbk=True)
    if "=" not in text:
        return {}
    body = text.split("=", 1)[1].strip().strip(';').strip('"')
    f = body.split("~")
    if len(f) < 50:
        return {}

    def g(i):
        return _f(f[i]) if i < len(f) else None

    price, prev = g(3), g(4)
    hi52, lo52 = g(67), g(68)
    pos52 = None
    if price and hi52 and lo52 and hi52 > lo52:
        pos52 = round((price - lo52) / (hi52 - lo52) * 100, 1)   # 52 周区间位置（0=最低 100=最高）
    return dict(
        code=code, name=f[1], price=price, prev_close=prev,
        chg=g(31), chg_pct=g(32), open=g(5), high=g(33), low=g(34),
        volume=g(36), amount=g(37), turnover=g(38),
        pe_ttm=g(39), pe_static=g(62), pb=g(46),
        float_mktcap=g(44), total_mktcap=g(45),
        limit_up=g(47), limit_down=g(48), vol_ratio=g(49), avg_price=g(51),
        high52=hi52, low52=lo52, pos52=pos52,
        dividend_yield=g(64), shares=g(72),
        time=f[30] if len(f) > 30 else None,
    )


# ------------------------------------------------------------------ 财务摘要
def finance(code: str, limit: int = 8) -> dict:
    """最近若干报告期的主要财务指标 + 趋势判断"""
    num = code[2:]
    suffix = "SH" if code.startswith("sh") else ("SZ" if code.startswith("sz") else "BJ")
    q = urllib.parse.urlencode({
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": ("SECUCODE,SECURITY_NAME_ABBR,REPORT_DATE,REPORT_TYPE,REPORT_DATE_NAME,"
                    "EPSJB,BPS,TOTALOPERATEREVE,PARENTNETPROFIT,TOTALOPERATEREVETZ,"
                    "PARENTNETPROFITTZ,KCFJCXSYJLR,ROEJQ,XSMLL,MGJYXJJE"),
        "filter": f'(SECUCODE="{num}.{suffix}")',
        "pageNumber": 1, "pageSize": limit,
        "sortColumns": "REPORT_DATE", "sortTypes": -1,
        "source": "HSF10", "client": "PC",
    })
    d = json.loads(_get(FIN_URL + "?" + q))
    rows = ((d.get("result") or {}).get("data")) or []
    periods = []
    for r in rows:
        periods.append(dict(
            report_date=(r.get("REPORT_DATE") or "")[:10],
            period=r.get("REPORT_DATE_NAME") or r.get("REPORT_TYPE"),
            revenue=(round(r["TOTALOPERATEREVE"] / 1e8, 2) if r.get("TOTALOPERATEREVE") else None),
            revenue_yoy=_f(r.get("TOTALOPERATEREVETZ")),
            profit=(round(r["PARENTNETPROFIT"] / 1e8, 2) if r.get("PARENTNETPROFIT") else None),
            profit_yoy=_f(r.get("PARENTNETPROFITTZ")),
            deducted=(round(r["KCFJCXSYJLR"] / 1e8, 2) if r.get("KCFJCXSYJLR") else None),
            roe=_f(r.get("ROEJQ")),
            eps=_f(r.get("EPSJB")),
            bps=_f(r.get("BPS")),
            margin=_f(r.get("XSMLL")),          # 销售毛利率（%）
            ocf_ps=_f(r.get("MGJYXJJE")),
        ))
    out = dict(periods=periods, name=(rows[0].get("SECURITY_NAME_ABBR") if rows else None))
    if periods:
        p0 = periods[0]
        out["latest"] = p0
        # 连续两期增速方向：判断景气是加速还是减速
        if len(periods) >= 2 and p0.get("revenue_yoy") is not None and periods[1].get("revenue_yoy") is not None:
            diff = p0["revenue_yoy"] - periods[1]["revenue_yoy"]
            out["revenue_trend"] = "加速" if diff > 1 else ("减速" if diff < -1 else "持平")
            out["revenue_trend_diff"] = round(diff, 2)
        if len(periods) >= 2 and p0.get("profit_yoy") is not None and periods[1].get("profit_yoy") is not None:
            out["profit_trend"] = ("加速" if p0["profit_yoy"] - periods[1]["profit_yoy"] > 1
                                   else "减速" if p0["profit_yoy"] - periods[1]["profit_yoy"] < -1 else "持平")
    return out


# ------------------------------------------------------------------ 券商研报
def reports(code: str, months: int = 6, limit: int = 30) -> dict:
    """近 N 个月个股研报：评级分布 / 目标价 / 一致预期 / 共识与分歧"""
    from datetime import date, timedelta
    end = date.today()
    begin = end - timedelta(days=months * 31)
    q = urllib.parse.urlencode({
        "industryCode": "*", "pageSize": limit, "industry": "*", "rating": "",
        "ratingChange": "", "beginTime": begin.isoformat(), "endTime": end.isoformat(),
        "pageNo": 1, "fields": "", "qType": 0, "orgCode": "", "code": code[2:],
        "rcode": "", "p": 1, "pageNum": 1,
    })
    d = json.loads(_get(REPORT_URL + "?" + q))
    items = d.get("data") or []
    out_items = []
    ratings, aims, eps_this = {}, [], []
    for it in items:
        rating = it.get("emRatingName") or it.get("sRatingName") or ""
        if rating:
            ratings[rating] = ratings.get(rating, 0) + 1
        aim = None
        for k in ("indvAimPriceT", "indvAimPriceL"):
            v = _f(it.get(k))
            if v:
                aim = v
                break
        if aim:
            aims.append(aim)
        eps = _f(it.get("predictThisYearEps"))
        if eps:
            eps_this.append(eps)
        info = it.get("infoCode")
        out_items.append(dict(
            title=it.get("title"), org=it.get("orgSName") or it.get("orgName"),
            date=(it.get("publishDate") or "")[:10], rating=rating,
            last_rating=it.get("lastEmRatingName") or "",
            rating_change=it.get("ratingChange") or "",
            target=aim,
            eps_this_year=eps, pe_this_year=_f(it.get("predictThisYearPe")),
            eps_next_year=_f(it.get("predictNextYearEps")),
            author=it.get("researcher") or it.get("author") or "",
            # 详情页（摘要+机构观点）与全文 PDF 直链，两者均实测 200 可用
            url=(f"https://data.eastmoney.com/report/zw_stock.jshtml?infocode={info}" if info else None),
            pdf=(f"https://pdf.dfcfw.com/pdf/H3_{info}_1.pdf" if info else None),
        ))
    out = dict(items=out_items, total=d.get("hits") or len(out_items), window_months=months)
    if ratings:
        out["rating_dist"] = ratings
        out["rating_main"] = max(ratings.items(), key=lambda kv: kv[1])[0]
    if aims:
        out["target_avg"] = round(sum(aims) / len(aims), 2)
        out["target_min"], out["target_max"] = min(aims), max(aims)
        out["target_n"] = len(aims)
    if eps_this:
        out["eps_consensus"] = round(sum(eps_this) / len(eps_this), 2)
    return out


_F10_CACHE: dict[str, tuple[float, dict]] = {}


def _f10_code(code: str) -> str:
    """sz002371 → SZ002371（F10 接口用大写市场前缀 + 6 位数字）"""
    c = code.lower()
    if len(c) == 6 and c.isdigit():
        return ("SH" if c.startswith(("6", "9")) else
                "BJ" if c.startswith(("8", "4")) else "SZ") + c
    return c[:2].upper() + c[2:]


def _f10(page: str, code: str, ttl: int = 600) -> dict:
    """F10 PageAjax 取数，进程内短缓存（一只股票一次生成会连带取多个块）"""
    key = f"{page}:{code}"
    hit = _F10_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    url = F10_BASE.format(page=page, code=_f10_code(code))
    try:
        d = json.loads(_get(url)) or {}
    except Exception:
        d = {}
    _F10_CACHE[key] = (time.time(), d)
    return d


def _ops(code: str) -> dict:
    return _f10("OperationsRequired", code)


def _pct(v) -> float | None:
    """东财占比字段是 0~1 的小数，统一换成百分数并保留 2 位"""
    n = _f(v)
    return round(n * 100, 2) if n is not None else None


def business_analysis(code: str) -> dict:
    """主营构成分析：分产品营收占比 + 分部毛利率，按报告期留最近 8 期。

    zygcfx 里 MAINOP_TYPE=1/2 内容重复（按行业/按产品），3 是分地区；
    为免重复占用证据包，只取 2 作为产品口径、3 作为地区口径。
    """
    d = _f10("BusinessAnalysis", code)
    raw = d.get("zygcfx") or []
    by_date: dict[str, dict[str, list[dict]]] = {}
    for r in raw:
        dt = (r.get("REPORT_DATE") or "")[:10]
        typ = str(r.get("MAINOP_TYPE") or "")
        if not dt or typ not in ("2", "3"):
            continue
        bucket = "product" if typ == "2" else "region"
        rev = _f(r.get("MAIN_BUSINESS_INCOME"))
        row = dict(
            name=r.get("ITEM_NAME"),
            revenue_yi=round(rev / 1e8, 2) if rev is not None else None,
            ratio_pct=_pct(r.get("MBI_RATIO")),
            gross_margin_pct=_pct(r.get("GROSS_RPOFIT_RATIO")),
        )
        by_date.setdefault(dt, {}).setdefault(bucket, []).append(row)
    if not by_date:                       # 少数公司只报 type=1，退回按行业口径
        for r in raw:
            dt = (r.get("REPORT_DATE") or "")[:10]
            typ = str(r.get("MAINOP_TYPE") or "")
            if not dt or typ != "1":
                continue
            rev = _f(r.get("MAIN_BUSINESS_INCOME"))
            by_date.setdefault(dt, {}).setdefault("product", []).append(dict(
                name=r.get("ITEM_NAME"),
                revenue_yi=round((rev or 0) / 1e8, 2),
                ratio_pct=_pct(r.get("MBI_RATIO")),
                gross_margin_pct=_pct(r.get("GROSS_RPOFIT_RATIO"))))
    periods = []
    for dt in sorted(by_date, reverse=True)[:8]:
        b = by_date[dt]
        for k in ("product", "region"):
            b.get(k, []).sort(key=lambda x: x.get("ratio_pct") or 0, reverse=True)
        periods.append(dict(report_date=dt, **b))
    scope = ((d.get("zyfw") or [{}])[0].get("BUSINESS_SCOPE") or "").strip()
    review = (d.get("jyps") or [{}])[0]
    top = periods[0].get("product", []) if periods else []
    return dict(
        scope=scope[:1800],
        review_date=(review.get("REPORT_DATE") or "")[:10],
        review=(review.get("BUSINESS_REVIEW") or "").strip()[:3000],
        periods=periods,
        latest_report=periods[0]["report_date"] if periods else None,
        latest_product=top,
        concentration=round(sum((x.get("ratio_pct") or 0) for x in top[:1]), 2),
    )


def company(code: str) -> dict:
    """公司简况：经营范围 / 公司简介 / 东财行业链"""
    d = _f10("CompanySurvey", code)
    j = (d.get("jbzl") or [{}])[0]
    keys = ("ORG_NAME", "ORG_NAME_EN", "FORMERNAME", "EM2016", "INDUSTRYCSRC1",
            "SECURITY_TYPE", "TRADE_MARKET", "LISTING_DATE", "BUSINESS_SCOPE",
            "ORG_PROFILE", "ORG_WEB", "REG_CAPITAL", "PROVINCE", "CHAIRMAN")
    out = {}
    for k in keys:
        v = j.get(k)
        if v is not None:
            out[k.lower()] = v.strip()[:2200] if isinstance(v, str) else v
    return out


def consensus(code: str) -> dict:
    """多家投研机构的目标价与盈利预测明细（三档情景估值的输入）。

    ybzy = 研报摘要，含 source（机构）、aim_price（目标价）、publish_time、评级；
    jgyc = 机构预测，含 ORG_NAME_ABBR、EPS2/PE2（次年预测）等，另有「近六月平均」行。
    同一机构多条取最新一条，避免老研报把目标价分布拉偏。
    """
    d = _ops(code)
    seen: dict[str, dict] = {}
    for r in d.get("ybzy") or []:
        price = _f(r.get("aim_price"))
        if not price or price <= 0:
            continue
        org = (r.get("source") or "").strip() or "未署名机构"
        dt = (r.get("publish_time") or "")[:10]
        row = dict(org=org, date=dt, price=round(price, 2),
                   rating=r.get("em_rating_name") or r.get("s_rating_name") or "",
                   title=(r.get("title") or "").strip()[:120])
        if org not in seen or dt > seen[org]["date"]:
            seen[org] = row
    targets = sorted(seen.values(), key=lambda x: x["price"])
    prices = [x["price"] for x in targets]

    forecasts = []
    avg_row = None
    for r in d.get("jgyc") or []:
        org = (r.get("ORG_NAME_ABBR") or "").strip()
        row = dict(org=org, date=(r.get("PUBLISH_DATE") or "")[:10],
                   eps_y2=_f(r.get("EPS2")), pe_y2=_f(r.get("PE2")),
                   eps_y3=_f(r.get("EPS3")), pe_y3=_f(r.get("PE3")),
                   year2=r.get("YEAR2"), year3=r.get("YEAR3"))
        if "平均" in org or not org:
            avg_row = row
        else:
            forecasts.append(row)
    n = len(prices)
    return dict(
        targets=targets, target_n=n,
        target_min=prices[0] if n else None,
        target_max=prices[-1] if n else None,
        target_avg=round(sum(prices) / n, 2) if n else None,
        target_median=(prices[n // 2] if n % 2 else
                       round((prices[n // 2 - 1] + prices[n // 2]) / 2, 2)) if n else None,
        forecasts=forecasts[:20], consensus_eps=avg_row,
        note=("研报目标价为各机构原文值，未做平台加工；机构数少于 3 家时三档情景参考价值有限"
              if n < 3 else "目标价按机构去重后取最新一条"),
    )


def notices(code: str, limit: int = 12) -> dict:
    """最新公告 + 重大公告指纹。指纹变化 → research.py 强制重建档案。"""
    d = _ops(code)
    items = []
    for r in (d.get("zxgg") or [])[:limit]:
        title = (r.get("title") or "").strip()
        dt = (r.get("notice_date") or r.get("display_time") or "")[:10]
        if not title:
            continue
        items.append(dict(date=dt, title=title[:160], art_code=r.get("art_code"),
                          major=bool(MAJOR_KW.search(title))))
    major = [f"{x['date']}|{x['title']}" for x in items if x["major"]]
    sig = "|".join(major[:8])
    return dict(items=items,
                major_count=len(major),
                major_hash=hashlib.md5(sig.encode("utf-8")).hexdigest()[:12] if sig else "",
                latest_date=items[0]["date"] if items else None)


def quotes_many(codes: list[str]) -> dict[str, dict]:
    """腾讯批量快照：一次取多只的 PE/PB/市值，供同业对比，避免逐只请求"""
    if not codes:
        return {}
    try:
        text = _get(QUOTE_URL + ",".join(codes), gbk=True)
    except Exception:
        return {}
    out = {}
    for c, body in re.findall(r'v_([a-z]{2}\d{6})="([^"]*)"', text):
        f = body.split("~")
        if len(f) < 47:
            continue
        out[c] = dict(code=c, name=f[1], price=_f(f[3]), chg_pct=_f(f[32]),
                      pe_ttm=_f(f[39]), pb=_f(f[46]),
                      total_mktcap=_f(f[45]), float_mktcap=_f(f[44]))
    return out


def peers(code: str, limit: int = 5) -> dict:
    """同申万二级行业的可比公司：从 members 快照取同业，按总市值选最接近的几家。"""
    members_dir = os.path.join(DATA_DIR, "snapshot", "members")
    sector, candidates = "", []
    if os.path.isdir(members_dir):
        for fn in sorted(os.listdir(members_dir)):
            if not fn.startswith("pt") or not fn.endswith(".json"):
                continue
            try:
                d = json.load(open(os.path.join(members_dir, fn), encoding="utf-8"))
            except Exception:
                continue
            ms = d.get("members") or []
            if any(m.get("code") == code for m in ms):
                sector = d.get("name") or sector
                candidates = [m for m in ms if m.get("code") != code]
                break
    if not candidates:
        return dict(sector=sector, items=[])
    candidates.sort(key=lambda m: m.get("rs60") or -999, reverse=True)
    pool = [m["code"] for m in candidates[:15]]
    q = quotes_many(pool)
    name_of = {m["code"]: m.get("name") for m in candidates[:15]}
    mine = (quotes_many([code]).get(code) or {}).get("total_mktcap")
    rows = []
    for c in pool:
        x = q.get(c)
        if not x or not x.get("total_mktcap"):
            continue
        rows.append(dict(code=c, name=name_of.get(c) or x.get("name"),
                         price=x.get("price"), chg_pct=x.get("chg_pct"),
                         pe_ttm=x.get("pe_ttm"), pb=x.get("pb"),
                         total_mktcap=x.get("total_mktcap")))
    if mine:
        rows.sort(key=lambda r: abs((r["total_mktcap"] or 0) - mine))
    else:
        rows.sort(key=lambda r: -(r["total_mktcap"] or 0))
    return dict(sector=sector, items=rows[:limit], basis="同行业按总市值接近度选取")


# ------------------------------------------------------------------ 合并 + 缓存
def _cache_path(code: str) -> str:
    # code 可能来自 HTTP 路径参数，拒绝 ..、斜杠等路径穿越输入
    from data_paths import safe_slug
    return os.path.join(DIR, f"{safe_slug(code, '股票代码')}.json")


def profile(code: str, refresh: bool = False) -> dict:
    """八块合一。缓存 24 小时；某块失败不影响其它块（分块记录 error）。

    SCHEMA 变化时旧缓存自动失效，避免新增的 business/consensus 等块长期为空。
    """
    p = _cache_path(code)
    if not refresh and os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                cached = json.load(f)
            if (time.time() - cached.get("_ts", 0) < TTL
                    and cached.get("_schema") == SCHEMA):
                return cached
        except Exception:
            pass

    out: dict = dict(code=code, fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                     _schema=SCHEMA)
    for key, fn in (("basic", basic), ("finance", finance), ("reports", reports),
                    ("business", business_analysis), ("company", company),
                    ("consensus", consensus), ("notices", notices), ("peers", peers)):
        try:
            out[key] = fn(code)
        except Exception as e:
            out[key] = {}
            out.setdefault("errors", {})[key] = f"{type(e).__name__}: {e}"
    if out.get("basic", {}).get("name"):
        out["name"] = out["basic"]["name"]
    elif out.get("finance", {}).get("name"):
        out["name"] = out["finance"]["name"]
    elif out.get("company", {}).get("org_name"):
        out["name"] = out["company"]["org_name"]
    out["_ts"] = time.time()
    try:
        os.makedirs(DIR, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import sys
    c = sys.argv[1] if len(sys.argv) > 1 else "sh603198"
    d = profile(c, refresh=True)
    print(json.dumps(d, ensure_ascii=False, indent=1)[:3000])
