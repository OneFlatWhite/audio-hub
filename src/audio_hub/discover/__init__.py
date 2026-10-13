"""发现墙（v0.7）：各站最新上架的聚合卡片墙 + 一键订阅 + iTunes 封面代理。

设计定案：
- 数据源是适配器 fetch_rss()（各站最新 50 条，RSS 内嵌 passkey 已认证），
  展示「你的站点上真实存在的最新上架」，不做通用热门榜、不抓种子详情页。
- PT 站列表不供封面；封面由 covers 层经 iTunes Search API 补齐（免费无 key，
  服务端代理 + 磁盘缓存），失败静默降级为占位卡。
- 一键订阅只是订阅引擎（SubType.ARTIST / SubType.ALBUM）的快捷入口。
"""

from __future__ import annotations

from .aggregator import DiscoverCard, DiscoverWall, WallCache, collect_wall
from .covers import cover_for, covers_dir, fill_covers, is_valid_key

__all__ = [
    "DiscoverCard",
    "DiscoverWall",
    "WallCache",
    "collect_wall",
    "cover_for",
    "covers_dir",
    "fill_covers",
    "is_valid_key",
]
