"""lackfill service 编排层测试:M2 转存链(主备降级)+ Emby 核销状态机。"""

import json
from types import SimpleNamespace

import pytest

from audio_hub.lackfill import ledger
from audio_hub.lackfill.p115 import P115Error
from audio_hub.lackfill.sa import SAError
from audio_hub.lackfill.service import LackfillService

pytestmark = pytest.mark.asyncio

NOW = "2026-10-22T00:00:00"
RECENT = "2026-10-21T00:00:00"
LONG_AGO = "2026-10-14T00:00:00"  # 8 天前,超默认 7 天超时

SHARE_URL = "https://115.com/s/abc123XYZ?password=wd40"


class FakeP115:
    def __init__(self, snap_files=None, snap_exc=None, receive_result=True):
        self.snap_files = snap_files
        self.snap_exc = snap_exc
        self.receive_result = receive_result
        self.snap_calls = []
        self.receive_calls = []
        self.closed = False

    async def inspect_share(self, share_code, receive_code=""):
        from types import SimpleNamespace

        return SimpleNamespace(package_name="pkg", files=await self.snap(share_code, receive_code))

    async def snap(self, share_code, receive_code=""):
        self.snap_calls.append((share_code, receive_code))
        if self.snap_exc is not None:
            raise self.snap_exc
        if self.snap_files is not None:
            return self.snap_files
        return [
            {"name": "E03.mkv", "path": "pkg/S01/E03.mkv", "file_id": "f3", "is_dir": False},
            {"name": "E05.mkv", "path": "pkg/S01/E05.mkv", "file_id": "f5", "is_dir": False},
        ]

    async def receive(self, *args):
        self.receive_calls.append(args)
        return self.receive_result

    async def aclose(self):
        self.closed = True


class FakeSA:
    def __init__(self, receive_result=True, receive_exc=None, **_kwargs):
        self.receive_result = receive_result
        self.receive_exc = receive_exc
        self.receive_calls = []
        self.closed = False

    async def receive(self, *args):
        self.receive_calls.append(args)
        if self.receive_exc is not None:
            raise self.receive_exc
        return self.receive_result

    async def submit_message(self, links, title):
        self.receive_calls.append((links, title))
        if self.receive_exc is not None:
            raise self.receive_exc
        return {"ok": self.receive_result}

    async def aclose(self):
        self.closed = True


class FakeEmby:
    def __init__(self, have=None):
        self.have = have if have is not None else {}
        self.episode_map_calls = []
        self.refresh_calls = 0
        self.closed = False

    async def episode_map(self, series_id):
        self.episode_map_calls.append(series_id)
        return self.have

    async def refresh_library(self):
        self.refresh_calls += 1

    async def aclose(self):
        self.closed = True


def _cfg(**over):
    base = {"wecom_webhook": None}
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def alerts(monkeypatch):
    recorded = []

    async def fake_send(webhook, content):
        recorded.append((webhook, content))
        return True

    monkeypatch.setattr("audio_hub.lackfill.service.send_wecom_callback", fake_send)
    return recorded


async def _ledger_rows(conn):
    cur = await conn.execute("SELECT * FROM lackfill_ledger")
    return [dict(r) for r in await cur.fetchall()]


