"""AY 主通道编排：复用转存、CD2 与核销链，失败保留 Re0。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from audio_hub import settings_store
from audio_hub.lackfill import ay, ledger, queue
from audio_hub.lackfill.models import ShowLack
from audio_hub.lackfill.service import LackfillService

pytestmark = pytest.mark.asyncio

NOW = "2026-10-26T01:02:03"
SHARE_URL = "https://115.com/s/abc123?password=wd40"
ED2K = "ed2k://|file|测试剧.S01E03.mkv|123|6A8A258330DB0DA10B020623AE256150|/"


class FakeAY:
    def __init__(self, resources):
        self.resources = resources
        self.calls = []

    async def query(self, tmdb_id, tg_id):
        self.calls.append((tmdb_id, tg_id))
        return {
            "resources": self.resources,
            "quota_left": 97,
            "http_status": 200,
            "message": "ok",
        }


class FakeTMDB:
    def __init__(self):
        self.season_calls = []

    async def season_aired_episode_numbers(self, tmdb_id, season, today):
        self.season_calls.append((tmdb_id, season, today))
        return [1, 2, 3]


class FakeP115:
    def __init__(self):
        self.receive_calls = []

    async def inspect_share(self, share_code, receive_code=""):
        files = [
            {"name": "S01E03.mkv", "path": "测试剧/S01/E03.mkv", "file_id": "f1", "is_dir": False},
            {"name": "S02E03.mkv", "path": "测试剧/S02/E03.mkv", "file_id": "f2", "is_dir": False},
        ]
        return SimpleNamespace(package_name="测试剧", files=files)

    async def receive(self, *args):
        self.receive_calls.append(args)
        return True


class FakeSA:
    def __init__(self):
        self.offline_calls = []

    async def offline(self, links, parent_id=""):
        self.offline_calls.append((links, parent_id))
        return {"ok": True}


class FakeEmby:
    def __init__(self):
        self.refresh_calls = 0

    async def refresh_library(self):
        self.refresh_calls += 1


class NoTransfer:
    async def __call__(self, *args, **kwargs):
        raise AssertionError("预览、无资源或预算不足时不得触发外部转存")


def _cfg(**over):
    values = {
        "enabled": True,
        "ay_api_enabled": True,
        "ay_api_url": "https://ay.example.com/api/user",
        "ay_api_token": "ay-secret",
        "ay_tg_id": "12345",
        "ay_max_links": 3,
        "ay_round_clicks": 100,
        "tmdb_api_key": "tmdb-key",
        "tmdb_language": "zh-CN",
        "p115_cookie": "115-cookie",
        "transfer_cid": 0,
        "sa_parent_id": "sa-folder",
        "cloud_verify_enabled": False,
        "wecom_webhook": "",
    }
    values.update(over)
    return SimpleNamespace(**values)


async def _queue(test_db, missing=None):
    await queue.upsert(
        test_db,
        ShowLack(
            tmdb_id=100,
            title="测试剧",
            year=2024,
            emby_id="emby-100",
            missing=missing or {1: [3], 2: [3]},
        ),
        "2026-10-25T00:00:00",
    )
    return await queue.get(test_db, 100)


async def _fill(test_db, resources, *, budget=100, execute=True, p115=None, sa=None, emby=None):
    return await LackfillService(_cfg()).fill_ay(
        test_db,
        await _queue(test_db),
        budget,
        ay=FakeAY(resources),
        tmdb=FakeTMDB(),
        now=NOW,
        execute=execute,
        p115=p115 or FakeP115(),
        sa=sa or FakeSA(),
        emby=emby or FakeEmby(),
    )


async def test_ay_share_success_registers_existing_pipeline_and_internal_quota(test_db):
    result = await _fill(test_db, [{"name": "测试剧 S01-S02", "link": SHARE_URL, "size": 2}])

    assert result["status"] == "ok"
    assert result["clicks"] == 2
    rows = await ledger.list_by_status(test_db, "pending")
    assert len(rows) == 1
    assert json.loads(rows[0]["missing_json"]) == {"1": [3], "2": [3]}
    assert rows[0]["package_name"] == "测试剧"
    assert await queue.get(test_db, 100) is not None
    quota = await ay.quota_status(test_db)
    assert quota == {"quota_left": 97, "updated_at": NOW}
    assert "__lackfill_ay_quota" not in await settings_store.load_overrides(test_db)


async def test_ay_ed2k_success_uses_offline_api(test_db):
    sa = FakeSA()
    result = await _fill(
        test_db,
        [{"name": "测试剧 S01E03", "link": ED2K, "size": 1}],
        sa=sa,
    )

    assert result["status"] == "ok"
    assert sa.offline_calls == [([ED2K], "sa-folder")]
    assert len(await ledger.list_by_status(test_db, "pending")) == 1


async def test_ay_empty_resources_keeps_re0_queue(test_db):
    result = await _fill(test_db, [])

    assert result["status"] == "none"
    assert result["clicks"] == 1
    assert "Re0" in result["reason"]
    assert await queue.get(test_db, 100) is not None
    assert await ledger.list_by_status(test_db, "pending") == []
    assert (await ay.quota_status(test_db))["quota_left"] == 97


async def test_ay_budget_short_circuit_does_not_query(test_db):
    queued = await _queue(test_db)
    api = FakeAY([{"name": "测试剧 S01", "link": SHARE_URL}])
    result = await LackfillService(_cfg()).fill_ay(
        test_db, queued, 1, ay=api, tmdb=FakeTMDB(), now=NOW,
        p115=NoTransfer(), sa=NoTransfer(), emby=FakeEmby(),
    )

    assert result["status"] == "budget"
    assert api.calls == []
    assert await queue.get(test_db, 100) is not None


async def test_ay_budget_covers_query_and_each_selected_submission(test_db):
    resources = [
        {"name": "测试剧 S01", "link": SHARE_URL, "size": 1},
        {"name": "测试剧 S02", "link": "https://115.com/s/def456", "size": 1},
    ]
    result = await _fill(test_db, resources, budget=2)

    assert result["status"] == "budget"
    assert result["clicks"] == 1
    assert len(result["picks"]) == 2
    assert await ledger.list_by_status(test_db, "pending") == []
    assert await queue.get(test_db, 100) is not None


async def test_ay_preview_queries_and_selects_without_transfer(test_db):
    result = await _fill(
        test_db,
        [{"name": "测试剧 S01-S02", "link": SHARE_URL}],
        execute=False,
    )

    assert result["status"] == "preview"
    assert result["selected_links"] == [SHARE_URL]
    assert result["resource_count"] == 1
    assert result["quota_left"] == 97
    assert await ledger.list_by_status(test_db, "pending") == []


async def test_ay_multiline_ed2k_is_submitted_as_one_complete_group(test_db):
    sa = FakeSA()
    result = await _fill(
        test_db,
        [{"name": "测试剧 S01-S02", "link": ED2K + "\n" + ED2K.replace("S01E03", "S02E03")}],
        sa=sa,
    )

    assert result["status"] == "ok"
    assert len(sa.offline_calls) == 1
    links, _ = sa.offline_calls[0]
    assert len(links) == 2
    assert "S01E03" in links[0] and "S02E03" in links[1]
    rows = await ledger.list_by_status(test_db, "pending")
    assert len(rows) == 1
    assert rows[0]["share_code"].startswith("group:")
    assert await queue.get(test_db, 100) is not None


async def test_ay_rejects_snapshot_without_missing_episode_before_receive(test_db):
    client = FakeP115()

    async def wrong_package(share_code, receive_code=""):
        return SimpleNamespace(package_name="测试剧", files=[
            {"name": "E01.mkv", "path": "测试剧/S01/E01.mkv", "file_id": "f1", "is_dir": False},
            {"name": "E02.mkv", "path": "测试剧/S02/E02.mkv", "file_id": "f2", "is_dir": False},
        ])

    client.inspect_share = wrong_package
    result = await _fill(
        test_db,
        [{"name": "测试剧 S01-S02", "link": SHARE_URL}],
        p115=client,
    )

    assert result["status"] == "failed"
    assert "目标缺集" in result["reason"]
    assert client.receive_calls == []
    assert await ledger.list_by_status(test_db, "pending") == []
    assert await queue.get(test_db, 100) is not None
