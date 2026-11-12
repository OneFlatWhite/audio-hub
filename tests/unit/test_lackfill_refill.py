"""Re0 回填分享链接的严格校验与去重。"""

from __future__ import annotations

import importlib

import pytest


def _refill():
    return importlib.import_module("audio_hub.lackfill.refill")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://115.com/s/abc123?password=wd40", "https://115.com/s/abc123?password=wd40"),
        ("https://115cdn.com/s/ABC999", "https://115.com/s/ABC999"),
        ("https://anxia.com/s/abc123#wd40", "https://115.com/s/abc123?password=wd40"),
        ("http://www.115cdn.net/s/abc123/", "https://115.com/s/abc123"),
    ],
)
def test_canonicalize_supported_share_urls(raw, expected):
    assert _refill().canonicalize_share_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://evil.example/?next=https://115.com/s/abc123",
        "https://115.com.evil.example/s/abc123",
        "javascript:alert(1)",
        "https://user:pass@115.com/s/abc123",
        "https://115.com/s/abc123?password=bad-code",
        "https://115.com/s/abc123?password=wd40#other",
        "not a url",
        "",
    ],
)
def test_rejects_malformed_or_disguised_urls(raw):
    with pytest.raises(ValueError):
        _refill().canonicalize_share_url(raw)


def test_normalize_share_links_deduplicates_without_flattening_order():
    raw = (
        "https://115cdn.com/s/abc123\n"
        "https://115.com/s/abc123\n"
        "https://115.com/s/def456?password=wd40"
    )
    assert _refill().normalize_share_links(raw) == [
        "https://115.com/s/abc123",
        "https://115.com/s/def456?password=wd40",
    ]


def test_normalize_share_links_rejects_more_than_three_distinct_links():
    links = [f"https://115.com/s/code{i}" for i in range(4)]
    with pytest.raises(ValueError, match="最多 3 条"):
        _refill().normalize_share_links(links)


def test_normalize_share_links_rejects_empty_input():
    with pytest.raises(ValueError, match="为空"):
        _refill().normalize_share_links(["", "\n"])


def test_classify_ed2k_strips_invisible_characters_and_uses_hash_identity():
    raw = (
        "ed2k://|file|善良\u200b妈妈的朋友 (2017) - 720p.HDRip|4021117121|"
        "6A8A258330DB0DA10B020623AE256150|/"
    )
    kind, normalized = _refill().classify_resource_link(raw)
    assert kind == "ed2k"
    assert normalized == (
        "ed2k://|file|善良妈妈的朋友 (2017) - 720p.HDRip|4021117121|"
        "6A8A258330DB0DA10B020623AE256150|/"
    )
    assert _refill().resource_identity(normalized) == "6A8A258330DB0DA10B020623AE256150"


def test_classify_share_and_reject_bad_ed2k_hash():
    kind, normalized = _refill().classify_resource_link("https://115cdn.com/s/abc123")
    assert kind == "share"
    assert normalized == "https://115.com/s/abc123"
    with pytest.raises(ValueError):
        _refill().classify_resource_link("ed2k://|file|bad|123|not-a-hash|/")


def test_ed2k_rejects_path_traversal_and_separators():
    for name in ("../../evil", "nested/path.mkv", "nested\\path.mkv"):
        with pytest.raises(ValueError):
            _refill().classify_resource_link(
                f"ed2k://|file|{name}|123|6A8A258330DB0DA10B020623AE256150|/"
            )
