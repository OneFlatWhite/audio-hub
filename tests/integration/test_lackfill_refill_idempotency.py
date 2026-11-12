"""真实路由的备选包、活动台账与并发幂等回归。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
from fastapi import FastAPI

from audio_hub.config import LackfillConfig
from audio_hub.lackfill import ledger, queue
from audio_hub.lackfill.models import ShowLack
from audio_hub.notify.base import MultiNotifier
from audio_hub.web.routes_lackfill import router

pytestmark = pytest.mark.asyncio

TOKEN = "idempotency-token"
HEADERS = {"X-AudioHub-Refill-Key": TOKEN}
FIRST = "https://115.com/s/aaa111"
SECOND = "https://115.com/s/bbb222"


class FakeP115:
    calls: ClassVar[list[str]] = []
    fail_for: ClassVar[set[str]] = set()
    only_season: ClassVar[set[str]] = set()

    def __init__(self, cookie):
        pass

    async def inspect_share(self, share_code, receive_code=""):
        return SimpleNamespace(package_name="测试剧", files=await self.snap(share_code, receive_code))

    async def snap(self, share_code, receive_code=""):
        files = [
            {"name": "S01E03.mkv", "file_id": "f1", "is_dir": False, "path": "测试剧/S01/E03.mkv"},
            {"name": "S02E03.mkv", "file_id": "f2", "is_dir": False, "path": "测试剧/S02/E03.mkv"},
        ]
        return files[:1] if share_code in self.only_season else files

    async def receive(self, share_code, receive_code, file_ids, cid):
        await asyncio.sleep(0.01)
        self.calls.append(share_code)
        return share_code not in self.fail_for

    async def aclose(self):
        return None


class FakeSA:
    def __init__(self, *args, **kwargs):
        pass

    async def receive(self, *args):
        raise AssertionError("主链失败时不在本测试使用 SA")

    async def aclose(self):
        return None


class FakeEmby:
    def __init__(self, *args):
        pass

    async def refresh_library(self):
        return None

    async def aclose(self):
        return None


@pytest.fixture
async def app(test_db, monkeypatch):
    FakeP115.calls = []
    FakeP115.fail_for = set()
    FakeP115.only_season = set()
    monkeypatch.setattr("audio_hub.lackfill.service.P115Client", FakeP115)
    monkeypatch.setattr("audio_hub.lackfill.service.SAClient", FakeSA)
    monkeypatch.setattr("audio_hub.lackfill.service.EmbyClient", FakeEmby)
    app = FastAPI()
    app.include_router(router)
    app.state.db = test_db
    app.state.config = SimpleNamespace(lackfill=LackfillConfig(
        enabled=True,
        emby_url="http://emby.test",
        emby_api_key="emby-key",
        tmdb_api_key="tmdb-key",
        p115_cookie="cookie",
        refill_api_key=TOKEN,
    ))
    app.state.notifier = MultiNotifier([])
    await queue.upsert(
        test_db,
        ShowLack(100, "测试剧", 2024, "emby-100", {1: [3], 2: [3]}),
        "2026-01-01T00:00:00",
    )
    return app


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _post(client, links):
    return await client.post(
        "/api/lackfill/refill_submit",
        headers=HEADERS,
        json={"tmdb_id": 100, "links": links},
    )


async def test_links_are_alternatives_and_first_success_stops(app):
    async with _client(app) as client:
        resp = await _post(client, [FIRST, SECOND])
    assert resp.status_code == 200
    assert resp.json()["data"]["accepted"] == 1
    assert len(resp.json()["data"]["results"]) == 1
    assert FakeP115.calls == ["aaa111"]
    assert len(await ledger.list_by_status(app.state.db, "pending")) == 1


async def test_failed_first_link_falls_through_to_second_without_overall_failure(app):
    FakeP115.fail_for = {"aaa111"}
    FakeSA.receive = lambda *args: None
    async with _client(app) as client:
        resp = await _post(client, [FIRST, SECOND])
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["accepted"] == 1
    assert resp.json()["success"] is True
    assert [r["status"] for r in data["results"]] == ["failed", "accepted"]
    assert FakeP115.calls == ["aaa111", "bbb222"]
    assert len(await ledger.list_by_status(app.state.db, "pending")) == 1


async def test_active_ledger_blocks_a_different_share(app):
    await ledger.insert(
        app.state.db,
        show_key="tmdb:100",
        season=None,
        missing={1: [3], 2: [3]},
        share_code="old999",
        package_name="pkg",
        now="2026-10-25T00:00:00",
        emby_id="emby-100",
    )
    async with _client(app) as client:
        resp = await _post(client, FIRST)
    assert resp.json()["data"]["accepted"] == 0
    assert resp.json()["data"]["results"][0]["status"] == "duplicate"
    assert FakeP115.calls == []
    assert len(await ledger.list_by_status(app.state.db, "pending")) == 1


async def test_concurrent_same_share_has_only_one_external_receive(app):
    async with _client(app) as client:
        first, second = await asyncio.gather(_post(client, FIRST), _post(client, FIRST))
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"]["accepted"] + second.json()["data"]["accepted"] == 1
    assert FakeP115.calls == ["aaa111"]
    assert len(await ledger.list_by_status(app.state.db, "pending")) == 1


async def test_wrong_season_is_rejected_before_receive_and_next_package_is_used(app):
    FakeP115.only_season = {"aaa111"}
    async with _client(app) as client:
        resp = await _post(client, [FIRST, SECOND])
    assert resp.status_code == 200
    assert resp.json()["data"]["accepted"] == 1
    assert [r["status"] for r in resp.json()["data"]["results"]] == ["failed", "accepted"]
    assert FakeP115.calls == ["bbb222"]
    row = (await ledger.list_by_status(app.state.db, "pending"))[0]
    assert row["share_code"] == "bbb222"
    assert row["package_name"] == "测试剧"
