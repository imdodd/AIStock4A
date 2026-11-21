"""生成「利弗莫尔六档记录法」验证报告

验证目标：自然回升/自然回撤 能否用算法判定；两种候选口径的差异有多大；
以及六档法结论与温斯坦阶段的对照关系。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.livermore_key import (analyze, summary, trade_signals, pair_trades, trade_stats,
                                   SEC_UP, NAT_UP, UP, SEC_DN, NAT_DN, DN, NOISE)

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(DATA_DIR, "raw")
SNAP = os.path.join(DATA_DIR, "snapshot")
OUT_DIR = os.path.abspath(os.path.join(HERE, "..", "reports"))

# 默认示例标的（大市值、流动性好）；想验证自己的股票池可在此替换，
# 或后续改为从配置/自选文件读取。需保证 raw/klines.json 里有对应 K 线。
TARGETS = [
    ("sh600519", "贵州茅台"),
    ("sz300750", "宁德时代"),
    ("sh600036", "招商银行"),
    ("sh601318", "中国平安"),
    ("sz000858", "五粮液"),
    ("sz000333", "美的集团"),
    ("sh600900", "长江电力"),
    ("sh000688", "科创50"),
    ("sh000001", "上证指数"),
]
MAIN = "sh600519"
KEEP = 260
HARD_STOP = 0.10        # 原书纪律：单笔亏损绝不放过 10%


def rows_of(raw, code):
    return [dict(date=n["date"], open=n["open"], high=n["high"], low=n["low"],
                 close=n["last"], volume=n.get("volume") or 0, amount=n.get("amount") or 0)
            for n in raw[code]["nodes"]]


def stage_map():
    m = {}
    try:
        mk = json.load(open(os.path.join(SNAP, "market.json"), encoding="utf-8"))
        for x in mk.get("indices", []):
            m[x["code"]] = dict(stage=x.get("stage"), sub=x.get("sub"),
                                stage_name=x.get("stage_name"), price=x.get("price"),
                                dev250=x.get("dev250"), rs60=x.get("rs60"))
    except Exception:
        pass
    try:
        wl = json.load(open(os.path.join(SNAP, "watchlist.json"), encoding="utf-8"))
        for x in wl:
            m[x["code"]] = dict(stage=x.get("stage"), sub=x.get("sub"),
                                stage_name=x.get("stage_name"), price=x.get("price"),
                                dev250=x.get("dev250"), rs60=x.get("rs60"))
    except Exception:
        pass
    return m


def ma(rows, n):
    out = []
    for i in range(len(rows)):
        out.append(None if i + 1 < n else round(sum(r["close"] for r in rows[i - n + 1:i + 1]) / n, 2))
    return out


def build():
    raw = json.load(open(os.path.join(RAW, "klines.json"), encoding="utf-8"))
    stages = stage_map()

    table, payload = [], {}
    tot = diff = 0
    agree = 0
    nat_ratio = []

    for code, name in TARGETS:
        rows = rows_of(raw, code)
        ra = analyze(rows, "pos")
        rb = analyze(rows, "amp")
        sa, sb = summary(ra["state"]), summary(rb["state"])

        for x, y in zip(ra["records"], rb["records"]):
            tot += 1
            diff += (x["col"] != y["col"])

        st = stages.get(code, {})
        stage = st.get("stage")
        trend_down = ra["state"]["trend"] == "down"
        if stage is None:
            ok = None
        else:
            ok = (trend_down and stage in (3, 4)) or ((not trend_down) and stage in (1, 2))
            agree += 1 if ok else 0

        nat_ratio.append(sa["自然占比"])
        table.append(dict(
            code=code, name=name,
            price=round(rows[-1]["close"], 2),
            stage=stage, sub=st.get("sub"), stage_name=st.get("stage_name"),
            pos_col=sa["当前列"], pos_trend=sa["趋势"],
            pos_nat_up=sa["自然回升"], pos_sec_up=sa["次级回升"], pos_up=sa["上升趋势"],
            pos_nat_dn=sa["自然回撤"], pos_sec_dn=sa["次级回撤"], pos_dn=sa["下降趋势"],
            amp_col=sb["当前列"], amp_nat_up=sb["自然回升"], amp_sec_up=sb["次级回升"],
            amp_up=sb["上升趋势"], amp_nat_dn=sb["自然回撤"], amp_sec_dn=sb["次级回撤"],
            amp_dn=sb["下降趋势"],
            pos_ratio=sa["自然占比"], amp_ratio=sb["自然占比"],
            agree=ok, segments=len(ra["records"]),
        ))

        if code == MAIN:
            keep = rows[-KEEP:]
            off = len(rows) - len(keep)
            ma250 = ma(rows, 250)[off:]
            recs = [r for r in ra["records"] if r["idx"] >= off]
            payload = dict(
                code=code, name=name,
                dates=[r["date"] for r in keep],
                bars=[[r["open"], r["close"], r["low"], r["high"]] for r in keep],
                ma250=ma250,
                swings=[dict(idx=s["idx"] - off, date=s["date"], price=round(s["price"], 2), kind=s["kind"],
                             tentative=bool(s.get("tentative")))
                        for s in ra["swings"] if s["idx"] >= off],
                records=[dict(idx=r["idx"] - off, date=r["date"], start_idx=r["start_idx"] - off,
                              start_date=r["start_date"], col=r["col"], changed=r["changed"],
                              reversed=r["reversed"], amp_pct=r["amp_pct"],
                              start_price=round(r["start_price"], 2), price=round(r["price"], 2),
                              key_mark=r["key_mark"], seg=r["seg"])
                         for r in recs],
                records_amp=[dict(idx=r["idx"] - off, date=r["date"], col=r["col"])
                             for r in rb["records"] if r["idx"] >= off],
                # 关键点：换列时被划下划线的那条价格（原书 pivotal point）
                key_points=[dict(idx=r["idx"] - off, date=r["date"], price=r["key_mark"],
                                 col=r["col"], rev=bool(r["reversed"]), start_date=r["start_date"])
                            for r in recs if r["changed"] and r["key_mark"]],
                state=dict(col=ra["state"]["col"], trend=ra["state"]["trend"],
                           key_up=ra["state"]["key_up"], key_dn=ra["state"]["key_dn"],
                           up_ext=ra["state"]["up_ext"], dn_ext=ra["state"]["dn_ext"]),
            )
            # 最近换列明细
            changes = [r for r in recs if r["changed"]][-14:]
            payload["changes"] = [dict(date=r["date"], frm="-", to=r["col"], seg=r["seg"],
                                       start_price=round(r["start_price"], 2), price=round(r["price"], 2),
                                       amp_pct=r["amp_pct"], key=r["key_mark"], rev=r["reversed"])
                                  for r in changes]

            # ---- 按关键点确认的买卖点（严格 point-in-time）----
            sigs = trade_signals(ra["records"], rows)
            tr_key = pair_trades(sigs, rows)
            tr_stp = pair_trades(sigs, rows, hard_stop=HARD_STOP)
            payload["signals"] = [dict(idx=s["idx"] - off, date=s["date"], type=s["type"],
                                       price=s["price"], trigger=s["trigger"],
                                       vol=s["vol_ratio"], lag=s["lag"])
                                  for s in sigs if s["idx"] >= off]
            payload["trades"] = [dict(
                buy_date=t["buy"]["date"], buy_price=t["buy"]["price"], buy_trig=t["buy"]["trigger"],
                buy_vol=t["buy"]["vol_ratio"], sell_date=t["sell"]["date"], sell_price=t["sell"]["price"],
                sell_trig=t["sell"]["trigger"], exit=t["exit"], pnl=t["pnl"],
                days=t["days"], result=t["result"])
                for t in tr_key if t["kind"] == "round" and t["buy"]["idx"] >= off]
            payload["open_pos"] = next((dict(date=t["buy"]["date"], price=t["buy"]["price"],
                                             trig=t["buy"]["trigger"]) for t in tr_key if t["kind"] == "open"), None)
            payload["stats"] = dict(key=trade_stats(sigs, tr_key, rows),
                                    stop=trade_stats(sigs, tr_stp, rows), hard_stop=HARD_STOP)

    agg = dict(
        segments=tot, diff=diff, diff_pct=round(diff / tot * 100, 1),
        agree=agree, total=len(TARGETS), nat_avg=round(sum(nat_ratio) / len(nat_ratio), 1),
        date=json.load(open(os.path.join(SNAP, "market.json"), encoding="utf-8")).get("date"),
    )
    return dict(agg=agg, table=table, main=payload, generated=datetime.now().strftime("%Y-%m-%d %H:%M"))


TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>利弗莫尔六档记录法 · 算法可行性验证</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<style>
:root{
  --bg:#0a0d13; --panel:#111620; --panel2:#161c28; --line:rgba(255,255,255,.07);
  --txt:#e6ebf5; --muted:#8b95a8; --red:#ff4d4f; --green:#2ecc71; --blue:#4d8dff; --amber:#f5a623;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1360px;margin:0 auto;padding:28px 24px 60px}
h1{font-size:22px;margin:0 0 6px;letter-spacing:.5px}
.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
.sub b{color:var(--blue);font-weight:600}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px;margin-bottom:6px}
.card .v{font-size:24px;font-weight:700;letter-spacing:.3px}
.card .v small{font-size:13px;color:var(--muted);font-weight:400;margin-left:4px}
.card .n{color:var(--muted);font-size:11.5px;margin-top:4px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
.panel h2{font-size:15px;margin:0 0 4px;font-weight:600}
.panel .hint{color:var(--muted);font-size:12px;font-weight:400;margin-left:8px}
#chart{width:100%;height:520px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:11.5px;text-align:right;background:var(--panel2);position:sticky;top:0}
th:first-child,td:first-child{text-align:left}
tbody tr:hover{background:rgba(77,141,255,.06)}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.t-up{background:rgba(255,77,79,.14);color:#ff7b7d}
.t-dn{background:rgba(46,204,113,.14);color:#5fdc98}
.t-neu{background:rgba(139,149,168,.15);color:#a8b2c4}
.t-warn{background:rgba(245,166,35,.15);color:#f5b74a}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-top:10px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.note{background:var(--panel2);border-left:3px solid var(--blue);padding:12px 14px;border-radius:0 8px 8px 0;font-size:13px;color:#c3ccdb}
.note b{color:#fff}
.warn{border-left-color:var(--amber)}
.tbl-wrap{max-height:520px;overflow:auto}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
ul{margin:6px 0 0;padding-left:18px;color:#c3ccdb}
li{margin:5px 0}
code{background:rgba(255,255,255,.06);padding:1px 6px;border-radius:4px;font-size:12px;color:#9fc3ff}
.tstats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0 14px}
.tstat{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tstat .k{color:var(--muted);font-size:11.5px;margin-bottom:5px}
.tstat .v{font-size:20px;font-weight:700;letter-spacing:.3px}
.tstat .n{color:var(--muted);font-size:11px;margin-top:3px}
@media(max-width:1080px){.cards{grid-template-columns:repeat(2,1fr)}.two{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <h1>利弗莫尔六档记录法 · 算法可行性验证</h1>
  <div class="sub">验证「自然回升 / 自然回撤」能否算法化 · 9 只标的 · 数据截至 <b>__DATA_DATE__</b> 收盘 · 生成于 __GEN__</div>

  <div class="cards">
    <div class="card"><div class="k">能否算法判定</div><div class="v" style="color:var(--green)">能</div><div class="n">ZigZag + 三级分层，无未来函数</div></div>
    <div class="card"><div class="k">两种口径分歧率</div><div class="v">__DIFF__<small>%</small></div><div class="n">__SEG__ 段中 __DIFFN__ 段判定不同</div></div>
    <div class="card"><div class="k">自然级占比（位置口径）</div><div class="v">__NAT__<small>%</small></div><div class="n">其余为次级 / 趋势列</div></div>
    <div class="card"><div class="k">与温斯坦阶段一致</div><div class="v">__AGREE__<small>/ __TOTAL__</small></div><div class="n">趋势方向 vs S1–S4 是否同向</div></div>
  </div>

  <div class="panel">
    <h2>__MAIN_NAME__ 的六档记录 vs 温斯坦阶段<span class="hint">背景色 = 当时所处的那一列；橙折线 = ZigZag 摆动点；灰虚线 = 年线 MA250；黄短横 = 关键点（换列时被划下划线的那条价格）</span></h2>
    <div id="chart"></div>
    <div class="legend">
      <span><i style="background:#ff4d4f"></i>上升趋势</span>
      <span><i style="background:#ff8b76"></i>自然回升</span>
      <span><i style="background:#ffc2b8"></i>次级回升</span>
      <span><i style="background:#2ecc71"></i>下降趋势</span>
      <span><i style="background:#63d297"></i>自然回撤</span>
      <span><i style="background:#b7e4c7"></i>次级回撤</span>
      <span><i style="background:#4a5160"></i>噪音（不记）</span>
      <span><i style="background:#ffd666"></i>关键点·趋势反转（标价）</span>
      <span><i style="background:rgba(245,166,35,.85)"></i>关键点·同级换列</span>
      <span>淡黄虚线 = 关键点一直有效，直到被下一个替代</span>
      <span>红/绿横虚线 = 当前有效关键点（突破转涨 / 跌破转跌）</span>
    </div>
  </div>

  <div class="panel">
    <h2>按关键点确认的买卖点<span class="hint">规则：下降趋势中收盘突破当时的关键点 → 买入（B）；上升趋势中收盘跌破当时的关键点 → 卖出（S）。全部按确认当日收盘成交，不预知未来，假信号照实保留</span></h2>
    <div class="tstats" id="tradeStats"></div>
    <div class="tbl-wrap" style="max-height:340px">
      <table><thead><tr>
        <th>买入日</th><th>买价</th><th>越过关键点</th><th>突破量比</th>
        <th>卖出日</th><th>卖价</th><th>跌破关键点</th>
        <th>收益</th><th>持有</th><th>结果</th>
      </tr></thead><tbody id="tradeBody"></tbody></table>
    </div>
    <div class="legend"><span><b>下表仅列图中区间</b>（2025-09 起），上方统计为全样本（含 2024 年早期 3 笔）</span><span>量比 = 确认日成交量 / 前 50 日均量，原书要求关键点突破需有量能配合（&gt;1.5 视为有效放大）</span><span>按收盘价复利，未计手续费与滑点</span></div>
  </div>

  <div class="panel">
    <h2>__MAIN_NAME__ 最近 14 次换列<span class="hint">「换列」即原书规则三：离开那一列时把最后一条价格划线，成为关键点</span></h2>
    <div class="tbl-wrap" style="max-height:340px">
      <table><thead><tr>
        <th>日期</th><th>方向</th><th>记入列</th><th class="num">段起点</th><th>终点</th><th>幅度</th><th>划线关键点</th><th>性质</th>
      </tr></thead><tbody id="chgBody"></tbody></table>
    </div>
  </div>

  <div class="panel">
    <h2>九只标的：六档当前列 vs 温斯坦阶段</h2>
    <div class="tbl-wrap">
      <table><thead><tr>
        <th>标的</th><th>现价</th><th>温斯坦</th><th>六档趋势</th><th>当前列</th>
        <th>自然回升</th><th>次级回升</th><th>上升趋势</th>
        <th>自然回撤</th><th>次级回撤</th><th>下降趋势</th><th>自然占比</th><th>一致</th>
      </tr></thead><tbody id="tblBody"></tbody></table>
    </div>
    <div class="legend"><span>计数口径 = 位置口径（pos）。幅度口径的对照见下节。</span></div>
  </div>

  <div class="panel">
    <h2>两种口径的判定差异<span class="hint">同一段逆向波动，位置口径与幅度口径给出的列可能不同</span></h2>
    <div class="two">
      <div>
        <div class="note">
          <b>位置口径（本报告主用）</b><br/>
          创新高/新低 → 趋势列；<br/>
          逆向波动越过「本轮逆向极值」→ 自然级；<br/>
          没越过 → 次级。<br/>
          <span style="color:var(--muted)">语义：看这次波动有没有<b>推进价格结构</b>。</span>
        </div>
      </div>
      <div>
        <div class="note warn">
          <b>幅度口径（对照）</b><br/>
          创新高/新低 → 趋势列；<br/>
          逆向幅度 ≥ 2×ATR → 自然级；<br/>
          在 1–2×ATR 之间 → 次级。<br/>
          <span style="color:var(--muted)">语义：看这次波动有没有<b>足够力度</b>。</span>
        </div>
      </div>
    </div>
    <ul>
      <li>两者在 <b>__DIFFPCT__%</b> 的段落上给出不同结论，说明「口径」不是细节，会直接改变叙事。</li>
      <li>位置口径在<b>强趋势股</b>上自然级极少（趋势内回撤几乎都被判次级），在<b>震荡区间</b>上自然级明显增多。</li>
      <li>幅度口径依赖 ATR 倍数，参数一动结论就变；位置口径只依赖摆动点提取，<b>更稳定也更接近原书「推进结构」的本意</b>。</li>
      <li>结论：<b>以位置口径为准</b>，幅度口径只作为「力度」的辅助读数，不单独成列。</li>
    </ul>
  </div>

  <div class="panel">
    <h2>口径与实现说明</h2>
    <ul>
      <li><b>摆动点</b>：ZigZag 扫描，反向走够 <code>1.2 × ATR20</code> 才确认转折；最后一根未确认的极值标为 tentative，用于判断「当前在哪一列」。</li>
      <li><b>噪音过滤</b>：幅度 &lt; 阈值不占列 —— 对应原书「不足 6 点不记」。原书的 6 点只适用于 30–150 美元区间的铁路股，直接搬到 A 股无效，故改为 ATR 自适应。</li>
      <li><b>趋势确认线</b>：最近一次创新高那一段的起点低点（<code>key_up</code>），跌破即转跌；创新低段的起点高点（<code>key_dn</code>），突破即转涨。等价于原书「主趋势列留下的关键点」，且随趋势推进自动上移/下移。</li>
      <li><b>无未来函数</b>：每一段都是「走够阈值才确认」，逐日回放不会偷看未来。</li>
      <li><b>买卖点规则</b>：下降趋势中收盘突破当时的关键点 → 买入；上升趋势中收盘跌破当时的关键点 → 卖出；同级换列只移动行动线、不触发交易。信号打在「段内首次越过关键线那天」（当时即可知晓），而非 ZigZag 极值确认日（滞后 9–11 个交易日，价已远离）。全部按当日收盘成交，不预知未来，假信号照实保留。</li>
      <li><b>两个时间口径（悬浮框同时显示）</b>：背景色带是<b>事后回看</b>——整段按最终归类涂色；「当时已确认」是<b>利弗莫尔口径</b>——突破关键点确认后才换列，确认前的日子仍属旧列。因此反弹途中悬停，背景可能已是红色、而当时确认列仍是下降趋势，两者都对，以「当时已确认」做回测、以背景色看结构。</li>
      <li><b>已知分歧</b>：原书说 100→88→95 时 95 应记「自然回升」。位置口径下 95 属次级（未越过本轮反弹极值前的第一波判自然，此处为第一波，故判<b>自然</b>）；但如果把障碍设为「本轮下跌起点 100」，95 就会退化为次级。<b>这处语义在原书中本身存在两种读法</b>，本报告采用前者。</li>
    </ul>
  </div>

  <div class="panel">
    <h2>对现有系统的意义</h2>
    <div class="note">
      现有 <code>engines/pivot.py</code> 只做了「突破 60 日最高收盘 + 量能确认」这一环，相当于六档法里的<b>「上升趋势」列确认</b>，
      中间的自然 / 次级两层被跳过了。补上之后能回答一个现在答不了的问题：
      <b>回调是正常修正（次级回撤），还是结构已经走坏（自然回撤 / 转下降趋势）</b>。
      最小改动是在 <code>pivot.py</code> 旁接入本模块的 <code>zigzag()</code> 与 <code>build_records()</code>，阈值复用已有的 ATR20，不新增参数。
    </div>
  </div>
</div>

<script>
const D = __DATA__;
const COL = {
  '上升趋势': {bg:'rgba(255,77,79,.20)', fg:'#ff4d4f'},
  '自然回升': {bg:'rgba(255,139,118,.18)', fg:'#ff8b76'},
  '次级回升': {bg:'rgba(255,194,184,.14)', fg:'#ffc2b8'},
  '下降趋势': {bg:'rgba(46,204,113,.20)', fg:'#2ecc71'},
  '自然回撤': {bg:'rgba(99,210,151,.18)', fg:'#63d297'},
  '次级回撤': {bg:'rgba(183,228,199,.14)', fg:'#b7e4c7'},
  '噪音':     {bg:'rgba(74,81,96,.25)',   fg:'#4a5160'}
};

// ---------- 主图 ----------
const m = D.main;
const KP = (m.key_points || []).slice();   // 关键点序列（时间升序）
const SIG = (m.signals || []).slice();     // 按关键点确认的买卖点
const areas = m.records.map(r => [
    {xAxis: r.start_date, itemStyle:{color: (COL[r.col]||COL['噪音']).bg}},
    {xAxis: r.date}
  ]);

const chart = echarts.init(document.getElementById('chart'));
chart.setOption({
  animation:false,
  backgroundColor:'transparent',
  grid:{left:58, right:64, top:18, bottom:56},
  tooltip:{
    trigger:'axis', axisPointer:{type:'cross'},
    backgroundColor:'rgba(17,22,32,.96)', borderColor:'rgba(255,255,255,.12)',
    textStyle:{color:'#e6ebf5', fontSize:12},
    formatter: p => {
      const i = p[0].dataIndex, d = m.dates[i];
      // 段归属 = 背景色口径：start_idx <= i < idx（事后回看，整段涂色）
      const seg = m.records.find(r => r.start_idx <= i && i < r.idx) ||
                  m.records[m.records.length - 1];
      // 当时确认 = 利弗莫尔口径：突破关键点确认后才换列
      const conf = m.records.slice().reverse().find(r => r.idx <= i);
      const k = m.bars[i];
      let s = `<b>${d}</b><br/>开 ${k[0]}　收 ${k[1]}<br/>低 ${k[2]}　高 ${k[3]}`;
      if (seg) s += `<br/>所在段（背景色）：<span style="color:${(COL[seg.col]||{}).fg||'#fff'}">${seg.col}</span>（${seg.start_date} 起）`;
      if (conf && seg && conf.col !== seg.col && seg.idx > i)
        s += `<br/>当时已确认：<span style="color:${(COL[conf.col]||{}).fg||'#fff'}">${conf.col}</span><span style="color:#8b95a8">（本段 ${seg.date} 才确认）</span>`;
      else if (conf)
        s += `<br/>当时已确认：<span style="color:${(COL[conf.col]||{}).fg||'#fff'}">${conf.col}</span>`;
      const kp = KP.find(x => x.date === d);
      if (kp) s += `<br/>关键点：<span style="color:#ffd666;font-weight:600">${kp.price}</span>`
              + `<span style="color:#8b95a8">（${kp.rev ? '趋势反转' : '同级换列'}时划下，此后有效）</span>`;
      return s;
    }
  },
  xAxis:{type:'category', data:m.dates, boundaryGap:true,
    axisLine:{lineStyle:{color:'rgba(255,255,255,.12)'}},
    axisLabel:{color:'#8b95a8', fontSize:11},
    splitLine:{show:false}},
  yAxis:{scale:true, position:'right',
    axisLabel:{color:'#8b95a8', fontSize:11},
    splitLine:{lineStyle:{color:'rgba(255,255,255,.05)'}}},
  dataZoom:[
    {type:'inside', start:40, end:100},
    {type:'slider', height:16, bottom:10, start:40, end:100,
     borderColor:'rgba(255,255,255,.1)', fillerColor:'rgba(77,141,255,.12)',
     handleStyle:{color:'#4d8dff'}, textStyle:{color:'#8b95a8', fontSize:10}}
  ],
  series:[
    {
      name:'K线', type:'candlestick', data:m.bars,
      itemStyle:{color:'#ff4d4f', color0:'#2ecc71', borderColor:'#ff4d4f', borderColor0:'#2ecc71'},
      markArea:{silent:true, data:areas},
      // 买卖点：B 打在当日最低价下方、S 打在最高价上方
      markPoint:{
        silent:true, animation:false,
        data:[
          ...SIG.filter(s => s.type === 'B').map(s => ({
            coord:[s.date, m.bars[s.idx][2]], symbol:'circle', symbolSize:16,
            symbolOffset:[0, 13],
            itemStyle:{color:'#ff4d4f', borderColor:'#fff', borderWidth:1.2},
            label:{formatter:'B', color:'#fff', fontSize:10, fontWeight:700}
          })),
          ...SIG.filter(s => s.type === 'S').map(s => ({
            coord:[s.date, m.bars[s.idx][3]], symbol:'circle', symbolSize:16,
            symbolOffset:[0, -13],
            itemStyle:{color:'#2ecc71', borderColor:'#fff', borderWidth:1.2},
            label:{formatter:'S', color:'#fff', fontSize:10, fontWeight:700}
          }))
        ]
      },
      // 当前有效关键点：上升趋势看 key_up（跌破转跌），下降趋势看 key_dn（突破转涨）
      markLine:(function(){
        const st = m.state || {}, up = st.trend === 'up';
        const y = up ? st.key_up : st.key_dn;
        if (y == null) return { data:[] };
        const c = up ? '#2ecc71' : '#ff4d4f';
        return {
          silent:true, symbol:'none', animation:false,
          lineStyle:{color:c, type:'dashed', width:1.3, opacity:.9},
          label:{position:'insideEndTop', color:c, fontSize:10, fontWeight:600,
                 formatter:`当前关键点 ${y} · ${up ? '跌破转跌' : '突破转涨'}`},
          data:[{yAxis:y}]
        };
      })()
    },
    {
      name:'MA250', type:'line', data:m.ma250, symbol:'none', smooth:false,
      lineStyle:{color:'rgba(200,210,230,.45)', width:1, type:'dashed'}, connectNulls:true
    },
    {
      name:'摆动点', type:'line',
      data:m.dates.map(d => { const s = m.swings.find(x => x.date === d); return s ? s.price : null; }),
      symbol:'circle', symbolSize:5, connectNulls:true,
      lineStyle:{color:'rgba(245,166,35,.55)', width:1.2},
      itemStyle:{color:'#f5a623', borderColor:'#0a0d13', borderWidth:1}
    },
    {
      // 关键点阶梯：一条关键点一直有效，直到被下一个关键点替代
      name:'关键点', type:'line', step:'end', symbol:'none', connectNulls:true,
      data:(function(){
        if (!KP.length) return [];
        const d = KP.map(k => [k.date, k.price]);
        d.push([m.dates[m.dates.length-1], KP[KP.length-1].price]);
        return d;
      })(),
      lineStyle:{color:'rgba(255,214,102,.38)', width:1, type:'dashed'},
      z:2
    },
    {
      // 关键点短横：换列时被划下划线的那条价格（反转点用亮黄 + 标价）
      name:'关键点', type:'scatter',
      data:KP.map(k => ({
        value:[k.date, k.price], price:k.price, rev:k.rev,
        symbolSize: k.rev ? [22, 4] : [15, 3],
        itemStyle:{ color: k.rev ? '#ffd666' : 'rgba(245,166,35,.85)' }
      })),
      symbol:'rect',
      label:{ show:true, position:'top', distance:4, fontSize:9,
              color:'#ffd666', formatter:p => p.data.rev ? p.data.price : '' },
      z:7
    }
  ]
});
window.addEventListener('resize', () => chart.resize());

// ---------- 买卖点统计与明细 ----------
(function(){
  const st = m.stats || {}, k = st.key || {}, s = st.stop || {};
  const cl = v => v == null ? '#8b95a8' : (v > 0 ? '#ff4d4f' : (v < 0 ? '#2ecc71' : '#8b95a8'));
  const sg = v => v == null ? '—' : (v > 0 ? '+' : '') + v + '%';
  const rr = (k.avg_win != null && k.avg_loss != null && k.avg_loss !== 0)
           ? Math.abs(k.avg_win / k.avg_loss).toFixed(2) : '—';
  document.getElementById('tradeStats').innerHTML = `
    <div class="tstat"><div class="k">纯关键点 · 累计收益</div>
      <div class="v" style="color:${cl(k.cum)}">${sg(k.cum)}</div>
      <div class="n">全样本 ${k.closed} 笔 · 胜率 ${k.win_rate}% · ${k.holding}</div></div>
    <div class="tstat"><div class="k">叠加 10% 硬止损 · 累计</div>
      <div class="v" style="color:${cl(s.cum)}">${sg(s.cum)}</div>
      <div class="n">原书纪律：单笔亏损绝不超过 10%</div></div>
    <div class="tstat"><div class="k">同期买入持有</div>
      <div class="v" style="color:${cl(k.buy_hold)}">${sg(k.buy_hold)}</div>
      <div class="n">首笔买点收盘 → 最新收盘（全样本）</div></div>
    <div class="tstat"><div class="k">盈亏比</div>
      <div class="v">${rr}</div>
      <div class="n">均盈 ${k.avg_win == null ? '—' : k.avg_win + '%'} / 均亏 ${k.avg_loss == null ? '—' : k.avg_loss + '%'}</div></div>`;

  const tb = document.getElementById('tradeBody');
  tb.innerHTML = (m.trades || []).map(t => {
    const ok = t.result === 'win';
    return `<tr>
      <td>${t.buy_date}</td><td>${t.buy_price}</td><td>${t.buy_trig}</td>
      <td>${t.buy_vol == null ? '—' : t.buy_vol + '×'}</td>
      <td>${t.sell_date}</td><td>${t.sell_price}</td><td>${t.sell_trig}</td>
      <td style="color:${ok ? '#ff4d4f' : '#2ecc71'};font-weight:600">${t.pnl > 0 ? '+' : ''}${t.pnl}%</td>
      <td>${t.days} 天</td>
      <td>${ok ? '<span class="tag t-up">盈利</span>' : '<span class="tag t-dn">亏损</span>'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="10" style="text-align:center;color:#8b95a8">本区间无完整交易</td></tr>';
})();

// ---------- 换列表 ----------
const chgBody = document.getElementById('chgBody');
(function(){
  const rows = (m.changes||[]).slice().reverse();
  chgBody.innerHTML = rows.map(r => {
    const c = COL[r.to] || {fg:'#fff'};
    return `<tr>
      <td>${r.date}</td>
      <td>${r.seg==='up'?'<span class="tag t-up">向上</span>':'<span class="tag t-dn">向下</span>'}</td>
      <td><span style="color:${c.fg};font-weight:600">${r.to}</span></td>
      <td>${r.start_price}</td><td>${r.price}</td>
      <td style="color:${r.seg==='up'?'#ff7b7d':'#5fdc98'}">${r.amp_pct.toFixed(1)}%</td>
      <td>${r.key==null?'—':r.key}</td>
      <td>${r.rev?'<span class="tag t-warn">趋势反转</span>':'<span class="tag t-neu">同级换列</span>'}</td>
    </tr>`;
  }).join('');
})();

// ---------- 总表 ----------
document.getElementById('tblBody').innerHTML = D.table.map(r => {
  const st = r.stage==null ? '—' : (r.stage>=3 ? `<span class="tag t-dn">S${r.stage}</span>`
             : r.stage===2 ? `<span class="tag t-up">S2</span>` : `<span class="tag t-neu">S${r.stage}</span>`);
  const c = COL[r.pos_col] || {fg:'#fff'};
  const ag = r.agree===null ? '<span class="tag t-neu">—</span>'
           : r.agree ? '<span class="tag t-up">同向</span>' : '<span class="tag t-warn">背离</span>';
  return `<tr>
    <td>${r.name}<span style="color:#8b95a8;font-size:11px;margin-left:6px">${r.code}</span></td>
    <td>${r.price}</td><td>${st}</td>
    <td>${r.pos_trend}</td>
    <td><span style="color:${c.fg};font-weight:600">${r.pos_col}</span></td>
    <td>${r.pos_nat_up}</td><td>${r.pos_sec_up}</td><td>${r.pos_up}</td>
    <td>${r.pos_nat_dn}</td><td>${r.pos_sec_dn}</td><td>${r.pos_dn}</td>
    <td>${r.pos_ratio}%</td><td>${ag}</td>
  </tr>`;
}).join('');
</script>
</body>
</html>
"""


