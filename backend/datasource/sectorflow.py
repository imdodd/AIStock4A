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

为什么要有熔断（_BREAK）
    东财对高频、无 cookie 的抓取会直接掐连接（RemoteDisconnected），且会从单个
    接口扩大到整个 push2/push2his 域名，持续几十分钟到数小时。原实现是「每次
    打开大盘页都 7 次翻页 + 每次点板块 1 次历史」，一旦被限流就不断重试、越试
    越久。这里失败一次就熔断 BREAK_SECONDS，期间直接用缓存/备源，不再撞墙。

本地行业（data/snapshot/sectors.json，申万二级 124 个）里 4 个银行二级
东财命名不带 Ⅱ 后缀，匹配时对两侧名称做归一化（去掉 Ⅰ/Ⅱ 罗马数字后缀）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request

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
_MEM: dict = {"rank": (0.0, None), "hist": {}, "tx": (0.0, None),
              "prefer_curl": False, "last_kline_error": ""}
_BREAK: dict = {"until": 0.0, "why": "", "hits": 0}


# ---------------------------------------------------------------- 基础工具

def _get(url: str, timeout: int = 8, retry: int = 1, headers: dict | None = None) -> dict:
    hdrs = headers or UA
    last = None
    # 一旦确认本机 urllib 被上游掐、curl 可用（prefer_curl），后续直接用 curl：
    # 避免每个请求都白白重试 urllib 2 次才走 curl（实测每板块从 4~5s 降到 <1s，
    # 请求数从 3 次降到 1 次——高频双倍请求会把东财推到限流，才是熔断的真正诱因）。
    if not _MEM.get("prefer_curl"):
        for i in range(retry + 1):
            try:
                req = urllib.request.Request(url, headers=hdrs)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.4 * (i + 1))
    try:
        cmd = ["curl", "-sS", "--max-time", str(max(5, timeout))]
        for k, v in hdrs.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
        p = subprocess.run(cmd, capture_output=True, timeout=max(12, timeout + 4))
        if p.returncode == 0 and p.stdout:
            _MEM["prefer_curl"] = True
            return json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        last = e
    raise last  # type: ignore[misc]


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


def _trip(why: str) -> None:
    """上游不可用，进熔断。"""
    _BREAK["until"] = time.time() + BREAK_SECONDS
    _BREAK["why"] = str(why)[:120]
    _BREAK["hits"] = int(_BREAK.get("hits") or 0) + 1


def upstream_state() -> dict:
    """给接口层/前端看的熔断状态。"""
    left = max(0, int(_BREAK["until"] - time.time()))
    return {"blocked": left > 0, "retry_in": left, "why": _BREAK["why"], "hits": _BREAK["hits"]}


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

def _tx_rows(refresh: bool = False) -> list[dict]:
    """腾讯申万二级排行（124 个，与本地 sectors.json 同一套代码）。"""
    t, cached = _MEM["tx"]
    if cached and not refresh and time.time() - t < 300:
        return cached
    rows: list[dict] = []
    for off in (0, 100):
        q = ("?board_type=hy2&sort_type=PriceRatio&direct=down&offset=%d&count=100" % off)
        d = (_get(TX_RANK_URL + q, timeout=10,
                  headers={"User-Agent": UA["User-Agent"]}).get("data") or {})
        for x in (d.get("rank_list") or []):
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
        if len(d.get("rank_list") or []) < 100:
            break
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
            _trip(f"东财 {type(e).__name__}: {e}")

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
            _trip(f"东财 fflow {type(e).__name__}: {e}")

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
        return bars
    except Exception as e:  # noqa: BLE001
        # 单个板块失败不触发全局熔断：批量补齐时某个 BK 偶发断连不该让剩余全部跳过。
        # 只记录原因返回空，由调用方按「连续失败 N 次」自行决定暂停。
        msg = f"东财 kline {bk} {type(e).__name__}: {e}"
        _MEM["last_kline_error"] = msg[:200]
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
