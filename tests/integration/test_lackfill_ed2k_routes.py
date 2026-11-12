"""ed2k 从油猴 API 到 SA 离线，不得经过 115 share/receive。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
from fastapi import FastAPI

from audio_hub.config import LackfillConfig
from audio_hub.lackfill import ledger, queue
from audio_hub.lackfill.models import ShowLack
from audio_hub.lackfill.sa import SAError
from audio_hub.notify.base import MultiNotifier
from audio_hub.web.routes_lackfill import router

pytestmark = pytest.mark.asyncio

TOKEN = "ed2k-token"
HEADERS = {"X-AudioHub-Refill-Key": TOKEN}
ED2K = "ed2k://|file|测试剧 S01E03.mkv|4021117121|6A8A258330DB0DA10B020623AE256150|/"
HASH = "6A8A258330DB0DA10B020623AE256150"


class FakeP115:
    def __init__(self, cookie):
        raise AssertionError("ed2k 不应构造 115 转存客户端")


class FakeSA:
    offline_calls: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        pass

    async def offline(self, links, parent_id=""):
        self.offline_calls.append((links, parent_id))
        return {"ok": True, "via": "api"}

    async def submit_message(self, links, title):
        raise SAError("本测试不应回退消息通道")

    async def receive(self, *args):
        raise AssertionError("ed2k 不应走 115 receive")

    async def aclose(self):
        return None


class FakeEmby:
    def __init__(self, *args):
        self.refreshed = 0

    async def refresh_library(self):
        self.refreshed += 1

    async def aclose(self):
        return None


@pytest.fixture
async def app(test_db, monkeypatch):
    FakeSA.offline_calls = []
    monkeypatch.setattr("audio_hub.lackfill.service.P115Client", FakeP115)
    monkeypatch.setattr("audio_hub.lackfill.service.SAClient", FakeSA)
    monkeypatch.setattr("audio_hub.lackfill.service.EmbyClient", FakeEmby)
    app = FastAPI()
    app.include_router(router)
    app.state.db = test_db
    app.state.config = SimpleNamespace(lackfill=LackfillConfig(
        enabled=True,
        emby_url="http://emby.test",
        emby_api_key="key",
        tmdb_api_key="key",
        refill_api_key=TOKEN,
        sa_parent_id="cid-1",
    ))
    app.state.notifier = MultiNotifier([])
    await queue.upsert(
        test_db,
        ShowLack(100, "测试剧", 2024, "emby-100", {1: [3]}),
        "2026-01-01T00:00:00",
    )
    return app


async def test_ed2k_goes_to_sa_offline_with_filename_package(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/lackfill/refill_submit",
            headers=HEADERS,
            json={"tmdb_id": 100, "links": [ED2K]},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["accepted"] == 1
    assert FakeSA.offline_calls == [([ED2K], "cid-1")]
    row = (await ledger.list_by_status(app.state.db, "pending"))[0]
    assert row["share_code"] == HASH
    assert row["package_name"] == "测试剧 S01E03.mkv"
