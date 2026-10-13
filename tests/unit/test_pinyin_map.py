"""拼音歌手名映射测试：订阅中文名 → 拼音索引 → 卡片还原。"""

from __future__ import annotations

from types import SimpleNamespace

from audio_hub.discover.pinyin_map import apply_artist_map, build_pinyin_index
from audio_hub.models import SubType, WatchEntry, WatchKind


def _entry(name: str, *, aliases=(), sub_type=SubType.ARTIST, kind=WatchKind.MUSIC,
           artist: str = "", enabled=True) -> WatchEntry:
    import uuid

    return WatchEntry(
        id=str(uuid.uuid4()), query=name, kind=kind, sub_type=sub_type,
        artist=artist, aliases=list(aliases), enabled=enabled,
    )


def _card(artist="", title="") -> SimpleNamespace:
    return SimpleNamespace(artist=artist, title=title)


def test_index_builds_pinyin_keys():
    idx = build_pinyin_index([_entry("邓紫棋", aliases=["G.E.M"])], "music")
    assert idx.get("dengziqi") == "邓紫棋"
    # 纯英文候选不进索引（无需映射）
    assert "gem" not in idx


def test_index_skips_disabled_and_other_kind():
    idx = build_pinyin_index([_entry("邓紫棋", enabled=False)], "music")
    assert idx == {}
    idx = build_pinyin_index([_entry("三体有声书", kind=WatchKind.AUDIOBOOK)], "music")
    assert idx == {}


def test_apply_replaces_artist_and_exact_title():
    idx = build_pinyin_index([_entry("邓紫棋")], "music")
    cards = [
        _card(artist="deng zi qi", title="x"),
        _card(artist="Dengziqi", title="dengziqi"),   # 大小写 / 连写都命中
        _card(artist="Someone Else", title="别的"),
    ]
    n = apply_artist_map(cards, idx)
    assert cards[0].artist == "邓紫棋" and cards[1].artist == "邓紫棋"
    assert cards[1].title == "邓紫棋"                  # 全串精确匹配的 title 也还原
    assert cards[2].artist == "Someone Else"           # 不相关不动
    assert n == 3


def test_apply_noop_without_index():
    cards = [_card(artist="qing lang")]
    assert apply_artist_map(cards, {}) == 0
    assert cards[0].artist == "qing lang"