class TestTransferOne:
    async def test_p115_success(self, test_db, alerts):
        p115, sa, emby = FakeP115(), FakeSA(), FakeEmby()
        svc = LackfillService(_cfg())
        result = await svc.transfer_one(
            test_db, "show1", "S01", [3, 5], SHARE_URL, "pkg",
            now=NOW, emby_id="emby1", p115=p115, sa=sa, emby=emby,
        )
        assert result["outcome"] == "ok"
        rows = await _ledger_rows(test_db)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == result["entry_id"]
        assert row["show_key"] == "show1"
        assert row["season"] == "S01"
        assert row["status"] == "pending"
        assert row["share_code"] == "abc123XYZ"
        assert row["package_name"] == "pkg"
        assert json.loads(row["missing_json"]) == [3, 5]
        assert row["emby_id"] == "emby1"
        assert emby.refresh_calls == 1
        assert sa.receive_calls == []
        assert p115.closed is False and sa.closed is False and emby.closed is False
        assert alerts == []

    async def test_transfer_registers_cd2_watch_and_defers_refresh(self, test_db, alerts):
        from audio_hub.lackfill import cloud

        p115, sa, emby = FakeP115(), FakeSA(), FakeEmby()
        cfg = _cfg(
            cloud_verify_enabled=True,
            cloud_staging_dir="/staging",
            cloud_feed_dir="/feed",
            cloud_share_move=True,
        )
        result = await LackfillService(cfg).transfer_one(
            test_db, "show1", "S01", [3, 5], SHARE_URL, "pkg",
            now=NOW, emby_id="emby1", p115=p115, sa=sa, emby=emby,
        )
        assert result["outcome"] == "ok"
        row = await cloud.get_by_ledger(test_db, result["entry_id"])
        assert row["mode"] == "move"
        assert row["status"] == "waiting"
        assert emby.refresh_calls == 0

    async def test_fallback_to_sa(self, test_db, alerts):
        p115, sa, emby = FakeP115(receive_result=False), FakeSA(), FakeEmby()
        svc = LackfillService(_cfg())
        result = await svc.transfer_one(
            test_db, "show1", "S01", [3, 5], SHARE_URL, "pkg",
            now=NOW, emby_id="emby1", p115=p115, sa=sa, emby=emby,
        )
        assert result["outcome"] == "ok"
        assert len(sa.receive_calls) == 1
        assert len(await _ledger_rows(test_db)) == 1
        assert emby.refresh_calls == 1
        assert alerts == []

    async def test_both_failed_alerts(self, test_db, alerts):
        p115 = FakeP115(receive_result=False)
        sa = FakeSA(receive_exc=SAError("boom"))
        emby = FakeEmby()
        svc = LackfillService(_cfg(wecom_webhook="https://hook"))
        result = await svc.transfer_one(
            test_db, "show1", "S01", [3, 5], SHARE_URL, "pkg",
            now=NOW, emby_id="emby1", p115=p115, sa=sa, emby=emby,
        )
        assert result["outcome"] == "failed"
        assert len(alerts) == 1
        assert await _ledger_rows(test_db) == []
        assert emby.refresh_calls == 0

    async def test_snap_error_no_fallback(self, test_db, alerts):
        p115 = FakeP115(snap_exc=P115Error("share gone"))
        sa, emby = FakeSA(), FakeEmby()
        svc = LackfillService(_cfg(wecom_webhook="https://hook"))
        result = await svc.transfer_one(
            test_db, "show1", "S01", [3, 5], SHARE_URL, "pkg",
            now=NOW, emby_id="emby1", p115=p115, sa=sa, emby=emby,
        )
        assert result["outcome"] == "failed"
        assert sa.receive_calls == []
        assert len(alerts) == 1
        assert await _ledger_rows(test_db) == []

    async def test_invalid_url(self, test_db, alerts):
        p115, sa, emby = FakeP115(), FakeSA(), FakeEmby()
        svc = LackfillService(_cfg(wecom_webhook="https://hook"))
        result = await svc.transfer_one(
            test_db, "show1", "S01", [3, 5], "not-a-share-url", "pkg",
            now=NOW, emby_id="emby1", p115=p115, sa=sa, emby=emby,
        )
        assert result["outcome"] == "failed"
        assert p115.snap_calls == []
        assert sa.receive_calls == []
        assert len(alerts) == 1
        assert await _ledger_rows(test_db) == []


class TestVerifyPending:
    async def _insert(self, conn, **over):
        kwargs = {
            "show_key": "show1",
            "season": "S01",
            "missing": [3, 5],
            "share_code": "abc123",
            "package_name": "pkg",
            "now": RECENT,
            "emby_id": "emby1",
        }
        kwargs.update(over)
        return await ledger.insert(conn, **kwargs)

    async def test_complete(self, test_db, alerts):
        await self._insert(test_db)
        emby = FakeEmby(have={1: {3, 5}})
        svc = LackfillService(_cfg())
        stats = await svc.verify_pending(test_db, now=NOW, emby=emby)
        assert stats == {"checked": 1, "complete": 1, "partial": 0, "bumped": 0, "timeout": 0}
        assert (await ledger.get(test_db, 1))["status"] == "complete"
        assert emby.closed is False

    async def test_partial(self, test_db, alerts):
        await self._insert(test_db)
        emby = FakeEmby(have={1: {3}})
        svc = LackfillService(_cfg())
        stats = await svc.verify_pending(test_db, now=NOW, emby=emby)
        assert stats["partial"] == 1
        row = await ledger.get(test_db, 1)
        assert row["status"] == "partial"
        assert json.loads(row["missing_json"]) == [5]

    async def test_bump_on_timeout_window(self, test_db, alerts):
        await self._insert(test_db, now=LONG_AGO)
        emby = FakeEmby(have={1: set()})
        svc = LackfillService(_cfg())
        stats = await svc.verify_pending(test_db, now=NOW, emby=emby)
        assert stats["bumped"] == 1
        row = await ledger.get(test_db, 1)
        assert row["status"] == "timeout"
        assert row["verify_round"] == 1

    async def test_timeout_after_max_rounds(self, test_db, alerts):
        await self._insert(test_db, now=LONG_AGO)
        for _ in range(2):
            await ledger.bump_round(test_db, 1, LONG_AGO)
        emby = FakeEmby(have={1: set()})
        svc = LackfillService(_cfg(wecom_webhook="https://hook"))
        stats = await svc.verify_pending(test_db, now=NOW, emby=emby)
        assert stats["timeout"] == 1
        assert (await ledger.get(test_db, 1))["status"] == "timeout"
        assert len(alerts) == 1
