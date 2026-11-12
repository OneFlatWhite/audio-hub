"""CD2 文件落地硬验证：稳定、移动、冲突、恢复与 Emby 刷新时序。"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from audio_hub.db import SCHEMA_VERSION
from audio_hub.lackfill import ledger

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 10, 25, 1, 0, 0)


def _cloud():
    return importlib.import_module("audio_hub.lackfill.cloud")


def _at(minutes=0):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def _cfg(source: Path, feed: Path, **over):
    values = {
        "cloud_verify_enabled": True,
        "cloud_staging_dir": str(source),
        "cloud_feed_dir": str(feed),
        "cloud_share_move": True,
        "cloud_stable_minutes": 5,
        "emby_url": "http://emby.test",
        "emby_api_key": "key",
    }
    values.update(over)
    from types import SimpleNamespace
    return SimpleNamespace(**values)


class FakeEmby:
    def __init__(self):
        self.refresh_calls = 0

    async def refresh_library(self):
        self.refresh_calls += 1

    async def aclose(self):
        return None


async def _entry(conn, package="测试剧 S01"):
    entry_id = await ledger.insert(
        conn,
        show_key="tmdb:100",
        season="S01",
        missing=[3],
        share_code="abc123",
        package_name=package,
        now="2026-10-25T00:00:00",
        emby_id="emby-100",
    )
    return entry_id


def _package(root: Path, name="测试剧 S01", content=b"video"):
    import os

    package = root / name
    package.mkdir(parents=True)
    file_path = package / "S01E03.mkv"
    file_path.write_bytes(content)
    old = T0.timestamp() - 3600
    os.utime(file_path, (old, old))
    return package


async def test_schema_v10_has_cloud_table(test_db):
    assert SCHEMA_VERSION == 10
    cur = await test_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='lackfill_cloud'"
    )
    assert await cur.fetchone() is not None


async def test_verify_requires_two_stable_snapshots_then_refreshes_once(test_db, tmp_path):
    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir()
    _package(feed)
    entry_id = await _entry(test_db)
    await cloud.insert(test_db, entry_id, "测试剧 S01", "verify", str(source), str(feed), T0.isoformat())
    emby = FakeEmby()

    first = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(), emby=emby)
    assert first["ready"] == 0 and first["waiting"] == 1
    assert emby.refresh_calls == 0
    second = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(6), emby=emby)
    assert second["ready"] == 1 and second["refreshed"] == 1
    assert (await cloud.get_by_ledger(test_db, entry_id))["status"] == "ready"
    assert emby.refresh_calls == 1
    third = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(12), emby=emby)
    assert third["checked"] == 0
    assert emby.refresh_calls == 1


async def test_empty_package_never_becomes_ready(test_db, tmp_path):
    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir(); (feed / "测试剧 S01").mkdir()
    entry_id = await _entry(test_db)
    await cloud.insert(test_db, entry_id, "测试剧 S01", "verify", str(source), str(feed), T0.isoformat())
    emby = FakeEmby()
    stats = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(20), emby=emby)
    assert stats["ready"] == 0 and stats["waiting"] == 1
    assert emby.refresh_calls == 0


async def test_growing_file_resets_stability_window(test_db, tmp_path):
    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir()
    package = _package(feed)
    entry_id = await _entry(test_db)
    await cloud.insert(test_db, entry_id, "测试剧 S01", "verify", str(source), str(feed), T0.isoformat())
    emby = FakeEmby()
    await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(), emby=emby)
    (package / "S01E03.mkv").write_bytes(b"video-is-now-longer")
    await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(1), emby=emby)
    row = await cloud.get_by_ledger(test_db, entry_id)
    assert row["status"] == "waiting"
    assert row["stable_since"] == _at(1)
    assert emby.refresh_calls == 0


async def test_move_transfers_whole_package_without_overwrite(test_db, tmp_path):
    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir()
    _package(source)
    entry_id = await _entry(test_db)
    await cloud.insert(test_db, entry_id, "测试剧 S01", "move", str(source), str(feed), T0.isoformat())
    emby = FakeEmby()
    await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(), emby=emby)
    stats = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(6), emby=emby)
    assert stats["ready"] == 1
    assert not (source / "测试剧 S01").exists()
    assert (feed / "测试剧 S01" / "S01E03.mkv").read_bytes() == b"video"
    assert emby.refresh_calls == 1


async def test_existing_feed_package_recovers_without_overwriting(test_db, tmp_path):
    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir()
    _package(source, content=b"source-copy")
    _package(feed, content=b"feed-already-present")
    entry_id = await _entry(test_db)
    await cloud.insert(test_db, entry_id, "测试剧 S01", "move", str(source), str(feed), T0.isoformat())
    emby = FakeEmby()
    await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(), emby=emby)
    stats = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(6), emby=emby)
    assert stats["ready"] == 1
    assert (feed / "测试剧 S01" / "S01E03.mkv").read_bytes() == b"feed-already-present"
    assert (source / "测试剧 S01" / "S01E03.mkv").read_bytes() == b"source-copy"


async def test_missing_mount_never_reports_ready(test_db, tmp_path):
    cloud = _cloud()
    source, feed = tmp_path / "missing-source", tmp_path / "missing-feed"
    entry_id = await _entry(test_db)
    await cloud.insert(test_db, entry_id, "测试剧 S01", "move", str(source), str(feed), T0.isoformat())
    emby = FakeEmby()
    stats = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(20), emby=emby)
    assert stats["ready"] == 0 and stats["waiting"] == 1
    row = await cloud.get_by_ledger(test_db, entry_id)
    assert row["status"] == "waiting" and "挂载" in row["error"]
    assert emby.refresh_calls == 0


async def test_cloud_snapshot_records_real_file_metadata(test_db, tmp_path):
    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir()
    package = _package(feed)
    entry_id = await _entry(test_db)
    await cloud.insert(test_db, entry_id, "测试剧 S01", "verify", str(source), str(feed), T0.isoformat())
    await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(), emby=FakeEmby())
    row = await cloud.get_by_ledger(test_db, entry_id)
    snapshot = json.loads(row["snapshot_json"])
    assert snapshot["S01E03.mkv"][0] == len(b"video")
    assert package.name == "测试剧 S01"


async def test_cloud_insert_rejects_unsafe_package_names(test_db):
    cloud = _cloud()
    entry_id = await _entry(test_db)
    for bad in ("../evil", "nested/path", ".."):
        with pytest.raises(ValueError):
            await cloud.insert(test_db, entry_id, bad, "move", "/staging", "/feed", T0.isoformat())


async def test_single_file_move_stabilizes_then_refreshes_once(test_db, tmp_path):
    import os

    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir()
    name = "测试剧.S01E03.mkv"
    source_file = source / name
    source_file.write_bytes(b"video")
    old = T0.timestamp() - 3600
    os.utime(source_file, (old, old))
    entry_id = await _entry(test_db, package=name)
    await cloud.insert(test_db, entry_id, name, "move_file", str(source), str(feed), T0.isoformat())
    emby = FakeEmby()

    first = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(), emby=emby)
    assert first["ready"] == 0 and first["waiting"] == 1
    second = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(6), emby=emby)
    assert second["ready"] == 1 and second["refreshed"] == 1
    assert not source_file.exists()
    assert (feed / name).read_bytes() == b"video"
    assert emby.refresh_calls == 1


async def test_zero_byte_files_never_become_ready(test_db, tmp_path):
    import os

    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir()
    package = _package(feed)
    file_path = package / "S01E03.mkv"
    file_path.write_bytes(b"")
    old = T0.timestamp() - 3600
    os.utime(file_path, (old, old))
    entry_id = await _entry(test_db)
    await cloud.insert(test_db, entry_id, "测试剧 S01", "verify", str(source), str(feed), T0.isoformat())
    emby = FakeEmby()

    await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(), emby=emby)
    stats = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(6), emby=emby)
    assert stats["ready"] == 0 and stats["waiting"] == 1
    assert emby.refresh_calls == 0


async def test_cloud_group_waits_for_every_file_then_refreshes_once(test_db, tmp_path):
    import os

    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir()
    names = ["E03.mkv", "E04.mkv"]
    entry_id = await _entry(test_db, package="测试剧 S01 整季")
    await cloud.insert(test_db, entry_id, "测试剧 S01 整季", "move_files", str(source), str(feed), T0.isoformat())
    await cloud.insert_files(test_db, entry_id, names, str(source), str(feed), T0.isoformat())

    def put(path, content=b"video"):
        path.write_bytes(content)
        old = T0.timestamp() - 3600
        os.utime(path, (old, old))

    put(source / names[0])
    emby = FakeEmby()
    await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(), emby=emby)
    stats = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(6), emby=emby)
    assert stats["ready"] == 0 and stats["waiting"] == 1
    assert not (source / names[0]).exists()
    assert (feed / names[0]).read_bytes() == b"video"
    assert emby.refresh_calls == 0

    put(source / names[1])
    await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(12), emby=emby)
    stats = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(18), emby=emby)
    assert stats["ready"] == 1 and stats["refreshed"] == 1
    assert emby.refresh_calls == 1
    files = await cloud.list_files(test_db, entry_id)
    assert [item["status"] for item in files] == ["ready", "ready"]
    stats = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(24), emby=emby)
    assert stats["checked"] == 0 and emby.refresh_calls == 1


async def test_cloud_group_does_not_overwrite_existing_file(test_db, tmp_path):
    import os

    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir()
    entry_id = await _entry(test_db, package="测试剧 S01 整季")
    await cloud.insert(test_db, entry_id, "测试剧 S01 整季", "move_files", str(source), str(feed), T0.isoformat())
    await cloud.insert_files(test_db, entry_id, ["E03.mkv", "E04.mkv"], str(source), str(feed), T0.isoformat())
    (feed / "E03.mkv").write_bytes(b"original")
    (source / "E03.mkv").write_bytes(b"new-copy")
    old = T0.timestamp() - 3600
    os.utime(feed / "E03.mkv", (old, old))
    os.utime(source / "E03.mkv", (old, old))
    emby = FakeEmby()
    await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(), emby=emby)
    stats = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(6), emby=emby)
    assert stats["ready"] == 0 and stats["waiting"] == 1
    assert (feed / "E03.mkv").read_bytes() == b"original"
    assert (source / "E03.mkv").read_bytes() == b"new-copy"
    assert emby.refresh_calls == 0


async def test_cloud_group_zero_byte_file_never_completes(test_db, tmp_path):
    import os

    cloud = _cloud()
    source, feed = tmp_path / "staging", tmp_path / "feed"
    source.mkdir(); feed.mkdir()
    entry_id = await _entry(test_db, package="测试剧 S01 整季")
    await cloud.insert(test_db, entry_id, "测试剧 S01 整季", "move_files", str(source), str(feed), T0.isoformat())
    await cloud.insert_files(test_db, entry_id, ["E03.mkv", "E04.mkv"], str(source), str(feed), T0.isoformat())
    (source / "E03.mkv").write_bytes(b"")
    (source / "E04.mkv").write_bytes(b"video")
    old = T0.timestamp() - 3600
    for path in (source / "E03.mkv", source / "E04.mkv"):
        os.utime(path, (old, old))
    emby = FakeEmby()
    await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(), emby=emby)
    stats = await cloud.poll_cloud_once(test_db, _cfg(source, feed), _at(6), emby=emby)
    assert stats["ready"] == 0 and stats["waiting"] == 1
    assert emby.refresh_calls == 0
