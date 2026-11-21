"""生成全 A 股「代码 → 名称」表，供平台搜索索引兜底。

东财 clist 单页上限 100 条，需并发翻页；结果落盘到
data/snapshot/stock_names.json，之后搜索全部走本地，不再联网。

用法：
    cd server && python -m tools.build_stock_names
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(DATA_DIR, "snapshot", "stock_names.json")
URL = "https://push2.eastmoney.com/api/qt/clist/get"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Referer": "https://quote.eastmoney.com/"}
# 沪市主板 / 科创板 / 深市主板 / 创业板 / 北交所
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def _page(pn: int, timeout: int = 12) -> list[dict]:
    u = (f"{URL}?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
         f"&fs={FS}&fields=f12,f13,f14")
    req = urllib.request.Request(u, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    node = d.get("data") or {}
    return node.get("diff") or [], node.get("total") or 0


def _prefix(code: str, market) -> str:
    if code.startswith(("8", "4", "9")) and str(market) == "0":
        return "bj"
    return "sh" if str(market) == "1" else "sz"


def build() -> dict:
    t0 = time.time()
    first, total = _page(1)
    pages = (total + 99) // 100
    rows = list(first)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_page, p): p for p in range(2, pages + 1)}
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
    payload = {"as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
               "total_expected": total, "count": len(out), "items": out}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[stock_names] 写入 {OUT}：{len(out)} / 预期 {total}，耗时 {time.time() - t0:.1f}s")
    return payload


if __name__ == "__main__":
    build()
