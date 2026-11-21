"""规则档：零成本、零依赖、永远可用。

它不调模型，所以也就不存在幻觉；reasons.brief() 算出的 headline / causes /
counters / risks / watch / confidence 直接作为契约产出。断网、无 Key、上游限流、
模型输出被契约拒绝，全部退到这一档。
"""
from __future__ import annotations

from producers.base import Producer, fallback


class RuleProducer(Producer):
    name = "rule"

    def generate(self, row: dict, rule: dict) -> dict:
        return fallback(rule, None)


__all__ = ["RuleProducer"]
