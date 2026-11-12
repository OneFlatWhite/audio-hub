"""转存预留：同剧跨请求/跨 worker 只能有一个活动意图。"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from audio_hub.db import SCHEMA_VERSION, init_db
from audio_hub.lackfill import ledger, queue
from audio_hub.lackfill.models import ShowLack

pytestmark = pytest.mark.asyncio

NOW = "2026-10-25T01:00:00"


def _reservations():
    return importlib.import_module("audio_hub.lackfill.reservations")


async def test_schema_v10_has_reservation_table(test_db):
    assert SCHEMA_VERSION == 11
    cur = await test_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='lackfill_reservations'"
    )
    assert await cur.fetchone() is not None


async def test_reserve_is_exclusive_and_release_allows_next(test_db):
    reservations = _reservations()
    assert await reservations.reserve(test_db, "tmdb:100", "aaa111", NOW) == "reserved"
    assert await reservations.reserve(test_db, "tmdb:100", "bbb222", NOW) == "reservation"
    await reservations.release(test_db, "tmdb:100")
    assert await reservations.reserve(test_db, "tmdb:100", "bbb222", NOW) == "reserved"


async def test_reserve_rejects_existing_active_ledger(test_db):
    reservations = _reservations()
    await ledger.insert(
        test_db,
        show_key="tmdb:100",
        season=None,
        missing={1: [3]},
        share_code="old123",
        package_name="pkg",
        now=NOW,
    )
    assert await reservations.reserve(test_db, "tmdb:100", "new456", NOW) == "ledger"


async def test_concurrent_reservations_have_one_winner(tmp_path):
    reservations = _reservations()
    path = tmp_path / "shared.db"
    first = await init_db(path)
    second = await init_db(path)
    try:
        results = await asyncio.gather(
            reservations.reserve(first, "tmdb:100", "same123", NOW),
            reservations.reserve(second, "tmdb:100", "same123", NOW),
        )
        assert len(results) == 2
        assert set(results) == {"reserved", "reservation"}
    finally:
        await first.close()
        await second.close()


async def test_recover_only_unstarted_reservations(test_db):
    reservations = _reservations()
    await reservations.reserve(test_db, "tmdb:100", "aaa111", NOW)
    await reservations.reserve(test_db, "tmdb:200", "bbb222", NOW)
    await reservations.mark_attempted(test_db, "tmdb:200", NOW)
    assert await reservations.recover_unstarted(test_db) == 1
    assert await reservations.get(test_db, "tmdb:100") is None
    assert (await reservations.get(test_db, "tmdb:200"))["stage"] == "attempted"


async def test_pending_queue_hides_active_reservation(test_db):
    reservations = _reservations()
    await queue.upsert(
        test_db,
        ShowLack(100, "测试剧", 2024, "emby-100", {1: [3]}),
        NOW,
    )
    await reservations.reserve(test_db, "tmdb:100", "aaa111", NOW)
    assert await queue.list_pending(test_db) == []
