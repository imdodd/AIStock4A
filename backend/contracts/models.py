"""三份产出契约（pydantic v2）：digest / ai_brief / news_patch。

设计原则：
  1. **模型即文档**：字段、默认值、枚举写在这里，schema 从这里导出；
  2. **宽容读取**：老的 415 个 cases 文件缺字段不报错（`extra="allow"` + 全默认值），
     但**落盘前校验是严格的**（见 validate.py）—— 读得进旧数据，写不进脏数据；
  3. **枚举与文字分家**：`impact` / `dir` 只能四值，长分析一律进 `note` / `impact_note`，
     杜绝 11-15 日文件里「118 字长文当枚举 key、统计卡全 0」的事故重演。
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

IMPACT_VALUES = ("利多", "利空", "中性", "分化")
MODE_VALUES = ("pregen", "llm", "rule")
CONFIDENCE_VALUES = ("high", "medium", "low")
CATEGORY_VALUES = ("us_stock", "fed", "us_gov", "cn_policy", "commodity", "other")
CODE_RE = re.compile(r"^(sh|sz|bj)\d{6}$")

# 短变体收敛；长文本一律拒绝（不能让模型把整段分析塞进枚举字段）
_IMPACT_VARIANTS: tuple[tuple[str, str], ...] = (
    ("中性偏多", "中性"), ("中性偏空", "中性"), ("中性略偏多", "中性"),
    ("中性略偏空", "中性"), ("利多为主", "利多"), ("利空为主", "利空"),
    ("偏多", "利多"), ("偏空", "利空"), ("利好", "利多"), ("利淡", "利空"),
    ("喜忧参半", "分化"), ("多空分化", "分化"), ("无明显方向", "中性"),
)


def normalize_impact(value: Any) -> str:
    """四值枚举归一化。短变体收敛，长分析文本直接拒绝。"""
    s = str(value or "").strip()
    if s in IMPACT_VALUES:
        return s
    if len(s) > 12:
        raise ValueError(
            f"impact/dir 只能是 {IMPACT_VALUES} 四值，收到 {len(s)} 字长文；"
            "分析文字请放 impact_note / note / logic")
    for needle, target in _IMPACT_VARIANTS:
        if needle in s:
            return target
    raise ValueError(f"impact/dir 只能是 {IMPACT_VALUES} 四值，收到 {s!r}")


class _Loose(BaseModel):
    """所有契约的共同基类：允许额外字段，缺字段给默认值。"""
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- 证据包
class DigestRow(_Loose):
    """tools.pregen.export() 产出的单只票摘要。字段远不止这些，全部靠 extra 保留。"""
    code: str = ""
    name: str = ""
    chg: float | None = None
    vol_ratio: float | None = None
    tag: str | None = None
    industry: str | None = None
    hist: dict[str, Any] = Field(default_factory=dict)
    flow: dict[str, Any] = Field(default_factory=dict)
    notices: list[str] = Field(default_factory=list)
    news: list[str] = Field(default_factory=list)
    rule_causes: list[str] = Field(default_factory=list)
    rule_risks: list[str] = Field(default_factory=list)


class DigestPack(_Loose):
    date: str
    count: int = 0
    items: list[DigestRow] = Field(default_factory=list)


# ---------------------------------------------------------------- AI 解读
class Cause(_Loose):
    type: str = ""
    text: str = ""
    weight: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    basis: str = ""                      # [依据:字段名] 里的字段名


class Source(_Loose):
    """检索产物：数字核对与可审计性的前提，没 sources 就不许出「分析」。"""
    title: str = ""
    url: str = ""
    time: str = ""
    snippet: str = ""


class AiBrief(_Loose):
    code: str = ""
    date: str = ""
    headline: str = ""
    summary: str = ""
    causes: list[Cause] = Field(default_factory=list)
    # reasons.brief() 的 counters 是 dict 列表、sources 是 {字段: bool}，
    # 契约必须能宽容读进这两种历史形状，否则规则兜底结果自己都过不了校验。
    counters: list[Any] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    watch: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    mode: Literal["pregen", "llm", "rule"] = "rule"
    generated_at: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"
    fallback_reason: str | None = None
    sources: Any = None


# ---------------------------------------------------------------- 资讯 patch
class ImpactRef(_Loose):
    code: str = ""
    name: str | None = None
    dir: str = "中性"
    note: str | None = None

    @field_validator("dir", mode="before")
    @classmethod
    def _dir(cls, v):
        return normalize_impact(v)


class SectorImpact(ImpactRef):
    pass


class StockImpact(ImpactRef):
    pass


class NewsItem(_Loose):
    title: str = ""
    url: str = ""
    source: str = ""
    time: str = ""
    summary: str = ""
    category: str = "other"
    impact: str = "中性"
    impact_note: str | None = None
    horizon: str = "短期"
    confidence: str = "中"
    logic: str = ""
    keywords: list[str] = Field(default_factory=list)
    sectors: list[SectorImpact] = Field(default_factory=list)
    stocks: list[StockImpact] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    fetched_by: str | None = None

    @field_validator("impact", mode="before")
    @classmethod
    def _impact(cls, v):
        return normalize_impact(v)

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        s = str(v or "other").strip()
        return s if s in CATEGORY_VALUES else "other"


class NewsPatch(_Loose):
    date: str
    summary: str = ""
    window: str = ""
    source_of_truth: str = ""
    items: list[NewsItem] = Field(default_factory=list)


__all__ = ["IMPACT_VALUES", "MODE_VALUES", "CONFIDENCE_VALUES", "CATEGORY_VALUES",
           "CODE_RE", "normalize_impact", "DigestRow", "DigestPack", "Cause",
           "Source", "AiBrief", "ImpactRef", "SectorImpact", "StockImpact",
           "NewsItem", "NewsPatch"]
