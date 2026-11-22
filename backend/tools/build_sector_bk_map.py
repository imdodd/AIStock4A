"""生成 pt → 东财 BK 静态映射种子（data/raw/sector_bk_map.json）。

背景：本地 124 个申万二级板块用 pt01801xxx 代码，但东财日 K 接口 push2his
只认 BKxxxx。腾讯 pt 源在全新安装时只回 1 根 K 线，算不出阶段、前端无图；
补齐方案是走东财 BK 日 K，因此需要一个稳定的 pt → BK 对照表。

对照来源（按优先级）：
  1. 磁盘上最近一次成功的东财 496 个 BK 排行缓存（sectorflow_rank.json，含 BK 代码 + 名称）
  2. 东财 clist 实时排行（sectorflow.ranking 的三源回落，网络可用时）

匹配用名称归一化（去 Ⅰ/Ⅱ/Ⅲ 后缀），与 sectorflow._norm_name 完全一致。
命中数低于阈值（默认 100）时不覆盖现有种子，避免申万分类调整时写入残缺映射。

用法（在 backend/ 目录）：
  python -m tools.build_sector_bk_map            # 生成/刷新种子
  python -m tools.build_sector_bk_map --dry-run  # 只报告命中率
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from data_paths import DATA_DIR  # noqa: E402
from datasource import sectorflow  # noqa: E402

RAW = os.path.join(DATA_DIR, "raw")
META_PATH = os.path.join(RAW, "sectors_meta.json")
MAP_PATH = os.path.join(RAW, "sector_bk_map.json")
RANK_CACHE = os.path.join(DATA_DIR, "cache", "sectorflow_rank.json")


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _bk_by_name(cache_path: str | None = None) -> tuple[dict, str]:
    """返回 {归一化名称: BK代码} 与来源标注，优先磁盘东财缓存，其次实时排行。"""
    ck = _read(cache_path or RANK_CACHE) or {}
    rows = ck.get("rows") or []
    if ck.get("source") == "eastmoney" and rows:
        out = {}
        for r in rows:
            bk = str(r.get("bk") or "").strip().upper()
            if bk.startswith("BK"):
                out.setdefault(sectorflow._norm_name(r.get("name")), bk)
        if out:
            return out, f"cache/eastmoney ({ck.get('saved_at') or '?'} · {len(rows)} 行)"

    # 缓存不是东财源（比如被腾讯 124 行覆盖）时，尝试实时拉一次东财 496 个 BK
    try:
        snap = sectorflow.ranking(refresh=True)
        if snap.get("source") == "eastmoney":
            out = {}
            for r in snap.get("rows") or []:
                bk = str(r.get("bk") or "").strip().upper()
                if bk.startswith("BK"):
                    out.setdefault(sectorflow._norm_name(r.get("name")), bk)
            if out:
                return out, "live/eastmoney"
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 实时东财排行失败：{type(e).__name__}: {e}")
    return {}, "none"


def main(argv=None) -> int:
    argv = list(argv or [])
    dry = "--dry-run" in argv
    minimum = 100
    if "--min" in argv:
        try:
            minimum = int(argv[argv.index("--min") + 1])
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cache_path = None
    if "--cache" in argv:
        idx = argv.index("--cache")
        if idx + 1 < len(argv):
            cache_path = argv[idx + 1]

    meta = _read(META_PATH) or {}
    if not isinstance(meta, dict) or not meta:
        print("[FAIL] 缺少 sectors_meta.json，无法生成映射")
        return 1

    by_name, source = _bk_by_name(cache_path)
    if not by_name:
        print("[FAIL] 无可用东财 BK 名称表（排行缓存非东财源且实时拉取失败）")
        return 1

    mapping, miss = {}, []
    for pt, m in meta.items():
        bk = by_name.get(sectorflow._norm_name((m or {}).get("name")))
        if bk:
            mapping[pt] = bk
        else:
            miss.append((pt, (m or {}).get("name")))

    print(f"[1/2] 来源 {source} · 名称库 {len(by_name)} → pt {len(meta)}："
          f"命中 {len(mapping)} · 未命中 {len(miss)}")
    if miss:
        print("   未命中样本：" + ", ".join(f"{c}:{n}" for c, n in miss[:8]))

    if len(mapping) < minimum:
        print(f"[FAIL] 命中 {len(mapping)} < 阈值 {minimum}，不覆盖现有映射", flush=True)
        return 1

    if dry:
        print(f"[DRY-RUN] 映射 {len(mapping)} 条，未写盘；样本 "
              + ", ".join(f"{p}→{b}" for p, b in list(mapping.items())[:5]))
        return 0

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "count": len(mapping),
        "map": mapping,
    }
    _write(MAP_PATH, payload)
    print(f"[2/2] 写入 {MAP_PATH}：{len(mapping)} 条 pt→BK 映射", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
