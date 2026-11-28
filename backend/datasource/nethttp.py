"""统一 HTTP 出口（全站唯一实现）。

## 为什么需要它

改造前同一件「取一个 JSON」的事，代码里有五份独立实现、五套独立重试、五套独立
熔断，彼此互不感知：

    sectorflow.py   重试 1 次 · 有长熔断 · 动态代理
    evidence.py     重试 2 次 · 无熔断 · 走 urllib 默认代理
    profile.py      重试 1 次 · 无熔断 · 走 urllib 默认代理
    tencent.py      重试 2 次 · 自建限速 · 走 urllib 默认代理
    fetch_news.py   重试 0 次 · host 级熔断 · 走 urllib 默认代理

后果是「一处被封、全链路继续撞」：sectorflow 那边进了熔断，档案页、K 线、研报
照样各撞各的，出口 IP 的行为评分越撞越低。而且只有 sectorflow 会动态重读代理
端口，其余四个在代理重启后永远打向死端口。

## 本模块提供什么

1. **动态代理**：每次请求前经 netproxy 重新判定（端口探活），不用重启进程。
2. **按 host 熔断**：`_BREAKS[host]`，落盘 `cache/http_break.json` 跨进程共享。
   东财 push2his 被封不该连累腾讯 qt.gtimg.cn，所以粒度是 host 而不是全局。
3. **连接层错误永不重试**：这是本次 P0 的硬规则。被 TCP 层掐断（rc=56 /
   RemoteDisconnected / 连接重置）说明是 IP 级风控，重试只会给封禁「续期」。
   普通 HTTP 错误（4xx/5xx 非限流）才允许按 retry 参数重试。
4. **全局限速**：令牌桶，按 host 配置速率，任何新增调用自动受控。

## 熔断时长

    连接层连续 3 次  → 3600s（IP 风控，宁可少取一天也不要续期）
    HTTP 429/501/503 → 600s（限流，冷却后再试）
    其它错误         → 不熔断，只记 streak
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from data_paths import DATA_DIR
from datasource import netproxy

CACHE_DIR = os.path.join(DATA_DIR, "cache")
BREAK_FILE = os.path.join(CACHE_DIR, "http_break.json")

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}

# 连接层「被掐断」的特征串（大小写不敏感）。命中即视为 IP 级风控，不重试。
TCP_CUT_MARKS = ("schannel", "remotedisconnected", "remote end closed",
                 "connection reset", "connection aborted", "connection refused",
                 "rc=56", "rc=35", "rc=52", "rc=7",
                 "eof occurred", "server closed abruptly", "missing close_notify",
                 "without response", "bad handshake")

STREAK_LIMIT = 3             # 连续多少次连接层失败触发长熔断
TCP_BREAK_SECONDS = 3600.0   # IP 级风控：一小时静默
THROTTLE_BREAK_SECONDS = 600.0
THROTTLE_CODES = (429, 501, 502, 503)

# 按 host 的令牌桶速率：(每秒令牌, 突发容量)
RATE_DEFAULT = (2.0, 5.0)
RATE_LIMITS = {
    "gtimg.cn": (20.0, 10.0),            # 腾讯批量行情，历史上允许高频（合并 3 个子域名）
    "sinajs.cn": (10.0, 6.0),
    "eastmoney.com": (2.0, 5.0),         # 东财 8 个子域名共用出口 IP，必须合并计数
}


class CircuitOpen(Exception):
    """该 host 处于熔断期，本次不发请求。"""


# ---------------------------------------------------------------- 熔断状态

_BREAKS: dict[str, dict] = {}          # host -> {"until", "why", "hits"}
_STREAKS: dict[str, int] = {}          # host -> 连续连接层失败次数
_DISK = {"mtime": -1.0}
_LOCKS = {"break": threading.Lock(), "rate": threading.Lock()}
_BUCKETS: dict[str, list] = {}         # host -> [tokens, last_ts]

# 本进程累计请求数（按 host）。所有「账单」都应该用这个数核对，而不是估算。
# 进程重启即清零，跨进程汇总靠各进程自己上报。
_CALLS: dict[str, int] = {}


def calls() -> dict[str, int]:
    with _LOCKS["rate"]:
        return dict(_CALLS)


def _bump_call(host: str) -> None:
    with _LOCKS["rate"]:
        _CALLS[host] = _CALLS.get(host, 0) + 1


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().split(":")[0]
    except Exception:  # noqa: BLE001
        return url


# 限速分组：熔断按 host 记（粒度细，能只停一个域名），
# 但限速必须按「同一个出口 IP 看到的域名族」合并。
# 否则东财 8 个域名各配 2/s，风控看到的是 16/s —— 等于把配额放大 8 倍打出去，
# 这正是「单个模块都不快、整体还是被封」的原因。
_BUCKET_GROUPS = (
    ("eastmoney.com", ("eastmoney.com",)),
    ("gtimg.cn", ("gtimg.cn",)),
    ("sinajs.cn", ("sinajs.cn",)),
)


def _bucket_key(host: str) -> str:
    h = (host or "").lower()
    for key, suffixes in _BUCKET_GROUPS:
        if any(h == s or h.endswith("." + s) for s in suffixes):
            return key
    return h


def _sync_from_disk() -> None:
    """跨进程共享熔断：别的进程 trip 了，这里也要看到。按 mtime 缓存读取。"""
    try:
        mt = os.path.getmtime(BREAK_FILE)
    except OSError:
        return
    if mt <= _DISK["mtime"]:
        return
    try:
        with open(BREAK_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001
        return
    hosts = d.get("hosts") or {}
    for h, v in hosts.items():
        if not isinstance(v, dict):
            continue
        cur = _BREAKS.get(h) or {}
        if float(v.get("until") or 0) > float(cur.get("until") or 0):
            _BREAKS[h] = {"until": float(v.get("until") or 0),
                          "why": str(v.get("why") or "")[:160],
                          "hits": int(v.get("hits") or 0)}
    _DISK["mtime"] = mt


def _write_disk() -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = BREAK_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"hosts": _BREAKS, "written_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, BREAK_FILE)
        _DISK["mtime"] = os.path.getmtime(BREAK_FILE)
    except Exception:  # noqa: BLE001
        pass


def trip(host: str, why: str, seconds: float) -> None:
    """给某个 host 记一次熔断。取最大截止时间，短熔断不会缩短已被判的长熔断。"""
    with _LOCKS["break"]:
        cur = _BREAKS.get(host) or {"until": 0.0, "hits": 0}
        until = max(float(cur.get("until") or 0), time.time() + seconds)
        _BREAKS[host] = {"until": until, "why": str(why)[:160],
                         "hits": int(cur.get("hits") or 0) + 1}
        _STREAKS.pop(host, None)
        _write_disk()


def clear(host: str | None = None) -> None:
    with _LOCKS["break"]:
        if host:
            _BREAKS.pop(host, None)
            _STREAKS.pop(host, None)
        else:
            _BREAKS.clear()
            _STREAKS.clear()
        _write_disk()


def blocked(url: str) -> tuple[float, str]:
    """返回 (还需静默秒数, 原因)。0 表示可发请求。"""
    h = _host(url)
    with _LOCKS["break"]:
        _sync_from_disk()
        b = _BREAKS.get(h) or {}
        left = float(b.get("until") or 0) - time.time()
    return (left, str(b.get("why") or "")) if left > 0 else (0.0, "")


def _bump_streak(host: str, why: str) -> None:
    n = _STREAKS.get(host, 0) + 1
    _STREAKS[host] = n
    if n >= STREAK_LIMIT:
        trip(host, f"连续 {n} 次被掐连接（疑似 IP 风控）: {why}", TCP_BREAK_SECONDS)


def _reset_streak(host: str) -> None:
    _STREAKS.pop(host, None)


# ---------------------------------------------------------------- 限速

def _rate_for(key: str) -> tuple[float, float]:
    for k, v in RATE_LIMITS.items():
        if key == k or key.endswith("." + k):
            return v
    return RATE_DEFAULT


def _acquire(host: str) -> None:
    """令牌桶：拿不到令牌就睡到够为止（不抛错，正常排队）。

    注意入参是 host，但桶按 `_bucket_key(host)` 分组——东财 8 个域名共用一个桶。
    """
    key = _bucket_key(host)
    rate, burst = _rate_for(key)
    while True:
        with _LOCKS["rate"]:
            now = time.time()
            b = _BUCKETS.get(key)
            if b is None:
                b = [burst, now]
                _BUCKETS[key] = b
            b[0] = min(burst, b[0] + (now - b[1]) * rate)
            b[1] = now
            if b[0] >= 1.0:
                b[0] -= 1.0
                return
            wait = (1.0 - b[0]) / rate
        time.sleep(min(max(wait, 0.01), 2.0))


# ---------------------------------------------------------------- 连接

_OPENER = {"url": None, "opener": None}


def _opener():
    """urllib opener：与 curl 共用同一套代理判定，失效即直连。"""
    px = netproxy.resolve()
    if _OPENER["opener"] is not None and _OPENER["url"] == px:
        return _OPENER["opener"]
    handler = urllib.request.ProxyHandler({"http": px, "https": px} if px else {})
    op = urllib.request.build_opener(handler)
    _OPENER.update({"url": px, "opener": op})
    return op


def _is_tcp_cut(exc: BaseException | None, text: str = "") -> bool:
    if isinstance(exc, (http.client.RemoteDisconnected, ConnectionResetError,
                        ConnectionAbortedError, ConnectionRefusedError)):
        return True
    blob = f"{type(exc).__name__}: {exc} {text}".lower()
    return any(m in blob for m in TCP_CUT_MARKS)


def _try_urllib(url: str, hdrs: dict, data: bytes | None, timeout: float) -> bytes:
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with _opener().open(req, timeout=timeout) as r:
        return r.read()


def _try_curl(url: str, hdrs: dict, data: bytes | None,
              timeout: float) -> tuple[bytes | None, str]:
    """curl 兜底。继承宿主代理（见 netproxy 注释），显式 TLS1.2 绕 schannel 抖动。"""
    cmd = ["curl", "-sS", "--tlsv1.2", "--max-time", str(max(5, int(timeout)))]
    for k, v in hdrs.items():
        cmd += ["-H", f"{k}: {v}"]
    if data:
        cmd += ["--data-binary", data.decode("utf-8", "ignore")]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True,
                           timeout=max(12, int(timeout) + 4), env=netproxy.curl_env())
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    if p.returncode == 0 and p.stdout:
        return p.stdout, ""
    return None, f"curl rc={p.returncode}: {(p.stderr or b'').decode('utf-8', 'replace').strip()[:120]}"


# ---------------------------------------------------------------- 对外入口

def http_bytes(url: str, *, headers: dict | None = None, data: bytes | None = None,
               timeout: float = 8.0, retry: int = 0,
               allow_curl: bool = True) -> bytes:
    """取原始字节。连接层错误永不重试（P0 硬规则），其余错误最多重试 retry 次。"""
    h = _host(url)
    left, why = blocked(url)
    if left > 0:
        raise CircuitOpen(f"{h} 熔断中，还需 {int(left)}s（{why[:60]}）")

    hdrs = dict(UA)
    if headers:
        hdrs.update(headers)

    last: BaseException | None = None
    for attempt in range(retry + 1):
        _acquire(h)
        _bump_call(h)
        try:
            raw = _try_urllib(url, hdrs, data, timeout)
            _reset_streak(h)
            return raw
        except urllib.error.HTTPError as e:
            last = e
            if e.code in THROTTLE_CODES:
                # 被限流：立刻熔断并抛给调用方走兜底，不再重试
                trip(h, f"HTTP {e.code} 限流", THROTTLE_BREAK_SECONDS)
                raise
            # 4xx/5xx 非限流：允许按 retry 重试，不试 curl（curl 只会得到同样状态码）
            if attempt < retry:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if _is_tcp_cut(e):
                # 关键：连接层被掐 = IP 级风控，重试只会给封禁续期，立即止损
                _bump_streak(h, _short(e))
                raise
            # 非连接层异常：允许一次 curl 兜底（TLS/协议栈差异有时能过）
            if allow_curl:
                raw, err = _try_curl(url, hdrs, data, timeout)
                if raw:
                    _reset_streak(h)
                    return raw
                if err and _is_tcp_cut(None, err):
                    _bump_streak(h, _short_text(err))
                    raise RuntimeError(err) from e
            if attempt < retry:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise
    raise last if last else RuntimeError("http_bytes 无结果")


def http_text(url: str, *, headers: dict | None = None, data: bytes | None = None,
              timeout: float = 8.0, retry: int = 0,
              allow_curl: bool = True) -> str:
    """取文本（UTF-8 解码，忽略坏字节）。"""
    return http_bytes(url, headers=headers, data=data, timeout=timeout,
                      retry=retry, allow_curl=allow_curl).decode("utf-8", "ignore")


def _short(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:140]


def _short_text(s: str) -> str:
    return str(s)[:140]


def http_json(url: str, *, timeout: float = 8.0, retry: int = 0,
              headers: dict | None = None, data: bytes | None = None,
              allow_curl: bool = True) -> dict:
    """取 JSON。容忍 JSONP 包裹（cb({...})）与 BOM。"""
    raw = http_text(url, headers=headers, data=data, timeout=timeout,
                    retry=retry, allow_curl=allow_curl).lstrip("\ufeff").strip()
    if not raw:
        raise ValueError("空响应")
    if raw[0] not in "{[":
        i, j = raw.find("("), raw.rfind(")")
        if 0 <= i < j:
            raw = raw[i + 1:j]
    return json.loads(raw)


# ---------------------------------------------------------------- 状态聚合

EASTMONEY_SUFFIX = "eastmoney.com"


def eastmoney_state() -> dict:
    """聚合东财各 host 的熔断状态，供 /api/upstream/status 与补数脚本判断。

    「任一东财 host 在熔断」即视为整体不可用——它们的出口 IP 是同一个，
    一个域名被封时其它域名通常也撑不久，继续打只会加重评分。
    """
    with _LOCKS["break"]:
        _sync_from_disk()
        now = time.time()
        live = {h: v for h, v in _BREAKS.items()
                if h.endswith(EASTMONEY_SUFFIX) and float(v.get("until") or 0) > now}
        until = max((float(v["until"]) for v in live.values()), default=0.0)
        why = ""
        if live:
            worst = max(live.items(), key=lambda kv: float(kv[1]["until"]))
            why = worst[1].get("why") or ""
        streak = max((n for h, n in _STREAKS.items() if h.endswith(EASTMONEY_SUFFIX)),
                     default=0)
    return {"blocked": until > now, "retry_in": max(0, int(until - now)),
            "why": why, "streak": streak, "hosts": sorted(live)}


def rate_state() -> dict:
    """当前各限速桶余量。诊断「到底是一个域名在打，还是八个域名合计在打」用。"""
    with _LOCKS["rate"]:
        return {k: {"tokens": round(float(v[0]), 2), "rate": _rate_for(k)[0]}
                for k, v in _BUCKETS.items()}


def status() -> dict:
    """全站出网健康度：熔断 + 代理，一眼区分「代理挂了」还是「上游封了」。"""
    em = eastmoney_state()
    px = netproxy.state()
    if not px["alive"] and px["proxy"] != "direct":
        hint = "代理端口已失效，请求会走直连（多半会失败）"
    elif em["blocked"]:
        hint = "东财处于 IP 风控熔断，静默期内不要重试"
    elif px["proxy"] == "direct":
        hint = "未配置代理，直连；能通就没问题"
    else:
        hint = "代理与上游均正常"
    return {"upstream": em, "proxy": px, "rate": rate_state(),
            "calls": calls(), "hint": hint}
