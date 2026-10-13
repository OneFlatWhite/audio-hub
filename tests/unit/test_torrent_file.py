"""真实 info_hash 提取（pipeline.torrent_file）+ 派发层写回。

覆盖：
- info_hash_from_torrent：合法种子算 sha1(info 原始字节)；垃圾输入抛 ValueError
- info_hash_from_magnet：40 位 hex / 32 位 base32 / 非 magnet
- fetch_and_hash：magnet 短路（无网络）；http 拉取失败回落 (None, None)
- Dispatcher 实跑：拿到真实哈希后以字节投递、任务行 info_hash 写回；
  取不到时回落 URL 投递且不动任务哈希
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
import respx

from audio_hub import repo
from audio_hub.adapters.base import MediaCategory, NormalizedTorrent
from audio_hub.config import QbittorrentConfig
from audio_hub.db import init_db
from audio_hub.models import Task, TaskState
from audio_hub.pipeline import dispatcher as dispatcher_mod
from audio_hub.pipeline.dispatcher import Dispatcher
from audio_hub.pipeline.torrent_file import (
    fetch_and_hash,
    fetch_torrent_bytes,
    info_hash_from_magnet,
    info_hash_from_torrent,
)

# ── 测试用最小 bencode 编码器 ────────────────────────────────

def benc(obj) -> bytes:
    if isinstance(obj, int):
        return b"i" + str(obj).encode() + b"e"
    if isinstance(obj, bytes):
        return str(len(obj)).encode() + b":" + obj
    if isinstance(obj, str):
        return benc(obj.encode())
    if isinstance(obj, list):
        return b"l" + b"".join(benc(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda kv: kv[0])
        return b"d" + b"".join(benc(k) + benc(v) for k, v in items) + b"e"
    raise TypeError(obj)


INFO = {
    b"name": b"album-x",
    b"piece length": 262144,
    b"pieces": b"\x00" * 20,
    b"length": 1024,
}
TORRENT_BYTES = benc({b"announce": b"https://pt.example/announce", b"info": INFO})
EXPECTED_HASH = hashlib.sha1(benc(INFO)).hexdigest()


class TestInfoHashFromTorrent:
    def test_valid_torrent(self):
        assert info_hash_from_torrent(TORRENT_BYTES) == EXPECTED_HASH

    def test_info_not_first_key(self):
        blob = benc({b"comment": b"hi", b"created by": b"pt", b"info": INFO})
        assert info_hash_from_torrent(blob) == EXPECTED_HASH

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            info_hash_from_torrent(b"")

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError):
            info_hash_from_torrent(b"i42e")
        with pytest.raises(ValueError):
            info_hash_from_torrent(b"not a torrent at all")

    def test_rejects_missing_info(self):
        with pytest.raises(ValueError):
            info_hash_from_torrent(benc({b"announce": b"https://x"}))

    def test_rejects_truncated(self):
        with pytest.raises(ValueError):
            info_hash_from_torrent(TORRENT_BYTES[:20])


class TestInfoHashFromMagnet:
    def test_hex40(self):
        h = "AB" * 20
        assert info_hash_from_magnet(f"magnet:?xt=urn:btih:{h}&dn=x") == h.lower()

    def test_base32(self):
        raw = b"\x11" * 20
        import base64
        b32 = base64.b32encode(raw).decode()
        assert info_hash_from_magnet(f"magnet:?xt=urn:btih:{b32}") == raw.hex()

    def test_non_magnet(self):
        assert info_hash_from_magnet("https://pt.example/download.php?id=1") is None
        assert info_hash_from_magnet("") is None


class TestFetch:
    @pytest.mark.asyncio
    async def test_magnet_short_circuits_without_network(self):
        h = "ab" * 20
        real_hash, blob = await fetch_and_hash(f"magnet:?xt=urn:btih:{h}")
        assert real_hash == h
        assert blob is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_ok(self):
        respx.get("https://pt.example/dl.php?id=1").mock(
            return_value=httpx.Response(200, content=TORRENT_BYTES)
        )
        real_hash, blob = await fetch_and_hash("https://pt.example/dl.php?id=1")
        assert real_hash == EXPECTED_HASH
        assert blob == TORRENT_BYTES

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_http_error_falls_back(self):
        respx.get("https://pt.example/dl.php?id=1").mock(
            return_value=httpx.Response(404, content=b"nope")
        )
        assert await fetch_and_hash("https://pt.example/dl.php?id=1") == (None, None)

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_html_response_falls_back(self):
        """站点返回登录页 HTML 时不得当成种子算哈希。"""
        respx.get("https://pt.example/dl.php?id=1").mock(
            return_value=httpx.Response(200, content=b"<html>login</html>")
        )
        assert await fetch_torrent_bytes("https://pt.example/dl.php?id=1") is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_network_error_falls_back(self):
        respx.get("https://pt.example/dl.php?id=1").mock(side_effect=httpx.ConnectError("x"))
        assert await fetch_and_hash("https://pt.example/dl.php?id=1") == (None, None)


# ── Dispatcher 实跑写回真实哈希 ──────────────────────────────

class RecordingQb:
    def __init__(self):
        self.calls: list[dict] = []

    def torrents_add(self, **kw):
        self.calls.append(kw)


def qb_config() -> QbittorrentConfig:
    return QbittorrentConfig(api_key="x", category="audio-hub", tag="hub", save_path="/seeds")


def make_task(**kw) -> Task:
    return Task(
        info_hash=kw.pop("info_hash", "f" * 40),
        site="mteam",
        title="G.E.M. 摩天动物园 FLAC",
        state=TaskState.DISCOVERED,
        download_url=kw.pop("download_url", "https://pt.example/download.php?id=99"),
        size_bytes=500 * 1024**2,
        **kw,
    )


class TestDispatcherRealHash:
    @pytest.mark.asyncio
    async def test_real_hash_written_back_and_bytes_posted(self, monkeypatch, tmp_path):
        """拉到真实哈希：qb 收字节、任务行写回真实哈希（监控按它关联 qb 种子）。"""
        async def _fake(_url):
            return EXPECTED_HASH, TORRENT_BYTES

        monkeypatch.setattr(dispatcher_mod, "fetch_and_hash", _fake)
        db = await init_db(str(tmp_path / "hub.db"))
        try:
            task = make_task()
            await repo.create_task(db, task)
            qb = RecordingQb()
            d = Dispatcher(qb_config(), client=qb, dry_run=False, db=db)

            result = await d.dispatch_tasks([task])

            assert result.ok_count == 1
            assert qb.calls[0]["torrent_files"] == TORRENT_BYTES
            assert "urls" not in qb.calls[0]
            async with db.execute(
                "SELECT info_hash, state FROM tasks WHERE id = ?", (task.id,)
            ) as cur:
                row = await cur.fetchone()
            assert row["info_hash"] == EXPECTED_HASH
            assert row["state"] == "dispatched"
            assert task.info_hash == EXPECTED_HASH
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_fallback_keeps_synthetic_hash_and_posts_url(self, monkeypatch, tmp_path):
        """拉不到种子字节：回落 URL 投递，任务保留合成哈希（不写入 None）。"""
        async def _none(_url):
            return None, None

        monkeypatch.setattr(dispatcher_mod, "fetch_and_hash", _none)
        db = await init_db(str(tmp_path / "hub.db"))
        try:
            task = make_task()
            await repo.create_task(db, task)
            qb = RecordingQb()
            d = Dispatcher(qb_config(), client=qb, dry_run=False, db=db)

            result = await d.dispatch_tasks([task])

            assert result.ok_count == 1
            assert qb.calls[0]["urls"] == task.download_url
            async with db.execute(
                "SELECT info_hash FROM tasks WHERE id = ?", (task.id,)
            ) as cur:
                row = await cur.fetchone()
            assert row["info_hash"] == "f" * 40
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_magnet_link_hash_written_without_bytes(self, monkeypatch, tmp_path):
        """magnet 链接：无字节可投（URL 投递），但真实哈希照样写回。"""
        magnet_hash = "cd" * 20
        t_url = f"magnet:?xt=urn:btih:{magnet_hash}"
        db = await init_db(str(tmp_path / "hub.db"))
        try:
            task = make_task(download_url=t_url)
            await repo.create_task(db, task)
            qb = RecordingQb()
            d = Dispatcher(qb_config(), client=qb, dry_run=False, db=db)

            result = await d.dispatch_tasks([task])

            assert result.ok_count == 1
            assert qb.calls[0]["urls"] == t_url
            assert task.info_hash == magnet_hash
            async with db.execute(
                "SELECT info_hash FROM tasks WHERE id = ?", (task.id,)
            ) as cur:
                assert (await cur.fetchone())["info_hash"] == magnet_hash
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_normalized_torrent_category_passthrough(self):
        """回归保护：make_task 里用不到的 NormalizedTorrent 字段不破坏构造。"""
        t = NormalizedTorrent(
            site="mteam", title="x", info_hash="a" * 40,
            category=MediaCategory.MUSIC, download_url="https://x", size_bytes=1,
        )
        assert t.category == MediaCategory.MUSIC
