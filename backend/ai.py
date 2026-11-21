"""AI 解读层：预生成优先 → 直连模型 → 规则兜底

三条通道，按序降级：

  1. pregen   读 data/cases/{date}/ai/{code}.json（由 WorkBuddy 侧盘后批量生成）
              —— 零延迟、零成本、点开秒出，是默认主通道
  2. llm      读 data/.secrets/llm.json 里的 OpenAI 兼容配置（base_url/api_key/model）
              —— 支持流式多轮对话，配置后自动启用
  3. rule     纯本地规则归因（reasons.py），保证任何情况下都不断供

模型被约束为「只复述证据包」：证据没有的一律答"未获取到"，不给买卖建议。
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

SYSTEM = """你是 A 股盘面复盘助手，任务是解释个股当日「为什么异动」，并回答用户追问。

硬性规则（违反即失败）：
1. 只能使用【证据包】里出现的事实。证据包没有的信息，一律回答「未获取到」，绝不用常识、记忆或推测补齐。
2. 每个判断后面用 [依据:字段] 标注来源字段名，例如 [依据:flow.main_net]。
3. 不给买卖建议：不出现「建议买入/卖出/加仓/减仓/目标价/止损位」这类表述。可以陈述关键价位与风险。
4. 严格区分事实与推断。推断必须写成「倾向于……，因为……」，并说明证据强度。
5. 中文，大白话，句子短。不要排比，不要「综上所述」「值得注意的是」这类空话。
6. 回答长度控制在 250 字以内，需要分点时最多 4 条。

解读板块异动时按这个结构：
结论：一句话说清今天发生了什么。
归因：最多三条，每条一行，「原因 —— 支撑数据」。
反证：一句话，说明为什么不是另一个更常见的原因。
风险：一到两条。"""


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
                    "请按「结论 / 归因 / 反证 / 风险」四段输出这只票今天的异动解读。"},
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
        {"role": "system", "content": SYSTEM},
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
