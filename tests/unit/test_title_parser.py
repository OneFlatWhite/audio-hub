"""title_parser 单元测试：覆盖 8 类命名模式 + album_key 归一化。

样本全部按真实站点（hhanclub / hdsky / audiences / mteam）常见写法编排。
断言只锁「歌手 / 专辑 / 年份 / 音质 / 卷册」这些去重真正依赖的字段，
不锁 title 内部的空格细节。
"""

from __future__ import annotations

import pytest

from audio_hub.models import ParsedTitle
from audio_hub.pipeline.title_parser import (
    QUALITY_WEIGHTS,
    album_key,
    album_key_for,
    cn_to_int,
    detect_bitrate,
    detect_quality_tag,
    extract_volume,
    normalize_key,
    parse,
)

# (title, kind, expected-subset)
SAMPLES: list[tuple[str, str, dict]] = [
    # ── 1. Artist - Album (Year) [FLAC ...] ──
    ("Adele - 25 (2015) [FLAC 24-96]", "music",
     {"artist": "Adele", "album": "25", "year": 2015, "quality_tag": "flac_24", "bitrate": "24/96"}),
    ("Taylor Swift - 1989 (Taylor's Version) (2023) [FLAC]", "music",
     {"artist": "Taylor Swift", "album": "1989 Taylor's Version", "year": 2023, "quality_tag": "flac_16"}),
    ("Daft Punk - Random Access Memories (2013) [FLAC 24bit/88.2kHz]", "music",
     {"artist": "Daft Punk", "album": "Random Access Memories", "year": 2013,
          "quality_tag": "flac_24", "bitrate": "24/88.2"}),
    ("Pink Floyd - The Dark Side of the Moon (1973) [DSD64]", "music",
     {"artist": "Pink Floyd", "album": "The Dark Side of the Moon", "year": 1973,
          "quality_tag": "dsd", "bitrate": "DSD64"}),
    ("Queen - Live at Wembley '86 (1992) [MP3 320k]", "music",
     {"artist": "Queen", "album": "Live at Wembley '86", "year": 1992, "quality_tag": "mp3", "bitrate": "320k"}),
    ("Radiohead - OK Computer OKNOTOK 1997 2017 (2017) [FLAC 24-44.1]", "music",
     {"artist": "Radiohead", "year": 2017, "quality_tag": "flac_24", "bitrate": "24/44.1"}),
    # ── 2. en dash / em dash / 全角横线 ──
    ("Norah Jones – Come Away With Me", "music",
     {"artist": "Norah Jones", "album": "Come Away With Me", "year": None}),
    ("Miles Davis — Kind of Blue [1959] [APE]", "music",
     {"artist": "Miles Davis", "album": "Kind of Blue", "year": 1959, "quality_tag": "ape"}),
    ("周杰伦－七里香", "music", {"artist": "周杰伦", "album": "七里香"}),
    ("陈奕迅-U87", "music", {"artist": "陈奕迅", "album": "U87"}),
    # ── 3. 【歌手】专辑 ──
    ("【周杰伦】范特西 [FLAC]", "music", {"artist": "周杰伦", "album": "范特西", "quality_tag": "flac_16"}),
    ("【CD】【FLAC分轨】【王菲】寓言 (2000)", "music",
     {"artist": "王菲", "album": "寓言", "year": 2000, "quality_tag": "flac_16"}),
    ("[Adele] 30 (2021) [WEB] [FLAC 24bit 44.1kHz]", "music",
     {"artist": "Adele", "album": "30", "year": 2021, "quality_tag": "flac_24", "bitrate": "24/44.1"}),
    # ── 4. 歌手《专辑》 ──
    ("周杰伦《叶惠美》2003 FLAC 分轨", "music",
     {"artist": "周杰伦", "album": "叶惠美", "year": 2003, "quality_tag": "flac_16"}),
    ("林俊杰 -《第二天堂》[WAV整轨]", "music",
     {"artist": "林俊杰", "album": "第二天堂", "quality_tag": "wav"}),
    ("【无损】孙燕姿《逆光》(2007) [APE]", "music",
     {"artist": "孙燕姿", "album": "逆光", "year": 2007, "quality_tag": "ape"}),
    ("《1989》 - Taylor Swift [FLAC]", "music",
     {"album": "1989", "quality_tag": "flac_16"}),
    # ── 5. 歌手 - 专辑 - 2020 - FLAC ──
    ("五月天 - 自传 - 2016 - FLAC", "music",
     {"artist": "五月天", "album": "自传", "year": 2016, "quality_tag": "flac_16"}),
    ("Coldplay - Parachutes - 2000 - MP3 - 320kbps", "music",
     {"artist": "Coldplay", "album": "Parachutes", "year": 2000, "quality_tag": "mp3", "bitrate": "320k"}),
    ("张学友 - 吻别 - 1993 - Remastered - WAV", "music",
     {"artist": "张学友", "album": "吻别", "year": 1993, "edition": "Remastered", "quality_tag": "wav"}),
    # ── 6. 站点前置标记 ──
    ("【CD】【FLAC分轨】张国荣 - 宠爱 (1995)", "music",
     {"artist": "张国荣", "album": "宠爱", "year": 1995, "quality_tag": "flac_16"}),
    ("[CD][ALAC] 邓紫棋 - 新的心跳 (2015)", "music",
     {"artist": "邓紫棋", "album": "新的心跳", "year": 2015, "quality_tag": "alac"}),
    ("[WEB][2022] 告五人 - 带你飞 [FLAC 24bit-48kHz]", "music",
     {"artist": "告五人", "album": "带你飞", "year": 2022, "quality_tag": "flac_24", "bitrate": "24/48"}),
    ("[SACD][DSF] Eagles - Hotel California (1976) 2.8GB", "music",
     {"artist": "Eagles", "album": "Hotel California", "year": 1976, "quality_tag": "dsd"}),
    # ── 7. 方括号年份 / 版本 ──
    ("[2003] Jay Chou - Ye Hui Mei [Deluxe Edition] [FLAC]", "music",
     {"artist": "Jay Chou", "album": "Ye Hui Mei", "year": 2003, "edition": "Deluxe Edition", "quality_tag": "flac_16"}),
    ("Beyond - 秘密警察 (豪华版) [1988] [FLAC]", "music",
     {"artist": "Beyond", "album": "秘密警察", "year": 1988, "edition": "豪华版"}),
    ("Oasis - (What's the Story) Morning Glory? (Remastered) (2014) [FLAC 24-44.1]", "music",
     {"artist": "Oasis", "year": 2014, "edition": "Remastered", "quality_tag": "flac_24"}),
    # ── 8. 有声书 ──
    ("盗墓笔记 第三卷 周建龙演播 [MP3 64k]", "audiobook",
     {"title": "盗墓笔记", "volume": 3, "quality_tag": "mp3"}),
    ("【有声书】三体 卷2 黑暗森林 主播：刘慈欣", "audiobook",
     {"volume": 2}),
    ("The Lord of the Rings Vol.1 The Fellowship of the Ring [M4B]", "audiobook",
     {"volume": 1, "artist": ""}),
    ("明朝那些事儿 第七部 [有声小说][MP3]", "audiobook",
     {"title": "明朝那些事儿", "volume": 7, "quality_tag": "mp3"}),
    ("鬼吹灯 第二十一集 艾宝良播讲", "audiobook", {"title": "鬼吹灯", "volume": 21}),
    ("Harry Potter Book 4 Goblet of Fire (Stephen Fry) [MP3 64kbps]", "audiobook",
     {"volume": 4}),
    ("[Audiobook] 平凡的世界 Part 2 [M4B]", "audiobook",
     {"title": "平凡的世界", "volume": 2}),
]


