"""慢速硬补板块日 K（限流期专用）。

场景：东财 push2his 的 kline 接口在短时间密集请求后进入拒绝期。拒绝有两种形态：
  · 软限流：返回 429 / 空数据，退避后能自行恢复；
  · 硬风控：TCP 层直接掐连接（urllib RemoteDisconnected / curl rc=56 schannel），
    这是 IP 级拉黑，越重试窗口越长，只能冷处理。
本脚本用「逐板块、大间隔、多轮次」慢慢磨，但 2026-11-26 起加了熔断门禁：
一旦 sectorflow 判定上游风控（连续 KLINE_STREAK_LIMIT 次被掐连接即长熔断 1 小时），
本轮立刻停手、后续轮次在熔断期内一个请求都不发，直接收工。**绝不主动复位熔断**——
旧版本每轮把 _BREAK 清零，等于每轮都拆掉保护再撞墙，正是 26 日撞了 1400 多次
却一次没成功的直接原因。

复用主脚本的 load_store/save_store/merge_bars/market_date/_read 与量纲校验，
不重写写盘逻辑——原子写、断层剔除的能力保持一致，绝不污染历史数据。

用法（在 backend/ 目录）：
  python3 -m jobs.slow_backfill_sector_klines            # 默认最多 12 轮
  python3 -m jobs.slow_backfill_sector_klines --rounds 20 --gap 4 --max-wait 1800
手动解禁：把 data/cache/sectorflow_break.json 的 until 改成 0，或删掉该文件。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from data_paths import DATA_DIR                              # noqa: E402
from jobs import fetch_sector_klines as fsk                 # noqa: E402
from datasource import sectorflow                            # noqa: E402

LOG_DIR = os.path.join(DATA_DIR, "logs")


def _log(msg: str, fh=None) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="慢速硬补板块日K（限流期专用）")
    ap.add_argument("--rounds", type=int, default=12, help="最多磨多少轮")
    ap.add_argument("--gap", type=float, default=3.0, help="板块之间的请求间隔秒")
    ap.add_argument("--rest", type=float, default=45.0, help="轮间基础休息秒（逐轮递增）")
    ap.add_argument("--max-wait", type=int, default=900,
                    help="熔断解禁等待上限秒；超过则直接收工，不再撞墙")
    args = ap.parse_args(argv)

    os.makedirs(LOG_DIR, exist_ok=True)
    fh = open(os.path.join(LOG_DIR, f"slow-backfill-{datetime.now():%Y-%m-%d}.log"),
              "a", encoding="utf-8")
    _log("=" * 60, fh)
    _log(f"慢速补齐开始：轮次上限 {args.rounds} · 板块间隔 {args.gap}s · 轮间休息 {args.rest}s起", fh)

    meta = fsk._read(fsk.META_FILE) or {}
    mapdoc = fsk._read(fsk.MAP_FILE) or fsk._read(fsk.MAP_FILE_FALLBACK) or {}
    bk_map = mapdoc.get("map") or {}
    if not meta or not bk_map:
        _log("[FAIL] 缺少 sectors_meta.json 或 sector_bk_map.json", fh)
        fh.close()
        return 1

    store = fsk.load_store()
    mdate = fsk.market_date(store)
    _log(f"本地标的 {len(store)} · 市场基准日 {mdate}", fh)

    def pending() -> list[tuple[str, str, str]]:
        """返回仍需补齐的 (pt, bk, 末日期)。"""
        out = []
        for pt in meta:
            bk = bk_map.get(pt)
            if not bk:
                continue
            bars = store.get(pt) or []
            last = bars[-1]["date"] if bars else ""
            if mdate and last >= mdate:
                continue
            out.append((pt, bk, last or "无"))
        return out

    todo0 = pending()
    if not todo0:
        _log("全部板块已到基准日，无需补齐", fh)
        fh.close()
        return 0
    _log(f"待补 {len(todo0)} 个板块，开始慢速磨", fh)

    t_start = time.time()
    zero_rounds = 0
    for rnd in range(1, args.rounds + 1):
        todo = pending()
        if not todo:
            _log(f"第 {rnd} 轮：全部已补齐，收工", fh)
            break

        # 熔断门禁（2026-11-26 改造）：旧版本在这里把 _BREAK 清零，等于每轮都
        # 强行拆掉保护去撞墙——东财一旦判定 IP 风控，这样只会不断续期封禁。
        # 现在熔断期间一个请求都不发，等解禁或直接收工。
        st = sectorflow.upstream_state()
        if st.get("blocked"):
            wait = int(st.get("retry_in") or 0)
            _log(f"── 上游熔断中：{str(st.get('why') or '')[:70]}", fh)
            _log(f"   解禁还需 {wait}s · 本轮跳过，不发任何请求", fh)
            if wait > args.max_wait:
                _log(f"   超过等待上限 {args.max_wait}s，提前收工；数据保持原样未污染", fh)
                break
            time.sleep(max(1, min(wait + 3, args.max_wait)))
            if sectorflow.upstream_state().get("blocked"):
                _log("   仍在熔断，收工；数据保持原样未污染", fh)
                break
            todo = pending()
            if not todo:
                _log("   熔断解除且已全部补齐，收工", fh)
                break

        _log(f"── 第 {rnd}/{args.rounds} 轮：待补 {len(todo)} ──", fh)
        got = 0

        for i, (pt, bk, last) in enumerate(todo, 1):
            try:
                fresh = sectorflow.fetch_bk_kline(bk, limit=fsk.LIGHT_LIMIT)
            except Exception as e:  # noqa: BLE001
                fresh = []
                err = f"{type(e).__name__}: {e}"
            else:
                err = (sectorflow._MEM.get("last_kline_error") or "").strip()
            if fresh and len(fresh) >= 1:
                before = len(store.get(pt) or [])
                store[pt] = fsk.merge_bars(store.get(pt), fresh)
                got += 1
                _log(f"   [{i}/{len(todo)}] {pt}←{bk} ✓ +{len(fresh)}根 "
                     f"末{store[pt][-1]['date']} ({before}→{len(store[pt])})", fh)
            else:
                _log(f"   [{i}/{len(todo)}] {pt}←{bk} ✗ 保留{last}根末{last}；{err[:70]}", fh)
            # 单个板块刚触发熔断 → 立刻停本轮，剩下的别再发请求
            if sectorflow.upstream_state().get("blocked"):
                left = len(todo) - i
                if left > 0:
                    _log(f"   ⚠ 熔断触发，停止本轮：剩余 {left} 个板块未发请求", fh)
                break
            time.sleep(args.gap)

        # 每轮结束：量纲校验 + 原子写盘（哪怕只补到 1 个也落盘，避免白干）
        fixed = 0
        from jobs.repair_sector_kline_scale import outlier_indexes
        for pt in [c for c in store if str(c).lower().startswith("pt")]:
            nodes = store[pt]
            bad = outlier_indexes(nodes)
            if not bad:
                continue
            bad_dates = {nodes[j]["date"] for j in bad}
            cleaned = [b for b in nodes if b["date"] not in bad_dates]
            if not cleaned:
                _log(f"   [量纲校验] {pt} 剔除后为空，保留原样", fh)
                continue
            store[pt] = cleaned
            fixed += 1
            _log(f"   [量纲校验] {pt} 剔除断层 {len(nodes)}→{len(cleaned)}", fh)

        if got or fixed:
            zero_rounds = 0
            fsk.save_store(store)
            _log(f"第 {rnd} 轮落盘：补 {got} 个板块 · 量纲修复 {fixed} · 耗时 "
                 f"{time.time()-t_start:.0f}s", fh)
        else:
            zero_rounds += 1
            _log(f"第 {rnd} 轮：0 成功，未写盘（原数据保持不动）", fh)
            # 连续空转说明不是抖动，是上游持续不认；继续磨只会拖长封禁窗口
            if zero_rounds >= 2 and not sectorflow.upstream_state().get("blocked"):
                _log("连续 2 轮 0 成功且未记录熔断，判定上游未恢复，收工", fh)
                break

        if pending() and rnd < args.rounds:
            rest = args.rest * rnd      # 逐轮递增休息，给东财更长恢复窗口
            rest = min(rest, 300)       # 上限 5 分钟
            _log(f"轮间休息 {rest:.0f}s 后再试 …", fh)
            time.sleep(rest)

    final_pending = pending()
    st_end = sectorflow.upstream_state()
    _log(f"收工：剩余待补 {len(final_pending)} 个 · 总耗时 {time.time()-t_start:.0f}s", fh)
    if final_pending:
        if st_end.get("blocked"):
            _log(f"上游熔断中（{str(st_end.get('why') or '')[:80]}），"
                 f"解禁还需 {st_end.get('retry_in')}s；数据保持原样未污染，解禁后再跑", fh)
        else:
            _log("东财可能仍未恢复；数据保持原样未污染，可稍后重跑", fh)
    fh.close()
    return 0 if not final_pending else 2


if __name__ == "__main__":
    raise SystemExit(main())
