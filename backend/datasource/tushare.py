"""Tushare 数据源（占位，M2 启用）

启用步骤：
    1. pip install tushare 并设置 TUSHARE_TOKEN 环境变量
    2. 行业板块用 sw_daily / index_daily，个股用 daily
    3. 市场情绪用 daily 全市场聚合（涨跌家数、涨跌停）
    4. 跑一遍 jobs.fetch_daily 后与腾讯源结果比对，确认阶段判定一致再切换

注意：Tushare 的申万二级行业代码与本项目 pt* 代码不同，需要一张映射表。
"""
from __future__ import annotations

from .base import DataSource


class TushareSource(DataSource):
    name = "tushare"

    def __init__(self, token: str | None = None, verbose: bool = True):
        raise NotImplementedError(
            "Tushare 适配器尚未实现。当前请使用 DATA_SOURCE=tencent。"
            "实现时继承 DataSource，保证返回同样的 {code: [bar]} 结构即可无缝替换。"
        )

    def fetch_kline(self, codes, limit=500, end=None, period="day", fq="qfq"):
        raise NotImplementedError

    def fetch_sentiment(self) -> dict:
        raise NotImplementedError
