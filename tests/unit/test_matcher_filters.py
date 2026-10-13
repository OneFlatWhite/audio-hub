"""matcher v0.6 订阅型匹配：按 sub_type 命中 + 订阅级过滤器 + reject_reason。

旧 API（match_candidates / score_candidate）由 test_matcher.py 覆盖，这里只测新入口。
"""

from __future__ import annotations

import uuid

import pytest

from audio_hub.adapters.base import DiscountType, MediaCategory, NormalizedTorrent
from audio_hub.models import SubType, WatchEntry, WatchKind
from audio_hub.pipeline import matcher
from audio_hub.pipeline.matcher import (
    DEFAULT_MIN_SIZE_BYTES,
    SubMatch,
    apply_filters,
    match_for_subscriptions,
    match_sub_type,
    match_subscription,
)
from audio_hub.pipeline.title_parser import QUALITY_WEIGHTS, parse

MB = 1024 ** 2


def make_watch(sub_type: SubType = SubType.ARTIST, query: str = "周杰伦", **kw) -> WatchEntry:
    kind = WatchKind.AUDIOBOOK if sub_type == SubType.AUDIOBOOK else WatchKind.MUSIC
    return WatchEntry(id=str(uuid.uuid4()), query=query, kind=kind, sub_type=sub_type, **kw)


def make_torrent(
    title: str,
    category: MediaCategory = MediaCategory.MUSIC,
    site: str = "hhanclub",
    size: int = 500 * MB,
    discount: DiscountType = DiscountType.UNKNOWN,
    seeders: int | None = None,
    **kw,
) -> NormalizedTorrent:
    return NormalizedTorrent(
        info_hash=uuid.uuid4().hex, title=title, title_normalized=title.lower(),
        site=site, category=category, download_url=f"https://{site}.me/dl?id=1",
        size_bytes=size, discount=discount, seeders=seeders, **kw,
    )


def test_quality_table_is_shared_with_title_parser():
    assert matcher._QUALITY_WEIGHTS is QUALITY_WEIGHTS
    assert matcher.detect_quality("WAV整轨")[0] == "wav"


# ── sub_type 命中 ─────────────────────────────────────────

class TestMatchSubType:
    def test_artist_hits_parsed_artist_or_title(self):
        w = make_watch(SubType.ARTIST, "周杰伦", artist="周杰伦", aliases=["Jay Chou"])
        t = make_torrent("周杰伦 - 叶惠美 (2003) [FLAC]")
        assert match_sub_type(w, t, parse(t.title)) == ["周杰伦"]
        t2 = make_torrent("Jay Chou - Fantasy [FLAC]")
        assert match_sub_type(w, t2, parse(t2.title)) == ["Jay Chou"]
        t3 = make_torrent("林俊杰 - 第二天堂 [FLAC]")
        assert match_sub_type(w, t3, parse(t3.title)) == []

    def test_artist_match_is_normalized(self):
        w = make_watch(SubType.ARTIST, "Taylor Swift")
        t = make_torrent("TAYLOR  SWIFT – 1989 [FLAC]")
        assert match_sub_type(w, t, parse(t.title))

    def test_album_requires_both_artist_and_album(self):
        w = make_watch(SubType.ALBUM, "叶惠美", artist="周杰伦", album="叶惠美")
        ok = make_torrent("周杰伦 - 叶惠美 (2003) [FLAC]")
        assert match_sub_type(w, ok, parse(ok.title)) == ["叶惠美", "周杰伦"]
        wrong_artist = make_torrent("某人 - 叶惠美 [FLAC]")
        assert match_sub_type(w, wrong_artist, parse(wrong_artist.title)) == []
        wrong_album = make_torrent("周杰伦 - 范特西 [FLAC]")
        assert match_sub_type(w, wrong_album, parse(wrong_album.title)) == []

    def test_album_without_artist_only_needs_album(self):
        w = make_watch(SubType.ALBUM, "Kind of Blue", album="Kind of Blue")
        t = make_torrent("Miles Davis - Kind of Blue (1959) [FLAC]")
        assert match_sub_type(w, t, parse(t.title)) == ["Kind of Blue"]

    def test_audiobook_hits_title_or_author(self):
        w = make_watch(SubType.AUDIOBOOK, "盗墓笔记", author="南派三叔")
        by_title = make_torrent("盗墓笔记 第三卷 周建龙演播 [MP3]", MediaCategory.AUDIOBOOK)
        by_author = make_torrent("南派三叔作品集 怒江之战 [MP3]", MediaCategory.AUDIOBOOK)
        other = make_torrent("鬼吹灯 第一卷 [MP3]", MediaCategory.AUDIOBOOK)
        assert match_sub_type(w, by_title, parse(by_title.title, "audiobook")) == ["盗墓笔记"]
        assert match_sub_type(w, by_author, parse(by_author.title, "audiobook")) == ["南派三叔"]
        assert match_sub_type(w, other, parse(other.title, "audiobook")) == []

    def test_keyword_uses_query_aliases_and_regex(self):
        w = make_watch(SubType.KEYWORD, "Hi-Res 合集", aliases=["hires collection"])
        t = make_torrent("VA - Hi-Res 合集 Vol.3 [FLAC 24-96]")
        assert match_sub_type(w, t, parse(t.title)) == ["Hi-Res 合集"]
        wr = make_watch(SubType.KEYWORD, "x", regex=r"^VA\s*-")
        assert match_sub_type(wr, t, parse(t.title)) == [r"^VA\s*-"]

    def test_invalid_regex_is_ignored_not_raised(self):
        w = make_watch(SubType.KEYWORD, "周杰伦", regex="([")
        t = make_torrent("周杰伦 - 叶惠美 [FLAC]")
        assert match_sub_type(w, t, parse(t.title)) == ["周杰伦"]


