"""matcher 单元测试：音质识别与旧评分构成（score_candidate）。

v0.6 起生产匹配走订阅型入口（match_subscription / apply_filters / score_subscription），
由 test_matcher_filters.py 与 test_subscription_engine.py 覆盖；旧批量入口
match_candidates 已随 search_round 一并删除。本文件只保留对仍留存函数的测试：
detect_quality（archive / score_subscription 在用）、score_candidate（保留的旧评分
参考实现，Dispatcher.dispatch 旧入口的 MatchResult 仍携带其字段）。

零视频误抓等完整过滤语义见 test_matcher_filters.py::TestApplyFilters。
"""

from __future__ import annotations

import uuid

import pytest

from audio_hub.adapters.base import DiscountType, MediaCategory, NormalizedTorrent
from audio_hub.models import WatchEntry, WatchKind
from audio_hub.pipeline.matcher import (
    detect_audiobook_format,
    detect_quality,
    score_candidate,
)


def make_entry(query: str = "周杰伦", kind: WatchKind = WatchKind.MUSIC, **kw) -> WatchEntry:
    return WatchEntry(id=str(uuid.uuid4()), query=query, kind=kind, **kw)


def make_torrent(
    title: str,
    category: MediaCategory = MediaCategory.MUSIC,
    site: str = "hhanclub",
    size: int = 500 * 1024**2,
    info_hash: str = "",
    discount: DiscountType = DiscountType.UNKNOWN,
    seeders: int | None = None,
    **kw,
) -> NormalizedTorrent:
    return NormalizedTorrent(
        info_hash=info_hash or uuid.uuid4().hex,
        title=title,
        title_normalized=title.lower(),
        site=site,
        category=category,
        download_url=f"https://{site}.me/download.php?id=1",
        size_bytes=size,
        discount=discount,
        seeders=seeders,
        **kw,
    )


# ── 音质识别 ────────────────────────────────────────────────

class TestQualityDetection:
    @pytest.mark.parametrize("text,expected", [
        ("Some Album FLAC", "flac_16"),
        ("Album 24bit/96kHz", "flac_24"),
        ("Album Hi-Res", "flac_24"),
        ("Album DSD64", "dsd"),
        ("Album APE", "ape"),
        ("Album 320k MP3", "mp3"),
        ("Album With No Quality Marker", "unknown"),
    ])
    def test_detect_quality(self, text, expected):
        assert detect_quality(text)[0] == expected

    def test_hires_outranks_flac(self):
        _, w24 = detect_quality("24bit FLAC")
        _, w16 = detect_quality("FLAC")
        assert w24 > w16

    def test_music_mp3_still_low_quality(self):
        """音乐 MP3 仍应是低质（0.40 < 0.7），有声书 MP3 合格。两套标准不可混淆。"""
        assert detect_quality("某专辑 MP3")[1] < 0.7
        assert detect_audiobook_format("某有声书 MP3")[1] >= 0.7


# ── 旧评分构成（score_candidate 保留，构成与 score_subscription 一致）────────

class TestScoring:
    def test_free_discount_boosts_score(self):
        entry = make_entry("周杰伦")
        t_free = make_torrent("周杰伦 FLAC", discount=DiscountType.FREE)
        t_norm = make_torrent("周杰伦 FLAC", discount=DiscountType.NORMAL)
        s_free, _, _ = score_candidate(entry, t_free, ["周杰伦"])
        s_norm, _, _ = score_candidate(entry, t_norm, ["周杰伦"])
        assert s_free > s_norm

    def test_seeder_boosts_score(self):
        entry = make_entry("周杰伦")
        t_seed = make_torrent("周杰伦 FLAC", seeders=10)
        t_dead = make_torrent("周杰伦 FLAC", seeders=0)
        s_seed, _, _ = score_candidate(entry, t_seed, ["周杰伦"])
        s_dead, _, _ = score_candidate(entry, t_dead, ["周杰伦"])
        assert s_seed > s_dead

    def test_score_capped_at_one(self):
        entry = make_entry("周杰伦", aliases=["Jay", "周杰倫", "周董"])
        t = make_torrent("周杰伦 FLAC 24bit", discount=DiscountType.FREE_2UP, seeders=99)
        score, _, _ = score_candidate(entry, t, ["周杰伦", "Jay", "周杰倫", "周董"])
        assert score <= 1.0
