"""新剧自动检索:发现 → 入队 → 毕业 闭环。"""

from __future__ import annotations

from datetime import date

import pytest

from audio_hub.config import LackfillConfig
from audio_hub.lackfill import newshow, queue


class FakeEmby:
    def __init__(self, series):
        self._series = series

    async def list_series(self):
        return self._series

    async def aclose(self):
        return None


class FakeTmdb:
    def __init__(self, trending, details, seasons):
        self.trending = trending
        self.details = details   # tmdb_id -> tv() 响应
        self.seasons = seasons   # (tmdb_id, season) -> [(ep, air_date)]

    async def trending_tv_week(self):
        return self.trending

    async def tv(self, tmdb_id):
        return self.details[tmdb_id]

    async def season_aired_episode_numbers(self, tmdb_id, season, today):
        aired = [ep for ep, air in self.seasons[(tmdb_id, season)] if air <= today]
        return sorted(aired)

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_discover_enqueue_and_graduate(test_db):
    cfg = LackfillConfig(enabled=True, new_show_daily_quota=2)
    tmdb = FakeTmdb(
        trending=[
            {"id": 101, "name": "新剧A", "first_air_date": "2026-10-01"},
            {"id": 202, "name": "新剧B", "first_air_date": "2026-09-01"},
            {"id": 303, "name": "库里已有", "first_air_date": "2026-01-01"},
        ],
        details={
            101: {"name": "新剧A", "seasons": [{"season_number": 1}]},
            202: {"name": "新剧B", "seasons": [{"season_number": 1}]},
        },
        seasons={
            (101, 1): [(1, date(2026, 10, 1)), (2, date(2026, 10, 8)), (3, date(2099, 1, 1))],
            (202, 1): [(1, date(2026, 9, 1))],
        },
    )
    today = date(2026, 10, 10)

    # 第一轮:Emby 空 → 发现 2 部(库里已有被跳过),入队且只算已播集(第3集未播不算)
    emby = FakeEmby([])
    stats = await newshow.run_once(test_db, cfg, emby, tmdb, today=today, now="2026-10-10T08:00:00")
    assert stats["discovered"] == 2
    assert stats["enqueued"] == 2
    rows = {r["tmdb_id"]: r for r in await queue.list_pending(test_db)}
    assert set(rows) == {101, 202}
    assert rows[101]["seasons"] == {"1": [1, 2]}   # 未播的第 3 集不进队列
    assert rows[101]["missing"] == 2

    # 第二轮:新剧A 已进 Emby → 毕业退出;新剧B 继续监控(无重复发现)
    emby_graduated = FakeEmby([
        {"emby_id": "e1", "name": "新剧A", "year": 2026, "tmdb_id": 101},
        {"emby_id": "e3", "name": "库里已有", "year": 2026, "tmdb_id": 303},
    ])
    stats2 = await newshow.run_once(test_db, cfg, emby_graduated, tmdb, today=today, now="2026-10-11T08:00:00")
    assert stats2["graduated"] == 1
    assert stats2["discovered"] == 0          # 101 已进库跳过、202 已监控不重复、303 库里已有跳过
    monitored = {r["tmdb_id"] for r in await newshow.list_monitored(test_db)}
    assert 101 not in monitored               # 毕业
    assert 202 in monitored


@pytest.mark.asyncio
async def test_quota_zero_disables_discovery_but_keeps_monitoring(test_db):
    cfg = LackfillConfig(enabled=True, new_show_daily_quota=0)
    tmdb = FakeTmdb(
        trending=[{"id": 501, "name": "X", "first_air_date": "2026-01-01"}],
        details={501: {"name": "X", "seasons": []}},
        seasons={},
    )
    # 预置一条监控
    await newshow.run_once(
        test_db, LackfillConfig(enabled=True, new_show_daily_quota=1),
        FakeEmby([]), tmdb, today=date(2026, 1, 2), now="2026-01-02T00:00:00",
    )
    stats = await newshow.run_once(
        test_db, cfg, FakeEmby([]), tmdb, today=date(2026, 1, 3), now="2026-01-03T00:00:00"
    )
    assert stats["discovered"] == 0           # 配额 0:不再发现
    assert stats["monitored"] == 1            # 已监控的仍被照看


@pytest.mark.asyncio
async def test_single_show_error_does_not_abort_round(test_db):
    class BrokenTmdb(FakeTmdb):
        async def tv(self, tmdb_id):
            if tmdb_id == 601:
                raise RuntimeError("tmdb 502")
            return await super().tv(tmdb_id)

    cfg = LackfillConfig(enabled=True, new_show_daily_quota=2)
    tmdb = BrokenTmdb(
        trending=[
            {"id": 601, "name": "坏剧", "first_air_date": ""},
            {"id": 602, "name": "好剧", "first_air_date": "2026-02-01"},
        ],
        details={602: {"name": "好剧", "seasons": [{"season_number": 1}]}},
        seasons={(602, 1): [(1, date(2026, 2, 1))]},
    )
    stats = await newshow.run_once(test_db, cfg, FakeEmby([]), tmdb, today=date(2026, 2, 2), now="t")
    assert stats["enqueued"] == 1             # 602 正常入队
    assert stats["errors"] and "坏剧" in stats["errors"][0]
