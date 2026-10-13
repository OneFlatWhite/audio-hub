"""matcher 单元测试：类型判别、体积过滤、置信度分级、去重准入。

覆盖 PRD v0.1 验收标准的关键点：零视频误抓。
"""

from __future__ import annotations

import uuid

import pytest

from audio_hub.adapters.base import DiscountType, MediaCategory, NormalizedTorrent
from audio_hub.models import WatchEntry, WatchKind
from audio_hub.pipeline.matcher import (
    detect_quality,
    match_candidates,
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


# ── 类型判别：零视频误抓（v0.1 核心验收）────────────────────

class TestVideoExclusion:
    def test_video_category_rejected(self):
        entries = [make_entry("周杰伦")]
        cands = [make_torrent("周杰伦 演唱会 BluRay 1080p", category=MediaCategory.VIDEO)]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 0
        assert len(r.rejected) == 1
        assert r.rejected[0].reject_reason == "video_or_concert"

    def test_concert_keyword_rejected_even_if_music_category(self):
        """分类标成音乐但标题含演唱会/BluRay，仍应硬排除。"""
        entries = [make_entry("周杰伦")]
        cands = [make_torrent(
            "周杰伦 嘉年华演唱会 2024 BluRay 1080p",
            category=MediaCategory.MUSIC,   # 站点分类误标
        )]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 0
        assert r.rejected[0].reject_reason == "video_or_concert"

    def test_mv_excluded(self):
        entries = [make_entry("周杰伦")]
        cands = [make_torrent("周杰伦 MV 合集 1080p", category=MediaCategory.MUSIC)]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 0


# ── 体积过滤 ────────────────────────────────────────────────

class TestSizeFilter:
    def test_too_small_rejected(self):
        entries = [make_entry("周杰伦")]
        cands = [make_torrent("周杰伦 单曲 FLAC", size=50 * 1024**2)]
        r = match_candidates(cands, entries, min_size_bytes=200 * 1024**2)
        assert len(r.accepted) == 0
        assert r.rejected[0].reject_reason.startswith("too_small")

    def test_normal_size_passes(self):
        entries = [make_entry("周杰伦")]
        cands = [make_torrent("周杰伦 范特西 FLAC", size=400 * 1024**2)]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1

    def test_max_size_rejected_when_set(self):
        entries = [make_entry("周杰伦")]
        cands = [make_torrent("周杰伦 全集 FLAC", size=50 * 1024**3)]
        r = match_candidates(cands, entries, max_size_bytes=10 * 1024**3)
        assert r.rejected[0].reject_reason.startswith("too_large")


# ── 订阅词命中 ──────────────────────────────────────────────

class TestWatchlistMatching:
    def test_query_hit_accepted(self):
        entries = [make_entry("周杰伦")]
        cands = [make_torrent("周杰伦 - 范特西 (2001) FLAC")]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1
        assert r.accepted[0].matched_terms == ["周杰伦"]

    def test_no_hit_rejected(self):
        entries = [make_entry("周杰伦")]
        cands = [make_torrent("林俊杰 - 曹操 FLAC")]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 0
        assert r.rejected[0].reject_reason == "no_watchlist_hit"

    def test_alias_hit(self):
        entries = [make_entry("周杰伦", aliases=["Jay Chou", "周杰倫"])]
        cands = [make_torrent("Jay Chou - Fantasy FLAC")]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1
        assert "Jay Chou" in r.accepted[0].matched_terms

    def test_custom_regex(self):
        entries = [make_entry("古典", regex=r"(bach|beethoven)")]
        cands = [make_torrent("Bach Goldberg Variations FLAC")]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1

    def test_disabled_entry_not_matched(self):
        entries = [make_entry("周杰伦", enabled=False)]
        cands = [make_torrent("周杰伦 FLAC")]
        # 调用方应先过滤 enabled，此处验证传入未启用项时不崩溃
        r = match_candidates(cands, entries)
        assert isinstance(r.total, int)


# ── 置信度分级 ──────────────────────────────────────────────

class TestConfidence:
    def test_high_confidence_auto_accept(self):
        entries = [make_entry("周杰伦", min_confidence="high")]
        cands = [make_torrent("周杰伦 - 范特西 FLAC", category=MediaCategory.MUSIC)]
        r = match_candidates(cands, entries)
        assert r.accepted[0].confidence == "high"

    def test_low_confidence_goes_to_review(self):
        """medium 置信度 + high 门槛 → 待审，不自动下载。"""
        entries = [make_entry("周杰伦", min_confidence="high")]
        # 命中词但音质不明（无 FLAC 等标记）→ 达不到 high
        cands = [make_torrent("周杰伦 某专辑", category=MediaCategory.MUSIC)]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 0
        assert len(r.review) == 1

    def test_relaxed_threshold_accepts_medium(self):
        """medium 置信度（命中词 + 低音质权重）在放宽门槛后应被采纳。

        MP3 的音质权重 0.40 < 0.7，达不到 high，但 > 0 故为 medium。
        """
        entries = [make_entry("周杰伦", min_confidence="medium")]
        cands = [make_torrent("周杰伦 某专辑 MP3", category=MediaCategory.MUSIC)]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1
        assert r.accepted[0].confidence == "medium"

    def test_auto_download_disabled_goes_to_review(self):
        entries = [make_entry("周杰伦", auto_download=False)]
        cands = [make_torrent("周杰伦 范特西 FLAC")]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 0
        assert len(r.review) == 1
        assert r.review[0].reject_reason == "auto_download_disabled"

    def test_unknown_category_capped_at_medium(self):
        """UNKNOWN 类型即使精确命中也最高 medium（宁漏勿错）。"""
        entries = [make_entry("周杰伦", min_confidence="high")]
        cands = [make_torrent("周杰伦 范特西 FLAC", category=MediaCategory.UNKNOWN)]
        r = match_candidates(cands, entries)
        # high 门槛 + medium 置信 → 待审
        assert len(r.accepted) == 0
        assert len(r.review) == 1


# ── 去重准入 ────────────────────────────────────────────────

class TestDedup:
    def test_known_hash_skipped(self):
        entries = [make_entry("周杰伦")]
        t = make_torrent("周杰伦 范特西 FLAC", info_hash="deadbeef")
        r = match_candidates([t], entries, known_hashes={"deadbeef"})
        assert r.total == 0   # 已知 hash 静默跳过，不计入任何桶

    def test_duplicate_in_same_round_deduped(self):
        entries = [make_entry("周杰伦")]
        t1 = make_torrent("周杰伦 范特西 FLAC", info_hash="same")
        t2 = make_torrent("周杰伦 范特西 FLAC (副本)", info_hash="same")
        r = match_candidates([t1, t2], entries)
        assert r.total == 1

    def test_multiple_entries_best_match_wins(self):
        """一个候选命中多个订阅项时，取命中最多的，不重复派发。"""
        entries = [make_entry("周杰伦"), make_entry("范特西")]
        cands = [make_torrent("周杰伦 范特西 FLAC")]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1


# ── 连载分卷去重（v0.4 模块 C）────────────────────────────────────

def make_book(title: str, **kw) -> NormalizedTorrent:
    return make_torrent(title, category=MediaCategory.AUDIOBOOK, size=2 * 1024**3, **kw)


class TestSeriesDedup:
    def test_part_no_parsed_into_match(self):
        entries = [make_entry("三体", kind=WatchKind.AUDIOBOOK, series_id=7)]
        r = match_candidates([make_book("三体 第2部 有声书 MP3")], entries)
        assert len(r.accepted) == 1
        m = r.accepted[0]
        assert m.series_id == 7 and m.part_no == 2
        assert "part_unparsed" not in m.detail

    def test_unparsed_part_keeps_flow_and_flags_detail(self):
        entries = [make_entry("三体", kind=WatchKind.AUDIOBOOK, series_id=7)]
        r = match_candidates([make_book("三体 有声书 全集 MP3")], entries)
        assert len(r.accepted) == 1              # 解析失败不拦截
        m = r.accepted[0]
        assert m.series_id == 7 and m.part_no is None
        assert m.detail.get("part_unparsed") is True

    def test_known_part_rejected_as_duplicate(self):
        entries = [make_entry("三体", kind=WatchKind.AUDIOBOOK, series_id=7)]
        r = match_candidates(
            [make_book("三体 第2部 有声书 MP3")], entries, known_parts={7: {1, 2}},
        )
        assert len(r.accepted) == 0
        assert len(r.rejected) == 1
        assert r.rejected[0].reject_reason.startswith("duplicate_part")
        assert r.rejected[0].part_no == 2

    def test_new_part_passes_when_others_known(self):
        entries = [make_entry("三体", kind=WatchKind.AUDIOBOOK, series_id=7)]
        r = match_candidates(
            [make_book("三体 第3部 有声书 MP3")], entries, known_parts={7: {1, 2}},
        )
        assert len(r.accepted) == 1 and r.accepted[0].part_no == 3

    def test_same_part_twice_in_round_only_first_passes(self):
        """两站同时放出同一部（不同 info_hash），只放行一个。"""
        entries = [make_entry("三体", kind=WatchKind.AUDIOBOOK, series_id=7)]
        cands = [
            make_book("三体 第1部 有声书 MP3", site="hhanclub"),
            make_book("三体 第1部 有声书 M4B", site="mteam"),
        ]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1
        assert len(r.rejected) == 1
        assert r.rejected[0].reject_reason.startswith("duplicate_part")

    def test_series_pattern_from_index_used(self):
        from audio_hub.models import SeriesEntry
        entries = [make_entry("凡人", kind=WatchKind.AUDIOBOOK, series_id=3)]
        idx = {3: SeriesEntry(id=3, name="凡人", part_pattern=r"vol\.(\d+)")}
        r = match_candidates(
            [make_book("凡人修仙传 vol.12 有声书 MP3")], entries, series_index=idx,
        )
        assert r.accepted[0].part_no == 12

    def test_duplicate_part_blocks_even_review_path(self):
        """重复分卷在分流之前拦截：不应进待审队列。"""
        entries = [make_entry("三体", kind=WatchKind.AUDIOBOOK, series_id=7, auto_download=False)]
        r = match_candidates(
            [make_book("三体 第2部 有声书 MP3")], entries, known_parts={7: {2}},
        )
        assert len(r.review) == 0 and len(r.rejected) == 1

    def test_entry_without_series_untouched(self):
        entries = [make_entry("三体", kind=WatchKind.AUDIOBOOK)]
        r = match_candidates([make_book("三体 第2部 有声书 MP3")], entries, known_parts={7: {2}})
        assert len(r.accepted) == 1
        assert r.accepted[0].series_id is None and r.accepted[0].part_no is None


# ── 有声书路由 ──────────────────────────────────────────────

class TestAudiobookRouting:
    def test_audiobook_matched_by_audiobook_entry(self):
        entries = [make_entry("三体", kind=WatchKind.AUDIOBOOK)]
        cands = [make_torrent(
            "三体 有声书 全集 MP3", category=MediaCategory.AUDIOBOOK, size=5 * 1024**3,
        )]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1

    def test_music_entry_does_not_match_audiobook(self):
        """音乐订阅项不应命中有声书（类型隔离）。"""
        entries = [make_entry("三体", kind=WatchKind.MUSIC)]
        cands = [make_torrent("三体 有声书 全集", category=MediaCategory.AUDIOBOOK, size=5*1024**3)]
        r = match_candidates(cands, entries)
        # audiobook 候选只会在 book_entries 池里找，music 订阅不在池中 → 拒绝
        assert len(r.accepted) == 0


class TestAudiobookConfidenceRegression:
    """回归测试：曾有 bug 导致有声书永远达不到 high 置信度。

    bug 1: score_candidate 的 high 条件写死 category == MUSIC
    bug 2: 有声书的 MP3 被按音乐音质阶梯扣到 0.40（< 0.7 门槛）
    两者叠加 → 整条有声书链路静默卡在待审队列，无任何报错。
    """

    def test_audiobook_mp3_reaches_high(self):
        """有声书 MP3 是合格格式，high 门槛下应被自动采纳。"""
        entries = [make_entry("三体", kind=WatchKind.AUDIOBOOK, min_confidence="high")]
        cands = [make_torrent(
            "三体 有声书 全集 MP3", category=MediaCategory.AUDIOBOOK, size=5 * 1024**3,
        )]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1, "有声书应能达到 high 置信度并自动采纳"
        assert r.accepted[0].confidence == "high"

    def test_audiobook_m4b_reaches_high(self):
        entries = [make_entry("明朝那些事儿", kind=WatchKind.AUDIOBOOK, min_confidence="high")]
        cands = [make_torrent(
            "明朝那些事儿 M4B 全集", category=MediaCategory.AUDIOBOOK, size=3 * 1024**3,
        )]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1
        assert r.accepted[0].quality == "m4b"

    def test_music_mp3_still_low_quality(self):
        """音乐 MP3 仍应是低质（0.40 < 0.7），达不到 high。两套标准不可混淆。"""
        from audio_hub.pipeline.matcher import detect_audiobook_format, detect_quality
        assert detect_quality("某专辑 MP3")[1] < 0.7
        assert detect_audiobook_format("某有声书 MP3")[1] >= 0.7

    def test_alias_hit_reaches_high(self):
        """回归：曾有 bug 使 high 只认 query 本身，别名命中被降级到 medium。"""
        entries = [make_entry("周杰伦", aliases=["Jay Chou"], min_confidence="high")]
        cands = [make_torrent("Jay Chou - Fantasy FLAC", category=MediaCategory.MUSIC)]
        r = match_candidates(cands, entries)
        assert len(r.accepted) == 1, "别名命中应能达到 high 置信度"


# ── 打分 ────────────────────────────────────────────────────

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
