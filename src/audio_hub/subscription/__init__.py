"""订阅引擎（v0.6 波次 2B）：RSS 捕获 + 主动搜索 + 决策（去重 / 洗版 / 派发 / 待审）。

对外只暴露 engine 模块的三个入口与两个 Summary；波次 3 的 scheduler 按签名接入。
"""

from .engine import (
    EVENT_SUBSCRIPTION_COMPLETED,
    EVENT_SUBSCRIPTION_HIT,
    EVENT_SUBSCRIPTION_UPGRADE,
    CaptureSummary,
    SearchSummary,
    build_queries,
    build_site_priority,
    decide,
    is_one_shot,
    run_rss_capture_once,
    run_subscription_search_once,
)

__all__ = [
    "EVENT_SUBSCRIPTION_COMPLETED",
    "EVENT_SUBSCRIPTION_HIT",
    "EVENT_SUBSCRIPTION_UPGRADE",
    "CaptureSummary",
    "SearchSummary",
    "build_queries",
    "build_site_priority",
    "decide",
    "is_one_shot",
    "run_rss_capture_once",
    "run_subscription_search_once",
]
