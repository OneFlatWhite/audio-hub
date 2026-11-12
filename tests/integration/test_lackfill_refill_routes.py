"""audio-hub 自有 Re0 回填桥：独立令牌、队列、转存与状态事件。"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
from fastapi import FastAPI

from audio_hub.config import LackfillConfig
from audio_hub.lackfill import ledger, queue, re0_status
from audio_hub.lackfill.models import ShowLack
from audio_hub.notify.base import MultiNotifier
from audio_hub.web.routes_lackfill import router

pytestmark = pytest.mark.asyncio

TOKEN = "test-refill-token"
HEADERS = {"X-AudioHub-Refill-Key": TOKEN}
SHARE_URL = "https://115cdn.com/s/abc123?password=wd40"


class FakeP115:
    def __init__(self, cookie):
        self.cookie = cookie

    async def inspect_share(self, share_code, receive_code=""):
        from types import SimpleNamespace

        return SimpleNamespace(package_name="测试剧", files=await self.snap(share_code, receive_code))

    async def snap(self, share_code, receive_code=""):
        return [
            {"name": "S01E03.mkv", "path": "测试剧/S01/E03.mkv", "file_id": "file-1", "is_dir": False},
            {"name": "S02E03.mkv", "path": "测试剧/S02/E03.mkv", "file_id": "file-2", "is_dir": False},
        ]

    async def receive(self, share_code, receive_code, file_ids, cid):
        return True

    async def aclose(self):
        return None


class FakeSA:
    def __init__(self, *args, **kwargs):
        pass

    async def receive(self, *args):
        raise AssertionError("主链成功时不应调用 SA")

    async def aclose(self):
        return None


class FakeEmby:
    def __init__(self, *args):
        self.refreshes = 0

    async def refresh_library(self):
        self.refreshes += 1

    async def aclose(self):
        return None


class FakeSink:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


@pytest.fixture
async def bridge_app(test_db, monkeypatch):
    monkeypatch.setattr("audio_hub.lackfill.service.P115Client", FakeP115)
    monkeypatch.setattr("audio_hub.lackfill.service.SAClient", FakeSA)
    monkeypatch.setattr("audio_hub.lackfill.service.EmbyClient", FakeEmby)
    app = FastAPI()
    app.include_router(router)
    app.state.db = test_db
    app.state.config = SimpleNamespace(
        lackfill=LackfillConfig(
            enabled=True,
            emby_url="http://emby.test",
            emby_api_key="emby-key",
            tmdb_api_key="tmdb-key",
            p115_cookie="115-cookie",
            refill_api_key=TOKEN,
        )
    )
    sink = FakeSink()
    app.state.notifier = MultiNotifier([sink])
    app.state.test_sink = sink
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://audio-hub.test",
    )


async def _seed_queue(test_db, tmdb_id: int = 100) -> None:
    await queue.upsert(
        test_db,
        ShowLack(
            tmdb_id=tmdb_id,
            title="测试剧",
            year=2024,
            emby_id=f"emby-{tmdb_id}",
            missing={1: [3], 2: [3]},
        ),
        "2024-01-01T00:00:00",
    )


class TestRefillAuth:
    async def test_queue_rejects_missing_or_wrong_token(self, bridge_app):
        async with _client(bridge_app) as client:
            for headers in ({}, {"X-AudioHub-Refill-Key": "wrong"}):
                resp = await client.get("/api/lackfill/refill_queue", headers=headers)
                assert resp.status_code == 401
                assert TOKEN not in resp.text

    async def test_unconfigured_token_returns_503(self, bridge_app):
        bridge_app.state.config.lackfill.refill_api_key = ""
        async with _client(bridge_app) as client:
            resp = await client.get("/api/lackfill/refill_queue")
        assert resp.status_code == 503

    async def test_submit_get_is_not_allowed(self, bridge_app):
        async with _client(bridge_app) as client:
            resp = await client.get("/api/lackfill/refill_submit", headers=HEADERS)
        assert resp.status_code == 405


class TestRefillQueue:
    async def test_queue_returns_audio_hub_items_and_excludes_inflight(self, bridge_app):
        test_db = bridge_app.state.db
        await _seed_queue(test_db, 100)
        await _seed_queue(test_db, 200)
        await ledger.insert(
            test_db,
            show_key="tmdb:200",
            season=None,
            missing={1: [3]},
            share_code="other999",
            package_name="pkg",
            now="2026-10-25T00:00:00",
            emby_id="emby-200",
        )

        async with _client(bridge_app) as client:
            resp = await client.get("/api/lackfill/refill_queue", headers=HEADERS)

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["success"] is True
        assert payload["data"]["count"] == 1
        item = payload["data"]["items"][0]
        assert item["tmdb_id"] == 100
        assert item["title"] == "测试剧"
        assert item["seasons"] == {"1": [3], "2": [3]}
        assert item["missing"] == 2
        expected_days = (date.today() - date(2024, 1, 1)).days
        assert item["wait_days"] == expected_days


class TestRefillSubmit:
    async def test_valid_submit_transfers_and_preserves_multi_season_missing(self, bridge_app):
        await _seed_queue(bridge_app.state.db)
        async with _client(bridge_app) as client:
            resp = await client.post(
                "/api/lackfill/refill_submit",
                headers=HEADERS,
                json={"tmdb_id": 100, "title": "浏览器标题", "links": [SHARE_URL]},
            )

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["success"] is True
        assert payload["data"]["accepted"] == 1
        rows = await ledger.list_by_status(bridge_app.state.db, "pending")
        assert len(rows) == 1
        assert rows[0]["show_key"] == "tmdb:100"
        assert rows[0]["season"] is None
        assert rows[0]["package_name"] == "测试剧"
        assert json.loads(rows[0]["missing_json"]) == {"1": [3], "2": [3]}
        assert rows[0]["share_code"] == "abc123"
        assert TOKEN not in resp.text

    async def test_same_share_is_not_transferred_twice(self, bridge_app):
        await _seed_queue(bridge_app.state.db)
        body = {"tmdb_id": 100, "links": SHARE_URL}
        async with _client(bridge_app) as client:
            first = await client.post("/api/lackfill/refill_submit", headers=HEADERS, json=body)
            second = await client.post("/api/lackfill/refill_submit", headers=HEADERS, json=body)

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["data"]["results"][0]["status"] == "duplicate"
        assert second.json()["data"]["accepted"] == 0
        assert len(await ledger.list_by_status(bridge_app.state.db, "pending")) == 1

    async def test_unknown_show_and_bad_link_are_rejected_without_ledger(self, bridge_app):
        async with _client(bridge_app) as client:
            unknown = await client.post(
                "/api/lackfill/refill_submit",
                headers=HEADERS,
                json={"tmdb_id": 999, "links": SHARE_URL},
            )
            await _seed_queue(bridge_app.state.db)
            bad = await client.post(
                "/api/lackfill/refill_submit",
                headers=HEADERS,
                json={"tmdb_id": 100, "links": "https://evil.example/?u=115.com/s/abc"},
            )

        assert unknown.status_code == 404
        assert bad.status_code == 422
        assert await ledger.list_by_status(bridge_app.state.db, "pending") == []

    async def test_disabled_lackfill_cannot_transfer(self, bridge_app):
        await _seed_queue(bridge_app.state.db)
        bridge_app.state.config.lackfill.enabled = False
        async with _client(bridge_app) as client:
            resp = await client.post(
                "/api/lackfill/refill_submit",
                headers=HEADERS,
                json={"tmdb_id": 100, "links": SHARE_URL},
            )
        assert resp.status_code == 409
        assert await ledger.list_by_status(bridge_app.state.db, "pending") == []


class TestRefillStatus:
    async def test_challenge_becomes_notification_event(self, bridge_app):
        async with _client(bridge_app) as client:
            resp = await client.post(
                "/api/lackfill/refill_status",
                headers=HEADERS,
                json={
                    "status": "challenge",
                    "tmdb_id": 100,
                    "title": "测试剧",
                    "reason": "需要人工完成 Cloudflare 验证",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["success"] is True
        events = bridge_app.state.test_sink.events
        assert len(events) == 1
        assert events[0].kind == "lackfill_refill"
        assert events[0].site == "re0.me"
        assert events[0].level == "warning"
        assert "Cloudflare" in events[0].detail["reason"]

    async def test_running_heartbeat_persists_without_notification(self, bridge_app):
        from datetime import datetime

        from audio_hub.lackfill import re0_status

        async with _client(bridge_app) as client:
            resp = await client.post(
                "/api/lackfill/refill_status",
                headers=HEADERS,
                json={
                    "status": "running",
                    "tmdb_id": 100,
                    "title": "测试剧",
                    "reason": "资源页",
                    "stage": "resource",
                    "attempts": 2,
                    "auto_running": True,
                    "script_version": "1.1.0",
                    "queue_count": 3,
                },
            )
        assert resp.status_code == 200, resp.text
        current = await re0_status.worker_status(
            bridge_app.state.db, datetime.now().isoformat()
        )
        assert current["online"] is True
        assert current["stage"] == "resource"
        assert current["attempts"] == 2
        assert current["script_version"] == "1.1.0"
        assert current["queue_count"] == 3
        assert bridge_app.state.test_sink.events == []
        assert TOKEN not in resp.text + str(current)

    async def test_failures_and_explicit_empty_control_ay_eligibility(self, bridge_app):
        from audio_hub.lackfill import re0_status

        async with _client(bridge_app) as client:
            for _ in range(3):
                await client.post(
                    "/api/lackfill/refill_status",
                    headers=HEADERS,
                    json={"status": "failed", "tmdb_id": 100, "title": "测试剧"},
                )
        assert await re0_status.eligible_for_ay(bridge_app.state.db, 100) is True
        assert len(bridge_app.state.test_sink.events) == 3

        async with _client(bridge_app) as client:
            resp = await client.post(
                "/api/lackfill/refill_status",
                headers=HEADERS,
                json={"status": "not_found", "tmdb_id": 200, "title": "另一部剧"},
            )
        assert resp.status_code == 200, resp.text
        assert await re0_status.eligible_for_ay(bridge_app.state.db, 200) is True
        assert len(bridge_app.state.test_sink.events) == 3


class TestRe0Points:
    POINTS_BODY: ClassVar[dict] = {
        "balance": 2944,
        "rollover": 1000,
        "weekly_used_percent": 0,
        "weekly_quota_raw": "1400 / 1400",
        "vip": "长期VIP",
    }

    async def test_points_requires_refill_token(self, bridge_app):
        async with _client(bridge_app) as client:
            resp = await client.post("/api/lackfill/re0_points", json=self.POINTS_BODY)
        assert resp.status_code == 401

    async def test_first_report_stores_baseline_and_emits_info_event(self, bridge_app):
        async with _client(bridge_app) as client:
            resp = await client.post(
                "/api/lackfill/re0_points", headers=HEADERS, json=self.POINTS_BODY
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["delta"] is None
        assert data["notified"] is True

        events = bridge_app.state.test_sink.events
        assert len(events) == 1
        assert events[0].kind == "lackfill_refill"
        assert events[0].level == "info"
        assert events[0].title == "Re0 积分基线"
        assert events[0].detail["balance"] == 2944
        assert "1400 / 1400" in events[0].detail["summary"]

        history = await re0_status.points_history(bridge_app.state.db)
        assert history["latest"]["balance"] == 2944
        assert len(history["history"]) == 1

    async def test_repeat_report_within_throttle_is_silent_with_delta(self, bridge_app):
        async with _client(bridge_app) as client:
            first = await client.post(
                "/api/lackfill/re0_points", headers=HEADERS, json=self.POINTS_BODY
            )
            second = await client.post(
                "/api/lackfill/re0_points",
                headers=HEADERS,
                json={**self.POINTS_BODY, "balance": 2900},
            )
        assert first.json()["data"]["notified"] is True
        assert second.json()["data"]["notified"] is False
        assert second.json()["data"]["delta"] == 2900 - 2944
        assert len(bridge_app.state.test_sink.events) == 1  # 节流：不刷屏
        history = await re0_status.points_history(bridge_app.state.db)
        assert len(history["history"]) == 2
        assert history["latest"]["balance"] == 2900

    async def test_weekly_report_after_old_baseline_emits_digest_with_delta(self, bridge_app):
        from datetime import datetime, timedelta

        week_ago = (datetime.now() - timedelta(days=8)).isoformat()
        await re0_status.save_points(
            bridge_app.state.db, {"balance": 3000, "rollover": 900}, week_ago
        )
        async with _client(bridge_app) as client:
            resp = await client.post(
                "/api/lackfill/re0_points",
                headers=HEADERS,
                json={**self.POINTS_BODY, "balance": 2944},
            )
        assert resp.json()["data"]["delta"] == 2944 - 3000
        assert resp.json()["data"]["notified"] is True
        events = bridge_app.state.test_sink.events
        assert len(events) == 1
        assert events[0].title == "Re0 积分周报"
        assert "-56" in events[0].detail["summary"]

    async def test_unparseable_fields_are_stored_as_null(self, bridge_app):
        async with _client(bridge_app) as client:
            resp = await client.post(
                "/api/lackfill/re0_points",
                headers=HEADERS,
                json={"balance": None, "weekly_quota_raw": ""},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["notified"] is True
        history = await re0_status.points_history(bridge_app.state.db)
        assert history["latest"]["balance"] is None
