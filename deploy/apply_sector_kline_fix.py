"""生产环境：用干净补丁包覆盖 124 个申万板块 K 线（修复腾讯/东财量纲污染）。

用法（在 backend/ 目录执行）：
    python ../deploy/apply_sector_kline_fix.py ../deploy/sector_kline_clean_20261125.json

行为：
  1. 自动定位数据目录（ASTOCK_DATA_DIR 环境变量，或默认 backend/data）。
  2. 读取补丁包，校验板块数/节点数/量纲（单序列内无 2 倍以上断层）。
  3. 备份现有 klines.json 为 klines.json.bak-pollution-<时间戳>。
  4. 只覆盖 pt 板块节点；个股/指数/其他代码原样保留。
  5. 原子写回 klines.json。
  6. 打印覆盖前后的板块量纲抽样对比，供人工复核。

注意：本脚本只改 raw/klines.json，不重建快照。
      写完后需执行 python -m jobs.build_snapshot 重建图表缓存。
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(os.path.dirname(HERE), "backend")   # deploy/ 与 backend/ 同级
if not os.path.isdir(os.path.join(BACKEND, "data_paths.py")) and \
   not os.path.exists(os.path.join(BACKEND, "data_paths.py")):
    # 兜底：脚本若被直接放进 backend/ 执行
    BACKEND = HERE if os.path.exists(os.path.join(HERE, "data_paths.py")) else BACKEND
sys.path.insert(0, BACKEND)

from data_paths import DATA_DIR  # 与生产服务同一套路径解析


def med(vals):
    vals = [v for v in vals if v and v > 0]
    return statistics.median(vals) if vals else 0.0


def has_jump(nodes, ratio=2.0, window=10):
    closes = [float(n.get("close") or n.get("last") or 0) for n in nodes]
    for i in range(window, len(closes) - window):
        p = med(closes[i - window:i])
        n = med(closes[i:i + window])
        if p > 0 and n > 0 and (p / n > ratio or n / p > ratio):
            return True, i
    return False, -1


def main(pkg_path: str) -> int:
    print(f"[info] 数据目录 DATA_DIR = {DATA_DIR}")
    kline_file = os.path.join(DATA_DIR, "raw", "klines.json")
    if not os.path.exists(kline_file):
        print(f"[ERROR] 找不到 {kline_file}")
        return 2

    pkg = json.load(open(pkg_path, encoding="utf-8"))
    sectors = pkg.get("sectors") or {}
    meta = pkg.get("_meta") or {}
    print(f"[info] 补丁包：{len(sectors)} 板块 / {meta.get('total_nodes', '?')} 根，"
          f"生成于 {meta.get('generated_at', '?')}")

    # 校验补丁包：每个板块都必须无断层
    bad_pkg = []
    for code, v in sectors.items():
        nodes = v.get("nodes") or []
        jump, i = has_jump(nodes)
        if jump or len(nodes) < 20:
            bad_pkg.append((code, len(nodes), i))
    if bad_pkg:
        print(f"[ERROR] 补丁包自身有 {len(bad_pkg)} 个板块未通过校验，拒绝写入：")
        for x in bad_pkg[:20]:
            print("   ", x)
        return 3
    print("[ok] 补丁包校验通过：所有板块量纲连续")

    raw = json.load(open(kline_file, encoding="utf-8"))
    before = {}
    for code in sectors:
        v = raw.get(code)
        nodes = v.get("nodes", []) if isinstance(v, dict) else (v or [])
        before[code] = med([float(n.get("close") or n.get("last") or 0) for n in nodes[-20:]])

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = kline_file + f".bak-pollution-{stamp}"
    shutil.copy2(kline_file, backup)
    print(f"[info] 已备份 → {backup}")

    replaced = 0
    for code, v in sectors.items():
        nodes = v["nodes"]
        if isinstance(raw.get(code), dict):
            raw[code]["nodes"] = nodes
        else:
            raw[code] = {"nodes": nodes}
        replaced += 1

    tmp = kline_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, kline_file)
    print(f"[ok] 已原子覆盖 {replaced} 个板块 → {kline_file}")

    print("\n[复核] 量纲抽样（生产旧值 → 新值）：")
    samples = ["pt01801039", "pt01801125", "pt01801054", "pt01801193",
               "pt01801104", "pt01801111", "pt01801083"]
    for code in samples:
        if code not in sectors:
            continue
        nodes = sectors[code]["nodes"]
        after = med([float(n.get("close") or n.get("last") or 0) for n in nodes[-20:]])
        print(f"   {code}: {before.get(code, 0):.1f} → {after:.1f}")

    print("\n[下一步] 重建图表缓存：")
    print("    cd " + BACKEND)
    print("    python -m jobs.build_snapshot")
    print("然后重启后端服务并强刷前端。")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
