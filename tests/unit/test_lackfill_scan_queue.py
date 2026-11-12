"""扫描结果进入 Re0 持久化队列，以及多季缺集核销。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from audio_hub.lackfill import ledger, queue
from audio_hub.lackfill.models import ShowLack
from audio_hub.lackfill.service import LackfillService

pytestmark = pytest.mark.asyncio

NOW = "2026-10-25T01:00:00"
LATER = "2026-10-25T02:00:00"


class FakeScanEmby:
    def __init__(self, shows, have):
        self._shows = shows
        self._have = have

    async def list_series(self):
        return self._shows

    async def episode_map(self, emby_id):
        return self._have.get(emby_id, {})

    async def aclose(self):
        return None


class FakeScanTmdb:
    def __init__(self, aired, fail_tmdb_ids=()):
        self._aired = aired
        self._fail_tmdb_ids = set(fail_tmdb_ids)

    async def tv(self, tmdb_id):
        if tmdb_id in self._fail_tmdb_ids:
            raise RuntimeError("TMDB unavailable")
        return {
            "seasons": [
                {"season_number": season}
                for season in sorted(self._aired.get(tmdb_id, {}))
            ]
        }

    async def season_aired_episode_numbers(self, tmdb_id, season, today):
        return self._aired[tmdb_id][season]

    async def aclose(self):
        return None


def _scan_cfg():
    return SimpleNamespace(
        emby_url="http://emby.test",
        emby_api_key="emby-key",
        tmdb_api_key="tmdb-key",
        tmdb_language="zh-CN",
        max_round_shows=2,
    )


def _show(tmdb_id: int) -> dict:
    return {
        "tmdb_id": tmdb_id,
        "emby_id": f"emby-{tmdb_id}",
        "name": f"剧{tmdb_id}",
        "year": 2024,
    }


def _queue_lack(tmdb_id: int, missing) -> ShowLack:
    return ShowLack(
        tmdb_id=tmdb_id,
        title=f"剧{tmdb_id}",
        year=2024,
        emby_id=f"emby-{tmdb_id}",
        missing=missing,
    )


class TestScanQueue:
    async def test_scan_upserts_and_removes_only_successfully_scanned_shows(
        self, test_db, monkeypatch
    ):
        for tmdb_id in (100, 200, 300):
            await queue.upsert(test_db, _queue_lack(tmdb_id, {1: [9]}), NOW)
        emby = FakeScanEmby(
            [_show(100), _show(200), _show(300)],
            {"emby-100": {1: {1}}, "emby-200": {1: {1, 2}}},
        )
        tmdb = FakeScanTmdb({100: {1: [1, 2, 3]}, 200: {1: [1, 2]}})
        monkeypatch.setattr("audio_hub.lackfill.service.EmbyClient", lambda *args: emby)
        monkeypatch.setattr("audio_hub.lackfill.service.TmdbClient", lambda *args: tmdb)

        report = await LackfillService(_scan_cfg()).scan(conn=test_db, now=LATER)

        assert report.lack_show_count == 1
        assert (await queue.get(test_db, 100))["seasons"] == {"1": [2, 3]}
        assert (await queue.get(test_db, 100))["updated_at"] == LATER
        assert await queue.get(test_db, 200) is None
        assert (await queue.get(test_db, 300))["seasons"] == {"1": [9]}

    async def test_invalid_tmdb_id_does_not_stop_following_shows(self, test_db, monkeypatch):
        bad = _show(100) | {"tmdb_id": "not-a-number"}
        emby = FakeScanEmby([bad, _show(200)], {"emby-200": {1: set()}})
        tmdb = FakeScanTmdb({200: {1: [1, 2]}})
        monkeypatch.setattr("audio_hub.lackfill.service.EmbyClient", lambda *args: emby)
        monkeypatch.setattr("audio_hub.lackfill.service.TmdbClient", lambda *args: tmdb)

        report = await LackfillService(_scan_cfg()).scan(conn=test_db, now=LATER)

        assert report.shows_scanned == 2
        assert len(report.errors) == 1
        assert "not-a-number" in report.errors[0]
        assert (await queue.get(test_db, 200))["seasons"] == {"1": [1, 2]}

    async def test_failed_show_keeps_previous_queue_entry(self, test_db, monkeypatch):
        await queue.upsert(test_db, _queue_lack(100, {1: [9]}), NOW)
        emby = FakeScanEmby([_show(100)], {})
        tmdb = FakeScanTmdb({}, fail_tmdb_ids={100})
        monkeypatch.setattr("audio_hub.lackfill.service.EmbyClient", lambda *args: emby)
        monkeypatch.setattr("audio_hub.lackfill.service.TmdbClient", lambda *args: tmdb)

        report = await LackfillService(_scan_cfg()).scan(conn=test_db, now=LATER)

        assert len(report.errors) == 1
        assert (await queue.get(test_db, 100))["seasons"] == {"1": [9]}
        assert (await queue.get(test_db, 100))["updated_at"] == NOW


class TestMultiSeasonVerify:
    async def _seed(self, conn):
        await queue.upsert(conn, _queue_lack(100, {1: [3], 2: [3]}), NOW)
        return await ledger.insert(
            conn,
            show_key="tmdb:100",
            season=None,
            missing={1: [3], 2: [3]},
            share_code="abc123",
            package_name="pkg",
            now=NOW,
            emby_id="emby-100",
        )

    async def test_same_episode_number_in_two_seasons_is_partial_not_complete(self, test_db):
        entry_id = await self._seed(test_db)
        emby = SimpleNamespace(
            episode_map=self._episode_map({1: {3}, 2: set()}),
            aclose=self._aclose,
        )

        stats = await LackfillService(SimpleNamespace(wecom_webhook=None)).verify_pending(
            test_db, now=LATER, emby=emby
        )

        assert stats["partial"] == 1
        row = await ledger.get(test_db, entry_id)
        assert row["status"] == "partial"
        assert json.loads(row["missing_json"]) == {"2": [3]}
        assert (await queue.get(test_db, 100))["seasons"] == {"2": [3]}

    async def test_multi_season_complete_closes_ledger_and_queue(self, test_db):
        entry_id = await self._seed(test_db)
        emby = SimpleNamespace(
            episode_map=self._episode_map({1: {3}, 2: {3}}),
            aclose=self._aclose,
        )

        stats = await LackfillService(SimpleNamespace(wecom_webhook=None)).verify_pending(
            test_db, now=LATER, emby=emby
        )

        assert stats["complete"] == 1
        assert (await ledger.get(test_db, entry_id))["status"] == "complete"
        assert await queue.get(test_db, 100) is None

    async def test_old_ledger_completion_preserves_newly_aired_missing_episodes(self, test_db):
        entry_id = await self._seed(test_db)
        await queue.upsert(test_db, _queue_lack(100, {1: [3], 2: [3, 4]}), LATER)
        emby = SimpleNamespace(
            episode_map=self._episode_map({1: {3}, 2: {3}}),
            aclose=self._aclose,
        )
        await LackfillService(SimpleNamespace(wecom_webhook=None)).verify_pending(
            test_db, now=LATER, emby=emby
        )
        assert (await ledger.get(test_db, entry_id))["status"] == "complete"
        assert (await queue.get(test_db, 100))["seasons"] == {"2": [4]}

    async def test_partial_old_ledger_does_not_overwrite_new_queue_episodes(self, test_db):
        await self._seed(test_db)
        await queue.upsert(test_db, _queue_lack(100, {1: [3], 2: [3, 4]}), LATER)
        emby = SimpleNamespace(
            episode_map=self._episode_map({1: {3}, 2: set()}),
            aclose=self._aclose,
        )
        await LackfillService(SimpleNamespace(wecom_webhook=None)).verify_pending(
            test_db, now=LATER, emby=emby
        )
        assert (await queue.get(test_db, 100))["seasons"] == {"2": [3, 4]}

    @staticmethod
    def _episode_map(have):
        async def _get(_emby_id):
            return have
        return _get

    @staticmethod
    async def _aclose():
        return None
