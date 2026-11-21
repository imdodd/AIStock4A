"""持仓股跟踪：方案生成 + 存储 + 历史回填

设计要点
--------
1. 每只股票一套「跟踪方案」（meta.json）：维度清单 + 频率 + 自动化提示词。
   维度在首次生成后固定（用户选择：季度校准），这样时间轴才能跨期比较。
2. 每期一个 JSON（periods/YYYY-MM-DD.json）：各维度取值 + 绿黄红状态 + 与上期差异。
   完整简报另存同名 .md，页面只读展示。
3. 方案里的阈值尽量用该股的真实数据锚定（当前 PE、52 周位置、股息率…），
   而不是空泛的「高/低」，否则每期判断会飘。

目录：
    data/tracking/index.json              全部跟踪中的标的摘要
    data/tracking/<code>/meta.json        方案
    data/tracking/<code>/periods/*.json   每期数值
    data/tracking/<code>/periods/*.md     每期简报
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(DATA_DIR, "tracking")
MEMBERS = os.path.join(DATA_DIR, "snapshot", "members")

# ---------------------------------------------------------------- 行业 → 维度模板
# key 为申万二级行业名里出现的关键词，命中第一个即用
SECTOR_RULES = [
    ("白酒", dict(
        tier="stable", freq="weekly",
        dims=[
            ("industry", "核心单品批价", "洞藏系列/古井/口子窖等主力单品一批价周变化（渠道调研/今日酒价/研报）",
             "绿=持平或上行；黄=环比跌 1% 内；红=连续两周跌超 1%",
             "批价是白酒景气最灵敏的前瞻指标，直接决定渠道利润和回款意愿"),
            ("industry", "渠道库存与回款", "经销商库存周数、预收款（合同负债）环比、回款进度",
             "绿=库存 <2 个月且预收环比增；黄=2~3 个月；红=超 3 个月或预收下滑",
             "库存高企时厂家只能压货或降价，是业绩变脸的前兆"),
            ("market", "旺季动销与宴席场景", "中秋/国庆/春节动销、宴席场次、开瓶率（研报/草根调研）",
             "绿=动销同比正增长；黄=持平；红=下滑",
             "白酒是典型场景消费，动销决定渠道补货节奏"),
            ("policy", "消费与酒类政策", "禁酒令执行力度、三公消费限制、消费税改革、地方促消费政策",
             "绿=政策友好或促消费加码；黄=无变化；红=限制性政策落地",
             "政策冲击对白酒估值的影响往往是台阶式的"),
        ])),
    ("半导体", dict(
        tier="growth", freq="twice_weekly",
        dims=[
            ("industry", "行业景气与价格", "晶圆厂稼动率、存储/模拟芯片现货价、设备招标中标数据",
             "绿=价格上行或招标放量；黄=持平；红=价格连续下跌",
             "半导体是周期成长股，价格与稼动率领先业绩 1~2 个季度"),
            ("industry", "国产替代与下游需求", "国产化率进展、消费电子/AI 服务器/汽车芯片拉货",
             "绿=替代加速或下游超预期；黄=平稳；红=需求转弱",
             "决定收入弹性的核心变量，比单季利润更重要"),
            ("policy", "产业政策与大基金", "大基金三期投向、专项补贴、出口管制与实体清单",
             "绿=政策/资金支持；黄=无变化；红=管制升级影响供应链",
             "政策直接改变可服务市场与估值中枢"),
        ])),
    ("通信设备", dict(
        tier="growth", freq="twice_weekly",
        dims=[
            ("industry", "光模块/连接器放量节奏", "1.6T 出货与验证进度、北美云厂商 capex 指引、大客户份额",
             "绿=放量超预期或份额提升；黄=按计划推进；红=砍单或延期",
             "光通信链条的股价几乎由放量斜率驱动"),
            ("industry", "上游芯片与价格", "光芯片/EML/DSP 供应、产品 ASP 与毛利率",
             "绿=供应缓解且 ASP 稳；黄=略降；红=价格战或断供",
             "决定毛利率能否守住"),
            ("market", "海外映射与订单", "美股同业（博通/Coherent）财报、海外招标与认证",
             "绿=海外同业超预期；黄=平淡；红=海外指引下修",
             "A 股光模块对海外 Capex 高度敏感"),
        ])),
    ("电池", dict(
        tier="growth", freq="twice_weekly",
        dims=[
            ("industry", "排产与价格", "月度排产、碳酸锂/正极材料价格、电芯报价",
             "绿=排产上行且价格企稳；黄=持平；红=排产下修或价格加速下跌",
             "量价是电池环节盈利的直接来源"),
            ("industry", "产能出清与竞争", "行业开工率、新增产能投放、龙头份额变化",
             "绿=落后产能出清；黄=僵持；红=价格战加剧",
             "供给格局决定周期底部何时出现"),
            ("policy", "出口与关税", "欧美关税/反补贴、海外建厂进度、储能招标政策",
             "绿=海外通道打开；黄=无变化；红=关税加码",
             "海外占比高的公司对贸易政策极敏感"),
        ])),
    ("医药", dict(
        tier="stable", freq="weekly",
        dims=[
            ("policy", "集采与医保", "集采中标/续约、医保谈判、创新药审批与支付政策",
             "绿=政策温和或利好；黄=无变化；红=降价超预期",
             "医药股的估值中枢由支付端政策决定"),
            ("industry", "终端需求与渠道", "医院门诊量、药店同店增长、渠道库存",
             "绿=同店正增长；黄=持平；红=下滑",
             "需求端变化通常先于报表一两个季度"),
        ])),
    ("食品", dict(
        tier="stable", freq="weekly",
        dims=[
            ("industry", "动销与渠道库存", "重点单品动销、经销商库存周数、终端促销力度",
             "绿=动销上行且库存降；黄=持平；红=库存积压或促销加大",
             "快消品的股价拐点大多先从渠道反馈出来"),
            ("industry", "原材料成本", "糖/包材/农产品价格、毛利率环比",
             "绿=成本下行；黄=持平；红=成本上行挤压毛利",
             "成本波动直接打到毛利率"),
        ])),
    ("家电", dict(
        tier="stable", freq="weekly",
        dims=[
            ("industry", "内销与以旧换新", "社零家电分项、以旧换新补贴执行、线上零售均价",
             "绿=补贴放量、均价上行；黄=持平；红=需求透支下滑",
             "补贴节奏决定短期销量脉冲"),
            ("industry", "出口与汇率", "出口金额增速、海运费、人民币汇率",
             "绿=出口正增长；黄=持平；红=出口转负",
             "白电/小家电海外占比高，汇率影响利润"),
        ])),
    ("银行", dict(
        tier="stable", freq="weekly",
        dims=[
            ("industry", "净息差与资产质量", "LPR 调整、存款成本、不良率与关注类占比",
             "绿=息差企稳且不良改善；黄=持平；红=息差继续收窄或不良抬头",
             "银行盈利的两个核心变量"),
            ("policy", "货币与监管政策", "降准降息、社融数据、资本新规",
             "绿=政策呵护；黄=无变化；红=让利压力加大",
             "政策决定息差与扩表节奏"),
        ])),
    # 2026-11-10 补充：实测 8 只持仓有 7 只落 DEFAULT_RULE（维度空泛），
    # 这几类是当前持仓/关注池里出现频率最高、又和已有关键词不重合的行业。
    ("游戏", dict(
        tier="growth", freq="twice_weekly",
        dims=[
            ("industry", "版号与新品排期", "国产/进口版号发放、重点新游上线与测试节点、版号储备",
             "绿=新品按期上线或版号新增；黄=排期平移；红=版号停发或重点产品延期",
             "游戏公司收入几乎由新品上线节奏决定，版号是前置闸门"),
            ("industry", "流水与买量", "iOS/安卓畅销榜排名、Sensor Tower/点点数据流水估算、买量成本",
             "绿=流水上行且买量成本稳定；黄=持平；红=流水下滑且买量费用率抬升",
             "流水决定收入，买量费用率决定净利，两者要一起看"),
            ("policy", "内容监管与未成年人保护", "版号政策、内容审查、防沉迷与未成年人限制、平台分成规则",
             "绿=政策边际友好；黄=无变化；红=限制性新规落地",
             "政策对游戏估值常是台阶式冲击"),
        ])),
    ("数字媒体", dict(
        tier="growth", freq="weekly",
        dims=[
            ("industry", "用户与会员数据", "MAU/DAU、付费会员数、ARPPU、会员提价执行情况",
             "绿=会员净增且 ARPPU 提升；黄=持平；红=会员流失或 ARPPU 下滑",
             "会员是长视频/阅读平台的收入地基"),
            ("industry", "内容排播与成本", "重点综艺/剧集排播、内容采购成本、版权摊销节奏",
             "绿=爆款带动且成本可控；黄=平播；红=重点内容缺席或成本超支",
             "内容投入与产出的时间错配是这类公司利润波动的主因"),
            ("policy", "内容与版权监管", "内容审查、版权保护、短剧/AI 生成内容新规",
             "绿=规则清晰利好；黄=无变化；红=下架/整改等限制性事件",
             "监管直接影响可上线内容供给"),
        ])),
    ("饮料乳品", dict(
        tier="stable", freq="weekly",
        dims=[
            ("industry", "原奶与包材成本", "生鲜乳均价、奶粉/包材价格、毛利率环比",
             "绿=成本下行；黄=持平；红=成本上行挤压毛利",
             "成本波动直接打到毛利率"),
            ("industry", "动销与渠道库存", "常温奶/低温奶动销、经销商库存周数、终端促销力度",
             "绿=动销上行且库存降；黄=持平；红=库存积压或促销加大",
             "快消品拐点大多先从渠道反馈出来"),
            ("market", "新品与结构升级", "高端单品占比、新品铺货率、单价变化",
             "绿=结构升级带动单价；黄=持平；红=靠降价保份额",
             "产品结构是乳企毛利率长期走向的决定项"),
        ])),
    ("光学光电子", dict(
        tier="growth", freq="twice_weekly",
        dims=[
            ("industry", "面板/模组价格与稼动率", "主流尺寸面板报价、行业稼动率、库存周数",
             "绿=价格上行且稼动率提升；黄=持平；红=价格连跌或砍单",
             "量价是显示链条盈利的直接来源"),
            ("industry", "下游需求与订单", "电视/显示器/车载/商用显示出货、大客户订单与认证",
             "绿=需求超预期或新客户导入；黄=按计划；红=订单下修",
             "代工/模组厂业绩高度依赖大客户拉货节奏"),
            ("market", "汇率与出口", "人民币汇率、出口金额增速、海运费",
             "绿=汇率利好且出口正增长；黄=持平；红=出口转负或汇兑损失扩大",
             "海外收入占比高的公司对汇率极敏感"),
        ])),
    ("电力", dict(
        tier="stable", freq="weekly",
        dims=[
            ("industry", "来水/燃料与发电量", "流域来水与库容（水电）、煤价（火电）、月度发电量",
             "绿=来水偏丰或煤价下行且发电量增；黄=持平；红=来水偏枯或煤价上行",
             "发电量与燃料成本共同决定度电利润"),
            ("industry", "电价与利用小时", "市场化交易电价、上网电价、机组利用小时",
             "绿=电价企稳且利用小时升；黄=持平；红=电价下调或利用小时降",
             "电价是收入端最直接的杠杆"),
            ("policy", "电力市场与并网政策", "绿电交易、容量电价、辅服补偿、新能源并网规则",
             "绿=政策增厚收益；黄=无变化；红=收益规则收紧",
             "电力公司的盈利中枢很大程度由规则决定"),
        ])),
    ("种植业", dict(
        tier="stable", freq="weekly",
        dims=[
            ("industry", "农产品价格", "棉花/粮食/糖等对应品种现货与期货价格、基差",
             "绿=价格上行；黄=持平；红=价格连续下跌",
             "种植与加工企业的收入直接跟着品种价格走"),
            ("industry", "种植面积与天气", "播种面积、产区天气与灾害、单产预估",
             "绿=单产改善；黄=持平；红=灾害导致减产",
             "供给端冲击往往先于报表体现"),
            ("policy", "收储与补贴", "最低收购价、目标价格补贴、进口配额与关税",
             "绿=政策托底加码；黄=无变化；红=补贴退坡或进口冲击",
             "政策直接改变价格底部与加工利润"),
        ])),
]

DEFAULT_RULE = dict(
    tier="normal", freq="weekly",
    dims=[
        ("industry", "行业景气度", "行业开工率/价格/月度数据，上下游景气变化",
         "绿=景气上行；黄=持平；红=下行", "行业 beta 往往是股价的主导变量"),
        ("market", "竞争格局与份额", "主要竞争对手动向、价格战、份额变化",
         "绿=份额提升；黄=稳定；红=份额流失", "决定毛利率与成长持续性"),
    ])

# 通用维度（每只股票都有）
UNIVERSAL = [
    ("capital", "主力资金与量能", "本周主力净流入/流出、连续净流入天数、周成交量相对 50 日均量（量比）",
     "绿=净流入且放量；黄=净流出但量能正常；红=连续两周净流出且缩量",
     "资金流向是短中期股价的直接推手，量能配合度决定突破真伪"),
    ("earnings", "业绩与预期", "最新报告期营收/净利同比、毛利率变化、机构一致预期 EPS 变化",
     "绿=增速加速或超预期；黄=符合预期；红=增速失速或预期下修",
     "业绩是估值的地基，预期下修往往先于股价下跌"),
    ("valuation", "估值水位", "PE(TTM)、PB 及其所处历史/同业分位，股息率与 10 年期国债利差",
     "绿=低于中枢或股息率有明显利差；黄=中枢附近；红=显著高于中枢",
     "相同基本面下，估值决定未来 1 年的赔率"),
    ("policy", "政策与监管", "行业专项政策、补贴/限制、税收与贸易环境变化",
     "绿=政策友好；黄=无变化；红=限制性政策落地",
     "政策冲击通常是台阶式重估，需第一时间识别"),
    ("market", "趋势与相对强度", "温斯坦阶段、距年线、RS60（相对沪深300）、52 周区间位置",
     "绿=Stage2 且 RS60 为正；黄=Stage1/3 震荡；红=Stage4 且 RS60 转负",
     "技术结构是资金与基本面博弈的结果，用于控制回撤"),
    ("disclosure", "公司事件", "股东增减持、解禁、回购、分红、重组、诉讼",
     "绿=增持/回购/分红提升；黄=无重大事件；红=大额减持/解禁/监管处罚",
     "事件型冲击常常脱离基本面，需要单独盯"),
]

FREQ = {
    "weekly": dict(kind="weekly", rrule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=8;BYMINUTE=20",
                   label="每周一 08:20"),
    "twice_weekly": dict(kind="twice_weekly", rrule="FREQ=WEEKLY;BYDAY=MO,TH;BYHOUR=8;BYMINUTE=20",
                         label="每周一、周四 08:20"),
    "biweekly": dict(kind="biweekly", rrule="FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;BYHOUR=8;BYMINUTE=20",
                     label="每两周周一 08:20"),
    "daily": dict(kind="daily", rrule="FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=30",
                  label="每个交易日 08:30"),
}

# 跟踪周期分层：维度各自绑定「数据什么时候会变」，而不是所有维度一刀切每期都查。
# 日频已有动态止损与持仓每日 AI 分析，周频是主任务，月/季/事件由独立自动化承载。
CADENCE = {
    "daily": dict(label="每交易日", when="盘前 08:30", rrule="FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=30"),
    "weekly": dict(label="每周", when="周一 / 周四 08:20", rrule="FREQ=WEEKLY;BYDAY=MO,TH;BYHOUR=8;BYMINUTE=20"),
    "twice_weekly": dict(label="每周两次", when="周一 / 周四 08:20", rrule="FREQ=WEEKLY;BYDAY=MO,TH;BYHOUR=8;BYMINUTE=20"),
    "monthly": dict(label="每月", when="每月 1 日 08:10", rrule="FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=8;BYMINUTE=10"),
    "quarterly": dict(label="每季 / 财报后", when="1、4、7、10 月 15 日 08:10",
                      rrule="FREQ=YEARLY;BYMONTH=1,4,7,10;BYMONTHDAY=15;BYHOUR=8;BYMINUTE=10"),
    "event": dict(label="事件驱动", when="每交易日盘后巡检公告", rrule="FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=16;BYMINUTE=45"),
}


def _rule_for(sector: str, chain: str = "") -> dict:
    """先按申万二级行业名匹配，未命中再用东财行业链（EM2016）做二级匹配。

    以前 8 只持仓有 7 只落 DEFAULT_RULE，因为「光学光电子」「数字媒体」这类名字
    不在关键词表里；EM2016 形如「电子设备-半导体-半导体材料」，能补上不少命中。
    """
    for kw, rule in SECTOR_RULES:
        if kw in (sector or ""):
            return rule
    for kw, rule in SECTOR_RULES:
        if kw in (chain or ""):
            return rule
    return DEFAULT_RULE


def _dependency_dims(code: str) -> tuple[list[dict], str]:
    """读研究档案里的结构化业绩依赖因素，转成跟踪维度。

    这是「关注页六章」与「持仓跟踪」共用的那份数据；档案没生成时返回空，
    方案里会明确标注缺依赖链，而不是偷偷套通用维度冒充。
    """
    try:
        import research
        dep = research.dependencies(code)
    except Exception:
        return [], ""
    chain = dep.get("chain") or ""
    dims = []
    for i, f in enumerate(dep.get("factors") or [], 1):
        th = f.get("threshold") or {}
        cad = f.get("cadence") if f.get("cadence") in CADENCE else "weekly"
        dims.append(dict(
            key="dep_" + _slug(str(f.get("name") or f"factor{i}")),
            angle="dependency", name=f.get("name") or f"依赖因素{i}",
            metric=(f"{f.get('proxy') or '未获取到代理指标'}"
                    f"（数据源：{f.get('source') or '未获取到'}）"),
            cadence=cad, source=f.get("source") or "",
            lead=f.get("lead") or "", invalidation=f.get("invalidation") or "",
            thresholds=dict(g=th.get("g") or "", y=th.get("y") or "",
                            r=th.get("r") or "", raw=f"证伪：{f.get('invalidation') or '未获取到'}"),
            why=f"依赖链第 {i} 环；领先性：{f.get('lead') or '未获取到'}；"
                f"证伪条件：{f.get('invalidation') or '未获取到'}",
        ))
    return dims, chain


def build_schedule(dims: list[dict]) -> list[dict]:
    """按 cadence 把维度分层，给出每条自动化要盯什么。"""
    groups: dict[str, list[str]] = {}
    for d in dims:
        c = d.get("cadence") if d.get("cadence") in CADENCE else "weekly"
        groups.setdefault(c, []).append(d["name"])
    out = []
    for c in ("daily", "weekly", "twice_weekly", "monthly", "quarterly", "event"):
        names = groups.get(c) or []
        if not names:
            continue
        info = CADENCE[c]
        out.append(dict(cadence=c, label=info["label"], when=info["when"],
                        rrule=info["rrule"], dimensions=names, n=len(names)))
    return out


# ---------------------------------------------------------------- 行业识别
_SECTOR_MAP: dict[str, tuple[str, str]] | None = None


def sector_of(code: str) -> tuple[str, str]:
    """代码 → (申万二级行业名, 细分态)；扫一遍成分股快照后缓存"""
    global _SECTOR_MAP
    if _SECTOR_MAP is None:
        _SECTOR_MAP = {}
        if os.path.isdir(MEMBERS):
            for fn in os.listdir(MEMBERS):
                if not fn.endswith(".json") or fn.startswith("_"):
                    continue
                try:
                    with open(os.path.join(MEMBERS, fn), encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue
                sname = d.get("name") or ""
                for m in d.get("members", []):
                    if m.get("code"):
                        _SECTOR_MAP.setdefault(m["code"], (sname, m.get("sub") or ""))
    return _SECTOR_MAP.get(code, ("", ""))


# _rule_for 已上移到 FREQ 之后（增加东财行业链 EM2016 的二级匹配）


# ---------------------------------------------------------------- 存储
def _dir(code: str) -> str:
    # code 会拼进文件路径且可能来自 HTTP 请求，必须拒绝 ..、斜杠等穿越输入
    from data_paths import safe_slug
    return os.path.join(DIR, safe_slug(code, "股票代码"))


def _meta_path(code: str) -> str:
    return os.path.join(_dir(code), "meta.json")


def load(code: str) -> dict | None:
    try:
        p = _meta_path(code)
    except Exception:            # 含路径穿越输入 → 视作「未跟踪」
        return None
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(meta: dict) -> dict:
    code = meta["code"]
    os.makedirs(os.path.join(_dir(code), "periods"), exist_ok=True)
    meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(_meta_path(code), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    _reindex()
    return meta


def remove(code: str) -> bool:
    meta = load(code)
    if not meta:
        return False
    meta["status"] = "stopped"
    meta["stopped_at"] = datetime.now().isoformat(timespec="seconds")
    save(meta)
    return True


def list_all() -> dict:
    p = os.path.join(DIR, "index.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _reindex() -> None:
    """把每只标的的摘要汇总，前端一次请求就能知道哪些股在跟踪"""
    idx = {}
    if os.path.isdir(DIR):
        for code in os.listdir(DIR):
            meta = load(code)
            if not meta:
                continue
            periods = period_list(code)
            idx[code] = dict(
                name=meta.get("name"), status=meta.get("status"),
                freq_label=(meta.get("frequency") or {}).get("label"),
                dims=len(meta.get("dimensions") or []),
                last_date=periods[0]["date"] if periods else None,
                last_verdict=periods[0].get("verdict") if periods else None,
                n_periods=len(periods),
                automation_id=(meta.get("automation") or {}).get("id"),
                automation_state=(meta.get("automation") or {}).get("state"),
            )
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 期数据
def period_dir(code: str) -> str:
    return os.path.join(_dir(code), "periods")


def save_period(code: str, period: dict) -> dict:
    d = period_dir(code)
    os.makedirs(d, exist_ok=True)
    period.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    with open(os.path.join(d, f"{period['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(period, f, ensure_ascii=False, indent=1)
    _reindex()
    return period


def runs(code: str, limit: int = 8) -> list[dict]:
    """每跑一次留下的诊断（哪一层、多少 token、哪些数字没找到出处）。
    定时任务出问题时，这里比日志好查——它跟期数据放在一起。"""
    d = os.path.join(_dir(code), "runs")
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d), reverse=True)[:max(1, int(limit))]:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        rec["file"] = fn
        out.append(rec)
    return out


def latest_run(code: str) -> dict | None:
    r = runs(code, 1)
    return r[0] if r else None


def period_list(code: str) -> list[dict]:
    d = period_dir(code)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            continue
    out.sort(key=lambda x: x.get("date", ""), reverse=True)
    return out


# ---------------------------------------------------------------- 环比派生
#
# 为什么放在读接口里算：历史期次已经落盘，改口径会永久污染历史；
# 而"这期比上期变了什么"是纯派生物，每次读都按最新规则重算最好。
#
# 两条硬约束，都是踩过的：
#   1. 只能在同一「口径段」内比。维度键集合一变（8 维 → 14 维），
#      同名 key 的语义也可能已经漂移，硬算环比会得出假的 ±。
#   2. 「本期无增量」不是缺失。维度按各自的数据发布节奏采集
#      （周频看资金/趋势、季频看财报），所以周报期里大部分维度本来就该没更新，
#      这类格子必须和"真的没采集到"区分开，否则表格 40% 是噪声。

GAP_WORDS = ("无增量", "未采集", "无数据", "未更新", "暂无")
_NUM_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(%|pct|亿|万手|万|元|天|倍|吨|kg)?")


def _prep_text(s: str) -> str:
    """把日期和年份抹掉，避免 '11-13' 被当成 '11' 和 '-13' 两个数"""
    t = str(s or "")
    t = re.sub(r"\d{1,2}-\d{1,2}(\s*[~～]\s*\d{1,2}-\d{1,2})?", " ", t)
    t = re.sub(r"(19|20)\d{2}\s*(年|Q[1-4]|三季报|半年报|年报)?", " ", t)
    return t


def _nums(s: str) -> list[tuple[float, str]]:
    out = []
    for m in _NUM_RE.finditer(_prep_text(s)):
        try:
            out.append((float(m.group(1)), (m.group(2) or "").strip()))
        except Exception:
            continue
    return out


def is_gap(value: dict | None) -> bool:
    """本期该维度没有新内容（按节奏本来就不采，或没采到）"""
    if not value:
        return True
    return any(w in str(value.get("value") or "") for w in GAP_WORDS)


def _delta_text(prev: dict, cur: dict) -> str:
    """同口径下给一句最直白的数值变化；抽不出可比数字就留空，不臆造"""
    pv, cv = _nums(prev.get("value")), _nums(cur.get("value"))
    if not pv or not cv or len(pv) != len(cv):
        return ""
    bits = []
    for (a, ua), (b, ub) in zip(pv, cv):
        if ua != ub or not ua:
            continue                       # 单位不一致就别比，容易误导
        d = b - a
        if abs(d) < 1e-9:
            continue
        if ua in ("%", "pct"):
            bits.append(f"{d:+.2f}pct")
        else:
            bits.append(f"{d:+.{0 if abs(d) >= 100 else 2}f}{ua}")
        if len(bits) >= 2:
            break
    if not bits:
        return ""
    return ("↑ " if sum(1 for x in bits if x.startswith("+")) else "↓ ") + " / ".join(bits)


def decorate(meta: dict, periods: list[dict]) -> dict:
    """给读接口补口径分段 / 维度生效起点 / 逐格环比 / 每期健康度

    返回 {periods, cohorts, health, changed_keys, gap_keys}，
    其中 periods 是深拷贝后的期次（每格多 chg / prev / delta 字段）。
    """
    ps = sorted((p for p in periods if p.get("date")), key=lambda x: x["date"])
    sigs = [frozenset(v.get("key") for v in (p.get("values") or []) if v.get("key")) for p in ps]

    # ---- 口径分段：与上一期维度集合不同就开新段 ----
    cidx: list[int] = []
    cur = -1
    for i in range(len(ps)):
        if i == 0 or sigs[i] != sigs[i - 1]:
            cur += 1
        cidx.append(cur)
    cohorts = []
    for i, p in enumerate(ps):
        if not cohorts or cohorts[-1]["index"] != cidx[i]:
            cohorts.append({"index": cidx[i], "from": p["date"], "to": p["date"],
                            "n": 0, "keys": sorted(sigs[i])})
        cohorts[-1]["to"] = p["date"]
        cohorts[-1]["n"] += 1

    # ---- 每个维度的生效起点（哪一期开始出现）----
    since: dict[str, str] = {}
    for p in ps:
        for v in (p.get("values") or []):
            k = v.get("key")
            if k and k not in since:
                since[k] = p["date"]

    # ---- 逐格环比（只在同段内比）----
    out_periods: list[dict] = []
    for i, p in enumerate(ps):
        cur_vals = {v.get("key"): v for v in (p.get("values") or []) if v.get("key")}
        prev_p = ps[i - 1] if i > 0 and cidx[i] == cidx[i - 1] else None
        prev_vals = {v.get("key"): v for v in (prev_p.get("values") or [])} if prev_p else {}
        # 跨段的上一期：口径变了，但同名维度还能拿来参照，标 cross 提示别当严格环比
        any_prev = {v.get("key"): v for v in ((ps[i - 1].get("values") or []) if i > 0 else [])}

        vals = []
        changed, news, gaps, statuses = [], [], [], []
        for k, v in cur_vals.items():
            cell = dict(v)
            cell["since"] = since.get(k)
            cell["gap"] = is_gap(v)
            pv = prev_vals.get(k)
            cross = False
            if not pv and not prev_p:
                pv = any_prev.get(k)          # 本段首期：退到上一段同名维度做参照
                cross = bool(pv)
            if not pv:
                cell["chg"] = "first" if i == 0 else "new"
            elif is_gap(v) and is_gap(pv):
                cell["chg"] = "same"                      # 两期都没新内容
            elif is_gap(v):
                cell["chg"] = "gap"                       # 本期按节奏没更新
            elif str(v.get("value")) == str(pv.get("value")):
                cell["chg"] = "same"
            else:
                cell["chg"] = "changed"
            if pv and cell["chg"] in ("changed", "same") and pv.get("status") != v.get("status"):
                cell["chg"] = "status"                    # 状态变了比数值变了更重要
                cell["status_from"] = pv.get("status")
            if cross:
                cell["cross"] = True
            if cell["chg"] in ("changed", "status") and pv:
                cell["prev"] = str(pv.get("value") or "")[:80]
                # 期次自己带来的 delta（AI 写的）优先保留，抽得出数字才覆盖
                cell["delta"] = _delta_text(pv, v) or cell.get("delta") or ""
            if cell["chg"] in ("changed", "status"):
                changed.append(k)
            elif cell["chg"] == "new":
                news.append(k)
            if cell["chg"] == "gap":
                gaps.append(k)
            if cell["chg"] == "status":
                statuses.append(k)
            vals.append(cell)

        # ---- 健康度：绿 +1 / 黄 0 / 红 -1，跳过无内容的格子 ----
        g = sum(1 for v in vals if v.get("status") == "g")
        y = sum(1 for v in vals if v.get("status") == "y")
        r = sum(1 for v in vals if v.get("status") == "r")
        judged = g + y + r
        score = round((g - r) / judged, 3) if judged else None

        pc = dict(p)
        pc["values"] = vals
        pc["cohort"] = cidx[i]
        pc["health"] = score
        pc["n_changed"] = len(changed)
        pc["n_new"] = len(news)
        pc["n_gap"] = len(gaps)
        pc["n_status"] = len(statuses)
        pc["n_nodata"] = sum(1 for v in vals if v.get("gap"))
        out_periods.append(pc)

    return {"periods": out_periods, "cohorts": cohorts, "since": since,
            "cohort_count": len(cohorts)}


# ---------------------------------------------------------------- 方案生成
def build_plan(code: str, name: str = "", profile: dict | None = None) -> dict:
    """按行业模板 + 该股真实数据生成跟踪方案（维度/阈值/频率/提示词）

    这是「AI 初稿」的规则版：阈值锚在真实数值上，维度按行业特性选，
    频率按波动特征定。之后由 AI 复核微调（可改维度措辞、增删个股特有维度）。
    """
    sector, sub = sector_of(code)
    prof = profile or {}
    chain = (prof.get("company") or {}).get("em2016") or ""
    rule = _rule_for(sector, chain)
    basic = prof.get("basic") or {}
    fin = prof.get("finance") or {}
    rep = prof.get("reports") or {}
    latest = fin.get("latest") or {}

    def _cad_for(name: str) -> str:
        """按「这个指标多久会变一次」定频率，不再所有维度都写「每期」"""
        if name in ("主力资金与量能", "趋势与相对强度"):
            return "daily"
        if name in ("公司事件", "政策与监管"):
            return "event"
        if name == "业绩与预期":
            return "quarterly"
        return "twice_weekly" if rule["tier"] == "growth" else "weekly"

    def anchor(text: str, value) -> str:
        return f"{text}（当前 {value}）" if value not in (None, "") else text

    dims = []
    sector_angles = {r[0] for r in rule["dims"]}
    # 行业特有维度
    for angle, dname, metric, thr, why in rule["dims"]:
        dims.append(dict(key=_slug(dname), angle=angle, name=dname, metric=metric,
                         cadence=_cad_for(dname), thresholds=_split_thr(thr), why=why))
    # 通用维度（按行业补锚点，避免空泛阈值；行业模板已覆盖的角度不重复）
    for angle, dname, metric, thr, why in UNIVERSAL:
        if angle in sector_angles and angle in ("policy", "industry"):
            continue                     # 行业维度里已有政策/景气专项，避免重复
        t = thr
        if dname == "估值水位":
            pe = basic.get("pe_ttm")
            pb = basic.get("pb")
            dy = basic.get("dividend_yield")
            if pe:
                t = (f"绿=PE(TTM) <{round(pe * 0.9, 1)} 或股息率 >{round((dy or 0) + 0.3, 2)}%；"
                     f"黄={round(pe * 0.9, 1)}~{round(pe * 1.15, 1)}；红=>{round(pe * 1.15, 1)}"
                     f"（按当前 PE {pe} / PB {pb} 锚定，季度校准）")
        elif dname == "业绩与预期":
            ry, py = latest.get("revenue_yoy"), latest.get("profit_yoy")
            if ry is not None:
                t = (f"绿=营收同比 >{round(ry + 2, 1)}% 或净利加速；"
                     f"黄=营收同比 {round(ry - 2, 1)}~{round(ry + 2, 1)}%；"
                     f"红=营收同比 <{round(ry - 2, 1)}%（按 {latest.get('period', '最新期')} "
                     f"营收 {ry:.1f}% / 净利 {(py or 0):.1f}% 锚定）")
        elif dname == "趋势与相对强度":
            t = ("绿=Stage2 且 RS60 >0 且站上年线；黄=Stage1/Stage3 震荡或 RS60 在 ±5 内；"
                 "红=Stage4 且 RS60 <-5 或跌破年线 5% 以上")
        dims.append(dict(key=_slug(dname), angle=angle, name=dname, metric=metric,
                         cadence=_cad_for(dname), thresholds=_split_thr(t), why=why))

    # 业绩依赖因素：直接复用研究档案 dep.json，避免两套 AI 各写各的
    dep_dims, dep_chain = _dependency_dims(code)
    dims.extend(dep_dims)
    freq = FREQ[rule["freq"]]
    schedule = build_schedule(dims)
    plan = dict(
        code=code, name=name or basic.get("name") or prof.get("name") or code,
        sector=sector or "未归类", sub=sub, industry_chain=chain or None,
        status="draft",
        frequency=dict(freq, basis=_freq_basis(sector, rule["tier"], len(dep_dims))),
        schedule=schedule,
        dependency_chain=dep_chain,
        dependency_status="ready" if dep_dims else "missing",
        dependency_note=("已引用研究档案的业绩依赖链，维度随财报/重大公告重建而更新"
                        if dep_dims else
                        "尚未生成研究档案，依赖链缺失；建议先在关注页/持仓页生成一次研究档案，再重启跟踪方案"),
        dimensions=dims,
        anchors=dict(price=basic.get("price"), pe_ttm=basic.get("pe_ttm"), pb=basic.get("pb"),
                     dividend_yield=basic.get("dividend_yield"), pos52=basic.get("pos52"),
                     high52=basic.get("high52"), low52=basic.get("low52"),
                     total_mktcap=basic.get("total_mktcap"),
                     latest_period=latest.get("period"), revenue=latest.get("revenue"),
                     revenue_yoy=latest.get("revenue_yoy"), profit=latest.get("profit"),
                     profit_yoy=latest.get("profit_yoy"), roe=latest.get("roe"),
                     rating_main=rep.get("rating_main"), eps_consensus=rep.get("eps_consensus"),
                     n_reports=rep.get("total")),
        created_at=datetime.now().isoformat(timespec="seconds"),
        automation=dict(id=None, state="pending_agent"),
    )
    plan["automation_prompt"] = build_prompt(plan)
    return plan


_KEY_MAP = {
    # 通用维度
    "主力资金与量能": "capital", "业绩与预期": "earnings", "估值水位": "valuation",
    "政策与监管": "policy", "趋势与相对强度": "trend", "公司事件": "events",
    # 白酒
    "核心单品批价": "pijia", "渠道库存与回款": "inventory", "旺季动销与宴席场景": "sellthru",
    "消费与酒类政策": "liquor_policy",
    # 半导体
    "行业景气与价格": "semi_price", "国产替代与下游需求": "localization",
    "产业政策与大基金": "semi_policy",
    # 通信设备
    "光模块/连接器放量节奏": "optics_volume", "上游芯片与价格": "chip_price",
    "海外映射与订单": "overseas",
    # 电池
    "排产与价格": "battery_volume", "产能出清与竞争": "capacity",
    "出口与关税": "tariff",
    # 医药
    "集采与医保": "tender", "终端需求与渠道": "hospital_demand",
    # 食品
    "动销与渠道库存": "sellthru", "原材料成本": "raw_material",
    # 家电
    "内销与以旧换新": "trade_in", "出口与汇率": "export_fx",
    # 银行
    "净息差与资产质量": "nim", "货币与监管政策": "monetary",
    # 默认通用
    "行业景气度": "industry_cycle", "竞争格局与份额": "competition",
}


def _slug(name: str) -> str:
    """维度名 → 稳定 key（不能用 Python hash：进程重启就变，历史数据会对不上）"""
    if name in _KEY_MAP:
        return _KEY_MAP[name]
    # 兜底：用字符编码拼一个确定性 key
    return "x" + "".join(str(ord(ch) % 97) for ch in name[:6])


def _split_thr(t: str) -> dict:
    """"绿=…；黄=…；红=…" → 结构化三档"""
    out = {"g": "", "y": "", "r": "", "raw": t}
    for part in re.split(r"[；;]\s*", t):
        m = re.match(r"\s*(绿|黄|红)\s*[=＝:：]\s*(.+)", part)
        if m:
            out[{"绿": "g", "黄": "y", "红": "r"}[m.group(1)]] = m.group(2).strip()
    return out


def _freq_basis(sector: str, tier: str, n_dep: int = 0) -> str:
    base = (f"{sector or '该行业'}价格与订单变化快、波动大，主频每周两次（周一/周四），既能跟上产业节奏又不至于噪音化"
            if tier == "growth" else
            f"{sector or '该行业'}基本面变化慢、以季度财报和月度数据驱动，主频周频足够捕捉拐点"
            if tier == "stable" else
            "通用节奏：主频周频覆盖主要变化，避免过度跟踪")
    if n_dep:
        return (f"{base}。已按 {n_dep} 个业绩依赖因素分层：日频盯价格/量能/动态止损，"
                "周频盯产业价格与订单，月频盯排产与月度数据，季频盯分部营收占比与毛利率，"
                "公告/政策走事件驱动；每条维度绑定自己的数据发布节奏，避免「每期都查一遍」")
    return base + "；尚未拿到业绩依赖链，频率仅按行业波动特征定"


def periods_dir_text(code: str) -> str:
    """给 AI 一个能直接用的绝对落盘路径。

    这里历史上写的是相对路径 ``backend/data/tracking/``，服务切到
    ``ASTOCK_DATA_DIR``（实际是 backend/data-live）之后这段提示词就指空了，
    定时任务照跑但写不进时间轴。路径统一从 DIR 推，不再手写。
    """
    # code 同样可能来自请求侧，落盘提示词里的路径也要过穿越校验
    from data_paths import safe_slug
    return os.path.join(DIR, safe_slug(code, "股票代码"), "periods").replace(os.sep, "/")


def build_prompt(plan: dict) -> str:
    """把方案翻译成可直接交给自动化的提示词"""
    code, name = plan["code"], plan["name"]
    dims = plan["dimensions"]
    freq = plan["frequency"]
    pdir = periods_dir_text(code)
    lines = [
        f"你是用户的持仓跟踪助手。执行「{name}（{code}）股价影响因素跟踪」，"
        f"主频：{freq['label']}，产出一份可直接用于开盘前决策的跟踪简报。",
        "",
        "【业绩依赖链 · 判断主线索】",
        plan.get("dependency_chain") or "未获取到依赖链（尚未生成研究档案），本期按普通行业维度跟踪。",
        "",
        "【周期分层 · 每条维度只在自己的数据更新周期检查】",
    ]
    for s in plan.get("schedule") or []:
        lines.append(f"- {s['label']}（{s['when']}）：{'、'.join(s['dimensions'])}")
    lines += [
        "本期没有新数据的维度写「本期无增量」并注明下次更新时间；严禁为了凑内容翻炒旧闻。",
        "",
        "【第 0 步 · 取行情基线】",
        f"1) westock-mcp data_quote（{code}）取现价、涨跌、PE、PB、市值；",
        f"2) westock-mcp data_kline（{code}, period=day, fq=qfq, limit=60）算距年线、RS60（对沪深300 sh000300）；",
        f"3) westock-mcp data_fund_flow（{code}, 起止=近 5 个交易日）取主力净流入；",
        f"4) westock-mcp data_technical（{code}, group=kdj,macd,rsi,boll）。",
        "",
        "【第 1 步 · 按固定维度检索（维度固定，不要新增或删减）】",
    ]
    for i, d in enumerate(dims, 1):
        cad = CADENCE.get(d.get("cadence") or "weekly", CADENCE["weekly"])["label"]
        lines.append(f"{i}. [{d['name']}]（{d['angle']} · {cad}）指标：{d['metric']}")
        lines.append(f"   阈值：绿={d['thresholds']['g']}；黄={d['thresholds']['y']}；红={d['thresholds']['r']}")
        lines.append(f"   判断依据：{d['why']}")
        if d.get("angle") == "dependency":
            lines.append(f"   证伪条件：{d.get('invalidation') or '未获取到'}"
                         f"（命中即说明业绩逻辑可能破了，必须标注「触发复核」）")
    lines += [
        "",
        "规则：某维度本期确无新增信息，写「本期无增量」并注明下次更新时间；严禁编造或翻炒旧闻；"
        "所有数字标注来源与时点。",
        "",
        "【第 2 步 · 定性判断】",
        "逐维度给「利多/中性/利空」+ 一句话依据；最后给本期综合判断（偏多/中性/偏空）与「最需要关注的一件事」。",
        "",
        "【第 3 步 · 与上期对比】",
        "读取上一期 JSON（若存在），对每个维度给出变化方向（↑改善 / ↓恶化 / →持平）与变化幅度。",
        "",
        "【第 4 步 · 落盘（重要，平台靠这两个文件渲染时间轴）】",
        f"1) 把本期数值写入 {pdir}/<今天日期>.json，结构：",
        '   {"date":"YYYY-MM-DD","verdict":"偏多|中性|偏空","summary":"一句话结论",'
        '"values":[{"key":"维度key","value":"本期值","status":"g|y|r|n","delta":"↑/↓/→ 幅度","note":"依据"}],'
        '"highlights":["变化最大的三件事"]}',
        f"2) 把完整简报写入同目录 <今天日期>.md（含各维度明细与来源链接）；",
        "3) 调用 present_files 展示该 md；",
        "4) 若某维度进入红区、命中业绩依赖因素的证伪条件，或出现重大事件"
        "（业绩预告变脸、大额减持、政策落地、上下游砍单），在简报开头标注「触发复核」，"
        "并写清：哪个变量的假设破了、影响链条上的哪一环、需要补看什么数据。",
        "",
        "【定位提醒与免责】",
        f"{name} 在用户组合中的角色请结合其行业（{plan.get('sector')}）与当前阶段（{plan.get('sub') or '未知'}）判断；"
        "简报仅为投研参考，不构成投资建议。",
    ]
    return "\n".join(lines)
