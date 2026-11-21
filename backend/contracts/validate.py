"""落盘前校验：把「AI 只复述证据」从口号变成可执行规则。

三道防幻觉（针对模型产出，rule 结果不跑）：
  1) 依据字段：`[依据:xxx]` 的 xxx 必须能在 digest 里解析到非 null 的值；
  2) 数字核对：文本里出现的每个数字都要能在证据包 / sources[].snippet 里找到
     （允许 ±0.01、1% 相对误差与万/亿/百分比单位换算）；
  3) 合规词：买卖建议词一律拒绝；confidence=low 时另禁「确认 / 必然」等强断言。

另加两条针对资讯链路的护栏：
  - 枚举与文字分家（在 models.normalize_impact 里完成，长文当 dir 直接 ValidationError）；
  - prompt injection：检索 snippet 里出现「忽略以上指令」类内容，整条拒绝且不落盘。

校验失败不写脏内容：build_cases 退到 reasons.brief() 的规则结果并附
`fallback_reason`；news patch 整条跳过并记 `rejected_reason`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from pydantic import ValidationError

from contracts.models import (AiBrief, CODE_RE, NewsItem, NewsPatch,
                              normalize_impact)

BASIS_RE = re.compile(r"\[依据:([^\]]+)\]")
NUM_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")
DATE_TIME_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}:\d{2}(?::\d{2})?")
URL_RE = re.compile(r"https?://\S+")
ADVICE_WORDS = ("建议买入", "建议卖出", "建议加仓", "建议减仓", "目标价", "止损位",
                "买入", "卖出", "加仓", "减仓", "满仓", "清仓", "推荐买", "可以买")
STRONG_WORDS = ("确认", "必然", "一定", "肯定", "板上钉钉", "百分之百")
INJECTION_WORDS = ("忽略以上指令", "忽略之前的指令", "忽略上述指令", "忽略所有指令",
                   "ignore previous", "ignore all previous", "ignore above",
                   "直接写日文件", "把 impact 改成", "把影响改成", "system prompt",
                   "你现在是", "do not follow", "disregard previous")
_SKIP_KEYS = {"date", "time", "url", "id", "code", "name", "source", "fetched_by",
              "category", "category_name", "title_url", "published_at"}
_MISSING = object()


@dataclass
class ValidationResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    normalized: dict | None = None

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


class ContractError(ValueError):
    """patch 顶层结构不合法（例如缺 date / items 不是数组）。"""


# ---------------------------------------------------------------- 数字
def extract_numbers(text: Any) -> list[float]:
    """从一段文本里抽出可比对的数值，剔除日期、时间、URL 与依据字段名里的数字。"""
    s = str(text or "")
    s = DATE_TIME_RE.sub(" ", s)
    s = URL_RE.sub(" ", s)
    s = BASIS_RE.sub(" ", s)
    out = []
    for m in NUM_RE.findall(s):
        try:
            out.append(float(m))
        except ValueError:
            continue
    return out


def _scaled(n: float) -> set[float]:
    """单位换算候选：元 / 万元 / 亿元 / 百分比，正负号都可。"""
    vals = {n, -n}
    for k in (100.0, 1e4, 1e8, 0.01, 1e-4, 1e-8):
        vals.add(n * k)
        vals.add(n / k if k else n)
    return vals


def collect_numbers(obj: Any, path: str = "") -> set[float]:
    """递归收集证据包里的全部数值（跳过标识、时间、URL 之类的非事实数字）。"""
    out: set[float] = set()
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out |= _scaled(float(obj))
        return out
    if isinstance(obj, str):
        if path.split(".")[-1] in _SKIP_KEYS:
            return out
        for n in extract_numbers(obj):
            out |= _scaled(n)
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) in _SKIP_KEYS:
                continue
            out |= collect_numbers(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= collect_numbers(v, path)
    return out


def _close(a: float, universe: set[float]) -> bool:
    return any(abs(a - b) <= max(0.011, abs(b) * 0.01) for b in universe)


def check_numbers(texts: Iterable[Any], evidence: Any,
                  extra_sources: Iterable[dict] | None = None) -> list[str]:
    universe = collect_numbers(evidence)
    for src in extra_sources or []:
        universe |= collect_numbers({"snippet": src.get("snippet", ""),
                                     "title": src.get("title", "")})
    if not universe:
        # 证据里没有数字本身不是错：很多事实型新闻（如「银行加码支持六张网」）
        # 就没有可核对的数值。只有当模型文本真的写出了数字，才有编数风险要拒。
        if any(extract_numbers(t) for t in texts):
            return ["证据包里没有可核对的数字，模型文本不允许出现任何数字"]
        return []
    reasons = []
    for text in texts:
        for n in extract_numbers(text):
            if not _close(n, universe):
                reasons.append(f"数字 {n:g} 不在证据包/sources 里（疑似编数）")
    # 去重，避免同一句话刷屏
    return list(dict.fromkeys(reasons))


# ---------------------------------------------------------------- 依据 / 合规
def resolve_path(row: dict, dotted: str) -> Any:
    cur: Any = row
    for part in dotted.strip().split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def check_basis(texts: Iterable[Any], row: dict) -> list[str]:
    reasons = []
    for text in texts:
        for name in BASIS_RE.findall(str(text or "")):
            value = resolve_path(row, name)
            if value is _MISSING:
                reasons.append(f"[依据:{name}] 不在证据包字段白名单内")
            elif value is None or value == "" or value == []:
                reasons.append(f"[依据:{name}] 指向 null/空值，不许对着缺失证据硬写")
    return list(dict.fromkeys(reasons))


def check_compliance(texts: Iterable[Any], confidence: str = "medium") -> list[str]:
    blob = " ".join(str(t or "") for t in texts)
    reasons = [f"出现买卖建议词「{w}」" for w in ADVICE_WORDS if w in blob]
    if confidence == "low":
        reasons += [f"低置信度却出现强断言「{w}」" for w in STRONG_WORDS if w in blob]
    return list(dict.fromkeys(reasons))


def check_injection(text: Any) -> str | None:
    s = str(text or "").lower()
    for w in INJECTION_WORDS:
        if w.lower() in s:
            return f"疑似 prompt injection：{w}"
    return None


# ---------------------------------------------------------------- AI 解读
def _brief_texts(b: dict) -> list[str]:
    out = [b.get("headline"), b.get("summary")]
    out += [c.get("text") for c in (b.get("causes") or []) if isinstance(c, dict)]
    for c in b.get("counters") or []:
        out.append(c.get("text") if isinstance(c, dict) else
                   c if isinstance(c, str) else None)
    out += list(b.get("risks") or [])
    out += list(b.get("watch") or [])
    return [str(x or "") for x in out]


def validate_brief(brief: dict, digest_row: dict) -> ValidationResult:
    """宽容读入 → 严格校验。rule 结果由代码生成，直接放行（但仍要过契约形状）。"""
    try:
        normalized = AiBrief.model_validate(brief).model_dump()
    except ValidationError as e:
        return ValidationResult(False, [f"AiBrief 契约不合法：{e.errors()[0]['msg']}"])
    if normalized.get("mode") == "rule":
        return ValidationResult(True, [], normalized)
    texts = _brief_texts(normalized)
    srcs = normalized.get("sources")
    reasons = check_basis(texts, digest_row)
    reasons += check_numbers(texts, digest_row,
                             srcs if isinstance(srcs, list) else None)
    reasons += check_compliance(texts, normalized.get("confidence") or "medium")
    return ValidationResult(not reasons, reasons, normalized)


# ---------------------------------------------------------------- 资讯 patch
def validate_news_item(item: dict, sectors_index: dict | None = None,
                       require_sources: bool = True,
                       number_context: Iterable[dict] | None = None) -> ValidationResult:
    try:
        normalized = NewsItem.model_validate(item).model_dump()
    except ValidationError as e:
        msg = e.errors()[0]
        return ValidationResult(False, [f"NewsItem 契约不合法：{msg['loc']} {msg['msg']}"])

    reasons: list[str] = []
    for key in ("title", "url", "source", "time"):
        if not str(normalized.get(key) or "").strip():
            reasons.append(f"缺必填字段 {key}")
    if not str(normalized.get("url") or "").startswith(("http://", "https://")):
        reasons.append("url 必须是 http(s) 真实链接")
    if not str(normalized.get("logic") or "").strip():
        reasons.append("缺传导逻辑 logic")

    srcs = normalized.get("sources") or []
    if require_sources and not srcs:
        reasons.append("缺 sources[]：没有可审计来源的分析不许落盘")
    for s in srcs:
        hit = check_injection(s.get("snippet") or s.get("title"))
        if hit:
            reasons.append(hit)
    for key in ("title", "summary", "impact_note", "logic"):
        hit = check_injection(normalized.get(key))
        if hit:
            reasons.append(hit)

    if sectors_index is not None:
        for s in normalized.get("sectors") or []:
            if not s.get("code") or s["code"] not in sectors_index:
                reasons.append(f"板块 code {s.get('code')!r} 不在 snapshot/sectors.json")
    for st in normalized.get("stocks") or []:
        if not CODE_RE.match(str(st.get("code") or "")):
            reasons.append(f"个股 code {st.get('code')!r} 不是 sh/sz/bj + 6 位")

    texts = [normalized.get("title"), normalized.get("summary"),
             normalized.get("impact_note"), normalized.get("logic")]
    for group in ("sectors", "stocks"):
        texts += [x.get("note") for x in (normalized.get(group) or [])]
    # 数字核对覆盖事实与判断文本；合规词只扫判断层文本，避免把
    # 「机构卖出」这类事实标题误判成买卖建议（事实层本就没有观点）。
    judgment = [normalized.get("impact_note"), normalized.get("logic")]
    for group in ("sectors", "stocks"):
        judgment += [x.get("note") for x in (normalized.get(group) or [])]
    # 证据白名单 = sources[] 摘录 + number_context（本轮检索语料全文）。
    # 只看摘录会把 LLM 摘片段时漏掉的真实数字误判成编数（实测一轮候选全灭）：
    # 语料里的数字是搜索真实返回的，不来自模型脑补，纳入白名单不算放松防幻觉——
    # 凭空造一个语料里也没有的数依然会被拒。仍绝不拿被校验文本自证。
    reasons += check_numbers([t for t in texts if t], {},
                             list(srcs) + [c for c in (number_context or [])
                                           if isinstance(c, dict)])
    reasons += check_compliance([t for t in judgment if t],
                                "low" if normalized.get("confidence") == "低"
                                else "medium")
    return ValidationResult(not reasons, list(dict.fromkeys(reasons)), normalized)


def validate_patch(patch: dict, sectors_index: dict | None = None,
                   require_sources: bool = True,
                   number_context: Iterable[dict] | None = None) -> tuple[dict, list[dict]]:
    """逐条校验；失败的整条跳过并给出 rejected_reason，绝不污染日文件。"""
    if not isinstance(patch, dict):
        raise ContractError("news_patch 顶层必须是对象")
    raw_items = patch.get("items") or []
    if not isinstance(raw_items, list):
        raise ContractError("news_patch.items 必须是数组")
    # 顶层单独校验（items 置空）：单条脏数据只能被跳过，不能把整份 patch 判死，
    # 否则 rejected_reason 就没法落到「条目」粒度上，也不符合增量合并的语义。
    bare = {k: v for k, v in patch.items() if k != "items"}
    bare["items"] = []
    try:
        p = NewsPatch.model_validate(bare)
    except ValidationError as e:
        raise ContractError(f"news_patch 顶层不合法：{e.errors()[0]['msg']}") from e
    out = p.model_dump()
    kept, rejected = [], []
    for raw in raw_items:
        res = validate_news_item(raw if isinstance(raw, dict) else {},
                                 sectors_index, require_sources, number_context)
        if res.ok:
            kept.append(res.normalized)
        else:
            rejected.append({"title": str((raw or {}).get("title") or "")[:60],
                             "rejected_reason": res.reason})
    out["items"] = kept
    return out, rejected


__all__ = ["ValidationResult", "ContractError", "extract_numbers", "collect_numbers",
           "check_numbers", "check_basis", "check_compliance", "check_injection",
           "resolve_path", "validate_brief", "validate_news_item", "validate_patch",
           "normalize_impact"]
