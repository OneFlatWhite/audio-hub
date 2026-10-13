"""拼音歌手名映射：把发现墙里的拼音艺人名还原成已订阅的中文名。

背景：部分站点 RSS 的标题里艺人是拼音（qing lang / dou zhi wen），
直接展示很难认。通用「拼音→汉字」不存在（同音字歧义），但**用户订阅里
本来就有中文名**——把订阅的歌手 / 别名生成拼音索引，反查即可精准还原。

规则：
- 索引只收包含汉字的候选名（query / artist / aliases）；
- pinyin 键取无声调连写小写（''join(lazy_pinyin(name))）；
- 应用时 artist 与 title 都做**全串精确匹配**才替换（部分匹配误伤率高）；
  只改显示，不动种子数据与去重键。
"""

from __future__ import annotations

import re

from pypinyin import lazy_pinyin

from ..models import SubType, WatchEntry


def _pinyin_key(name: str) -> str:
    """任意显示串 → 归一拼音键：去声调连写小写，并剥掉字母数字以外的字符
    （"邓紫棋" / "deng zi qi" / "Dengziqi" 归一为同一个键）。"""
    py = "".join(lazy_pinyin(name or "")).lower()
    return re.sub(r"[^a-z0-9]", "", py)


def _has_hanzi(name: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in name or "")


def build_pinyin_index(entries: list[WatchEntry], kind: str) -> dict[str, str]:
    """启用的订阅（含别名）→ {拼音键: 中文名}。同名先到先得（订阅即用户意志）。"""
    index: dict[str, str] = {}
    for e in entries:
        if not e.enabled or e.kind.value != kind:
            continue
        names: list[str] = []
        if e.sub_type == SubType.ARTIST:
            names.append(e.artist or e.query)
        elif e.sub_type == SubType.ALBUM and e.artist:
            names.append(e.artist)
        names.extend(e.aliases or [])
        for name in names:
            name = (name or "").strip()
            if not name or not _has_hanzi(name):
                continue
            py = _pinyin_key(name)
            if py:
                index.setdefault(py, name)
    return index


def apply_artist_map(cards: list, index: dict[str, str]) -> int:
    """原地替换卡片上的拼音 artist / title（全串精确匹配）。返回替换次数。"""
    if not index:
        return 0
    replaced = 0
    for card in cards:
        for field in ("artist", "title"):
            value = (getattr(card, field, "") or "").strip()
            if not value:
                continue
            hit = index.get(_pinyin_key(value))
            if hit and hit != value:
                setattr(card, field, hit)
                replaced += 1
    return replaced
