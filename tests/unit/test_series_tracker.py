"""series.tracker 单测：分集号解析、登记、完结判定与事件。"""

from __future__ import annotations

import pytest

from audio_hub import repo
from audio_hub.models import PartEntry, SeriesEntry, Task, WatchEntry, WatchKind
from audio_hub.notify.base import Event
from audio_hub.series.tracker import (
    DEFAULT_PART_PATTERN,
    build_series_context,
    extract_part_no,
    refresh_status,
    register_part,
    register_task,
)


class FakeNotifier:
    def __init__(self):
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


# ── 分集号解析 ──────────────────────────────────────────────

class TestExtractPartNo:
    @pytest.mark.parametrize("title,expected", [
        ("三体 第3集 有声书 MP3", 3),
        ("明朝那些事儿 第 12 部 演播", 12),
        ("盗墓笔记 第7卷 M4B", 7),
        ("The Expanse Part 7 Audiobook", 7),
        ("Dune Audiobook EP05 MP3", 5),
        ("Wheel of Time E12 M4B", 12),
        ("鬼吹灯 (03) 有声书", 3),
        ("三体 有声书 全集 MP3 无编号", None),
        ("", None),
    ])
    def test_default_pattern(self, title, expected):
        assert extract_part_no(title) == expected

    def test_year_in_parens_not_treated_as_part(self):
        """括号数字限 1~3 位，年份 (2020) 不应被当作分集号。"""
        assert extract_part_no("三体 有声书 (2020) MP3") is None

    def test_explicit_default_equals_none(self):
        assert extract_part_no("第3集", DEFAULT_PART_PATTERN) == extract_part_no("第3集")

    def test_custom_pattern(self):
        assert extract_part_no("凡人修仙传 卷十二 vol.12 MP3", r"vol\.(\d+)") == 12

    def test_custom_pattern_takes_precedence_over_default(self):
        # 默认规则会抓到 "第1部"，自定义规则只认 "#N"
        assert extract_part_no("第1部 #9", r"#(\d+)") == 9

    def test_custom_pattern_no_match_returns_none(self):
        assert extract_part_no("第3集", r"vol\.(\d+)") is None

    def test_invalid_pattern_falls_back_to_default(self):
        assert extract_part_no("三体 第3集", r"第(\d+") == 3

    def test_pattern_without_group_falls_back_to_default(self):
        assert extract_part_no("三体 第3集", r"\d+") == 3

    def test_custom_pattern_case_insensitive(self):
        assert extract_part_no("Book VOL.4", r"vol\.(\d+)") == 4


# ── 登记与完结判定（真 DB） ─────────────────────────────────

def _task(title: str, part_no: int | None = None, series_id: int | None = None) -> Task:
    return Task(
        id=f"t-{title}", info_hash=f"h-{title}", site="hhanclub", title=title,
        part_no=part_no, series_id=series_id,
    )