@pytest.mark.parametrize("title,kind,expected", SAMPLES, ids=[s[0][:40] for s in SAMPLES])
def test_parse_samples(title, kind, expected):
    parsed = parse(title, kind)
    assert isinstance(parsed, ParsedTitle)
    assert parsed.raw == title
    for field, want in expected.items():
        assert getattr(parsed, field) == want, f"{field}: {getattr(parsed, field)!r} != {want!r}\n{parsed}"


def test_sample_count_at_least_30():
    assert len(SAMPLES) >= 30


def test_parse_empty_and_whitespace():
    assert parse("").artist == "" and parse("").album == ""
    assert parse("   ").title == ""
    assert parse(None).raw == ""  # type: ignore[arg-type]


def test_parse_single_segment_goes_to_album_not_artist():
    p = parse("Greatest Hits Collection [FLAC]")
    assert p.artist == ""
    assert p.album == "Greatest Hits Collection"
    assert p.title == p.album


def test_parse_accepts_enum_kind():
    from audio_hub.models import WatchKind
    p = parse("盗墓笔记 第一卷", WatchKind.AUDIOBOOK)
    assert p.volume == 1 and p.title == "盗墓笔记"


def test_live_in_album_middle_is_not_stripped():
    p = parse("Queen - Live at Wembley '86 (1992)")
    assert p.album == "Live at Wembley '86"
    assert p.edition is None


