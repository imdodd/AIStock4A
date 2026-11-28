"""出网代理判定（全站唯一实现）。

背景（2026-11-26 两次实测修正）：

1. WorkBuddy/IDE 宿主注入的本机 HTTP(S)_PROXY（127.0.0.1:<随机端口>）在本机是
   访问东财的必经出网通道，不是污染。剥离代理会让东财请求立刻 RemoteDisconnected
   或 curl rc=56 schannel；保留代理则 0.4s 正常返回。生产环境配了公网代理时同理，
   剥掉等于断网。

2. 代理端口会随宿主会话变化（实测 55974 → 52321）。长驻进程的 os.environ 是
   启动那一刻的快照，端口一换就永远指向死端口，表现为所有请求恒定 0.1s 报
   rc=56，看起来像被上游封禁，实际是打在了已关闭的端口上。因此不能只在导入期
   读一次，每次取数前都要重新判定并探活。

重要：不要靠「扫本机监听端口」猜代理。实测本机 36 个监听端口里命中率最高的恰是
钉钉(62910)、WorkBuddy(62056)、WPS 这些无关进程——把请求发到它们身上比不通更糟
（会拿到莫名其妙的响应）。所以只认三个显式来源，逐个探活：

    1. ASTOCK_PROXY 环境变量（显式指定，最高优先，生产用这个）
    2. <DATA_DIR>/cache/proxy.url 文件（可热改，改完无需重启服务）
    3. 系统 HTTP(S)_PROXY 环境变量

全部探不通就直连（清空代理变量），绝不猜。

本模块只做「代理发现」，不含任何业务逻辑，供 http.py / sectorflow.py 共用。
"""
from __future__ import annotations

import os
import socket
import time
from urllib.parse import urlparse

from data_paths import DATA_DIR

CACHE_DIR = os.path.join(DATA_DIR, "cache")

PROXY_PORTS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
               "all_proxy", "ALL_PROXY")
RECHECK = 30.0               # 30s 内复用上次结果，避免每个请求都探活

_CACHE: dict = {"url": "", "checked": 0.0, "source": ""}


def norm(v: str) -> str:
    """'http://127.0.0.1:7890' / '127.0.0.1:7890' / 'socks5://...' → 统一 URL。"""
    s = str(v or "").strip()
    if not s:
        return ""
    s = s.splitlines()[0].strip()       # 文件里可能带换行
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    return s


def alive(url: str, timeout: float = 0.3) -> bool:
    """只探 TCP 端口是否在监听，不发起真实转发（快、无流量）。"""
    try:
        p = urlparse(url)
        if not p.hostname:
            return False
        with socket.create_connection((p.hostname, p.port or 80), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def proxy_file() -> str:
    return os.path.join(CACHE_DIR, "proxy.url")


def sources() -> list[tuple[str, str]]:
    """按优先级返回 [(来源说明, 代理URL)]，只做归一化不做探活。"""
    out: list[tuple[str, str]] = []
    u = norm(os.environ.get("ASTOCK_PROXY") or "")
    if u:
        out.append(("ASTOCK_PROXY", u))
    try:
        with open(proxy_file(), encoding="utf-8") as f:
            u = norm(f.read())
        if u:
            out.append(("cache/proxy.url", u))
    except OSError:
        pass
    seen = {x[1] for x in out}
    for k in PROXY_PORTS:                       # https 优先于 http
        u = norm(os.environ.get(k) or "")
        if u and u not in seen:
            seen.add(u)
            out.append((k, u))
    return out


def resolve(force: bool = False) -> str:
    """返回当前可用的代理 URL（空串=直连）。带 30s 缓存，端口失效立刻重探。"""
    now = time.time()
    cached = _CACHE["url"]
    if not force and now - _CACHE["checked"] < RECHECK:
        # 缓存期内也复检端口：代理重启后端口会立刻消失，不能干等缓存过期
        if not cached or alive(cached):
            return cached

    for src, url in sources():
        if alive(url):
            if url != cached or src != _CACHE.get("source"):
                _CACHE.update({"url": url, "source": src, "checked": now})
            else:
                _CACHE["checked"] = now
            return url

    # 全部探不通 → 直连，不猜端口
    _CACHE.update({"url": "", "source": "direct", "checked": now})
    return ""


def curl_env() -> dict:
    """curl 子进程环境：按当前可用代理重建，不含失效的旧端口。"""
    env = dict(os.environ)
    for k in PROXY_PORTS:                       # 先清掉所有旧代理变量
        env.pop(k, None)
    url = resolve()
    if url:
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env[k] = url
    return env


def state() -> dict:
    """给接口层看当前代理判定结果（排查「到底是代理还是上游」时用）。"""
    url = resolve(force=True)
    return {"proxy": url or "direct", "source": _CACHE.get("source", ""),
            "alive": bool(url) and alive(url)}
