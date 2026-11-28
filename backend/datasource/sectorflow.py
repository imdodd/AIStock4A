"""申万行业资金流：主源东财，备源腾讯，双重本地缓存

数据源（公开接口，无鉴权）：

  主源 东财
    排行    push2.eastmoney.com  clist fs=m:90+t:2（申万一/二/三级行业，496 个）
            fid=f62 按主力净流入排序；f12=板块代码(BKxxxx) f14=名称 f3=涨跌幅
            f62=主力净流入 f184=主力净占比 f66=超大单净额 f72=大单净额
            注：pz 单页上限 100，需翻页
    历史    push2his.eastmoney.com  fflow/daykline secid=90.BKxxxx
            f51 日期 f52 主力 f53 小单 f54 中单 f55 大单 f56 超大单
            f57 主力占比 f61 超大单占比 f62 收盘 f63 涨跌幅
            其中 主力 = 大单 + 超大单

  备源 腾讯（东财不可用时自动切）
    proxy.finance.qq.com  rank/pt/getRank board_type=hy2 —— 正好是本地那 124 个
    申万二级，字段 zljlr=主力净流入(万元) zllr=主力流入 zllc=主力流出
    zljlr_d5 / zljlr_d20=近 5/20 日主力净流入 zdf=涨跌幅 turnover=成交额(万元)
    没有超大/大单拆分与逐日历史，所以只能给「今日 / 近5日 / 近20日」三段摘要，
    前端会把柱图换成 3 根柱子并标注口径。

为什么要有熔断
    东财对高频、无 cookie 的抓取会直接掐连接（RemoteDisconnected / curl rc=56），
    且会从单个接口扩大到整个 push2/push2his 域名，持续几十分钟到数小时。原实现是
    「每次打开大盘页都 7 次翻页 + 每次点板块 1 次历史」，一旦被限流就不断重试、
    越试越久。熔断期内直接用缓存/备源，不再撞墙。

    2026-11-27 P0：熔断与代理判定已上移到 datasource/nethttp.py + netproxy.py，
    按 host 记账并落盘 cache/http_break.json 跨进程共享。本模块只保留
    `upstream_state()`（聚合东财全部 host）与 `_trip()` 两个兼容入口。

本地行业（data/snapshot/sectors.json，申万二级 124 个）里 4 个银行二级
东财命名不带 Ⅱ 后缀，匹配时对两侧名称做归一化（去掉 Ⅰ/Ⅱ 罗马数字后缀）。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlparse

# 2026-11-27 P0：代理判定与 HTTP 出口收敛为全站单一实现。
# 以前每个模块各写一套 urllib/curl/重试/熔断，导致「一处被封、全链路继续撞」。
from datasource import nethttp, netproxy

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/",
}
CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
FFLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TX_RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(DATA_DIR, "cache")
RANK_CACHE = os.path.join(CACHE_DIR, "sectorflow_rank.json")
HIST_CACHE_DIR = os.path.join(CACHE_DIR, "sectorflow_hist")

BREAK_SECONDS = 600          # 上游失败后静默 10 分钟，期间只读缓存
# K 线接口（push2his kline）的封禁形态与行情列表不同：不返回 429，而是 TCP 层
# 直接断连（urllib RemoteDisconnected / curl rc=56 schannel）。这种是 IP 级风控，
# 越重试窗口越长，所以一旦连续 KLINE_STREAK_LIMIT 次命中就直接长静默。
KLINE_STREAK_LIMIT = 3
KLINE_BREAK_SECONDS = 3600   # 60 分钟，宁可少补一天也不要续期封禁
# 判定「被掐连接」的特征串（大小写不敏感）
_TCP_CUT_MARKS = ("schannel", "remotedisconnected", "connection reset",
                  "connection aborted", "rc=56", "rc=35", "rc=52",
                  "eof occurred", "server closed abruptly", "missing close_notify",
                  "without response")
# 熔断状态落盘共享：EOD / 资讯 / 前端是各自独立的 Python 进程，进程内内存熔断
# 互不感知——A 进程撞限流进了熔断，B 进程照样继续打，把整个出口 IP 越撞越死。
# 这里把熔断写到一个共享文件，任何进程 trip 后所有进程都能看到，实现「一处
# 被限、全链路静默」。文件在 cache 目录，随数据目录走；读侧按 mtime 缓存，
# 几乎无额外 IO 开销。
_MEM: dict = {"rank": (0.0, None), "hist": {}, "tx": (0.0, None), "snap": (0.0, None),
              "prefer_curl": False, "last_kline_error": "",
              "kline_streak": 0, "kline_last_ok": 0.0}


# ---------------------------------------------------------------- 基础工具

# 2026-11-26 实测修正（重要）：WorkBuddy/IDE 宿主注入的本机 HTTP(S)_PROXY
# （127.0.0.1:<随机端口>，端口每次会话都变）在本机是访问东财的必经出网通道，不是污染。
# 对照实测（同一时刻、同一 URL）：
#   保留代理 → 东财 urllib 0.40s 成功 / curl 0.37s 成功 / 腾讯正常
#   剥离代理 → 东财 urllib RemoteDisconnected / curl (56) schannel rc56，腾讯仍通
# 先前版本误判本机代理为 schannel 故障源并主动剥离，导致 curl 走直连必挂、_MEM
# 熔断后整链降级腾讯备源——这正是 26 日板块 K 线 124 个全 0 成功的真正原因之一。
# 结论：curl 直接继承宿主环境，不剥任何代理（公网代理同理必须保留，否则生产断网）。
#
# 2026-11-26 二次修正（动态重读）：宿主注入的代理端口会随沙箱会话变化
# （实测 55974 → 52321，旧端口关闭后所有请求恒定 0.1s 报 rc=56，看起来像
# 被上游封禁，实际是打在了死端口上）。而长驻进程的 os.environ 是启动快照，
# 因此代理地址不能只在模块导入时读一次。
#
# 重要：不要靠"扫本机监听端口"猜代理。实测本机 36 个监听端口里，命中率最高
# 的恰是钉钉(62910)/WorkBuddy(62056)/WPS 这些无关进程——把请求发到它们
# 身上比不通更糟（会拿到莫名其妙的响应）。所以只认三个显式来源，并逐个探活：
#   1. ASTOCK_PROXY 环境变量（显式指定，最高优先，生产用这个）
#   2. <DATA_DIR>/cache/proxy.url 文件（可热改，改完无需重启服务）
#   3. 系统 HTTP(S)_PROXY
# 全部探不通就直连（清空代理变量），绝不猜。
_PROXY_PORTS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
                "all_proxy", "ALL_PROXY")
_PROXY_CACHE: dict = {"url": "", "checked": 0.0, "source": ""}
_PROXY_RECHECK = 30.0        # 30s 内复用上次结果，避免每个板块都探活


def _norm_proxy(v: str) -> str:
    """兼容旧名：代理归一化统一走 netproxy。"""
    return netproxy.norm(v)


# 代理判定统一由 datasource/netproxy.py 提供（全站唯一实现，详见该模块注释）。
# 以下保留旧名别名，兼容既有调用方与运维脚本，行为完全一致。
_PROXY_PORTS = netproxy.PROXY_PORTS
_PROXY_CACHE = netproxy._CACHE
_port_alive = netproxy.alive
_proxy_file = netproxy.proxy_file
_proxy_sources = netproxy.sources
_resolve_proxy = netproxy.resolve
_curl_env = netproxy.curl_env
proxy_state = netproxy.state


def _get(url: str, timeout: int = 8, retry: int = 1, headers: dict | None = None) -> dict:
    """取东财 JSON：统一走 datasource/nethttp。

    2026-11-27 P0 改造：原来这里是独立的一套 urllib/curl 双通道 + prefer_curl
    状态机，与档案、研报、腾讯、资讯四个模块的实现互不相通。现在全部收敛到
    nethttp，一次拿到三件事：
      · 动态代理（端口失效自动重探，不必重启进程）
      · 按 host 熔断，落盘跨进程共享（一处被封，全链路静默）
      · 连接层被掐（rc=56 / RemoteDisconnected）永不重试，避免给封禁续期
    """
    return nethttp.http_json(url, timeout=timeout, retry=retry, headers=headers)


def _num(v):
    """东财日K线拆出来的都是字符串（'-' 表示无数据），统一转 float / None。"""
    if isinstance(v, (int, float)):
        return v
    try:
        s = str(v).strip()
        return float(s) if s and s != "-" else None
    except Exception:
        return None


def _norm_name(n: str) -> str:
    return re.sub(r"[ⅠⅡⅢ]+$", "", str(n or "").strip())


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _trip(why: str, seconds: float | None = None,
          host: str = "push2his.eastmoney.com") -> None:
    """上游不可用 → 交给 nethttp 的 host 级熔断（落盘跨进程共享）。

    host 默认取 K 线域名；排行/资金流（push2）调用时显式传自己的域名，
    这样「只封了 K 线」不会连带停掉资金流，反之亦然。
    """
    nethttp.trip(host, why, float(seconds if seconds is not None else BREAK_SECONDS))


def upstream_state() -> dict:
    """东财整体熔断状态（多 host 聚合、跨进程共享）。

    任一东财 host 处于熔断期就算 blocked——它们共用同一个出口 IP，一个域名
    被封时其它域名通常也撑不久，继续打只会加重行为评分。
    """
    st = nethttp.eastmoney_state()
    return {**st, "hits": int(st.get("streak") or 0), "shared": True}


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path: str, obj) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------- 主源：东财

def _em_ranking() -> list[dict]:
    rows: dict[str, dict] = {}
    for pn in range(1, 8):
        q = ("?fid=f62&po=1&pz=100&pn=%d&np=1&fltt=2&invt=2"
             "&fs=m:90+t:2&fields=f12,f14,f3,f62,f184,f66,f72" % pn)
        data = (_get(CLIST_URL + q).get("data") or {})
        diff = data.get("diff") or []
        for x in diff:
            rows[x.get("f14")] = {
                "bk": x.get("f12"), "name": x.get("f14"), "src": "eastmoney",
                "chg": _num(x.get("f3")),
                "main": _num(x.get("f62")),
                "main_pct": _num(x.get("f184")),
                "super": _num(x.get("f66")),
                "big": _num(x.get("f72")),
            }
        if not diff or len(rows) >= (data.get("total") or 0):
            break
        time.sleep(0.25)
    if not rows:
        raise RuntimeError("东财 clist 返回空列表")
    return list(rows.values())


# ---------------------------------------------------------------- 备源：腾讯

# 腾讯 hy2 每页条数。2026-11-27 实测 count=200 一次返回全部 124 条（0.20s），
# 原来 offset=0/100 两页是纯浪费——第二页只为了接住可能超出的 24 条。
# 保留「满页就继续翻」的循环：万一以后板块扩容超过 200 条，行为自动正确。
TX_PAGE = 200
TX_PAGE_MAX = 5              # 防御性上限，避免异常响应导致死循环


def _tx_rows(refresh: bool = False) -> list[dict]:
    """腾讯申万二级排行（124 个，与本地 sectors.json 同一套代码）。"""
    t, cached = _MEM["tx"]
    if cached and not refresh and time.time() - t < 300:
        return cached
    rows: list[dict] = []
    off = 0
    for _ in range(TX_PAGE_MAX):
        q = ("?board_type=hy2&sort_type=PriceRatio&direct=down&offset=%d&count=%d"
             % (off, TX_PAGE))
        d = (_get(TX_RANK_URL + q, timeout=10,
                  headers={"User-Agent": UA["User-Agent"]}).get("data") or {})
        page = d.get("rank_list") or []
        for x in page:
            code = str(x.get("code") or "").upper()
            if not code:
                continue
            zl = _num(x.get("zljlr"))                  # 万元
            to = _num(x.get("turnover"))               # 万元
            rows.append({
                "bk": code, "name": x.get("name"), "src": "tencent",
                "chg": _num(x.get("zdf")),
                "main": int(zl * 1e4) if zl is not None else None,
                "main_pct": round(zl / to * 100, 2) if (zl is not None and to) else None,
                "super": None, "big": None,
                "main5": int(_num(x.get("zljlr_d5")) * 1e4) if _num(x.get("zljlr_d5")) is not None else None,
                "main20": int(_num(x.get("zljlr_d20")) * 1e4) if _num(x.get("zljlr_d20")) is not None else None,
                "inflow": _num(x.get("zllr")), "outflow": _num(x.get("zllc")),
            })
        # 不满一页说明已经取完；满页才继续翻（板块扩容时自动接住）
        if len(page) < TX_PAGE:
            break
        off += TX_PAGE
    if not rows:
        raise RuntimeError("腾讯 hy2 排行返回空列表")
    _MEM["tx"] = (time.time(), rows)
    return rows


# ---------------------------------------------------------------- 排行入口

def _sorted(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r.get("main") is not None, r.get("main") or 0),
                  reverse=True)


def ranking(refresh: bool = False) -> dict:
    """全部申万行业主力资金流快照，按主力净流入降序。

    返回 {"rows", "source", "stale", "asof", "note", "upstream"}：
      source  eastmoney | tencent | cache
      stale   True 表示不是刚刚实时抓到的（熔断/失败后回落缓存）
    顺序：东财实时 → 腾讯实时 → 磁盘缓存（东财）→ 磁盘缓存（腾讯）
    """
    t, cached = _MEM["rank"]
    if cached and not refresh and time.time() - t < 300:
        return cached

    blocked = upstream_state()["blocked"]
    note = ""

    # 1) 东财
    if not blocked:
        try:
            rows = _sorted(_em_ranking())
            out = {"rows": rows, "source": "eastmoney", "stale": False,
                   "asof": _now(), "note": "", "upstream": upstream_state()}
            _MEM["rank"] = (time.time(), out)
            _write_json(RANK_CACHE, {"saved_at": out["asof"], "source": "eastmoney", "rows": rows})
            return out
        except Exception as e:  # noqa: BLE001
            _trip(f"东财 {type(e).__name__}: {e}", host="push2.eastmoney.com")

    # 2) 腾讯
    try:
        rows = _sorted(_tx_rows(refresh=refresh))
        out = {"rows": rows, "source": "tencent", "stale": False, "asof": _now(),
               "note": "东财限流，已切腾讯口径（仅今日/近5日/近20日，无超大单拆分）",
               "upstream": upstream_state()}
        _MEM["rank"] = (time.time(), out)
        _write_json(RANK_CACHE, {"saved_at": out["asof"], "source": "tencent", "rows": rows})
        return out
    except Exception as e:  # noqa: BLE001
        note = f"腾讯备源也失败：{type(e).__name__}"

    # 3) 磁盘缓存
    ck = _read_json(RANK_CACHE) or {}
    rows = ck.get("rows") or []
    if rows:
        out = {"rows": rows, "source": "cache", "stale": True,
               "asof": ck.get("saved_at"), "note": (note + "；" if note else "") + "显示上次成功抓取的数据",
               "upstream": upstream_state()}
        _MEM["rank"] = (time.time(), out)
        return out
    raise RuntimeError(f"资金流无可用数据（{note or '上游全部失败，且无本地缓存'}）")


# ---------------------------------------------------------------- 全板块快照（批量）

# 一次 clist 就能拿到的列：
#   f12 板块代码  f14 名称  f2 最新  f17 今开  f15 最高  f16 最低  f18 昨收
#   f5 成交量(手) f6 成交额(元) f3 涨跌幅
#   f62 主力净流入(元) f184 主力净占比 f66 超大单 f72 大单
# 也就是说「全部板块的当日 OHLCV + 资金流」= 1 次请求，而不是 124 次逐板块 kline。
SNAP_FIELDS = "f12,f14,f2,f15,f16,f17,f18,f5,f6,f3,f62,f184,f66,f72"
SNAP_PZ_BIG = 1000           # 优先每页 1000 条（496 个行业一页装得下）
SNAP_PZ_SMALL = 100          # 被上游拒绝放大时退回旧分页
SNAP_TTL = 60.0              # 快照内存缓存：盘中 60s，收盘后调用方自会落库


def sector_snapshot(fresh: bool = False) -> dict:
    """一次 clist 取回**全部行业板块**的当日 OHLCV + 资金流，键为 BK 代码。

    为什么要有它
      补齐板块日 K 原来是「124 个板块逐个抓 kline」，124 次串行请求。但每天只有
      「今天这一根」是新的，其余 500 根早在 klines.json 里。clist 是列表快照接口，
      一次就能返回全部板块的当前字段值，于是 124 次 → 1 次（pz 被拒时 5 次）。

    局限（调用方必须知道）
      1. 只有「当日一根」，补不了历史。落后 >1 天仍要逐板块抓 kline。
      2. 盘中拿到的是**未收盘的半根 K**，绝不能直接写进 klines.json。
         本函数只负责取数并标 `closed`，写不写由 jobs/fetch_sector_klines 判断。
      3. 部分板块的 f15/f16/f17 可能返回 '-'，此时 open/high/low 用 close 兜底，
         并置 `ohlc_partial=True`，调用方可据此决定是否采信。

    返回 {asof, closed, source, pz, pages, total, count, rows: {BK: {...}}}
    """
    t, cached = _MEM.get("snap") or (0.0, None)
    if cached is not None and not fresh and time.time() - t < SNAP_TTL:
        return cached
    if upstream_state()["blocked"]:
        raise RuntimeError("东财熔断中，板块快照不可用")

    rows: dict[str, dict] = {}
    pz, pages, total = SNAP_PZ_BIG, 0, 0
    # 先试 pz=1000；若上游拒绝放大（返回空 diff）再退 100 逐页翻。
    for attempt_pz in (SNAP_PZ_BIG, SNAP_PZ_SMALL):
        pz, pn, pages, got_any = attempt_pz, 1, 0, False
        rows.clear()
        while pn <= 12:                      # 496 个行业 / 100 = 5 页，12 页足够防御
            q = (f"?fid=f12&po=0&pz={pz}&pn={pn}&np=1&fltt=2&invt=2"
                 f"&fs=m:90+t:2&fields={SNAP_FIELDS}")
            d = _get(CLIST_URL + q, timeout=10).get("data") or {}
            diff = d.get("diff") or []
            total = int(d.get("total") or 0)
            pages += 1
            if diff:
                got_any = True
            for x in diff:
                bk = str(x.get("f12") or "").upper()
                close = _num(x.get("f2"))
                if not bk.startswith("BK") or not close or close <= 0:
                    continue
                op, hi, lo = _num(x.get("f17")), _num(x.get("f15")), _num(x.get("f16"))
                rows[bk] = {
                    "name": x.get("f14"), "close": close,
                    "open": op or close, "high": hi or close, "low": lo or close,
                    "prev_close": _num(x.get("f18")),
                    "volume": _num(x.get("f5")) or 0,
                    "amount": _num(x.get("f6")),
                    "chg": _num(x.get("f3")),
                    "main": _num(x.get("f62")),
                    "main_pct": _num(x.get("f184")),
                    "super": _num(x.get("f66")),
                    "big": _num(x.get("f72")),
                    "ohlc_partial": not (op and hi and lo),
                }
            if not diff or (total and len(rows) >= total):
                break
            pn += 1
            time.sleep(0.2)
        if got_any or attempt_pz == SNAP_PZ_SMALL:
            break
        time.sleep(0.3)                      # 放大被拒，稍等再退旧分页

    if not rows:
        raise RuntimeError("东财 clist 板块快照返回空")
    # 15:05 前拿到的当日 K 是半根，closed=False；调用方不得写库。
    hm = time.strftime("%H:%M")
    out = {"asof": _now(), "closed": hm >= "15:05", "source": "eastmoney",
           "pz": pz, "pages": pages, "total": total, "count": len(rows), "rows": rows}
    _MEM["snap"] = (time.time(), out)
    return out


# ---------------------------------------------------------------- 历史

def history(bk: str, days: int = 15) -> dict:
    """单板块近 N 个交易日主力资金流。

    东财 BK 代码走 fflow/daykline（逐日 + 超大/大单拆分）；腾讯 pt 代码没有逐日
    历史，退化为「今日 / 近5日 / 近20日」三段摘要，返回结构仍是 days[]，前端
    柱图可直接画。
    """
    bk = str(bk or "").strip().upper()
    days = max(1, min(30, int(days or 15)))

    if bk.startswith("PT"):
        return _tx_history(bk)
    if not bk.startswith("BK"):
        raise ValueError(f"板块代码不合法：{bk}")

    mkey = f"{bk}:{days}"
    hit = _MEM["hist"].get(mkey)
    if hit and time.time() - hit[0] < 300:
        return hit[1]

    cpath = os.path.join(HIST_CACHE_DIR, f"{bk}.json")
    if not upstream_state()["blocked"]:
        try:
            q = (f"?lmt={days}&klt=101&secid=90.{bk}&fields1=f1,f2,f3,f7"
                 f"&fields2=f51,f52,f53,f54,f55,f56,f57,f61,f62,f63")
            data = (_get(FFLOW_URL + q).get("data") or {})
            items = []
            for line in (data.get("klines") or []):
                p = line.split(",")
                if len(p) < 10:
                    continue
                items.append({
                    "date": p[0],
                    "main": _num(p[1]),          # 主力净流入（元）
                    "small": _num(p[2]),
                    "mid": _num(p[3]),
                    "big": _num(p[4]),
                    "super": _num(p[5]),
                    "main_pct": _num(p[6]),      # 主力净占比 %
                    "super_pct": _num(p[7]),     # 超大单净占比 %
                    "close": _num(p[8]),         # 板块指数收盘
                    "chg": _num(p[9]),           # 板块涨跌幅 %
                })
            items = items[-days:]
            if items:
                out = {"bk": bk, "name": data.get("name"), "source": "eastmoney",
                       "stale": False, "asof": _now(), "days": items}
                _MEM["hist"][mkey] = (time.time(), out)
                _write_json(cpath, {"saved_at": out["asof"], "data": out})
                return out
            raise RuntimeError("fflow 返回空")
        except Exception as e:  # noqa: BLE001
            _trip(f"东财 fflow {type(e).__name__}: {e}", host="push2his.eastmoney.com")

    ck = (_read_json(cpath) or {}).get("data") or {}
    if ck.get("days"):
        out = dict(ck)
        out.update({"stale": True, "note": "东财限流，显示缓存数据",
                    "asof": (_read_json(cpath) or {}).get("saved_at") or out.get("asof")})
        _MEM["hist"][mkey] = (time.time(), out)
        return out
    raise RuntimeError("板块资金流历史取数失败，且无本地缓存")


def fetch_bk_kline(bk: str, limit: int = 500) -> list[dict]:
    """抓单个东财板块（BKxxxx）的日 K 线，返回标准 bar（升序）。

    用于补齐本地 pt 板块的 K 线：腾讯 pt 源在全新安装时只回 1 根，算不出
    阶段序列；东财 push2his 的 secid=90.BKxxxx 能拿到上千根完整日 K。

    fields2 顺序为东财 kline 标准「日期,开盘,收盘,最高,最低,成交量,成交额,…」，
    注意是「开收高低」而非标准 bar 的「开高低收」，这里显式重排 f52/f53/f54/f55。
    上游被熔断时直接返回空列表，由调用方回落到本地已有数据，不破坏现有库。
    """
    bk = str(bk or "").strip().upper()
    if not bk.startswith("BK"):
        return []
    if upstream_state()["blocked"]:
        return []
    try:
        q = (f"?secid=90.{bk}&fields1=f1,f2,f3,f4,f5,f6"
             f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
             f"&klt=101&fqt=1&lmt={int(limit)}&end=20500101")
        data = (_get(KLINE_URL + q, timeout=10).get("data") or {})
        bars = []
        for line in (data.get("klines") or []):
            p = line.split(",")
            if len(p) < 7:
                continue
            close = _num(p[2])
            if not close or close <= 0:
                continue
            bars.append({
                "date": p[0],
                "open": _num(p[1]) or close,
                "close": close,
                "high": _num(p[3]) or close,
                "low": _num(p[4]) or close,
                "volume": _num(p[5]) or 0,
                "amount": _num(p[6]),
            })
        bars.sort(key=lambda x: x["date"])
        if bars:
            _MEM["kline_streak"] = 0          # 通了即清零，偶发失败不累积
            _MEM["kline_last_ok"] = time.time()
        return bars
    except Exception as e:  # noqa: BLE001
        # 2026-11-27 P0 改造：连接层判定与长熔断已统一由 nethttp 处理
        # （按 host 累计 streak，连续 3 次即 3600s 静默并落盘共享）。
        # 这里只做两件事：记录原因给上层脚本打印；镜像 streak 供日志观察。
        msg = f"东财 kline {bk} {type(e).__name__}: {e}"
        _MEM["last_kline_error"] = msg[:200]
        _MEM["kline_streak"] = int(nethttp.eastmoney_state().get("streak") or 0)
        return []


def _tx_history(pt: str) -> dict:
    """腾讯口径：用今日 / 近 5 日 / 近 20 日主力净流入拼 3 根柱。"""
    rows = _MEM["tx"][1] or []
    row = next((r for r in rows if str(r.get("bk")).upper() == pt), None)
    if not row:
        try:
            row = next((r for r in _tx_rows(refresh=True) if str(r.get("bk")).upper() == pt), None)
        except Exception:  # noqa: BLE001
            row = None
    if not row:
        raise RuntimeError(f"{pt} 无腾讯资金流数据")
    seq = [("今日", row.get("main"), row.get("main_pct")),
           ("近5日", row.get("main5"), None),
           ("近20日", row.get("main20"), None)]
    items = [{"date": k, "main": v, "main_pct": p, "super": None, "big": None,
              "small": None, "mid": None, "chg": None, "close": None}
             for k, v, p in seq if v is not None]
    if not items:
        raise RuntimeError(f"{pt} 腾讯资金流为空")
    return {"bk": pt, "name": row.get("name"), "source": "tencent", "stale": False,
            "asof": _now(), "days": items,
            "note": "腾讯口径：今日/近5日/近20日累计，无超大单拆分"}


# ---------------------------------------------------------------- 映射到本地板块

_MAP_CACHE: dict = {"mtime": 0.0, "data": {}}


def _load_map() -> dict:
    """读离线生成的 BK→pt 映射表（见 tools/build_flow_sector_map.py）。"""
    path = os.path.join(DATA_DIR, "snapshot", "flow_sector_map.json")
    try:
        mt = os.path.getmtime(path)
        if mt != _MAP_CACHE["mtime"]:
            with open(path, encoding="utf-8") as f:
                _MAP_CACHE["data"] = (json.load(f).get("map") or {})
            _MAP_CACHE["mtime"] = mt
    except Exception:
        pass
    return _MAP_CACHE["data"]


def attach_local(rows: list[dict]) -> list[dict]:
    """把资金流行业挂到本地 124 个申万二级板块上，供前端双击定位。

    两套清单口径不同：东财 m:90+t:2 是 496 个申万一/二/三级（2021 版），
    本地 sectors.json 是 124 个旧版申万二级。因此不再用「互为子串」的名称
    模糊匹配（会把「银行」错配成「股份制银行Ⅱ」），优先级改为：
      1) 板块代码直接命中本地清单（腾讯备源走这条，124 个一一对应）→ exact
      2) 名称归一化后完全一致 → exact
      3) 离线成分股映射表 → parent（细分归入父行业）/ members（弱匹配）
    非 exact 的行带 approx=True，前端的阶段/RS60 只作参考并明确标注。
    """
    try:
        with open(os.path.join(DATA_DIR, "snapshot", "sectors.json"), encoding="utf-8") as f:
            loc = json.load(f)
        items = loc if isinstance(loc, list) else loc.get("items", [])
    except Exception:
        items = []
    by_name = {_norm_name(s.get("name")): s for s in items if s.get("name")}
    by_code = {s.get("code"): s for s in items}
    mp = _load_map()

    for r in rows:
        r.setdefault("code", None)
        r.setdefault("local_name", None)
        r["match"] = "none"
        r["coverage"] = 0.0
        r["approx"] = False
        r["via"] = None
        bk = str(r.get("bk") or "").upper()
        # 1) 代码直撞（腾讯 pt 代码就是本地清单的代码）
        s = by_code.get(bk) if bk.startswith("PT") else None
        if s:
            r["match"] = "exact"
            r["coverage"] = 1.0
            r["via"] = "板块代码一致（腾讯申万二级）"
        # 2) 名称归一化一致
        if not s:
            s = by_name.get(_norm_name(r.get("name")))
        # 3) 映射表
        m = mp.get(bk) or {}
        if not s and m.get("code") in by_code:
            s = by_code[m["code"]]
        if not s:
            # 映射表尚未生成时的保守兜底：只允许「细分名包含本地名」这一种方向
            key = _norm_name(r.get("name"))
            if key and not mp:
                cands = [x for n, x in by_name.items() if n and n in key]
                if cands:
                    s = sorted(cands, key=lambda x: len(_norm_name(x["name"])), reverse=True)[0]
        if not s:
            continue
        r["code"] = s.get("code")
        r["local_name"] = s.get("name")
        r["stage"] = s.get("stage")
        r["sub"] = s.get("sub")
        r["rs60"] = s.get("rs60")
        if r["match"] == "exact":
            pass
        elif m.get("code") == s.get("code"):
            r["match"] = m.get("match") or "members"
            r["coverage"] = m.get("coverage") or 0.0
            r["hits"] = m.get("hits") or 0
            r["total"] = m.get("total") or 0
            r["via"] = m.get("via")
        elif _norm_name(r.get("name")) == _norm_name(s.get("name")):
            r["match"] = "exact"
            r["coverage"] = 1.0
            r["via"] = "名称一致"
        else:
            r["match"] = "name-parent"
            r["coverage"] = 0.0
            r["via"] = "名称包含（细分归入父行业）"
        r["approx"] = r["match"] != "exact"
    return rows
