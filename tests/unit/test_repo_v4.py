"""v0.4 七表的 repo 层单测：每表至少一次 insert + list + count，以及 migration 幂等。

不测业务逻辑（归档决策 / 校验 / 连载解析由第二波实现），只验证 SQL 与模型映射一致。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from audio_hub import repo
from audio_hub.db import init_db
from audio_hub.models import (
    ArchiveDecision,
    EventRecord,
    LibraryItem,
    PartEntry,
    QuarantineItem,
    RecycleEntry,
    SeedRecord,
    SeriesEntry,
    Task,
    TaskState,
    WatchEntry,
    WatchKind,
)
from audio_hub.timeutil import utcnow


async def _columns(db, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        return {r["name"] for r in await cur.fetchall()}


class TestMigrationV4:
    async def test_init_db_twice_is_idempotent(self, tmp_path: Path):
        db_path = tmp_path / "twice.db"
        conn = await init_db(db_path)
        await conn.close()
        conn = await init_db(db_path)   # 第二次不得报 "table already exists" / "duplicate column"
        try:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                names = {r["name"] for r in await cur.fetchall()}
        finally:
            await conn.close()
        for t in ("library_items", "recycle_entries", "quarantine_items", "series",
                  "series_parts", "seed_records", "events"):
            assert t in names

    async def test_tasks_and_watchlist_gain_v4_columns(self, test_db):
        task_cols = await _columns(test_db, "tasks")
        assert {"series_id", "part_no", "validation_result", "archive_decision"} <= task_cols
        assert "series_id" in await _columns(test_db, "watchlist")

    async def test_task_roundtrip_with_v4_fields(self, test_db):
        task = Task(info_hash="h1", site="hhanclub", title="X",
                    series_id=7, part_no=2, archive_decision=ArchiveDecision.UPGRADE.value)
        tid = await repo.create_task(test_db, task)
        await repo.set_task_state(test_db, tid, TaskState.QUARANTINED,
                                  validation_result='{"ok": false}')
        async with test_db.execute("SELECT * FROM tasks WHERE id = ?", (tid,)) as cur:
            r = await cur.fetchone()
        assert r["series_id"] == 7 and r["part_no"] == 2
        assert r["archive_decision"] == "upgrade"
        assert r["state"] == "quarantined"
        assert r["validation_result"] == '{"ok": false}'

    async def test_watch_entry_series_id_roundtrip(self, test_db):
        await repo.add_watch_entry(test_db, WatchEntry(
            id="w1", query="三体", kind=WatchKind.AUDIOBOOK, series_id=3))
        [entry] = await repo.list_watch_entries(test_db)
        assert entry.series_id == 3


class TestLibraryItems:
    async def test_insert_list_count(self, test_db):
        iid = await repo.add_library_item(test_db, LibraryItem(
            watch_id="w1", task_id="t1", title_norm="album a", kind=WatchKind.MUSIC,
            quality="FLAC 24/96", quality_score=90, path="/musiclab/library/a"))
        assert isinstance(iid, int) and iid > 0
        await repo.add_library_item(test_db, LibraryItem(
            watch_id="w2", title_norm="book b", kind=WatchKind.AUDIOBOOK,
            path="/audiolab/library/b"))

        assert await repo.count_library_items(test_db) == 2
        assert await repo.count_library_items(test_db, kind=WatchKind.AUDIOBOOK) == 1

        rows = await repo.list_library_items(test_db, watch_id="w1")
        assert [r.title_norm for r in rows] == ["album a"]
        assert rows[0].quality_score == 90

        got = await repo.get_library_item(test_db, iid)
        assert got is not None and got.path == "/musiclab/library/a"

    async def test_find_and_update(self, test_db):
        await repo.add_library_item(test_db, LibraryItem(
            watch_id="w1", title_norm="dup", quality_score=50, path="/p/low"))
        await repo.add_library_item(test_db, LibraryItem(
            watch_id="w1", title_norm="dup", quality_score=80, path="/p/high"))
        best = await repo.find_library_item(test_db, "w1", "dup")
        assert best is not None and best.path == "/p/high"
        assert await repo.find_library_item(test_db, "w1", "missing") is None

        await repo.update_library_item(test_db, best.id, quality_score=99, bogus=1)
        assert (await repo.get_library_item(test_db, best.id)).quality_score == 99

        assert await repo.delete_library_item(test_db, best.id) is True
        assert await repo.count_library_items(test_db) == 1

    async def test_list_pagination(self, test_db):
        for i in range(5):
            await repo.add_library_item(test_db, LibraryItem(title_norm=f"t{i}", path=f"/p{i}"))
        page = await repo.list_library_items(test_db, limit=2, offset=2)
        assert len(page) == 2


class TestRecycleEntries:
    async def test_insert_list_count_restore(self, test_db):
        rid = await repo.add_recycle_entry(test_db, RecycleEntry(
            library_item_id=1, original_path="/lib/a", recycle_path="/recycle/a",
            reason="upgrade"))
        assert rid > 0
        assert await repo.count_recycle_entries(test_db) == 1
        assert await repo.count_recycle_entries(test_db, restored=False) == 1

        [e] = await repo.list_recycle_entries(test_db, restored=False)
        assert e.reason == "upgrade" and e.restored_at is None

        await repo.mark_recycle_restored(test_db, rid)
        assert await repo.count_recycle_entries(test_db, restored=False) == 0
        got = await repo.get_recycle_entry(test_db, rid)
        assert got is not None and got.restored_at is not None

    async def test_expired_query(self, test_db):
        old = utcnow() - timedelta(days=40)
        await repo.add_recycle_entry(test_db, RecycleEntry(
            original_path="/a", recycle_path="/r/a", created_at=old))
        await repo.add_recycle_entry(test_db, RecycleEntry(
            original_path="/b", recycle_path="/r/b"))
        expired = await repo.list_expired_recycle_entries(
            test_db, before=utcnow() - timedelta(days=30))
        assert [e.original_path for e in expired] == ["/a"]


class TestQuarantineItems:
    async def test_insert_list_count_status(self, test_db):
        qid = await repo.add_quarantine_item(test_db, QuarantineItem(
            task_id="t1", scratch_path="/scratch/x", reasons=["no_audio", "ext:exe"]))
        await repo.add_quarantine_item(test_db, QuarantineItem(
            task_id="t2", scratch_path="/scratch/y", status="released"))

        assert await repo.count_quarantine_items(test_db) == 1          # 默认只数 held
        assert await repo.count_quarantine_items(test_db, status=None) == 2

        [held] = await repo.list_quarantine_items(test_db, status="held")
        assert held.reasons == ["no_audio", "ext:exe"]

        await repo.set_quarantine_status(test_db, qid, "discarded")
        got = await repo.get_quarantine_item(test_db, qid)
        assert got.status == "discarded" and got.resolved_at is not None
        assert await repo.count_quarantine_items(test_db) == 0


class TestSeries:
    async def test_insert_list_count_update(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(
            watch_id="w1", name="三体", total_parts=3, part_pattern=r"第(\d+)部"))
        assert sid > 0
        assert await repo.count_series(test_db) == 1
        assert await repo.count_series(test_db, status="completed") == 0

        [s] = await repo.list_series(test_db)
        assert s.name == "三体" and s.last_part == 0

        by_watch = await repo.get_series_by_watch(test_db, "w1")
        assert by_watch is not None and by_watch.id == sid

        await repo.update_series(test_db, sid, status="completed", last_part=3)
        got = await repo.get_series(test_db, sid)
        assert got.status == "completed" and got.last_part == 3

    async def test_parts_unique_and_bump_last_part(self, test_db):
        sid = await repo.add_series(test_db, SeriesEntry(name="S"))
        assert await repo.add_series_part(test_db, PartEntry(
            series_id=sid, part_no=2, task_id="t2", title="第二部")) is True
        assert await repo.add_series_part(test_db, PartEntry(
            series_id=sid, part_no=2, task_id="t2-dup")) is False     # 幂等
        assert await repo.add_series_part(test_db, PartEntry(
            series_id=sid, part_no=1, task_id="t1")) is True

        parts = await repo.list_series_parts(test_db, sid)
        assert [p.part_no for p in parts] == [1, 2]
        assert await repo.count_series_parts(test_db, sid) == 2
        assert await repo.count_parts_by_series(test_db) == {sid: 2}
        assert (await repo.get_series(test_db, sid)).last_part == 2


class TestSeedRecords:
    async def test_upsert_list_count_update(self, test_db):
        rec = SeedRecord(task_id="t1", info_hash="h1", site="hhanclub",
                         required_seconds=72 * 3600, required_ratio=1.0)
        await repo.upsert_seed_record(test_db, rec)
        await repo.upsert_seed_record(test_db, SeedRecord(
            task_id="t2", info_hash="h2", site="mteam", status="satisfied"))
        # 同 task_id 再次 upsert 应覆写而非报错
        rec.seed_seconds = 3600
        await repo.upsert_seed_record(test_db, rec)

        assert await repo.count_seed_records(test_db) == 2
        assert await repo.count_seed_records(test_db, status="seeding") == 1

        [seeding] = await repo.list_seed_records(test_db, status="seeding")
        assert seeding.task_id == "t1" and seeding.seed_seconds == 3600
        assert (await repo.list_seed_records(test_db, site="mteam"))[0].task_id == "t2"

        when = utcnow()
        await repo.update_seed_record(test_db, "t1", ratio=1.5, status="satisfied",
                                      hr_satisfied_at=when)
        got = await repo.get_seed_record(test_db, "t1")
        assert got.ratio == pytest.approx(1.5)
        assert got.status == "satisfied"
        assert got.hr_satisfied_at is not None


class TestEvents:
    async def test_insert_list_count_purge(self, test_db):
        eid = await repo.add_event(test_db, EventRecord(
            kind="dispatched", level="info", site="hhanclub", title="X", detail='{"a":1}'))
        assert eid > 0
        await repo.add_event(test_db, EventRecord(kind="failed", level="error", title="Y"))
        await repo.add_event(test_db, EventRecord(
            kind="failed", level="error", created_at=utcnow() - timedelta(days=90)))

        assert await repo.count_events(test_db) == 3
        assert await repo.count_events(test_db, kind="failed") == 2
        assert await repo.count_events(test_db, level="info") == 1
        assert await repo.count_events_by_kind(test_db) == {"dispatched": 1, "failed": 2}

        rows = await repo.list_events(test_db, kind="dispatched")
        assert len(rows) == 1 and rows[0].detail == '{"a":1}'
        got = await repo.get_event(test_db, eid)
        assert got is not None and got.site == "hhanclub"

        n = await repo.purge_events_before(test_db, utcnow() - timedelta(days=30))
        assert n == 1
        assert await repo.count_events(test_db) == 2
