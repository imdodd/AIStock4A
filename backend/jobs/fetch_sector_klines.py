"""补齐申万二级板块（pt*）的日 K 线：静态种子优先，东财 BK 源兜底。

背景：
  全新安装时，腾讯 pt 行业指数源只回 1 根 K 线，build_snapshot 算不出
  MA250/阶段序列，板块页整块无图、指标停在 sectors_meta 静态种子的旧值。
  补齐有两条来源：
    1) 静态种子 sector_klines_seed.json（随代码入仓，124 板块 312/512 根，末 11-20）
       —— 服务器只读本地文件、零网络请求，从根本上绕开东财对云主机 IP 的频率限流。
    2) 东财 push2his secid=90.BKxxxx —— 种子缺某板块或想刷新到更新日期时才抓。
  默认种子优先：种子命中即合并，绝不联网；只有种子缺失/覆盖不足的板块才走东财。

设计（每日严格增量，2026-11-25 修正停更缺陷）：
  · 新鲜度基准 = 非 pt 标的（指数/个股）的末日期；pt 末日期 >= 基准 → 跳过。
  · 落后 1~STALE_DAYS 天 → 轻量增量：只抓东财最近 LIGHT_LIMIT 根按日期并集合并，
    不查种子（种子末日期更旧，无意义），串行间隔 REQ_SLEEP 秒，避免盘后突发。
  · 落后 > STALE_DAYS 天或历史不足 MIN_BARS → 全量补齐：种子优先（零网络），
    种子未覆盖才抓东财 500 根。单板块有效根数 < MIN_BARS 不覆盖。
  · 落后 > ALERT_LAG_DAYS 天时打印 [WARN]，防止再次「悄悄停更」（11-25 事故）。
  · 连续失败 STREAK_LIMIT 次才暂停，成功即清零，支持断点续跑。
  · 全程原子写 klines.json；任一步异常都不会损坏现有文件。

用法（在 backend/ 目录）：
  python -m jobs.fetch_sector_klines                 # 每日增量（落后1~7天轻量抓12根，落后更多全量）
  python -m jobs.fetch_sector_klines --source clist  # 只用 clist 批量快照补当日（1 次请求）
  python -m jobs.fetch_sector_klines --source legacy # 强制旧行为（逐板块抓 kline）
  python -m jobs.fetch_sector_klines --shadow        # 影子比对：两路都抓，不写 klines.json
  python -m jobs.fetch_sector_klines --no-seed       # 全量补齐时跳过种子，强制走东财
  python -m jobs.fetch_sector_klines --dry-run       # 只报告待补清单，不抓不写
  python -m jobs.fetch_sector_klines --force         # 忽略新鲜度，全量重抓全部板块
  python -m jobs.fetch_sector_klines --min 60        # 自定义历史充足阈值
  python -m jobs.fetch_sector_klines --only pt01801039   # 单板块（如重抓非金属材料Ⅱ）
  python -m jobs.fetch_sector_klines --only pt01801039 --force --no-seed

2026-11-27 改造：clist 批量快照（124 次 → 1 次）
  · `--source auto`（默认，可用 ASTOCK_SECTOR_KLINE_SOURCE 覆盖）：轻量增量优先用
    `sectorflow.sector_snapshot()` 一次取回全部板块的当日 OHLCV + 资金流；
  · 只有当「板块末日 == 市场末日」（即只差今天这一根）才走 clist，落后多天仍逐板块补历史；
  · **15:05 前不写库**：盘中 clist 返回的是未收盘的半根 K，写进 klines.json 会污染历史；
  · 九成板块涨跌幅为 0 时判定休市，同样拒写（节假日手动跑的防护）；
  · `--shadow` 让两路同时抓、只落差异报告，用于解封后验证 clist 口径与 kline 一致。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from data_paths import DATA_DIR, DEFAULT_DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

RAW = os.path.join(DATA_DIR, "raw")
KLINE_FILE = os.path.join(RAW, "klines.json")
META_FILE = os.path.join(RAW, "sectors_meta.json")
# pt→BK 映射是随代码入仓的静态种子（全实例共用），可能存在默认目录而非实例目录。
# 优先实例目录（运维若手动刷新过），否则回落默认入仓目录。
MAP_FILE = os.path.join(RAW, "sector_bk_map.json")
MAP_FILE_FALLBACK = os.path.join(DEFAULT_DATA_DIR, "raw", "sector_bk_map.json")
# 静态 K 线种子（随代码入仓）：与映射种子同样优先实例目录、回落默认入仓目录
SEED_FILE = os.path.join(RAW, "sector_klines_seed.json")
SEED_FILE_FALLBACK = os.path.join(DEFAULT_DATA_DIR, "raw", "sector_klines_seed.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")

MIN_BARS = 60           # 少于此根数视为历史不足，需要补齐
FULL_LIMIT = 500        # 全量抓取根数（覆盖 276 根计算窗口）
LIGHT_LIMIT = 12        # 落后几天的轻量增量：只抓最近 12 根合并
STALE_DAYS = 7          # 落后超过 7 天转全量补齐（历史可能大段缺失）
ALERT_LAG_DAYS = 1      # 板块末日期落后市场超过 1 天即告警，防悄悄停更
REQ_SLEEP = 0.3         # 东财串行请求间隔（限流优化：不再并发猛打）

# 板块 K 线的取数源：auto = clist 优先、失败回落 legacy
SOURCE_DEFAULT = (os.environ.get("ASTOCK_SECTOR_KLINE_SOURCE") or "auto").strip().lower()


def log(msg, fh=None):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_store() -> dict[str, list[dict]]:
    """读 klines.json → {code: [bar,...]}（已是标准节点格式）；不存在返回空。"""
    from datasource.base import normalize_nodes
    raw = _read(KLINE_FILE) or {}
    store = {}
    for code, v in raw.items():
        nodes = v.get("nodes") if isinstance(v, dict) else v
        bars = normalize_nodes(nodes or [])
        if bars:
            store[code] = bars
    return store


def save_store(store: dict) -> None:
    os.makedirs(RAW, exist_ok=True)
    payload = {c: {"nodes": bars} for c, bars in store.items()}
    tmp = KLINE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, KLINE_FILE)


def merge_bars(old: list[dict] | None, incoming: list[dict]) -> list[dict]:
    """按日期并集合并：新数据覆盖同日旧数据，保留本地独有的历史。"""
    byd = {b["date"]: b for b in (old or [])}
    for b in incoming:
        byd[b["date"]] = b
    return [byd[d] for d in sorted(byd)]


def market_date(store: dict) -> str | None:
    """用非板块标的（指数/个股）的末日期作为市场基准；无则退回全部标的。"""
    idx = [v[-1]["date"] for c, v in store.items()
           if not c.startswith("pt") and v]
    if idx:
        return max(idx)
    return max((v[-1]["date"] for v in store.values() if v), default=None)


def _days_between(a: str, b: str) -> int:
    try:
        from datetime import date
        ya, ma, da = (int(x) for x in a.split("-")[:3])
        yb, mb, db = (int(x) for x in b.split("-")[:3])
        return (date(yb, mb, db) - date(ya, ma, da)).days
    except Exception:
        return 10 ** 6


def _trading_gap(a: str, b: str) -> int:
    """a→b 之间隔了几个工作日（近似交易日，不含节假日）。

    用途：判断「板块只差今天这一根」。clist 只能补当日一根，
    若 a→b 之间隔了 2 个以上工作日，说明中间还有缺口，必须逐板块抓历史，
    否则 clist 会把「倒数第二根」永久跳过（11-26 封禁后就是这种场景）。
    """
    try:
        from datetime import date, timedelta
        d0 = date(*(int(x) for x in a.split("-")[:3]))
        d1 = date(*(int(x) for x in b.split("-")[:3]))
    except Exception:
        return 99
    n, d = 0, d0
    while d < d1 and n <= 10:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def main(argv=None) -> int:
    argv = list(argv or [])
    ap = argparse.ArgumentParser(description="补齐申万二级板块日K（东财BK源）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-seed", action="store_true", help="跳过静态种子，强制走东财")
    ap.add_argument("--min", type=int, default=MIN_BARS)
    ap.add_argument("--limit", type=int, default=FULL_LIMIT)
    ap.add_argument("--only", default="")
    ap.add_argument("--source", choices=("auto", "clist", "legacy"), default=SOURCE_DEFAULT,
                    help="板块 K 线取数源：auto=clist 优先/失败回落 legacy（默认）")
    ap.add_argument("--shadow", action="store_true",
                    help="影子比对：clist 与 legacy 同时抓，只落差异报告，不写 klines.json")
    args = ap.parse_args(argv)

    os.makedirs(LOG_DIR, exist_ok=True)
    fh = open(os.path.join(LOG_DIR, f"sector-kline-{datetime.now():%Y-%m-%d}.log"), "a", encoding="utf-8")
    t0 = time.time()
    log("=" * 60, fh)
    log("板块日K补齐开始", fh)

    meta = _read(META_FILE) or {}
    mapdoc = _read(MAP_FILE) or _read(MAP_FILE_FALLBACK) or {}
    bk_map = mapdoc.get("map") or {}
    if not meta or not bk_map:
        log("[FAIL] 缺少 sectors_meta.json 或 sector_bk_map.json 映射种子", fh)
        fh.close()
        return 1

    only = {c.strip().lower() for c in args.only.split(",") if c.strip()} or None
    store = load_store()
    mdate = market_date(store)

    # 静态种子：优先实例目录，回落默认入仓目录；读失败/禁用则为空
    seed: dict[str, list[dict]] = {}
    seed_asof = ""
    if not args.no_seed:
        sdoc = _read(SEED_FILE) or _read(SEED_FILE_FALLBACK) or {}
        seed_raw = sdoc.get("map") or {}
        seed_asof = sdoc.get("asof") or ""
        for c, v in seed_raw.items():
            nodes = v.get("nodes") if isinstance(v, dict) else v
            bars = [b for b in (nodes or []) if b.get("date")]
            if len(bars) >= args.min:
                seed[c] = bars
    log(f"本地标的 {len(store)} · 市场最新交易日 {mdate} · 映射种子 {len(bk_map)} 条 · "
        f"静态种子 {len(seed)} 板块(末{seed_asof or '无'}){' [已禁用]' if args.no_seed else ''}", fh)

    # 划分待办：新鲜 → 跳过；落后 1~STALE_DAYS 天 → 轻量增量（抓最近 LIGHT_LIMIT 根）；
    # 落后 > STALE_DAYS 天或历史不足 → 全量补齐（种子优先）。
    # 关键：不能再把「落后 1 天」当「已最新」跳过——11-25 板块停更事故的根因。
    todo, skip, stale_alert = [], [], []
    for pt in meta:
        if only and pt not in only:
            continue
        bk = bk_map.get(pt)
        if not bk:
            skip.append((pt, "无BK映射"))
            continue
        bars = store.get(pt) or []
        lag = _days_between(bars[-1]["date"], mdate) if (mdate and bars) else 0
        if not args.force and bars and len(bars) >= args.min and lag <= 0:
            skip.append((pt, f"已最新({len(bars)}根,{bars[-1]['date']})"))
            continue
        if lag > ALERT_LAG_DAYS:
            stale_alert.append((pt, lag, bars[-1]["date"] if bars else "无"))
        if args.force or not bars or len(bars) < args.min or lag > STALE_DAYS:
            mode = "全量"
        else:
            mode = "轻量"
        todo.append((pt, bk, len(bars), mode, lag))

    if stale_alert:
        log(f"[WARN] {len(stale_alert)} 个板块末日期落后市场超过 {ALERT_LAG_DAYS} 天：", fh)
        for pt, lag, last in stale_alert[:10]:
            log(f"   {pt} 末 {last} 落后 {lag} 天", fh)
        if len(stale_alert) > 10:
            log(f"   ... 其余 {len(stale_alert)-10} 个略", fh)
    n_light = sum(1 for x in todo if x[3] == "轻量")
    log(f"② 待补 {len(todo)}（轻量 {n_light} · 全量 {len(todo)-n_light}）· 跳过 {len(skip)}", fh)
    for pt, why in skip[:6]:
        log(f"   skip {pt}: {why}", fh)

    if args.dry_run:
        for pt, bk, n, mode, lag in todo[:20]:
            log(f"   todo {pt} → {bk} [{mode}] 落后{lag}天 (本地{n}根)", fh)
        log(f"[DRY-RUN] 未抓数、未写盘", fh)
        fh.close()
        return 0

    if not todo:
        log("全部板块已最新，无需抓取，klines.json 未改动", fh)
        fh.close()
        return 0

    from datasource import sectorflow

    # ---- clist 批量快照：一次拿全部板块当日 OHLCV + 资金流 ----
    # 只在「只差今天这一根」时启用；15:05 前/休市/周末一律不写，宁可少一天也不污染历史。
    snap, snap_day, snap_note = None, None, ""
    today_iso = datetime.now().strftime("%Y-%m-%d")
    gap = _trading_gap(mdate, today_iso) if mdate else 99
    only_today = (gap == 1)          # 市场末日恰好是上一个交易日 → 只差今天
    if args.source != "legacy" and todo:
        if not only_today:
            snap_note = (f"市场末日 {mdate} 距今天 {today_iso} 隔 {gap} 个工作日，"
                         f"中间有缺口，本轮回退逐板块补齐")
            log(f"① clist 快照跳过：{snap_note}", fh)
        else:
            try:
                snap = sectorflow.sector_snapshot()
                srows = snap.get("rows") or {}
                zero = sum(1 for r in srows.values() if not r.get("chg"))
                if not snap.get("closed"):
                    snap_note = "未到 15:05，clist 拿到的是未收盘半根，只读不写"
                elif datetime.now().weekday() >= 5:
                    snap_note = "周末，clist 快照不作为新交易日写入"
                elif srows and zero >= len(srows) * 0.9:
                    snap_note = f"九成板块涨跌幅为 0（{zero}/{len(srows)}），疑似休市，拒写"
                else:
                    snap_day = today_iso
                log(f"① clist 快照 {snap['count']} 个板块（pz={snap['pz']} · {snap['pages']} 页 · "
                    f"total={snap['total']}）· 写入日 {snap_day or '不写'}"
                    + (f" · {snap_note}" if snap_note else ""), fh)
                if args.shadow:
                    snap_day = snap_day or mdate      # 影子模式只比对，用市场末日对齐
            except Exception as e:  # noqa: BLE001
                snap_note = f"{type(e).__name__}: {e}"
                snap = None
                log(f"① clist 快照失败，回落逐板块抓取：{snap_note}", fh)
                if args.source == "clist":
                    log("[FAIL] --source clist 指定但快照不可用，klines.json 未改动", fh)
                    fh.close()
                    return 1

    got, failed, short = 0, 0, 0
    zero_gain = 0                   # 抓回来但不比本地新的板块数（不计成功，见合并处）
    got_light, got_full = 0, 0
    from_seed = 0                   # 本次由静态种子补齐的板块数（零网络）
    from_clist = 0                  # 本次由 clist 批量快照补齐的板块数
    shadow_rows: list[dict] = []    # --shadow 比对明细
    streak = 0                      # 连续失败计数（仅东财抓取路径）：单个板块偶发失败跳过即可
    STREAK_LIMIT = 5
    for i, (pt, bk, n, mode, lag) in enumerate(todo, 1):
        src = "种子"
        local_last = (store.get(pt) or [{}])[-1].get("date")
        # clist 只覆盖「当日一根」：本地末日必须正好是市场末日（即只差今天），
        # 落后多天说明中间还有缺口，硬用 clist 会留下断档，必须逐板块抓 kline。
        can_clist = bool(snap_day and snap and only_today and mode == "轻量"
                         and local_last == mdate and bk in (snap.get("rows") or {}))

        if args.shadow:
            srow = ((snap or {}).get("rows") or {}).get(bk) or {}
            legacy = sectorflow.fetch_bk_kline(bk, limit=min(args.limit, LIGHT_LIMIT))
            last = legacy[-1] if legacy else {}
            diff = (round((srow["close"] - last["close"]) / last["close"] * 100, 3)
                    if srow.get("close") and last.get("close") else None)
            shadow_rows.append({
                "pt": pt, "bk": bk, "day": snap_day,
                "clist": {k: srow.get(k) for k in
                          ("open", "high", "low", "close", "volume", "amount", "chg", "ohlc_partial")},
                "legacy": {k: last.get(k) for k in
                           ("date", "open", "high", "low", "close", "volume", "amount")},
                "close_diff_pct": diff,
            })
            log(f"   [shadow {i}/{len(todo)}] {pt}←{bk} clist={srow.get('close')} "
                f"legacy={last.get('close')} 偏差={diff}% "
                f"{'[OHLC缺失]' if srow.get('ohlc_partial') else ''}", fh)
            time.sleep(REQ_SLEEP)
            continue

        # 轻量增量只抓东财最近 LIGHT_LIMIT 根；不查种子（种子末日期更旧，无意义）。
        #
        # 全量模式用种子，但只在「种子确实比本地新」时才用。种子与本地末日相同
        # 或更旧时，merge 后零增量却仍会 got+=1 记成「补齐成功」——2026-11-27 排查
        # 发现 49 个板块末日期钉在 11-11 就是这样：每轮 EOD 都报成功，数据一天没动。
        fresh = []
        if mode == "全量":
            cand = seed.get(pt) or []
            if cand and (not local_last or cand[-1]["date"] > local_last):
                fresh = cand
        if can_clist:
            srow = snap["rows"][bk]
            fresh = [{"date": snap_day, "open": srow["open"], "high": srow["high"],
                      "low": srow["low"], "close": srow["close"],
                      "volume": srow["volume"], "amount": srow["amount"]}]
            src = "clist"
        elif not fresh:
            src = "东财"
            limit = min(args.limit, LIGHT_LIMIT) if mode == "轻量" else args.limit
            fresh = sectorflow.fetch_bk_kline(bk, limit=limit)
        # 轻量模式只抓 12 根，不适用 MIN_BARS 门槛；只要抓到有效根就合并。
        min_ok = 1 if mode == "轻量" else args.min
        if len(fresh) < min_ok:
            short += 1
            if src == "东财":
                streak += 1
                why = (sectorflow._MEM.get("last_kline_error") or "").strip()
                extra = f"；上游异常：{why}" if why else ""
            else:
                streak = 0          # 种子路径不消耗上游配额
                extra = ""
            log(f"   [{i}/{len(todo)}] {pt}←{bk} [{mode}/{src}] 仅 {len(fresh)} 根(<{min_ok})，"
                f"保留本地 {n} 根（连续失败 {streak}）{extra}", fh)
            # 底层已判定 IP 风控熔断时立刻停手：剩下的一律不发请求，等解禁重跑
            if src == "东财" and sectorflow.upstream_state().get("blocked"):
                log(f"   [{i}/{len(todo)}] 上游已熔断，剩余 {len(todo)-i} 个跳过；"
                    f"解禁后再跑，本地数据保持原样", fh)
                failed += len(todo) - i
                break
            if src == "东财" and streak >= STREAK_LIMIT:
                log(f"   [{i}/{len(todo)}] 连续 {streak} 个失败，判定上游不可用，"
                    f"剩余 {len(todo)-i} 个跳过并保留原数据；下次重跑从未补的继续", fh)
                failed += len(todo) - i
                break
            if src == "东财":
                time.sleep(min(0.5 * (2 ** (streak - 1)), REQ_SLEEP))
            continue
        streak = 0                  # 成功即清零
        store[pt] = merge_bars(store.get(pt), fresh)
        new_last = (store[pt] or [{}])[-1].get("date")
        if local_last and new_last and new_last <= local_last:
            # 合了等于没合：拿回来的数据不比本地新。以前这种情况照样 got+=1，
            # 于是「每天报补齐成功、数据一动不动」——49 个板块钉在 11-11 的元凶。
            zero_gain += 1
            log(f"   [{i}/{len(todo)}] {pt}←{bk} [{mode}/{src}] 零增量"
                f"（本地末 {local_last}，新数据末 {new_last}），不计成功", fh)
            continue
        got += 1
        if mode == "轻量":
            got_light += 1
        else:
            got_full += 1
        if src == "种子":
            from_seed += 1
        elif src == "clist":
            from_clist += 1
        else:
            time.sleep(REQ_SLEEP)          # 只有逐板块抓 kline 才需要刻意间隔
        log(f"   [{i}/{len(todo)}] {pt}←{bk} [{mode}/{src}] {len(fresh)} 根 → 合并后 {len(store[pt])} 根"
            f" (末 {store[pt][-1]['date']})", fh)

    # ---- 影子比对：不写库，只落差异报告，供解封后核对 clist 与 kline 口径 ----
    if args.shadow:
        path = os.path.join(LOG_DIR, f"sector-clist-shadow-{datetime.now():%Y-%m-%d}.json")
        bad = [x for x in shadow_rows if x["close_diff_pct"] is None
               or abs(x["close_diff_pct"]) > 0.05]
        json.dump({"asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "snap_day": snap_day, "total": len(shadow_rows),
                   "mismatch": len(bad), "items": shadow_rows},
                  open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        log(f"③ 影子比对 {len(shadow_rows)} 个板块，偏差>0.05% 或缺失 {len(bad)} 个 → {path}", fh)
        log("   klines.json 未改动（--shadow）", fh)
        fh.close()
        return 0

    # 防复发校验：腾讯 pt 快照一旦混进东财 BK 序列，会出现量纲断层
    # （2026-11-24 首次、2026-11-27 复发）。写盘前剔除少数派段。
    #
    # 注意：这里的剔除是「宁可少几根也不留断层」的兜底，不是修复。当污染段是
    # 长历史（例：元件 308 根旧口径 + 10 根东财真值）时，剔除只会把历史削光，
    # 所以低于 MIN_BARS 就保留原样、只告警，交给 jobs.repair_sector_kline_scale
    # 用东财重抓整段覆盖——那才是能补回历史的正解。
    from jobs.repair_sector_kline_scale import outlier_indexes
    fixed_jumps = 0
    for pt in [c for c in store if str(c).lower().startswith("pt")]:
        nodes = store[pt]
        bad = outlier_indexes(nodes)
        if not bad:
            continue
        bad_dates = {nodes[j]["date"] for j in bad}
        cleaned = [b for b in nodes if b["date"] not in bad_dates]
        if len(cleaned) < MIN_BARS:
            log(f"   [量纲校验] {pt} 剔除后仅剩 {len(cleaned)} 根（<{MIN_BARS}），"
                f"保留原样并告警；请跑 python -m jobs.repair_sector_kline_scale --only {pt}", fh)
            continue
        store[pt] = cleaned
        fixed_jumps += 1
        log(f"   [量纲校验] {pt} 剔除断层 {len(nodes)} → {len(cleaned)} 根", fh)

    if got or fixed_jumps:
        save_store(store)
        log(f"③ 写入 {KLINE_FILE}：本次补齐 {got} 个板块（轻量 {got_light} · 全量 {got_full}，"
            f"其中 clist {from_clist} · 种子 {from_seed} · 东财 {got-from_seed-from_clist}）"
            f"· 历史不足保留 {short} · 失败/熔断 {failed} · 零增量 {zero_gain}", fh)
    else:
        log(f"③ 无板块补齐成功（不足 {short} · 失败 {failed} · 零增量 {zero_gain}），"
            f"klines.json 未改动", fh)

    log(f"完成，耗时 {time.time()-t0:.1f}s", fh)
    fh.close()
    return 0 if (got or not todo) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
