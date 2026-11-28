"""生成全 A 股「代码 → 名称」表，供平台搜索索引兜底。

2026-11-27 改造：不再假设「clist 单页上限 100」。
先按 pz=1000 试一页（覆盖全 A 股约 5000+ 只的 1/6～1/5，实际 5～6 页），
若上游拒绝放大（返回空 diff）则退回 pz=100 逐页翻，行为与旧版一致。
请求统一走 datasource.nethttp，享受动态代理 + 熔断 + 限速，不再裸 urllib。

结果落盘到 data/snapshot/stock_names.json，之后搜索全部走本地，不再联网。

用法：
    cd server && python -m tools.build_stock_names
    cd server && python -m tools.build_stock_names --pz 100     # 强制旧分页
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from data_paths import DATA_DIR
from datasource import nethttp

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(DATA_DIR, "snapshot", "stock_names.json")
URL = "https://push2.eastmoney.com/api/qt/clist/get"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Referer": "https://quote.eastmoney.com/"}
# 沪市主板 / 科创板 / 深市主板 / 创业板 / 北交所
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

PZ_BIG = 1000       # 优先尝试的每页条数
PZ_SMALL = 100      # 放大被拒时的回退值（旧行为）
PAGE_WORKERS = 4    # 翻页并发；nethttp 令牌桶会再限一道，实际不会打爆


def _page(pn: int, pz: int = PZ_SMALL, timeout: int = 12) -> tuple[list[dict], int]:
    u = (f"{URL}?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2&fid=f3"
         f"&fs={FS}&fields=f12,f13,f14")
    d = nethttp.http_json(u, timeout=timeout, headers=UA)
    node = d.get("data") or {}
    return node.get("diff") or [], node.get("total") or 0


def _prefix(code: str, market) -> str:
    if code.startswith(("8", "4", "9")) and str(market) == "0":
        return "bj"
    return "sh" if str(market) == "1" else "sz"


def build(pz_pref: int = PZ_BIG) -> dict:
    t0 = time.time()
    first, total = _page(1, pz_pref)
    pz = pz_pref
    if not first and pz_pref != PZ_SMALL:
        # 上游不接受放大后的 pz（风控或接口限制），退回旧分页，绝不因此整表失败
        print(f"[stock_names] pz={pz_pref} 返回空，回退 pz={PZ_SMALL}", flush=True)
        pz = PZ_SMALL
        first, total = _page(1, pz)
    pages = max(1, (total + pz - 1) // pz)
    rows = list(first)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
            futs = {pool.submit(_page, p, pz): p for p in range(2, pages + 1)}
            for f in as_completed(futs):
                try:
                    part, _ = f.result()
                    rows.extend(part)
                except Exception as e:  # 个别页失败不影响整体，下次刷新会补上
                    print(f"[stock_names] 第 {futs[f]} 页失败：{type(e).__name__}: {e}")
    out, seen = [], set()
    for r in rows:
        raw = str(r.get("f12") or "").strip()
        name = str(r.get("f14") or "").strip()
        if len(raw) != 6 or not raw.isdigit() or not name or name == "-":
            continue
        # clist 会混入退市股与北交所定向可转债，搜索里只保留正常股票
        if any(w in name for w in ("定转", "转债", "退")) or name.startswith("PT"):
            continue
        code = _prefix(raw, r.get("f13")) + raw
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": name})
    out.sort(key=lambda x: x["code"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    payload = {"as_of": time.strftime("%Y-%m-%d %H:%M:%S"), "pz": pz,
               "pages": pages, "total_expected": total, "count": len(out),
               "items": out}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[stock_names] 写入 {OUT}：{len(out)} / 预期 {total}，"
          f"pz={pz} 共 {pages} 页，耗时 {time.time() - t0:.1f}s")
    return payload


def _save_payload(items: list[dict], total: int, source: str, **extra) -> dict:
    items = sorted(items, key=lambda x: x["code"])
    payload = {"as_of": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source,
               "total_expected": total, "count": len(items), "items": items}
    payload.update(extra)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[stock_names] {source} 写入 {OUT}：{len(items)} 只", flush=True)
    return payload


def _fallback_tencent() -> dict:
    """东财 502/限流时用腾讯 hy2 的 124 个申万二级成分股拼出名称表。"""
    import re
    from datasource.tencent import TencentSource
    from config.universe import sector_codes, names as universe_names

    stock_code = re.compile(r"^(sh|sz|bj)\d{6}$")
    src = TencentSource()
    out, seen, failed = [], set(), 0
    for pt in sector_codes():
        try:
            rows = src.sector_members(pt) or []
        except Exception:
            failed += 1
            continue
        for x in rows:
            code = str(x.get("code") or "")
            name = str(x.get("name") or "").strip()
            if not stock_code.match(code) or not name or code in seen:
                continue
            seen.add(code)
            out.append({"code": code, "name": name})
    for code, name in (universe_names() or {}).items():
        if stock_code.match(code) and name and code not in seen:
            seen.add(code)
            out.append({"code": code, "name": name})
    if not out:
        raise RuntimeError("腾讯兜底也没有拿到任何名称")
    return _save_payload(out, len(out), "tencent_hy2", failed_sectors=failed)


def _fallback_minimal() -> dict:
    """最后兜底：只写持仓 / 自选 / 板块名，保证搜索索引不空。"""
    import re
    from config.universe import names as universe_names
    stock_code = re.compile(r"^(sh|sz|bj)\d{6}$")
    out = [{"code": c, "name": n} for c, n in (universe_names() or {}).items()
           if stock_code.match(c) and n]
    return _save_payload(out, len(out), "universe_minimal")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="刷新全 A 股代码→名称表")
    ap.add_argument("--pz", type=int, default=PZ_BIG,
                    help=f"每页条数（默认 {PZ_BIG}；设 {PZ_SMALL} 走旧版逐页翻）")
    a = ap.parse_args()
    try:
        build(max(1, a.pz))
    except Exception as e:  # 东财 502 很常见，不能让 EOD 第 ④ 步直接 FAIL
        print(f"[stock_names] 东财抓取失败：{type(e).__name__}: {e}，切换腾讯兜底", flush=True)
        try:
            _fallback_tencent()
        except Exception as e2:
            print(f"[stock_names] 腾讯兜底失败：{type(e2).__name__}: {e2}", flush=True)
            if os.path.exists(OUT):
                print("[stock_names] 保留既有名称表，EOD 继续", flush=True)
            else:
                _fallback_minimal()
