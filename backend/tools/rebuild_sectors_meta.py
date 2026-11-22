"""重建本地申万二级板块骨架（data/raw/sectors_meta.json）。

背景：sectors_meta.json 是「代码 -> 名称 + 指标」的静态表，EOD 抓取阶段要用它
拿到 124 个 pt 行业代码，快照阶段要用它渲染板块列表。仓库自带一份公开样例种子，
但申万分类会调整、行业会改名，所以需要一条能从线上列表重建骨架的路径。

数据源（复用 sectorflow.ranking 的三级回落，无新增依赖）：
  1. 东财 clist m:90+t:2 —— 申万行业列表（一/二/三级混合，BK 代码）
  2. 腾讯 hy2 排行 —— 124 个申万二级，代码就是本地 pt01801xxx
  3. 磁盘缓存 —— 上游被限流时的最后一次成功结果

BK -> pt 的换算优先级：名称归一化精确匹配（去 Ⅰ/Ⅱ/Ⅲ 后缀）> flow_sector_map.json
里 match=exact 的映射；腾讯源直接把 PT01801xxx 小写即可。只刷新「代码 -> 名称」，
已有 stage/rs60 等指标原样保留，新出现的板块填中性值，等快照阶段算出真实指标。

上游不可用或换算结果异常少时，不覆盖现有文件，EOD 主流程不会被阻断。

用法（在 backend/ 目录）：
  python -m tools.rebuild_sectors_meta            # 刷新骨架
  python -m tools.rebuild_sectors_meta --dry-run  # 只报告，不落盘
  python -m tools.rebuild_sectors_meta --min 100  # 自定义最少板块数（默认 100）
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
MAP_PATH = os.path.join(DATA_DIR, "snapshot", "flow_sector_map.json")
# EOD 用这个时间戳判断是否该刷新：仓库自带种子的 mtime 是 clone 时间，不能当刷新依据
STAMP_PATH = os.path.join(DATA_DIR, "cache", "sectors_meta_refreshed.json")


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _neutral_meta(name: str) -> dict:
    """新板块的中性占位；快照阶段有 K 线时会重算全部指标。"""
    return {
        "name": name,
        "rs60": 0.0, "dev30": 0.0, "dev250": 0.0, "dev60": 0.0,
        "slope": 0.0, "dist52": 0.0, "chg20": 0.0, "chg60": 0.0,
        "stage": None, "sub": None,
    }


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _flow_map() -> dict[str, str]:
    """东财 BK -> 本地 pt，只取名称精确匹配的映射，避免三级细分污染二级。"""
    doc = _read_json(MAP_PATH) or {}
    out = {}
    for bk, v in (doc.get("map") or {}).items():
        if isinstance(v, dict) and v.get("match") == "exact" and v.get("code"):
            out[str(bk).upper()] = str(v["code"]).lower()
    return out


def build_fresh(base: dict, rows: list[dict], source: str) -> tuple[dict, dict]:
    """把线上申万行业清单换算成 {pt: name}，返回 (结果, 统计)。"""
    by_name = {}
    for pt, meta in base.items():
        nm = sectorflow._norm_name((meta or {}).get("name"))
        if nm:
            by_name.setdefault(nm, pt)   # 重名不猜，保留第一个

    bk_map = _flow_map()
    fresh: dict[str, str] = {}
    stat = {"source": source, "rows": len(rows), "by_name": 0, "by_map": 0,
            "by_code": 0, "skipped": 0}

    for r in rows:
        code = str(r.get("bk") or "").strip().upper()
        name = str(r.get("name") or "").strip()
        if not code or not name:
            stat["skipped"] += 1
            continue

        pt = None
        # 腾讯 hy2 / 缓存里的腾讯行：代码与本地 pt 同一套
        if r.get("src") == "tencent" or code.startswith("PT"):
            pt = code.lower()
            stat["by_code"] += 1
        else:
            # 东财 BK：先按名称精确匹配本地 124 个二级行业
            pt = by_name.get(sectorflow._norm_name(name))
            if pt:
                stat["by_name"] += 1
            else:
                pt = bk_map.get(code)
                if pt:
                    stat["by_map"] += 1
        if not pt:
            stat["skipped"] += 1
            continue
        # 同名多行（东财一/三级与二级重名）保留首次命中，避免覆盖
        fresh.setdefault(pt, name)
    return fresh, stat


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

    base = _read_json(META_PATH) or {}
    if not isinstance(base, dict):
        base = {}

    try:
        snap = sectorflow.ranking(refresh=True)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 申万行业列表抓取失败：{type(e).__name__}: {e}；保留现有骨架")
        return 0

    rows = snap.get("rows") or []
    fresh, stat = build_fresh(base, rows, snap.get("source") or "unknown")
    print(f"[1/2] 列表 {stat['source']} · {stat['rows']} 行 → pt {len(fresh)} 个"
          f"（代码 {stat['by_code']} / 名称 {stat['by_name']} / 映射 {stat['by_map']}"
          f" / 跳过 {stat['skipped']}）", flush=True)

    if len(fresh) < minimum:
        print(f"[WARN] 有效板块仅 {len(fresh)} 个（阈值 {minimum}），不覆盖现有文件", flush=True)
        return 0

    changed, added = [], []
    out = dict(base)
    for pt, name in fresh.items():
        old = out.get(pt)
        if not old:
            out[pt] = _neutral_meta(name)
            added.append(pt)
        elif old.get("name") != name:
            changed.append(f"{pt}: {old.get('name')} → {name}")
            old["name"] = name

    if dry:
        print(f"[DRY-RUN] 新增 {len(added)} · 改名 {len(changed)}，未写盘", flush=True)
        for x in changed[:10]:
            print("   " + x, flush=True)
        return 0

    _write_json(META_PATH, out)
    _write_json(STAMP_PATH, {"refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "source": snap.get("source") or "unknown",
                             "sectors": len(out)})
    print(f"[2/2] 写入 {META_PATH}：板块 {len(out)} · 新增 {len(added)} · 改名 {len(changed)}"
          f" · 生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    for x in changed[:10]:
        print("   " + x, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