def main():
    data = build()
    html = (TPL
            .replace("__DATA__", json.dumps(data, ensure_ascii=False))
            .replace("__DATA_DATE__", str(data["agg"]["date"]))
            .replace("__GEN__", data["generated"])
            .replace("__DIFF__", str(data["agg"]["diff_pct"]))
            .replace("__DIFFPCT__", str(data["agg"]["diff_pct"]))
            .replace("__DIFFN__", str(data["agg"]["diff"]))
            .replace("__SEG__", str(data["agg"]["segments"]))
            .replace("__NAT__", str(data["agg"]["nat_avg"]))
            .replace("__AGREE__", str(data["agg"]["agree"]))
            .replace("__TOTAL__", str(data["agg"]["total"]))
            .replace("__MAIN_NAME__", data["main"]["name"]))

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "livermore-key-20261101.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] {out}  {len(html)/1024:.0f} KB")
    print(f"[agg] {data['agg']}")
    for r in data["table"]:
        print(f"  {r['name']:6s} S{r['stage']} | pos={r['pos_col']:5s} 自然{r['pos_nat_up']:2d}/{r['pos_sec_up']:2d}"
              f" 趋势{r['pos_trend']} | amp={r['amp_col']:5s} | 一致={r['agree']}")


if __name__ == "__main__":
    main()
