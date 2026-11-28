"""AI 解读层：预生成优先 → 直连模型 → 规则兜底

三条通道，按序降级：

  1. pregen   读 data/cases/{date}/ai/{code}.json（由 WorkBuddy 侧盘后批量生成）
              —— 零延迟、零成本、点开秒出，是默认主通道
  2. llm      读 data/.secrets/llm.json 里的 OpenAI 兼容配置（base_url/api_key/model）
              —— 支持流式多轮对话，配置后自动启用
  3. rule     纯本地规则归因（reasons.py），保证任何情况下都不断供

证据包是当天可核验的事实，但回答不局限于证据包：允许用公开证券知识补背景与逻辑，
必须区分「依据:字段」的事实、「知识」的通用常识与「推断」的判断；严守证券相关范围。
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime

import reasons

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
CASE_DIR = os.path.join(DATA_DIR, "cases")
SECRET = os.path.join(DATA_DIR, ".secrets", "llm.json")

SYSTEM = """你是 A 股证券研究与盘面复盘助手，服务对象是研究个股的投资者。

【回答范围】
只回答证券相关问题：A 股 / 港股 / 美股、行业与公司基本面、题材与产业链逻辑、盘面与资金行为、
估值与财报、宏观与政策对市场的影响、技术形态含义、同类公司对比、历史相似行情参照。
用户问证券无关的问题（生活、娱乐、编程、医疗等），先礼貌拒绝：
「我只回答证券相关的问题。可以聊这只票的题材逻辑、估值或盘面。」再引导回当前标的。
问题表面无关但能接到当前个股投资逻辑的，按证券角度回答。

【信息使用】
1. 【证据包】是当天可核验的事实，优先使用，并在判断后用 [依据:字段] 标注来源，例如 [依据:flow.main_net]。
2. 证据包没有的信息，允许用你掌握的公开证券知识补充：行业商业模式、题材驱动逻辑、产业链上下游、
   同类公司、历史类似炒作路径、财务指标含义、市场交易机制。这类内容标 [知识]，不得冒充当天事实。
3. 当天数据证据包确实没有（如板块整体涨跌、资金流先后顺序），直说「当天数据未取到」，
   然后用 [知识] 讲清一般判断方法，绝不编造具体数字、公告或研报观点。
4. 严格区分事实与推断。推断写成「倾向于……，因为……」，并说明证据强弱。
5. 用户要「多给信息」时，按「事实 → 知识 → 推断」三层展开，让人看懂为什么，而不只是给结论。

【红线】
不给买卖建议：不出现「建议买入/卖出/加仓/减仓/目标价/止损位/该买该卖」这类表述。
可以陈述事实、风险与待观察信号。中文、大白话、句子短，不写「综上所述」「值得注意的是」这类空话。
结尾统一附「以上仅为公开信息分析，不构成投资建议。」

【长度】
结论类不超过 400 字、最多 5 条；对话类按问题详略，一般 300~500 字，复杂问题可分 5~6 点。

解读个股当日异动按这个结构：
结论：一句话说清今天发生了什么。
归因：最多三条，每条一行「原因 —— 支撑数据 [依据:字段]」。
业务逻辑：用 [知识] 讲清这个题材/行业的驱动逻辑与延续条件；证据包没给也要讲一般逻辑。
反证：一句话说明为什么不是另一个更常见的原因。
风险：一到两条。
观察：接下来可核验的信号。"""


SYSTEM_CHAT = SYSTEM + """

