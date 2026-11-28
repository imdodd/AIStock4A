"""给历史擒龙池子 / 验证文件补「监管异动」字段（reg）。

监管标签是后加的：老文件里没有 reg，接口读的时候只能拿本地日K缓存现场补算，
缓存没暖过就是一片空白。这个脚本按三条做：

  1) 把涉及的股票 + 对应指数日K一次抓齐，落 raw/regwatch_klines.json
     （之后建池 / 验证 / 回看都读缓存，零网络）；
  2) 按各自交易日落盘 reg：池子用池子日，验证用验证日（次日）；
  3) 幂等——重跑只是用同样的K线算同样的结果。

用法：
    python -m jobs.backfill_dragon_regwatch            # 最近 10 个池子日
    python -m jobs.backfill_dragon_regwatch --days 30
    python -m jobs.backfill_dragon_regwatch --dry-run  # 只报会改哪些文件
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engines import dragon, regwatch   # noqa: E402


def _read(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="回溯多少个池子日")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-fetch", action="store_true", help="只用现有缓存，不联网")
    args = ap.parse_args()

    pools = dragon.dates("pool", args.days)
    verdicts = [d for d in dragon.dates("verdict", args.days)]
    if not pools and not verdicts:
        print("没有池子/验证文件，先跑一次盘后建池")
        return 1

    targets = []          # (path, trade_date, codes)
    for d in pools:
        p = dragon._path("pool", d)
        obj = _read(p) or {}
        codes = [i.get("code") for i in obj.get("items") or [] if i.get("code")]
        if codes:
            targets.append((p, d, codes, "pool"))
    for d in verdicts:
        p = dragon._path("verdict", d)
        obj = _read(p) or {}
        codes = [i.get("code") for i in obj.get("items") or [] if i.get("code")]
        td = obj.get("verified_on") or d
        if codes:
            targets.append((p, td, codes, "verdict"))

    all_codes = sorted({c for _, _, codes, _ in targets for c in codes})
    idx = sorted({regwatch.index_of(c) for c in all_codes})
    latest = max(t for _, t, _, _ in targets)
    print(f"池子日 {len(pools)} 个 / 验证文件 {len(verdicts)} 个 · 涉及 {len(all_codes)} 只 "
          f"· 最新交易日 {latest}")
    if args.dry_run:
        for p, td, codes, kind in targets:
            print(f"  [dry] {p.name}  交易日 {td}  {len(codes)} 只")
        return 0

    cache = regwatch.ensure_bars(all_codes + idx, upto=latest,
                                 allow_fetch=not args.no_fetch, verbose=True)
    miss = [c for c in all_codes if not (cache.get(c) or {}).get("d")]
    if miss:
        print(f"  缺日K跳过：{len(miss)} 只（多为北交所 / 新股）")

    changed = 0
    for path, td, _codes, kind in targets:
        obj = _read(path)
        if not obj:
            continue
        items = obj.get("items") or []
        before = sum(1 for i in items if i.get("reg"))
        regwatch.annotate(items, td, allow_fetch=False)
        after = sum(1 for i in items if i.get("reg"))
        obj["items"] = items
        dragon._dump(path, obj)
        changed += 1
        print(f"  {path.name}: {kind} 交易日 {td} · 有监管数据 {before} → {after} / {len(items)}")

    # 统计当天口径
    for path, td, _c, kind in targets:
        if kind != "verdict":
            continue
        obj = _read(path) or {}
        st = regwatch.stats(obj.get("items") or [])
        print(f"  {path.stem} · 严重 {st['severe']} / 异动 {st['alert'] + st['warn']} / 临界 {st['near']}")
    print(f"回补完成：改写 {changed} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
