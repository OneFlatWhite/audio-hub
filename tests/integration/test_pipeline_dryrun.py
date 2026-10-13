"""Pipeline 端到端 dry-run 集成测试。

验证：搜索（mock 适配器）→ 匹配 → 派发（dry-run）全链路，
不触发任何真实网络请求或 qb 调用。
"""

from __future__ import annotations

import uuid

import pytest

from audio_hub.adapters.base import BaseAdapter, MediaCategory, NormalizedTorrent
from audio_hub.config import QbittorrentConfig
from audio_hub.models import WatchEntry, WatchKind
from audio_hub.pipeline.dispatcher import Dispatcher
from audio_hub.pipeline.matcher import match_candidates
from audio_hub.pipeline.search import search_round


class FakeAdapter(BaseAdapter):
    """测试用假适配器：返回预设候选，不发真实请求。"""

    def __init__(self, name: str, torrents: list[NormalizedTorrent], fail_search: bool = False):
        # 跳过 BaseAdapter.__init__（不需要真实 config）
        self.config = None
        self.site_name = name
        self._torrents = torrents
        self._fail_search = fail_search

    async def fetch_rss(self, query=None):
        return self._torrents

    async def search(self, keyword):
        if self._fail_search:
            from audio_hub.adapters.base import AdapterError
            raise AdapterError("search 参数被站点忽略")
        return [t for t in self._torrents if keyword.lower() in t.title.lower()]

    def parse_title(self, raw):
        return {"category": MediaCategory.MUSIC, "format_tags": [], "discount": None}


def make_torrent(title, category=MediaCategory.MUSIC, site="fake", size=500*1024**2, **kw):
    return NormalizedTorrent(
        info_hash=kw.pop("info_hash", uuid.uuid4().hex),
        title=title, title_normalized=title.lower(), site=site, category=category,
        download_url=f"https://{site}.me/download.php?id=1", size_bytes=size, **kw,
    )


def make_entry(query, kind=WatchKind.MUSIC, **kw):
    return WatchEntry(id=str(uuid.uuid4()), query=query, kind=kind, **kw)


# ── 搜索编排 ────────────────────────────────────────────────

class TestSearchOrchestration:
    @pytest.mark.asyncio
    async def test_search_aggregates_from_adapter(self):
        torrents = [make_torrent("周杰伦 范特西 FLAC")]
        adapters = {"fake": FakeAdapter("fake", torrents)}
        result = await search_round(adapters, [make_entry("周杰伦")])
        assert len(result.candidates) == 1

    @pytest.mark.asyncio
    async def test_search_fallback_on_search_failure(self):
        """search 抛错时应 fallback 到 fetch_rss。"""
        torrents = [make_torrent("周杰伦 范特西 FLAC")]
        adapters = {"fake": FakeAdapter("fake", torrents, fail_search=True)}
        result = await search_round(adapters, [make_entry("周杰伦")])
        assert len(result.candidates) == 1
        assert "fake" in result.fallback_used

    @pytest.mark.asyncio
    async def test_empty_watchlist_skips(self):
        adapters = {"fake": FakeAdapter("fake", [make_torrent("x")])}
        result = await search_round(adapters, [])
        assert result.total_raw == 0

    @pytest.mark.asyncio
    async def test_cross_entry_dedup(self):
        """同一 hash 被多个订阅项命中只保留一次。"""
        t = make_torrent("周杰伦 范特西 FLAC", info_hash="samehash")
        adapters = {"fake": FakeAdapter("fake", [t])}
        result = await search_round(adapters, [make_entry("周杰伦"), make_entry("范特西")])
        assert len(result.candidates) == 1


# ── 端到端 dry-run ──────────────────────────────────────────

class TestEndToEndDryRun:
    @pytest.mark.asyncio
    async def test_music_accepted_and_dryrun_dispatched(self):
        """完整链路：音乐候选 → 采纳 → dry-run 派发成功，不碰 qb。"""
        torrents = [
            make_torrent("周杰伦 范特西 FLAC 分轨", category=MediaCategory.MUSIC),
            make_torrent("周杰伦 演唱会 BluRay 1080p", category=MediaCategory.VIDEO),
        ]
        adapters = {"fake": FakeAdapter("fake", torrents)}
        entries = [make_entry("周杰伦", min_confidence="medium")]

        search_result = await search_round(adapters, entries)
        match_result = match_candidates(search_result.candidates, entries)

        # 演唱会必须被排除（零视频误抓）
        assert all(
            m.torrent.category != MediaCategory.VIDEO for m in match_result.accepted
        )

        dispatcher = Dispatcher(QbittorrentConfig(api_key="x"), client=None, dry_run=True)
        dispatch_result = await dispatcher.dispatch(match_result.accepted)

        assert dispatch_result.dry_run is True
        assert dispatch_result.ok_count == len(match_result.accepted)
        assert len(dispatch_result.failed) == 0

    @pytest.mark.asyncio
    async def test_dryrun_never_touches_qb_client(self):
        """dry-run 即使传入 client 也不应调用它。"""
        class ExplodingClient:
            def torrents_add(self, *a, **kw):
                raise AssertionError("dry-run 不应调用 qb！")

        torrents = [make_torrent("周杰伦 范特西 FLAC")]
        adapters = {"fake": FakeAdapter("fake", torrents)}
        entries = [make_entry("周杰伦", min_confidence="medium")]

        sr = await search_round(adapters, entries)
        mr = match_candidates(sr.candidates, entries)
        dispatcher = Dispatcher(QbittorrentConfig(api_key="x"), client=ExplodingClient(), dry_run=True)
        result = await dispatcher.dispatch(mr.accepted)
        assert result.ok_count >= 1   # 没抛异常即证明未调用 qb

    @pytest.mark.asyncio
    async def test_defensive_block_video_in_dispatch(self):
        """即使 matcher 漏判，dispatcher 也应防御性拦截 VIDEO。"""
        from audio_hub.pipeline.matcher import MatchResult
        video = make_torrent("某演唱会 BluRay", category=MediaCategory.VIDEO)
        # 手动构造一个被错误标为 accept 的 VIDEO
        m = MatchResult(torrent=video, action="accept", confidence="high", score=1.0, quality="n/a")
        dispatcher = Dispatcher(QbittorrentConfig(api_key="x"), client=None, dry_run=False)
        result = await dispatcher.dispatch([m])
        assert len(result.failed) == 1
        assert "defensive_block" in result.failed[0][1]


# ── 空候选链路 ──────────────────────────────────────────────

class TestEmptyPaths:
    @pytest.mark.asyncio
    async def test_no_candidates_dispatch_noop(self):
        dispatcher = Dispatcher(QbittorrentConfig(api_key="x"), client=None, dry_run=True)
        result = await dispatcher.dispatch([])
        assert result.ok_count == 0

    @pytest.mark.asyncio
    async def test_only_review_items_not_dispatched(self):
        """待审项不应被派发（dispatcher 只处理 action==accept）。"""
        torrents = [make_torrent("周杰伦 某专辑无音质标记")]
        adapters = {"fake": FakeAdapter("fake", torrents)}
        entries = [make_entry("周杰伦", min_confidence="high")]  # 高门槛
        sr = await search_round(adapters, entries)
        mr = match_candidates(sr.candidates, entries)
        assert len(mr.review) >= 1
        dispatcher = Dispatcher(QbittorrentConfig(api_key="x"), client=None, dry_run=True)
        result = await dispatcher.dispatch(mr.accepted)  # 传入 accepted（应为空）
        assert result.ok_count == 0