class TestRegisterPart:
    async def test_register_parses_title_and_bumps_last_part(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="三体", total_parts=3))
        series = await repo.get_series(test_db, sid)
        notifier = FakeNotifier()

        assert await register_part(test_db, series, _task("三体 第1部 MP3"), notifier) is True
        parts = await repo.list_series_parts(test_db, sid)
        assert [p.part_no for p in parts] == [1]
        assert parts[0].task_id == "t-三体 第1部 MP3"
        assert (await repo.get_series(test_db, sid)).last_part == 1
        assert notifier.events == []          # 1/3 未完结

    async def test_register_prefers_task_part_no(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S"))
        series = await repo.get_series(test_db, sid)
        # 标题写第 3 集，但 matcher 已解析出 part_no=5 → 以 task.part_no 为准
        assert await register_part(test_db, series, _task("X 第3集", part_no=5)) is True
        assert [p.part_no for p in await repo.list_series_parts(test_db, sid)] == [5]

    async def test_register_uses_series_pattern(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S", part_pattern=r"vol\.(\d+)"))
        series = await repo.get_series(test_db, sid)
        assert await register_part(test_db, series, _task("X vol.8")) is True
        assert [p.part_no for p in await repo.list_series_parts(test_db, sid)] == [8]

    async def test_register_unparsable_returns_false(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S"))
        series = await repo.get_series(test_db, sid)
        assert await register_part(test_db, series, _task("无编号 全集")) is False
        assert await repo.count_series_parts(test_db, sid) == 0

    async def test_register_duplicate_part_returns_false(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S"))
        series = await repo.get_series(test_db, sid)
        assert await register_part(test_db, series, _task("S 第2部")) is True
        assert await register_part(test_db, series, _task("S 第2部 重制")) is False
        assert await repo.count_series_parts(test_db, sid) == 1

    async def test_register_completes_and_emits_once(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="三体", total_parts=2))
        series = await repo.get_series(test_db, sid)
        notifier = FakeNotifier()

        await register_part(test_db, series, _task("三体 第1部"), notifier)
        await register_part(test_db, series, _task("三体 第2部"), notifier)

        s = await repo.get_series(test_db, sid)
        assert s.status == "completed"
        assert s.last_part == 2
        assert notifier.kinds() == ["series_completed"]
        ev = notifier.events[0]
        assert ev.title == "三体"
        assert ev.detail["series_id"] == sid
        assert ev.detail["last_part"] == 2 and ev.detail["total_parts"] == 2

        # 已完结后再登记重复 / 新分集都不会重复发事件
        await register_part(test_db, series, _task("三体 第2部 again"), notifier)
        assert notifier.kinds() == ["series_completed"]

    async def test_register_without_notifier_is_fine(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S", total_parts=1))
        series = await repo.get_series(test_db, sid)
        assert await register_part(test_db, series, _task("S 第1部")) is True
        assert (await repo.get_series(test_db, sid)).status == "completed"

    async def test_register_task_resolves_series_by_id(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S"))
        assert await register_task(test_db, _task("S 第4部", series_id=sid)) is True
        assert (await repo.get_series(test_db, sid)).last_part == 4

    async def test_register_task_without_series_id_noop(self, test_db):
        assert await register_task(test_db, _task("S 第4部")) is False

    async def test_register_task_missing_series_noop(self, test_db):
        assert await register_task(test_db, _task("S 第4部", series_id=9999)) is False


class TestRefreshStatus:
    async def test_recomputes_last_part_from_parts(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S", last_part=99))
        await repo.add_series_part(test_db, PartEntry(series_id=sid, part_no=2))
        await repo.add_series_part(test_db, PartEntry(series_id=sid, part_no=5))
        s = await refresh_status(test_db, sid)
        assert s.last_part == 5
        assert s.status == "active"

    async def test_no_parts_resets_to_zero(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S", last_part=3))
        s = await refresh_status(test_db, sid)
        assert s.last_part == 0

    async def test_unknown_total_never_completes(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S", total_parts=None))
        await repo.add_series_part(test_db, PartEntry(series_id=sid, part_no=50))
        notifier = FakeNotifier()
        s = await refresh_status(test_db, sid, notifier)
        assert s.status == "active"
        assert notifier.events == []

    async def test_total_raised_reverts_completed_to_active(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S", total_parts=1))
        await repo.add_series_part(test_db, PartEntry(series_id=sid, part_no=1))
        notifier = FakeNotifier()
        assert (await refresh_status(test_db, sid, notifier)).status == "completed"
        assert notifier.kinds() == ["series_completed"]

        await repo.update_series(test_db, sid, total_parts=3)
        s = await refresh_status(test_db, sid, notifier)
        assert s.status == "active"
        assert notifier.kinds() == ["series_completed"]      # 回退不发事件

    async def test_paused_stays_paused_until_complete(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S", total_parts=3, status="paused"))
        await repo.add_series_part(test_db, PartEntry(series_id=sid, part_no=1))
        assert (await refresh_status(test_db, sid)).status == "paused"

    async def test_missing_series_returns_none(self, test_db):
        assert await refresh_status(test_db, 12345) is None


class TestBuildSeriesContext:
    async def test_empty_when_no_series_entries(self, test_db):
        ctx = await build_series_context(test_db, [WatchEntry(query="x", kind=WatchKind.AUDIOBOOK)])
        assert ctx.series_index == {} and ctx.known_parts == {}

    async def test_collects_patterns_and_parts(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S", part_pattern=r"vol\.(\d+)"))
        await repo.add_series_part(test_db, PartEntry(series_id=sid, part_no=1))
        await repo.add_series_part(test_db, PartEntry(series_id=sid, part_no=3))
        entries = [
            WatchEntry(id="w1", query="s", kind=WatchKind.AUDIOBOOK, series_id=sid),
            WatchEntry(id="w2", query="t", kind=WatchKind.AUDIOBOOK, series_id=4242),  # 不存在
        ]
        ctx = await build_series_context(test_db, entries)
        assert set(ctx.series_index) == {sid}
        assert ctx.series_index[sid].part_pattern == r"vol\.(\d+)"
        assert ctx.known_parts == {sid: {1, 3}}
