"""ed2k 离线编排：SA API 主链、加密消息备链与台账。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from audio_hub.lackfill import ledger
from audio_hub.lackfill.sa import SAError
from audio_hub.lackfill.service import LackfillService

pytestmark = pytest.mark.asyncio

NOW = "2026-10-25T01:00:00"
ED2K = "ed2k://|file|测试剧 S01E03.mkv|4021117121|6A8A258330DB0DA10B020623AE256150|/"
HASH = "6A8A258330DB0DA10B020623AE256150"


class FakeSA:
    def __init__(self, offline_error=False, message_error=False):
        self.offline_error = offline_error
        self.message_error = message_error
        self.offline_calls = []
        self.message_calls = []

    async def offline(self, links, parent_id=""):
        self.offline_calls.append((links, parent_id))
        if self.offline_error:
            raise SAError("offline failed")
        return {"ok": True, "via": "api"}

    async def submit_message(self, links, title):
        self.message_calls.append((links, title))
        if self.message_error:
            raise SAError("message failed")
        return {"ok": True, "via": "http"}

    async def aclose(self):
        return None


class FakeEmby:
    def __init__(self):
        self.refresh_calls = 0

    async def refresh_library(self):
        self.refresh_calls += 1

    async def aclose(self):
        return None


def _cfg(**over):
    values = {
        "sa_parent_id": "cid-1",
        "wecom_webhook": None,
    }
    values.update(over)
    return SimpleNamespace(**values)


async def test_offline_success_uses_ed2k_hash_and_real_package_name(test_db):
    sa = FakeSA()
    emby = FakeEmby()
    result = await LackfillService(_cfg()).offline_one(
        test_db,
        "tmdb:100",
        "S01",
        [3],
        ED2K,
        "测试剧 S01 整季",
        now=NOW,
        emby_id="emby-100",
        sa=sa,
        emby=emby,
    )
    assert result["outcome"] == "ok"
    assert sa.offline_calls == [([ED2K], "cid-1")]
    assert sa.message_calls == []
    row = await ledger.get(test_db, result["entry_id"])
    assert row["share_code"] == HASH
    assert row["package_name"] == "测试剧 S01 整季"
    assert emby.refresh_calls == 1


async def test_offline_falls_back_to_encrypted_message(test_db):
    sa = FakeSA(offline_error=True)
    result = await LackfillService(_cfg()).offline_one(
        test_db, "tmdb:100", "S01", [3], ED2K, "测试剧 S01 整季",
        now=NOW, emby_id="emby-100", sa=sa, emby=FakeEmby(),
    )
    assert result["outcome"] == "ok"
    assert len(sa.message_calls) == 1
    assert len(await ledger.list_by_status(test_db, "pending")) == 1


async def test_offline_both_channels_failed_has_no_ledger(test_db):
    sa = FakeSA(offline_error=True, message_error=True)
    result = await LackfillService(_cfg()).offline_one(
        test_db, "tmdb:100", "S01", [3], ED2K, "测试剧 S01 整季",
        now=NOW, emby_id="emby-100", sa=sa, emby=FakeEmby(),
    )
    assert result["outcome"] == "failed"
    assert await ledger.list_by_status(test_db, "pending") == []


async def test_offline_registers_cd2_watch_and_defers_emby_refresh(test_db, tmp_path):
    from audio_hub.lackfill import cloud

    sa = FakeSA()
    emby = FakeEmby()
    cfg = _cfg(
        cloud_verify_enabled=True,
        cloud_staging_dir=str(tmp_path / "staging"),
        cloud_feed_dir=str(tmp_path / "feed"),
        cloud_share_move=True,
    )
    result = await LackfillService(cfg).offline_one(
        test_db, "tmdb:100", "S01", [3], ED2K, "测试剧 S01 整季",
        now=NOW, emby_id="emby-100", sa=sa, emby=emby,
    )
    assert result["outcome"] == "ok"
    row = await cloud.get_by_ledger(test_db, result["entry_id"])
    assert row["mode"] == "move_file"
    assert row["package_name"] == "测试剧 S01E03.mkv"
    assert row["status"] == "waiting"
    assert emby.refresh_calls == 0


async def test_offline_batch_submits_once_and_registers_each_real_file(test_db, tmp_path):
    from audio_hub.lackfill import cloud, refill

    second = ED2K.replace("S01E03", "S01E04").replace(HASH, "7B8A258330DB0DA10B020623AE256150")
    links = ED2K + "\n" + second
    sa = FakeSA()
    emby = FakeEmby()
    cfg = _cfg(
        cloud_verify_enabled=True,
        cloud_staging_dir=str(tmp_path / "staging"),
        cloud_feed_dir=str(tmp_path / "feed"),
    )
    result = await LackfillService(cfg).offline_one(
        test_db, "tmdb:100", None, {1: [3, 4]}, links, "测试剧 S01 整季",
        now=NOW, emby_id="emby-100", sa=sa, emby=emby,
    )
    assert result["outcome"] == "ok"
    assert sa.offline_calls == [([ED2K, second], "cid-1")]
    assert len(sa.message_calls) == 0
    row = await ledger.get(test_db, result["entry_id"])
    assert row["share_code"] == refill.resource_identity(links)
    assert row["share_code"].startswith("group:")
    parent = await cloud.get_by_ledger(test_db, result["entry_id"])
    assert parent["mode"] == "move_files"
    files = await cloud.list_files(test_db, result["entry_id"])
    assert [item["file_name"] for item in files] == ["测试剧 S01E03.mkv", "测试剧 S01E04.mkv"]
    assert emby.refresh_calls == 0


async def test_batch_rejects_same_filename_with_different_hashes():
    from audio_hub.lackfill import refill

    other = ED2K.replace(HASH, "7B8A258330DB0DA10B020623AE256150")
    with pytest.raises(ValueError, match="同名不同"):
        refill.resource_identity(ED2K + "\n" + other)
