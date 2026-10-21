"""lackfill 缺集补齐台账（lackfill_ledger，schema v6）的迁移与 CRUD 测试。

结构对照 test_migration_v5.py：全新建库幂等、v5 老库升级、CRUD 行为。
"""
from __future__ import annotations

import json

import aiosqlite
import pytest

from audio_hub import db as dbmod
from audio_hub.db import SCHEMA_VERSION, init_db
from audio_hub.lackfill import ledger

pytestmark = pytest.mark.asyncio


async def _tables(conn) -> set[str]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    rows = await cur.fetchall()
    return {r[0] for r in rows}


async def _indexes(conn) -> set[str]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    rows = await cur.fetchall()
    return {r[0] for r in rows}


async def _columns(conn, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    return {r[1] for r in rows}


async def _user_version(conn) -> int:
    cur = await conn.execute("PRAGMA user_version")
    row = await cur.fetchone()
    return row[0]


async def _make_v5_db(path):
    """手工搭一个 v5 时代的库：跑 V1..V5 + 列迁移，user_version=5。"""
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    for mig in (
        dbmod._MIGRATION_V1,
        dbmod._MIGRATION_V2,
        dbmod._MIGRATION_V3,
        dbmod._MIGRATION_V4,
        dbmod._MIGRATION_V5,
    ):
        await conn.executescript(mig)
    await dbmod._migrate_task_columns(conn)
    await dbmod._migrate_watchlist_columns(conn)
    await conn.execute("PRAGMA user_version = 5")
    await conn.commit()
    return conn


_LEDGER_COLUMNS = {
    "id",
    "show_key",
    "season",
    "missing_json",
    "share_code",
    "package_name",
    "emby_id",
    "status",
    "verify_round",
    "created_at",
    "updated_at",
}


class TestFreshInit:
    async def test_schema_version_is_6(self, test_db):
        assert SCHEMA_VERSION == 6
        assert await _user_version(test_db) == 6

    async def test_ledger_table_exists(self, test_db):
        assert "lackfill_ledger" in await _tables(test_db)

    async def test_ledger_columns(self, test_db):
        assert _LEDGER_COLUMNS <= await _columns(test_db, "lackfill_ledger")

    async def test_ledger_status_index(self, test_db):
        assert "idx_lackfill_ledger_status" in await _indexes(test_db)


class TestIdempotent:
    async def test_init_twice_keeps_table(self, tmp_path):
        db_path = tmp_path / "twice.db"
        conn = await init_db(db_path)
        await conn.close()
        conn = await init_db(db_path)
        try:
            assert "lackfill_ledger" in await _tables(conn)
            assert await _user_version(conn) == 6
        finally:
            await conn.close()


class TestUpgradeFromV5:
    async def test_v5_db_gains_ledger_table(self, tmp_path):
        db_path = tmp_path / "old.db"
        conn = await _make_v5_db(db_path)
        assert "lackfill_ledger" not in await _tables(conn)
        await conn.close()

        conn = await init_db(db_path)
        try:
            assert "lackfill_ledger" in await _tables(conn)
            assert await _user_version(conn) == 6
        finally:
            await conn.close()


class TestLedgerCrud:
    async def _insert(self, conn, **overrides) -> int:
        kwargs = {
            "show_key": "show-1",
            "season": "S01",
            "missing": [3, 5],
            "share_code": "abc123",
            "package_name": "pkg",
            "now": "2026-10-21T00:00:00",
        }
        kwargs.update(overrides)
        return await ledger.insert(conn, **kwargs)

    async def test_insert_and_get_roundtrip(self, test_db):
        entry_id = await self._insert(test_db)
        row = await ledger.get(test_db, entry_id)
        assert row is not None
        assert row["show_key"] == "show-1"
        assert row["season"] == "S01"
        assert json.loads(row["missing_json"]) == [3, 5]
        assert row["share_code"] == "abc123"
        assert row["package_name"] == "pkg"
        assert row["status"] == "pending"
        assert row["verify_round"] == 0
        assert row["created_at"] == "2026-10-21T00:00:00"
        assert row["updated_at"] == "2026-10-21T00:00:00"

    async def test_get_missing_returns_none(self, test_db):
        assert await ledger.get(test_db, 999) is None

    async def test_update_status_refreshes_updated_at(self, test_db):
        entry_id = await self._insert(test_db)
        await ledger.update_status(test_db, entry_id, "complete", "2026-10-21T01:00:00")
        row = await ledger.get(test_db, entry_id)
        assert row["status"] == "complete"
        assert row["updated_at"] == "2026-10-21T01:00:00"
        assert row["created_at"] == "2026-10-21T00:00:00"

    async def test_update_missing_rewrites_json(self, test_db):
        entry_id = await self._insert(test_db)
        await ledger.update_missing(test_db, entry_id, [5], "2026-10-21T02:00:00")
        row = await ledger.get(test_db, entry_id)
        assert json.loads(row["missing_json"]) == [5]
        assert row["updated_at"] == "2026-10-21T02:00:00"

    async def test_bump_round_increments(self, test_db):
        entry_id = await self._insert(test_db)
        await ledger.bump_round(test_db, entry_id, "2026-10-21T03:00:00")
        row = await ledger.get(test_db, entry_id)
        assert row["verify_round"] == 1
        assert row["updated_at"] == "2026-10-21T03:00:00"

    async def test_list_by_status_filters(self, test_db):
        first = await self._insert(test_db, show_key="show-a")
        second = await self._insert(test_db, show_key="show-b")
        await ledger.update_status(test_db, first, "complete", "2026-10-21T01:00:00")

        pending = await ledger.list_by_status(test_db, "pending")
        assert [r["id"] for r in pending] == [second]

        both = await ledger.list_by_status(test_db, "pending", "complete")
        assert {r["id"] for r in both} == {first, second}