def test_year_in_album_position_preserved():
    p = parse("Taylor Swift - 1989 [FLAC]")
    assert p.artist == "Taylor Swift" and p.album == "1989"
    assert p.year is None


# ── 子函数 ────────────────────────────────────────────────

def test_quality_weights_table_labels_unique_and_sorted():
    labels = [label for _, _, label in QUALITY_WEIGHTS]
    assert len(labels) == len(set(labels))
    weights = [w for _, w, _ in QUALITY_WEIGHTS]
    assert weights == sorted(weights, reverse=True)


@pytest.mark.parametrize("text,tag", [
    ("DSD256", "dsd"), ("24bit/96kHz", "flac_24"), ("Hi-Res FLAC", "flac_24"),
    ("FLAC分轨", "flac_16"), ("APE", "ape"), ("wav整轨", "wav"), ("ALAC", "alac"),
    ("MP3 320K", "mp3"), ("random words", None),
])
def test_detect_quality_tag(text, tag):
    assert detect_quality_tag(text) == tag


@pytest.mark.parametrize("text,br", [
    ("24bit/96kHz", "24/96"), ("24 bit 192 kHz", "24/192"), ("16-44.1", "16/44.1"),
    ("DSD128", "DSD128"), ("320kbps", "320k"), ("V0", "V0"), ("2015", None),
])
def test_detect_bitrate(text, br):
    assert detect_bitrate(text) == br


@pytest.mark.parametrize("text,n", [
    ("三", 3), ("十", 10), ("十二", 12), ("二十一", 21), ("两", 2), ("一百零三", 103),
    ("12", 12), ("", None), ("卷", None),
])
def test_cn_to_int(text, n):
    assert cn_to_int(text) == n


@pytest.mark.parametrize("title,n", [
    ("盗墓笔记 第三卷", 3), ("三体 卷2", 2), ("Foo Vol.12", 12), ("Foo Volume 3", 3),
    ("Foo Part 7", 7), ("明朝那些事儿 第七部", 7), ("鬼吹灯 第二十一集", 21),
    ("Harry Potter Book 4", 4), ("没有卷号的书", None),
])
def test_extract_volume(title, n):
    assert extract_volume(title) == n


# ── album_key ─────────────────────────────────────────────

def test_normalize_key_strips_version_and_punct():
    assert normalize_key("Ye Hui Mei (Deluxe Edition)") == "yehuimei"
    assert normalize_key("葉惠美 [FLAC]") == normalize_key("叶惠美")
    assert normalize_key("Ｔａｙｌｏｒ  Swift") == "taylorswift"


def test_album_key_same_album_different_wording():
    k1 = album_key("Jay Chou", "Ye Hui Mei (Deluxe Edition)")
    k2 = album_key("jay chou", "YE HUI MEI")
    k3 = album_key("Jay  Chou", "Ye-Hui-Mei [Remastered]")
    assert k1 == k2 == k3 == "jaychou|yehuimei"


def test_album_key_differs_for_different_albums():
    assert album_key("Adele", "25") != album_key("Adele", "30")


def test_album_key_empty_when_both_blank():
    assert album_key("", "") == ""
    assert album_key("", "   ") == ""
    assert album_key("", "X") == "|x"


def test_album_key_for_music_uses_artist_album():
    p = parse("周杰伦 - 叶惠美 (2003) [FLAC]")
    assert album_key_for(p, "music") == "周杰伦|叶惠美"


def test_album_key_for_music_falls_back_to_full_key():
    p = parse("Some Compilation 2020 [FLAC]")
    key = album_key_for(p, "music")
    assert key.startswith("|") and len(key) > 1


def test_album_key_for_audiobook_includes_volume():
    p1 = parse("盗墓笔记 第三卷 周建龙演播", "audiobook")
    p2 = parse("盗墓笔记 第四卷 周建龙演播", "audiobook")
    p3 = parse("盗墓笔记 周建龙演播 [MP3]", "audiobook")
    assert album_key_for(p1, "audiobook") == "盗墓笔记|vol3"
    assert album_key_for(p2, "audiobook") == "盗墓笔记|vol4"
    assert album_key_for(p3, "audiobook") == "盗墓笔记|"
    assert album_key_for(ParsedTitle(raw=""), "audiobook") == ""