【对话补充】
先直接回答用户的问题，再补一层相关证券背景知识，让用户看懂「为什么」，不只是「是多少」。
用户问「能不能持续 / 是不是逆势 / 风险在哪 / 和同行比如何」这类判断题时，
按「证据包事实 → 行业常识 [知识] → 带强弱的推断」三层回答，别只复述证据包。
涉及数字时明确区分：哪些是证据包里的当天数字，哪些是行业一般区间，两者不得混用。"""


def config() -> dict | None:
    """OpenAI 兼容配置：data/.secrets/llm.json 或环境变量"""
    try:
        d = json.load(open(SECRET, encoding="utf-8"))
        if d.get("api_key") and d.get("base_url") and d.get("model"):
            return d
    except Exception:
        pass
    if os.environ.get("LLM_API_KEY") and os.environ.get("LLM_MODEL"):
        return {
            "base_url": os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            "api_key": os.environ["LLM_API_KEY"],
            "model": os.environ["LLM_MODEL"],
        }
    return None


def pregen_path(day: str, code: str) -> str:
    return os.path.join(CASE_DIR, day, "ai", f"{code}.json")


def load_pregen(day: str, code: str) -> dict | None:
    p = pregen_path(day, code)
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p, encoding="utf-8"))
        return d if d.get("headline") else None
    except Exception:
        return None


def save_pregen(day: str, code: str, data: dict) -> str:
    p = pregen_path(day, code)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = dict(data, code=code, date=day, mode="pregen",
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p


def _case_digest(case: dict) -> str:
    """给模型看的证据包（裁掉体积大且无信息量的字段）"""
    slim = {k: v for k, v in case.items() if k not in ("schema", "built_at", "peers", "news")}
    slim["peers"] = (case.get("peers") or [])[:6]
    slim["news"] = [{"date": n["date"], "title": n["title"], "hit": n.get("hit")}
                    for n in (case.get("news") or [])[:6]]
    slim["notices"] = (case.get("notices") or [])[:6]
    if (case.get("flow") or {}).get("rows"):
        slim["flow"] = {k: v for k, v in case["flow"].items() if k != "rows"}
    return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))


def _http_stream(cfg: dict, messages: list[dict]):
    """OpenAI 兼容 /chat/completions 流式调用，逐段 yield 文本"""
    body = json.dumps({
        "model": cfg["model"], "messages": messages, "stream": True,
        "temperature": cfg.get("temperature", 0.4),
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
                delta = (d.get("choices") or [{}])[0].get("delta") or {}
                txt = delta.get("content")
                if txt:
                    yield txt
            except Exception:
                continue


def brief(case: dict, refresh: bool = False) -> dict:
    """解读：规则归因打底，pregen / llm 补充叙述"""
    base = reasons.brief(case)
    day, code = case.get("date"), case.get("code")

    pre = load_pregen(day, code)
    if pre and not refresh:
        return {**base, **{k: pre[k] for k in
                           ("headline", "causes", "risks", "watch", "summary")
                           if pre.get(k)}, "mode": "pregen",
                "generated_at": pre.get("generated_at") or base["generated_at"]}

    cfg = config()
    if cfg:
        try:
            msgs = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content":
                    f"【证据包】{_case_digest(case)}\n\n"
                    "请按「结论 / 归因 / 业务逻辑 / 反证 / 风险 / 观察」六段输出这只票今天的异动解读。"},
            ]
            text = "".join(_http_stream(cfg, msgs)).strip()
            if text:
                return {**base, "headline": base["headline"], "summary": text,
                        "mode": "llm", "model": cfg["model"],
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        except Exception as e:
            base["llm_error"] = f"{type(e).__name__}: {e}"

    return base


CHAT_FALLBACK = (
    "页内对话还没接通模型通道。\n\n"
    "当前可用的是本地规则归因（有事实、有排序，但没有自由问答）。"
    "接通后可以问我题材逻辑、产业链、估值口径、同类公司对比等证券相关问题，"
    "我不会回答与证券无关的问题。\n"
    "要开对话有两个办法：\n"
    "1) 在 server/data/.secrets/llm.json 里填入 {\"base_url\":\"https://api.deepseek.com/v1\","
    "\"api_key\":\"sk-...\",\"model\":\"deepseek-chat\"}，刷新页面即生效；\n"
    "2) 点上方「复制上下文」，把证据包粘到 WorkBuddy 里问我。"
)


def chat_stream(case: dict, messages: list[dict]):
    """多轮对话流式生成；无模型通道时返回一段说明文本"""
    cfg = config()
    if not cfg:
        yield CHAT_FALLBACK
        return
    hist = [m for m in messages if m.get("role") in ("user", "assistant")][-8:]
    msgs = [
        {"role": "system", "content": SYSTEM_CHAT},
        {"role": "user", "content": f"【证据包】{_case_digest(case)}"},
        {"role": "assistant", "content": "证据包已收到，请提问。"},
    ] + hist
    try:
        for chunk in _http_stream(cfg, msgs):
            yield chunk
    except Exception as e:
        yield f"\n\n[模型调用失败：{type(e).__name__}: {e}]"


if __name__ == "__main__":
    import sys
    from datasource import evidence as ev
    code = sys.argv[1] if len(sys.argv) > 1 else "sz000995"
    c = ev.build_case(code)
    r = brief(c)
    print(r.get("mode"), "→", r.get("headline"))
    if r.get("summary"):
        print(r["summary"][:500])
