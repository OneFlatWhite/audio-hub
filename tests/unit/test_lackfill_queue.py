"""Re0 回填队列：schema v9、扫描结果持久化与重复转存防护。"""

from __future__ import annotations

import importlib
import json

import aiosqlite
import pytest

from audio_hub import db as dbmod
from audio_hub.db import SCHEMA_VERSION, init_db
from audio_hub.lackfill import ledger
from audio_hub.lackfill.models import ShowLack

pytestmark = pytest.mark.asyncio

NOW = "2026-10-25T01:00:00"
LATER = "2026-10-25T02:00:00"


def _queue():
    return importlib.import_module("audio_hub.lackfill.queue")


def _lack(tmdb_id: int = 100, *, title: str = "测试剧", missing=None) -> ShowLack:
    return ShowLack(
        tmdb_id=tmdb_id,
        title=title,
        year=2024,
        emby_id=f"emby-{tmdb_id}",
        missing=missing if missing is not None else {1: [3, 5], 2: [3]},
    )


async def _tables(conn) -> set[str]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in await cur.fetchall()}


async def _make_v6_db(path):
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    for mig in (
        dbmod._MIGRATION_V1,
        dbmod._MIGRATION_V2,
        dbmod._MIGRATION_V3,
        dbmod._MIGRATION_V4,
        dbmod._MIGRATION_V5,
        dbmod._MIGRATION_V6,
    ):
        await conn.executescript(mig)
    await dbmod._migrate_task_columns(conn)
    await dbmod._migrate_watchlist_columns(conn)
    await conn.execute("PRAGMA user_version = 6")
    await conn.commit()
    return conn


class TestQueueSchema:
    async def test_schema_version_is_11(self, test_db):
        assert SCHEMA_VERSION == 11
        cur = await test_db.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == 11

    async def test_queue_table_exists(self, test_db):
        assert "lackfill_queue" in await _tables(test_db)

    async def test_v6_database_gains_queue_without_losing_ledger(self, tmp_path):
        path = tmp_path / "v6.db"
        old = await _make_v6_db(path)
        await ledger.insert(
            old,
            show_key="tmdb:100",
            season=None,
            missing=[3],
            share_code="abc123",
            package_name="pkg",
            now=NOW,
            emby_id="emby-100",
        )
        assert "lackfill_queue" not in await _tables(old)
        await old.close()

        conn = await init_db(path)
        try:
            assert "lackfill_queue" in await _tables(conn)
            assert (await ledger.get(conn, 1))["share_code"] == "abc123"
        finally:
            await conn.close()


class TestQueueCrud:
    async def test_upsert_preserves_first_seen_and_replaces_missing(self, test_db):
        queue = _queue()
        await queue.upsert(test_db, _lack(), NOW)
        await queue.upsert(
            test_db,
            _lack(title="新剧名", missing={2: [8]}),
            LATER,
        )

        row = await queue.get(test_db, 100)
        assert row["title"] == "新剧名"
        assert row["seasons"] == {"2": [8]}
        assert row["missing"] == 1
        assert row["created_at"] == NOW
        assert row["updated_at"] == LATER

    async def test_remove_deletes_only_target_show(self, test_db):
        queue = _queue()
        await queue.upsert(test_db, _lack(100), NOW)
        await queue.upsert(test_db, _lack(200), NOW)
        await queue.remove(test_db, 100)
        assert await queue.get(test_db, 100) is None
        assert (await queue.get(test_db, 200))["tmdb_id"] == 200

    async def test_update_missing_keeps_queue_row_current(self, test_db):
        queue = _queue()
        await queue.upsert(test_db, _lack(), NOW)
        await queue.update_missing(test_db, 100, {1: [5], 2: [3]}, LATER)
        row = await queue.get(test_db, 100)
        assert row["seasons"] == {"1": [5], "2": [3]}
        assert row["missing"] == 2
        assert row["updated_at"] == LATER

    async def test_pending_excludes_inflight_and_orders_oldest_first(self, test_db):
        queue = _queue()
        await queue.upsert(test_db, _lack(100), LATER)
        await queue.upsert(test_db, _lack(200), NOW)
        entry_id = await ledger.insert(
            test_db,
            show_key="tmdb:100",
            season=None,
            missing={1: [3, 5], 2: [3]},
            share_code="abc123",
            package_name="pkg",
            now=NOW,
            emby_id="emby-100",
        )

        assert [r["tmdb_id"] for r in await queue.list_pending(test_db)] == [200]

        await ledger.update_status(test_db, entry_id, "timeout", LATER)
        assert [r["tmdb_id"] for r in await queue.list_pending(test_db)] == [200, 100]

    async def test_missing_json_is_an_object_not_a_flat_episode_list(self, test_db):
        queue = _queue()
        await queue.upsert(test_db, _lack(), NOW)
        cur = await test_db.execute(
            "SELECT missing_json FROM lackfill_queue WHERE tmdb_id = 100"
        )
        raw = json.loads((await cur.fetchone())[0])
        assert raw == {"1": [3, 5], "2": [3]}