# ── 过滤器 ────────────────────────────────────────────────

class TestApplyFilters:
    def test_passes_with_defaults(self):
        assert apply_filters(make_watch(), make_torrent("周杰伦 - 叶惠美 [FLAC]"), "flac_16") == ""

    def test_hard_exclude_video(self):
        reason = apply_filters(make_watch(), make_torrent("周杰伦 演唱会 BluRay 1080p"), None)
        assert reason == "video_or_concert"

    def test_exclude_keywords_any_hit_rejects(self):
        w = make_watch(exclude_keywords=["Live", "演唱会"])
        assert apply_filters(w, make_torrent("周杰伦 - 地表最强 Live [FLAC]"), "flac_16").startswith("excluded_keyword(Live)")
        assert apply_filters(w, make_torrent("周杰伦 - 叶惠美 [FLAC]"), "flac_16") == ""

    def test_include_keywords_all_required(self):
        w = make_watch(include_keywords=["FLAC", "分轨"])
        assert apply_filters(w, make_torrent("周杰伦 - 叶惠美 [FLAC分轨]"), "flac_16") == ""
        assert apply_filters(w, make_torrent("周杰伦 - 叶惠美 [FLAC]"), "flac_16") == "missing_keyword(分轨)"

    @pytest.mark.parametrize("discount,ok", [
        (DiscountType.FREE, True), (DiscountType.FREE_2UP, True),
        (DiscountType.PERCENT_50, False), (DiscountType.NORMAL, False), (DiscountType.UNKNOWN, False),
    ])
    def test_free_only(self, discount, ok):
        w = make_watch(free_only=True)
        reason = apply_filters(w, make_torrent("周杰伦 - 叶惠美", discount=discount), "flac_16")
        assert (reason == "") is ok
        if not ok:
            assert reason.startswith("not_free(")

    def test_size_defaults_to_200mb_floor(self):
        w = make_watch()
        assert apply_filters(w, make_torrent("x", size=DEFAULT_MIN_SIZE_BYTES - 1), None).startswith("too_small(")
        assert apply_filters(w, make_torrent("x", size=DEFAULT_MIN_SIZE_BYTES), None) == ""

    def test_size_overrides(self):
        w = make_watch(min_size_mb=50, max_size_mb=1000)
        assert apply_filters(w, make_torrent("x", size=60 * MB), None) == ""
        assert apply_filters(w, make_torrent("x", size=40 * MB), None).startswith("too_small(")
        assert apply_filters(w, make_torrent("x", size=1500 * MB), None).startswith("too_large(")

    def test_unknown_size_is_not_rejected(self):
        assert apply_filters(make_watch(min_size_mb=500), make_torrent("x", size=0), None) == ""

    @pytest.mark.parametrize("pref,tag,title,ok", [
        ("any", "mp3", "x MP3", True),
        ("lossless_only", "mp3", "x MP3 320k", False),
        ("lossless_only", None, "x AAC", False),
        ("lossless_only", "flac_16", "x FLAC", True),
        ("lossless_only", None, "x", True),
        ("hires_only", "flac_16", "x FLAC", False),
        ("hires_only", "flac_24", "x FLAC 24-96", True),
        ("hires_only", "dsd", "x DSD64", True),
        ("hires_only", None, "x", False),
    ])
    def test_preferred_quality(self, pref, tag, title, ok):
        reason = apply_filters(make_watch(preferred_quality=pref), make_torrent(title), tag)
        assert (reason == "") is ok, reason
        if not ok:
            assert reason.startswith("quality_below_preference(")

    def test_min_seeders_only_when_known(self):
        w = make_watch(min_seeders=3)
        assert apply_filters(w, make_torrent("x", seeders=1), None) == "seeders_below_min(1<3)"
        assert apply_filters(w, make_torrent("x", seeders=3), None) == ""
        assert apply_filters(w, make_torrent("x", seeders=None), None) == ""

    def test_seeders_read_tolerantly(self):
        """2A 可能把 seeders 做成可选字段；缺属性时按未知处理。"""
        t = make_torrent("x")
        object.__setattr__(t, "seeders", None)
        assert apply_filters(make_watch(min_seeders=5), t, None) == ""


