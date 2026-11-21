"""内置调度：交易日历 + 固定分桶 job + job_defs 自定义任务 + 启动追补。

默认完全不启用（main.py 只在 SCHEDULER_ENABLED=1 时挂载），因此不影响
主服务的路由集合与响应。详见 README「调度层」与 astock-refactor-plan.md 阶段 4。
"""
from . import calendar, cron, handlers, state  # noqa: F401

__all__ = ["calendar", "cron", "handlers", "state"]
