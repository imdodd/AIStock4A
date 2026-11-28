"""腾讯公开行情数据源（无需鉴权，脚本可直连）

背景：westock-mcp 是 OAuth 2.1 保护的 MCP 端点，脚本侧必须先完成浏览器授权才能取数；
而腾讯财经的公开行情接口没有任何鉴权要求，且数据口径与 westock 基本一致
（抽样核对：渔业 6/6、白酒 19/19、证券 48/48 完全一致）。

因此把「行业成分股 + 个股日 K 线」这两类主力数据切到本数据源，
每日自动抓取不再依赖人工授权。

端点：
    板块成分  https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?board_code=pt01801081
    日 K 线   https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz002897,day,,,420,qfq
    实时行情  https://qt.gtimg.cn/q=sz002897   （GBK 文本）

K 线行格式（注意字段顺序）：[date, open, close, high, low, volume]
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .base import DataSource
from . import nethttp

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BOARD_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
QUOTE_URL = "https://qt.gtimg.cn/q="
# 情绪兜底（腾讯无全市场涨跌家数，借东财公开接口）
EM_FENBU_URL = ("https://push2ex.eastmoney.com/getTopicZDFenBu"
                "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt")
EM_ZTPOOL_URL = ("https://push2ex.eastmoney.com/getTopicZTPool"
                 "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
                 "&Pageindex=0&pagesize=1&sort=fbt:asc")
SINA_KLINE_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php"
                  "/var/CN_MarketDataService.getKLineData")

# 单个行业成分股一次取多少（接口实测上限 200，超过会返回错误）
BOARD_PAGE = 200

# ---- 全局限流控制 ----
# 教训：12 线程约 60 req/s 跑 4000+ 只后，腾讯侧直接回 501 Not Implemented 把整个 IP 掐掉，
# 之后连正常股票都取不到，得等十几分钟。所以这里做两件事：
#   1) 限速：任意两次请求之间至少间隔 MIN_INTERVAL 秒（全进程串行令牌）
#   2) 熔断：一旦出现 501/429，暂停 BLOCK_COOLDOWN 秒再继续，而不是继续撞墙
WORKERS = 6
MIN_INTERVAL = 0.05          # 秒/请求，约 20 req/s 上限
BLOCK_COOLDOWN = 45.0        # 被限流后的冷却秒数
_flow_lock = threading.Lock()
_last_req = [0.0]
_blocked_until = [0.0]


def _wait_turn():
    """全局限速 + 熔断等待"""
    while True:
        with _flow_lock:
            now = time.time()
            if now < _blocked_until[0]:
                wait = _blocked_until[0] - now
            else:
                gap = _last_req[0] + MIN_INTERVAL - now
                if gap > 0:
                    _last_req[0] = now + gap
                    time.sleep(gap)
                    return
                _last_req[0] = now
                return
        time.sleep(min(wait, 5.0))


def _trip_breaker(seconds: float = BLOCK_COOLDOWN):
    with _flow_lock:
        now = time.time()
        # 只在未被熔断时起算，避免并发的多个 501 把截止时间无限往后推
        if _blocked_until[0] < now:
            _blocked_until[0] = now + seconds


# 腾讯 K 线被掐 IP 后的整批降级开关：到期时间戳。
# 501 是 IP 级封禁（通常十几分钟），此时再对每只股票发一次腾讯请求毫无意义，
# 反而会不断刷新冷却时间。第一次 501 后 10 分钟内直接走新浪兜底。
_sina_only_until = [0.0]
_sina_lock = threading.Lock()

# 最近一次 fetch_kline 的诊断：整批取不到时给调用方打印真实原因，
# 而不是让上层写死"多为接口限流"（DNS 失败 / 网络断连 / 空数据都不同）。
# 结构：{"tx": 原因或None, "sina": 原因或None, "sina_only": bool}
last_kline_diag: dict = {"tx": None, "sina": None, "sina_only": False}


def _err_text(e) -> str:
    """把异常压成一行可读原因，带 HTTP 状态码 / DNS 关键字。"""
    if e is None:
        return ""
    s = f"{type(e).__name__}: {e}"
    if isinstance(e, urllib.error.HTTPError):
        s = f"HTTP {e.code} {e.reason}"
    return s[:160]


def _mark_sina_only(seconds: float = 600):
    with _sina_lock:
        _sina_only_until[0] = max(_sina_only_until[0], time.time() + seconds)


def _get_json(url: str, retry: int = 2) -> dict:
    """取腾讯 JSON：统一走 nethttp（限速 + host 熔断 + 连接层不重试）。

    2026-11-27 P0：限速从本模块的 _wait_turn（0.05s 进程内串行令牌）换成
    nethttp 的按 host 令牌桶（qt.gtimg.cn 20/s、突发 10），语义等价但线程安全
    且全站统一。501/429 时 nethttp 直接给该 host 记 600s 熔断，这里同步打开
    新浪降级开关，避免继续对腾讯发请求。
    """
    try:
        return nethttp.http_json(url, timeout=20, retry=retry, headers=UA)
    except nethttp.CircuitOpen:
        _mark_sina_only()
        raise
    except urllib.error.HTTPError as e:
        if e.code in (429, 501, 503):
            _trip_breaker()
            _mark_sina_only()
        raise


class TencentSource(DataSource):
    name = "tencent-public"

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def _log(self, *a):
        if self.verbose:
            print(*a)

    # ------------------------------------------------------------ 行业成分股
    def sector_members(self, ptcode: str) -> list[dict]:
        """申万二级行业成分股（pt 代码直接用）

        返回 [{code, name, price, chg, mktcap, pe, turnover}]，按总市值降序。
        """
        q = urllib.parse.urlencode({
            "board_code": ptcode, "sort_type": "price", "direct": "down",
            "offset": 0, "count": BOARD_PAGE,
        })
        d = _get_json(BOARD_URL + "?" + q)
        lst = ((d.get("data") or {}).get("rank_list")) or []
        out = []
        for x in lst:
            code = x.get("code")
            if not code:
                continue
            out.append({
                "code": code,
                "name": x.get("name") or code,
                "price": _f(x.get("zxj")),
                "chg": _f(x.get("zdf")),
                "mktcap": _f(x.get("zsz")),      # 总市值（亿元）
                "pe": _f(x.get("pe_ttm")),
            })
        return out

    # ------------------------------------------------------------ 日 K 线
    def fetch_kline(self, codes, limit: int = 420, end=None, period: str = "day",
                    fq: str = "qfq") -> dict[str, list[dict]]:
        """并发抓 K 线；单只约 0.1~0.3 秒，60 只约 1 秒。

        腾讯被限流（501）或某只取不到时，自动落到新浪日 K 兜底（不复权）。
        复权口径差异只影响除权除息日附近，对阶段判定影响可忽略。
        """
        codes = [c for c in dict.fromkeys(codes) if c]
        out: dict[str, list[dict]] = {}
        tx_err: list[str] = []
        sina_err: list[str] = []

        def one(code: str):
            bars = None
            if time.time() >= _sina_only_until[0]:
                fqflag = fq or ""
                param = f"{code},{period},,,{limit},{fqflag}".rstrip(",")
                url = f"{KLINE_URL}?param={param}"
                try:
                    d = _get_json(url, retry=1)
                    node = (d.get("data") or {}).get(code)
                    if node:
                        arr = node.get("qfqday") or node.get("day") or node.get("hfqday") or []
                        bars = _bars(arr)
                    if not bars:
                        tx_err.append(f"{code}:空数据")
                except urllib.error.HTTPError as e:
                    tx_err.append(_err_text(e))
                    if e.code in (429, 501, 503):
                        _mark_sina_only()
                except Exception as e:
                    tx_err.append(_err_text(e))
                    bars = None
            if not bars:                     # 腾讯限流/无数据 → 新浪兜底
                try:
                    bars = _sina_kline(code, limit)
                    if not bars:
                        sina_err.append(f"{code}:空数据")
                except Exception as e:
                    sina_err.append(_err_text(e))
            return code, bars

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for code, bars in ex.map(one, codes):
                if bars:
                    out[code] = bars
        sina_only = time.time() < _sina_only_until[0]
        last_kline_diag.clear()
        last_kline_diag.update({
            "tx": tx_err[0] if tx_err else None,
            "sina": sina_err[0] if sina_err else None,
            "sina_only": sina_only,
        })
        src = "新浪兜底" if sina_only else "腾讯"
        self._log(f"  [日K·{src}] 请求 {len(codes)} 只，成功 {len(out)} 只")
        return out

    # ------------------------------------------------------------ 实时行情
    def quotes(self, codes) -> dict[str, dict]:
        """轻量实时行情：现价 / 涨跌幅 / 名称（GBK 文本，一次可带几十个代码）"""
        codes = [c for c in dict.fromkeys(codes) if c]
        out: dict[str, dict] = {}
        for i in range(0, len(codes), 50):
            chunk = codes[i:i + 50]
            try:
                text = nethttp.http_bytes(QUOTE_URL + ",".join(chunk), timeout=15,
                                          headers=UA).decode("gbk", "ignore")
            except Exception:
                continue
            for line in text.split(";"):
                line = line.strip()
                if not line.startswith("v_") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                code = key[2:]
                f = val.strip('"').split("~")
                if len(f) < 33:
                    continue
                out[code] = {
                    "name": f[1], "price": _f(f[3]), "prev": _f(f[4]),
                    "chg": _f(f[32]) if len(f) > 32 else None,
                    "high": _f(f[33]) if len(f) > 33 else None,
                    "low": _f(f[34]) if len(f) > 34 else None,
                    "volume": _f(f[36]) if len(f) > 36 else None,
                }
        return out

    # ------------------------------------------------------------ 实时快照（异动筛选用，字段更全）
    def snapshots(self, codes) -> dict[str, dict]:
        """扩展实时快照：含换手率/成交额/振幅/量比/行情时间戳（GBK 字段 30/37/38/43/49）"""
        codes = [c for c in dict.fromkeys(codes) if c]
        out: dict[str, dict] = {}
        for i in range(0, len(codes), 60):
            chunk = codes[i:i + 60]
            try:
                text = nethttp.http_bytes(QUOTE_URL + ",".join(chunk), timeout=15,
                                          headers=UA).decode("gbk", "ignore")
            except Exception:
                continue
            for line in text.split(";"):
                line = line.strip()
                if not line.startswith("v_") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                code = key[2:]
                f = val.strip('"').split("~")
                if len(f) < 50 or not f[3]:
                    continue
                ts = f[30] if len(f) > 30 else ""          # yyyymmddhhmmss
                qdate = None
                if len(ts) >= 8 and ts[:8].isdigit():
                    qdate = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
                out[code] = {
                    "name": f[1], "price": _f(f[3]), "prev": _f(f[4]),
                    "open": _f(f[5]) if len(f) > 5 else None,        # 今开
                    "chg": _f(f[32]) if len(f) > 32 else None,
                    "high": _f(f[33]) if len(f) > 33 else None,
                    "low": _f(f[34]) if len(f) > 34 else None,
                    "volume": _f(f[36]) if len(f) > 36 else None,      # 手
                    "amount": _f(f[37]) if len(f) > 37 else None,      # 万元
                    "turnover": _f(f[38]) if len(f) > 38 else None,    # 换手率 %
                    "amplitude": _f(f[43]) if len(f) > 43 else None,   # 振幅 %
                    "vol_ratio": _f(f[49]) if len(f) > 49 else None,   # 量比
                    "high52": _f(f[67]) if len(f) > 67 else None,      # 52周最高
                    "low52": _f(f[68]) if len(f) > 68 else None,       # 52周最低
                    "quote_date": qdate,                                # 行情实际日期
                }
            if i + 60 < len(codes):
                _wait_turn()
        return out

    # ------------------------------------------------------------ 情绪
    def fetch_sentiment(self) -> dict:
        """市场情绪：腾讯本身不含全市场涨跌家数，改用东财公开涨跌分布 + 涨停池。

        口径：
            getTopicZDFenBu  返回 {key: 家数}，key 为涨跌幅百分比的整数档
                             （1 → 涨 0~1%，11 → 涨停，-1 → 跌 0~1%，-11 → 跌停，0 → 平盘）
            getTopicZTPool   data.tc = 涨停家数
        局限：东财这两个接口只给分布与涨停，不给「N 日新高/新低家数」，
             故 new_high_* / new_low_* 留 None（前端显示 —），不用兜底常量冒充。
        """
        d = self._em_json(EM_FENBU_URL)
        data = (d or {}).get("data") or {}
        fenbu = data.get("fenbu") or []
        if not fenbu:
            raise RuntimeError("东财涨跌分布返回为空")

        cnt = {}
        for row in fenbu:
            for k, v in (row or {}).items():
                try:
                    cnt[int(k)] = int(v)
                except (TypeError, ValueError):
                    continue

        up = sum(v for k, v in cnt.items() if k > 0)
        down = sum(v for k, v in cnt.items() if k < 0)
        flat = cnt.get(0, 0)
        total = up + down + flat
        limit_up = cnt.get(11, 0)
        limit_down = cnt.get(-11, 0)

        # 涨停池 tc 更权威（含 20cm 与连板），与分布档取大值
        zt = None
        try:
            zt = ((self._em_json(EM_ZTPOOL_URL) or {}).get("data") or {}).get("tc")
        except Exception:
            zt = None

        qdate = str(data.get("qdate") or "")
        date = f"{qdate[0:4]}-{qdate[4:6]}-{qdate[6:8]}" if len(qdate) >= 8 else \
            time.strftime("%Y-%m-%d")

        return {
            "date": date,
            "up": up, "down": down, "flat": flat, "total": total,
            "limit_up": max(limit_up, int(zt or 0)),
            "limit_down": limit_down,
            "up_ratio": round(up / total * 100, 2) if total else 0,
            "down_ratio": round(down / total * 100, 2) if total else 0,
            "updown_ratio": round(up / down, 2) if down else 0,
            "new_high_20": None, "new_low_20": None,
            "new_high_250": None, "new_low_250": None,
            "source": "eastmoney getTopicZDFenBu+getTopicZTPool",
        }

    @staticmethod
    def _em_json(url: str) -> dict:
        # 东财接口统一出口：动态代理 + host 熔断 + 连接层不重试
        return nethttp.http_json(url, timeout=12,
                                 headers={**UA, "Referer": "https://quote.eastmoney.com/"})

    def close(self) -> None:
        """无连接池，占位以对齐 DataSource 收尾约定。"""
        return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bars(arr) -> list[dict]:
    """[date, open, close, high, low, volume] → 标准 bar（升序）"""
    out = []
    for row in arr or []:
        if len(row) < 6:
            continue
        try:
            d, o, c, h, l, v = row[0], float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
        except (TypeError, ValueError):
            continue
        if c <= 0:
            continue
        out.append({"date": str(d), "open": o, "high": h, "low": l,
                    "close": c, "volume": v, "amount": None})
    out.sort(key=lambda x: x["date"])
    return out


def _sina_kline(code: str, limit: int) -> list[dict] | None:
    """新浪日 K 兜底（不复权）。腾讯限流时自动启用。

    返回jsonp包裹的数组：var([{day, open, high, low, close, volume}, ...])
    成交量单位是股（腾讯是手），只影响绝对值不影响量比等相对指标。
    """
    n = min(int(limit), 1023)                # 新浪单次上限
    url = f"{SINA_KLINE_URL}?symbol={code}&scale=240&ma=no&datalen={n}"
    try:
        text = nethttp.http_text(url, timeout=15, headers=UA)
        i, j = text.find("["), text.rfind("]")
        if i < 0 or j <= i:
            return None
        arr = json.loads(text[i:j + 1])
        out = []
        for row in arr:
            try:
                c = float(row["close"])
                if c <= 0:
                    continue
                out.append({"date": str(row["day"]), "open": float(row["open"]),
                            "high": float(row["high"]), "low": float(row["low"]),
                            "close": c, "volume": float(row.get("volume") or 0),
                            "amount": None})
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda x: x["date"])
        return out or None
    except Exception:
        return None
