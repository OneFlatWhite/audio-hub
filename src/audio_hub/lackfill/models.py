"""补缺集的数据结构:逐剧缺集清单与整轮扫描报告。

两个 dataclass 都刻意保持「纯数据 + 只读派生属性」:不持有客户端、不做 IO,
方便上层直接序列化入库或推通知。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ShowLack:
    """单部剧集的缺集记录。

    Attributes:
        tmdb_id: TMDB 剧集 ID。
        title: 剧名(取自 Emby,便于展示)。
        year: 首播年份,可能缺失。
        emby_id: Emby 里的剧集条目 ID,后续补齐任务定位用。
        missing: 季号 → 该季缺失集号列表(升序);只含有缺集的季。
    """

    tmdb_id: int
    title: str
    year: int | None
    emby_id: str
    missing: dict[int, list[int]]

    @property
    def missing_count(self) -> int:
        """缺集总数(各季缺失集数求和)。"""
        return sum(len(eps) for eps in self.missing.values())


@dataclass
class ScanReport:
    """一轮缺集扫描的报告。

    Attributes:
        started_at: 扫描开始时间。
        finished_at: 扫描结束时间;未结束时为 None。
        shows_total: Emby 库里的剧集总数。
        shows_scanned: 本轮实际处理(含跳过)的剧集数,受单轮预算限制。
        lacks: 有缺集的剧集清单。
        errors: 过程中收集的错误描述(如缺 TMDB ID、单剧请求失败)。
    """

    started_at: datetime
    finished_at: datetime | None = None
    shows_total: int = 0
    shows_scanned: int = 0
    lacks: list[ShowLack] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def lack_show_count(self) -> int:
        """有缺集的剧集数。"""
        return len(self.lacks)

    @property
    def lack_episode_count(self) -> int:
        """缺集总数(逐剧求和)。"""
        return sum(lack.missing_count for lack in self.lacks)

    def to_dict(self) -> dict:
        """转为可直接 JSON 序列化的字典;季号键转字符串,时间转 ISO 格式。"""
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "shows_total": self.shows_total,
            "shows_scanned": self.shows_scanned,
            "lack_show_count": self.lack_show_count,
            "lack_episode_count": self.lack_episode_count,
            "lacks": [
                {
                    "tmdb_id": lack.tmdb_id,
                    "title": lack.title,
                    "year": lack.year,
                    "emby_id": lack.emby_id,
                    "missing": {str(s): eps for s, eps in lack.missing.items()},
                    "missing_count": lack.missing_count,
                }
                for lack in self.lacks
            ],
            "errors": list(self.errors),
        }
