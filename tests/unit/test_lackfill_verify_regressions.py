"""审查回归：旧台账、超时天数、换包轮次与部分进展。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from audio_hub.lackfill import ledger, queue
from audio_hub.lackfill.models import ShowLack
from audio_hub.lackfill.service import LackfillService

pytestmark = pytest.mark.asyncio

NOW = "2026-10-25T00:00:00"
OLD = "2026-10-14T00:00:00"


def _emby(have):
    async def episode_map(_id):
        return have
    async def aclose():
        return None
    return SimpleNamespace(episode_map=episode_map, aclose=aclose)


async def _entry(conn, *, missing=None, season=None, now=OLD, verify_round=0):
    return await ledger.insert(
        conn,
        show_key="tmdb:100",
        season=season,
        missing={1: [3]} if missing is None else missing,
        share_code="abc123",
        package_name="pkg",
        now=now,
        emby_id="emby-100",
        verify_round=verify_round,
    )


def _cfg(**over):
    values = {"wecom_webhook": None, "max_verify_rounds": 3}
    values.update(over)
    return SimpleNamespace(**values)


async def test_unseasoned_legacy_list_requires_manual_review(test_db):
    entry_id = await _entry(test_db, missing=[3])
    await LackfillService(_cfg()).verify_pending(
        test_db, now=NOW, emby=_emby({1: {3}, 2: set()})
    )
    assert (await ledger.get(test_db, entry_id))["status"] == "manual"


async def test_verify_timeout_days_is_used_instead_of_fixed_seven_days(test_db):
    entry_id = await _entry(test_db)
    stats = await LackfillService(_cfg(verify_timeout_days=30)).verify_pending(
        test_db, now=NOW, emby=_emby({1: set()})
    )
    assert stats["bumped"] == 0
    assert (await ledger.get(test_db, entry_id))["verify_round"] == 0


async def test_first_timeout_closes_attempt_and_reopens_queue(test_db):
    entry_id = await _entry(test_db)
    await queue.upsert(test_db, ShowLack(100, "测试剧", 2024, "emby-100", {1: [3]}), OLD)
    stats = await LackfillService(_cfg()).verify_pending(
        test_db, now=NOW, emby=_emby({1: set()})
    )
    assert stats["bumped"] == 1
    row = await ledger.get(test_db, entry_id)
    assert row["status"] == "timeout"
    assert row["verify_round"] == 1
    assert [item["tmdb_id"] for item in await queue.list_pending(test_db)] == [100]


async def test_third_failed_attempt_is_final_timeout(test_db):
    entry_id = await _entry(test_db, verify_round=2)
    stats = await LackfillService(_cfg()).verify_pending(
        test_db, now=NOW, emby=_emby({1: set()})
    )
    assert stats["timeout"] == 1
    assert (await ledger.get(test_db, entry_id))["verify_round"] == 3


async def test_partial_progress_resets_no_progress_round(test_db):
    entry_id = await _entry(test_db, missing={1: [3, 5]}, now=NOW, verify_round=2)
    await LackfillService(_cfg()).verify_pending(
        test_db, now=NOW, emby=_emby({1: {3}})
    )
    assert (await ledger.get(test_db, entry_id))["verify_round"] == 0


async def test_new_attempt_inherits_previous_round(test_db):
    entry_id = await _entry(test_db, verify_round=1)
    await ledger.update_status(test_db, entry_id, "timeout", NOW)
    next_round = await ledger.latest_round(test_db, "tmdb:100")
    new_id = await ledger.insert(
        test_db,
        show_key="tmdb:100",
        season=None,
        missing={1: [3]},
        share_code="def456",
        package_name="pkg2",
        now=NOW,
        verify_round=next_round,
    )
    assert next_round == 1
    assert (await ledger.get(test_db, new_id))["verify_round"] == 1
