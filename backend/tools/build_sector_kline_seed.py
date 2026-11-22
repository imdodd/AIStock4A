"""生成申万二级板块「静态 K 线种子」入仓，供全新安装服务器零网络补齐。

背景：
  服务器（香港云主机）直连东财 push2his 会被频率限流：124 个板块逐个抓，
  每个走「urllib 重试2次→curl 兜底」实发 3 次请求，几十次后触发域名级限流。
  与其让每台服务器各自撞墙，不如在数据源可用的一侧一次性抓全、生成静态种子
  入仓，服务器只读种子、零网络请求即可拥有完整板块历史与 K 线图。

数据来源（本地已积累的真实数据，无需联网）：
  · {src}/raw/klines.json        124 个 pt 板块的 312/512 根长历史（末 11-18）
  · {src}/snapshot/charts/pt*.json  同 124 个板块的最新窗口（末 11-20）
  两者按日期并集合并 → 完整且最新的种子。charts 的 bars 字段顺序是
  [open, close, low, high]（见 engines/chart.py），此处显式转成标准 OHLC。

输出：
  {out}/sector_klines_seed.json  —— 结构 {"count":N,"asof":"...","map":{pt:{"nodes":[...]}}}
  仅当有效板块 >= MIN_SECTORS 且每板块 >= MIN_BARS 根时才写，失败不覆盖已有种子。

用法（在 backend/ 目录）：
  python -m tools.build_sector_kline_seed                       # 读 data-live，写入仓目录
  python -m tools.build_sector_kline_seed --src data-live --dry-run
  python -m tools.build_sector_kline_seed --src /path/to/data --out /path/to/raw
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from data_paths import DATA_DIR, DEFAULT_DATA_DIR  # noqa: E402

MIN_SECTORS = 100       # 有效板块数低于此值不覆盖种子（防止半截数据入仓）
MIN_BARS = 250          # 单板块根数低于此值视为历史不足，不计入有效板块


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _build_seed(src: str) -> tuple[dict, str]:
    """合并 klines 长历史 + charts 最新窗口 → {pt: [bar,...]} 与最新日期。"""
    raw = _read(os.path.join(src, "raw", "klines.json")) or {}
    chart_files = glob.glob(os.path.join(src, "snapshot", "charts", "pt*.json"))

    # 1) 长历史：klines.json 的 pt 板块
    store: dict[str, dict[str, dict]] = {}
    for code, v in raw.items():
        if not str(code).startswith("pt"):
            continue
        nodes = v.get("nodes") if isinstance(v, dict) else v
        if not nodes:
            continue
        store[code] = {b["date"]: b for b in nodes if b.get("date")}

    # 2) 最新窗口：charts/pt*.json（bars=[open,close,low,high]，volumes 平行）
    added = 0
    for f in chart_files:
        code = os.path.basename(f)[:-5]
        d = _read(f) or {}
        dates = d.get("dates") or []
        bars = d.get("bars") or []
        vols = d.get("volumes") or []
        if not dates or not bars:
            continue
        byd = store.setdefault(code, {})
        for i, (dt, b) in enumerate(zip(dates, bars)):
            if len(b) < 4 or not dt:
                continue
            try:
                o, c, l, h = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            except (TypeError, ValueError):
                continue
            vol = None
            if i < len(vols):
                try:
                    vol = float(vols[i])
                except (TypeError, ValueError):
                    vol = None
            if dt not in byd:
                added += 1
            byd[dt] = {"date": dt, "open": o, "close": c, "low": l,
                       "high": h, "volume": vol, "amount": None}

    _log(f"长历史板块 {len([c for c, v in store.items() if v])} · "
         f"charts文件 {len(chart_files)} · 新增/更新最新节点 {added}")

    result = {}
    for code, byd in store.items():
        result[code] = [byd[d] for d in sorted(byd)]
    asof = max((b[-1]["date"] for b in result.values() if b), default="")
    return result, asof


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成板块静态K线种子入仓")
    ap.add_argument("--src", default=DATA_DIR, help="数据来源目录（含 raw/ 与 snapshot/）")
    ap.add_argument("--out", default=os.path.join(DEFAULT_DATA_DIR, "raw"),
                    help="种子输出目录（入仓目录 backend/data/raw）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    store, asof = _build_seed(args.src)
    valid = {c: bars for c, bars in store.items() if len(bars) >= MIN_BARS}
    _log(f"合并后板块 {len(store)} · 有效(>={MIN_BARS}根) {len(valid)} · 最新日期 {asof}")
    dist = {}
    for bars in valid.values():
        n = len(bars)
        dist[n] = dist.get(n, 0) + 1
    _log(f"有效板块根数分布 {sorted(dist.items(), reverse=True)[:6]}")

    if len(valid) < MIN_SECTORS:
        _log(f"[FAIL] 有效板块 {len(valid)} < {MIN_SECTORS}，拒绝覆盖种子")
        return 1
    if not asof:
        _log("[FAIL] 无有效日期，拒绝覆盖种子")
        return 1

    out_file = os.path.join(args.out, "sector_klines_seed.json")
    if args.dry_run:
        sample = next(iter(valid.items()))
        _log(f"[DRY-RUN] 将写入 {out_file}：{len(valid)} 板块，样本 {sample[0]} "
             f"{len(sample[1])} 根 末 {sample[1][-1]['date']}")
        return 0

    os.makedirs(args.out, exist_ok=True)
    payload = {
        "count": len(valid),
        "asof": asof,
        "source": "本地 klines.json + charts/pt*.json 合并",
        "note": "静态种子：服务器只读、零网络请求；asof 落后时需用东财BK增量更新",
        "map": {c: {"nodes": bars} for c, bars in valid.items()},
    }
    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_file)
    size_mb = os.path.getsize(out_file) / 1024 / 1024
    _log(f"[OK] 种子已写入 {out_file}：{len(valid)} 板块 · 末 {asof} · {size_mb:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