# ── 组合入口 ──────────────────────────────────────────────

class TestMatchSubscription:
    def test_unmatched_returns_matched_false(self):
        m = match_subscription(make_watch(query="林俊杰"), make_torrent("周杰伦 - 叶惠美 [FLAC]"))
        assert isinstance(m, SubMatch)
        assert m.matched is False and m.accepted is False
        assert m.album_key == "周杰伦|叶惠美"

    def test_matched_and_accepted(self):
        m = match_subscription(make_watch(), make_torrent("周杰伦 - 叶惠美 (2003) [FLAC]", seeders=5))
        assert m.matched and m.accepted
        assert m.quality_tag == "flac_16" and m.confidence == "high" and m.score > 0.5
        assert m.parsed.year == 2003 and m.seeders == 5

    def test_matched_but_filtered_keeps_reason(self):
        w = make_watch(preferred_quality="hires_only")
        m = match_subscription(w, make_torrent("周杰伦 - 叶惠美 [FLAC]"))
        assert m.matched and not m.accepted
        assert m.reject_reason.startswith("quality_below_preference(")

    def test_unknown_category_without_quality_marker_is_not_high(self):
        m = match_subscription(make_watch(), make_torrent("周杰伦 - 叶惠美", MediaCategory.UNKNOWN))
        assert m.matched and m.confidence == "low"

    def test_unknown_category_with_quality_marker_can_be_high(self):
        """站内搜索结果常无分类；订阅已限定类型，不再因 UNKNOWN 强制降级。"""
        m = match_subscription(make_watch(), make_torrent("周杰伦 - 叶惠美 [FLAC]", MediaCategory.UNKNOWN))
        assert m.matched and m.confidence == "high"

    def test_category_mismatch_rejected(self):
        w = make_watch(SubType.AUDIOBOOK, "周杰伦")
        m = match_subscription(w, make_torrent("周杰伦 - 叶惠美 [FLAC]", MediaCategory.MUSIC))
        assert m.matched and m.reject_reason == "category_mismatch(music!=audiobook)"

    def test_audiobook_mp3_is_high_confidence(self):
        w = make_watch(SubType.AUDIOBOOK, "盗墓笔记")
        m = match_subscription(w, make_torrent("盗墓笔记 全集 MP3", MediaCategory.UNKNOWN))
        assert m.matched and m.confidence == "high"

    def test_audiobook_album_key_includes_volume(self):
        w = make_watch(SubType.AUDIOBOOK, "盗墓笔记")
        m = match_subscription(w, make_torrent("盗墓笔记 第三卷 周建龙演播 [MP3]", MediaCategory.AUDIOBOOK))
        assert m.matched and m.album_key == "盗墓笔记|vol3"


class TestMatchForSubscriptions:
    def test_one_torrent_can_hit_multiple_watches(self):
        artist = make_watch(SubType.ARTIST, "周杰伦")
        album = make_watch(SubType.ALBUM, "叶惠美", artist="周杰伦", album="叶惠美")
        other = make_watch(SubType.ARTIST, "林俊杰")
        t = make_torrent("周杰伦 - 叶惠美 (2003) [FLAC]")
        out = match_for_subscriptions([t], [artist, album, other])
        assert {m.watch.id for m in out} == {artist.id, album.id}

    def test_site_restriction(self):
        w = make_watch(sites=["hdsky"])
        out = match_for_subscriptions([make_torrent("周杰伦 - 叶惠美 [FLAC]", site="hhanclub")], [w])
        assert out == []
        out = match_for_subscriptions([make_torrent("周杰伦 - 叶惠美 [FLAC]", site="hdsky")], [w])
        assert len(out) == 1

    def test_filtered_matches_are_returned_with_reason(self):
        w = make_watch(free_only=True)
        out = match_for_subscriptions([make_torrent("周杰伦 - 叶惠美 [FLAC]")], [w])
        assert len(out) == 1 and not out[0].accepted and out[0].reject_reason.startswith("not_free(")
