"""llm_direct 档：服务端直调 OpenAI 兼容 API，零新增依赖（urllib）。

与 ai.py 的在线 llm 通道的区别：这里**只在盘后批处理里调**，输出的是结构化
AiBrief 全字段而不是一大段自由文本，且必须过 contracts.validate_brief 的三道
校验才允许落盘。在线 /api/ai/brief 仍优先读 pregen，点击不再触发模型调用。

无 Key / 断网 / 模型不支持 JSON / 契约不过 → 一律 fallback(rule, reason)，
不抛异常、不写脏内容。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime

import ai
from contracts.validate import validate_brief
from producers.base import Producer, fallback

SYSTEM = """你是 A 股盘面复盘助手，任务是把给定的【证据包】和【规则归因】讲成人话。
硬性规则（违反即整份结果被拒）：
1. 只能复述证据包里出现的事实，不许新增数字、公司名、事件或因果链；证据包没有的写「未获取到」。
2. 每个判断后面用 [依据:字段] 标注来源字段名；字段名只能从用户消息里的【可引用字段】清单中选，不许自己造。
3. 不许给买卖建议：不出现「买入/卖出/加仓/减仓/目标价/止损位/建议」这类词。
4. 严格区分事实与推断；推断写成「倾向于……，因为……」，并说明证据强度。
5. 中文、大白话、句子短；不写排比，不写「综上所述」。
6. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释。字段固定为：
   {"headline":"一句话结论","summary":"250 字以内的四段式解读",
    "causes":[{"type":"原因类型","text":"原因 —— 支撑数据 [依据:字段]","weight":0.0,
               "evidence":["证据"],"basis":"字段名"}],
    "counters":["反证"],"risks":["风险"],"watch":["后续观察"],
    "confidence":"high|medium|low"}
   causes 最多 4 条、risks 最多 4 条、watch 最多 3 条。"""


def chat(cfg: dict, messages: list[dict], tools: list[dict] | None = None,
         timeout: int = 60) -> dict:
    """OpenAI 兼容 /chat/completions 非流式调用；返回 content + tool_calls。"""
    body: dict = {
        "model": cfg["model"], "messages": messages, "stream": False,
        "temperature": cfg.get("temperature", 0.2),
        "max_tokens": int(cfg.get("max_tokens", 1200)),
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        doc = json.loads(resp.read().decode("utf-8", "ignore"))
    msg = ((doc.get("choices") or [{}])[0].get("message") or {})
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        calls.append({"id": c.get("id"), "name": fn.get("name"),
                      "args": args if isinstance(args, dict) else {}})
    return {"content": msg.get("content") or "", "tool_calls": calls,
            "message": msg}


def _parse_json(text: str) -> dict | None:
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except ValueError:
        pass
    # 模型偶尔会在 JSON 前后加一句说明，退而取第一个完整对象
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            d = json.loads(s[i:j + 1])
            return d if isinstance(d, dict) else None
        except ValueError:
            return None
    return None


def _available_fields(obj, prefix: str = "", out: list | None = None,
                      cap: int = 80) -> list[str]:
    """把证据包拍平成「值非空的点分字段路径」清单，供模型引用白名单。

    模型不知道 digest 里哪些字段有值，不给清单就会引用 rs60 / dist52 这类
    常见但本包为 null 的字段，然后被契约整份拒掉（真调实测暴露的问题）。
    """
    out = out if out is not None else []
    if len(out) >= cap:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _available_fields(v, f"{prefix}.{k}" if prefix else str(k), out, cap)
            if len(out) >= cap:
                break
    elif isinstance(obj, list):
        if obj:
            out.append(prefix)          # 非空列表整体可引用（如 notices / rule_causes）
    elif obj is not None and obj != "":
        out.append(prefix)
    return out


class LlmDirectProducer(Producer):
    name = "llm_direct"

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg

    def _config(self) -> dict | None:
        return self.cfg if self.cfg is not None else ai.config()

    def generate(self, row: dict, rule: dict) -> dict:
        cfg = self._config()
        if not cfg:
            return fallback(rule, "no_llm_key：未配置 LLM_API_KEY / llm.json，退规则归因")
        fields = _available_fields(row)
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content":
                "【证据包】" + json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                + "\n【可引用字段】（[依据:x] 只能从这里面选）"
                + "、".join(fields)
                + "\n【规则归因（事实与权重，只能复述不许改写）】"
                + json.dumps({k: rule.get(k) for k in
                              ("headline", "causes", "counters", "risks", "watch",
                               "facts", "confidence")},
                             ensure_ascii=False, separators=(",", ":"))
                + "\n请输出约定的 JSON 对象。"},
        ]
        try:
            text = chat(cfg, messages).get("content") or ""
            draft = _parse_json(text)
            if not draft:
                return fallback(rule, "llm_non_json：模型没有输出结构化 JSON")
            draft.update({
                "code": row.get("code") or "", "date": row.get("date") or "",
                "mode": "llm", "model": cfg.get("model"),
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "confidence": draft.get("confidence") or rule.get("confidence") or "medium",
            })
            res = validate_brief(draft, row)
            if not res.ok:
                return fallback(rule, "contract_rejected：" + res.reason)
            return res.normalized
        except Exception as exc:
            return fallback(rule, f"llm_error：{type(exc).__name__}: {exc}")


__all__ = ["LlmDirectProducer", "chat", "SYSTEM", "_available_fields"]
