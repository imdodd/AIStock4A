"""数据源适配层

目的：把「数据从哪来」与「数据怎么算」彻底分开。
     engines/ 只认标准化的 row（date/open/high/low/close/volume/amount），
     jobs/   只认 DataSource 接口，
     换数据源（westock → Tushare → 本地库）时只改 datasource/ 下的实现。

统一行格式（bar）：
    {"date": "2026-10-30", "open": 1.0, "high": 1.0, "low": 1.0,
     "close": 1.0, "volume": 1234.0, "amount": 5678.0}
    注意：date 升序（早 → 晚），volume 单位为手，amount 单位为元（缺失填 None）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class DataSource(ABC):
    """所有数据源实现的统一接口"""

    name: str = "base"

    @abstractmethod
    def fetch_kline(self, codes: list[str], limit: int = 500, end: str | None = None,
                    period: str = "day", fq: str = "qfq") -> dict[str, list[dict]]:
        """返回 {code: [bar, ...]}，缺失的代码不出现在结果里"""

    @abstractmethod
    def fetch_sentiment(self) -> dict:
        """返回市场情绪快照（涨跌家数、涨跌停、新高新低）"""

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def normalize_nodes(nodes: list[dict]) -> list[dict]:
    """把各数据源返回的原始节点统一成标准 bar（升序）

    注意 westock 的收盘价字段叫 last，不是 close；两个都认，避免静默取到 0。
    """
    out = []
    for n in nodes or []:
        d = n.get("date") or n.get("trade_date") or n.get("day")
        if not d:
            continue
        d = str(d)
        if len(d) == 8 and d.isdigit():  # 20261030 → 2026-10-30
            d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        close = n.get("close")
        if close in (None, ""):
            close = n.get("last")
        try:
            bar = {
                "date": d,
                "open": float(n.get("open") or 0),
                "high": float(n.get("high") or 0),
                "low": float(n.get("low") or 0),
                "close": float(close or 0),
                "volume": float(n.get("volume") or n.get("vol") or 0),
                "amount": float(n["amount"]) if n.get("amount") not in (None, "") else None,
            }
        except (TypeError, ValueError):
            continue
        if bar["close"] <= 0:  # 收盘价缺失的行没有分析价值，丢弃
            continue
        out.append(bar)
    out.sort(key=lambda x: x["date"])
    return out
