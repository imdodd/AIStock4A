"""产出契约：模型即文档，落盘前一律过 validate。

  from contracts import DigestPack, AiBrief, NewsPatch, validate_brief, validate_patch
"""
from __future__ import annotations

from contracts.models import (AiBrief, CATEGORY_VALUES, CODE_RE, CONFIDENCE_VALUES,
                              Cause, DigestPack, DigestRow, IMPACT_VALUES,
                              MODE_VALUES, NewsItem, NewsPatch, SectorImpact,
                              Source, StockImpact, normalize_impact)
from contracts.validate import (ContractError, ValidationResult, check_basis,
                                check_compliance, check_injection, check_numbers,
                                collect_numbers, extract_numbers, resolve_path,
                                validate_brief, validate_news_item, validate_patch)

__all__ = ["DigestPack", "DigestRow", "AiBrief", "Cause", "Source", "NewsItem",
           "NewsPatch", "SectorImpact", "StockImpact", "IMPACT_VALUES",
           "MODE_VALUES", "CONFIDENCE_VALUES", "CATEGORY_VALUES", "CODE_RE",
           "normalize_impact", "ValidationResult", "ContractError",
           "validate_brief", "validate_news_item", "validate_patch",
           "check_basis", "check_numbers", "check_compliance", "check_injection",
           "extract_numbers", "collect_numbers", "resolve_path"]
