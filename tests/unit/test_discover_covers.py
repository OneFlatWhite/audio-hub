"""发现墙封面层单元测试：iTunes 命中 / 负缓存 / 异常降级 / 缓存复用 / 键校验。

全部经 respx mock，不触网。
"""

from __future__ import annotations

import json

import httpx
import respx

from audio_hub.discover.covers import cache_key, cover_for, is_valid_key, read_meta

SEARCH = "https://itunes.apple.com/search"
ART_100 = "https://is1-ssl.mzstatic.com/image/thumb/Music123/v4/abc/100x100bb.jpg"
ART_600 = "https://is1-ssl.mzstatic.com/image/thumb/Music123/v4/abc/600x600bb.jpg"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _hit_payload() -> dict:
    return {"resultCount": 1, "results": [{"artistName": "周杰伦", "collectionName": "叶惠美",
                                           "artworkUrl100": ART_100}]}


async def test_hit_fetches_and_caches(tmp_path):
    """命中：搜索取第一条 artworkUrl100，换 600x600 抓图落盘，返回键。"""
    with respx.mock:
        search = respx.get(SEARCH).mock(return_value=httpx.Response(200, json=_hit_payload()))
        art = respx.get(ART_600).mock(
            return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
        key = await cover_for("周杰伦", "叶惠美", "music", cache_dir=tmp_path, provider="itunes")
    assert key == cache_key("周杰伦", "叶惠美", "music")
    assert search.called and art.called
    # 搜索参数：entity=album / country=CN
    assert "entity=album" in str(search.calls[0].request.url)
    assert "country=CN" in str(search.calls[0].request.url)
    assert (tmp_path / f"{key}.img").read_bytes() == JPEG
    meta = read_meta(tmp_path, key)
    assert meta["hit"] is True and meta["source_url"] == ART_600
    assert meta["content_type"] == "image/jpeg"


async def test_cache_reuse_no_second_request(tmp_path):
    """磁盘缓存命中后不再发任何 HTTP 请求。"""
    with respx.mock:
        respx.get(SEARCH).mock(return_value=httpx.Response(200, json=_hit_payload()))
        art = respx.get(ART_600).mock(
            return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
        key1 = await cover_for("周杰伦", "叶惠美", "music", cache_dir=tmp_path, provider="itunes")
        assert art.call_count == 1
    # 第二次调用：不开 respx（若有外呼会真触网失败）也应拿到键
    key2 = await cover_for("周杰伦", "叶惠美", "music", cache_dir=tmp_path, provider="itunes")
    assert key2 == key1


async def test_no_result_negative_cache(tmp_path):
    """无结果：返回 None 且写负缓存，24h 内不再外呼。"""
    with respx.mock:
        search = respx.get(SEARCH).mock(
            return_value=httpx.Response(200, json={"resultCount": 0, "results": []}))
        assert await cover_for("某某冷门", "不存在的专辑", "music",
                               cache_dir=tmp_path, provider="itunes") is None
        assert search.call_count == 1
        # 负缓存期内：第二次不再发请求
        assert await cover_for("某某冷门", "不存在的专辑", "music",
                               cache_dir=tmp_path, provider="itunes") is None
        assert search.call_count == 1
    key = cache_key("某某冷门", "不存在的专辑", "music")
    meta = read_meta(tmp_path, key)
    assert meta["hit"] is False and not (tmp_path / f"{key}.img").exists()


async def test_network_error_degrades_to_none(tmp_path):
    """网络异常 / HTTP 错误 / 响应坏 JSON：静默降级 None，且不写负缓存（瞬时故障）。"""
    key = cache_key("周杰伦", "叶惠美", "music")
    with respx.mock:
        respx.get(SEARCH).mock(side_effect=httpx.ConnectError("dns boom"))
        assert await cover_for("周杰伦", "叶惠美", "music",
                               cache_dir=tmp_path, provider="itunes") is None
    with respx.mock:
        respx.get(SEARCH).mock(return_value=httpx.Response(503, text="upstream"))
        assert await cover_for("周杰伦", "叶惠美", "music",
                               cache_dir=tmp_path, provider="itunes") is None
    with respx.mock:
        respx.get(SEARCH).mock(return_value=httpx.Response(200, text="not json"))
        assert await cover_for("周杰伦", "叶惠美", "music",
                               cache_dir=tmp_path, provider="itunes") is None
    # 瞬时故障不落任何缓存（下次还会重试）
    assert not (tmp_path / f"{key}.json").exists()
    assert not (tmp_path / f"{key}.img").exists()


async def test_audiobook_entity(tmp_path):
    """有声书用 entity=audiobook。"""
    with respx.mock:
        search = respx.get(SEARCH).mock(return_value=httpx.Response(200, json=_hit_payload()))
        respx.get(ART_600).mock(
            return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
        key = await cover_for("刘慈欣", "三体", "audiobook",
                              cache_dir=tmp_path, provider="itunes")
    assert key is not None
    assert "entity=audiobook" in str(search.calls[0].request.url)


async def test_provider_none_no_request(tmp_path):
    """provider=none：直接 None，不发请求、不写缓存。"""
    assert await cover_for("周杰伦", "叶惠美", "music",
                           cache_dir=tmp_path, provider="none") is None
    assert list(tmp_path.iterdir()) == []


def test_key_validation():
    """代理键校验：仅 40 位小写 hex。"""
    assert is_valid_key("a" * 40)
    assert not is_valid_key("")
    assert not is_valid_key("a" * 39)
    assert not is_valid_key("A" * 40)          # 大写不接受
    assert not is_valid_key("../etc/passwd")
    assert not is_valid_key("https://evil.example/x")
    assert not is_valid_key("a" * 40 + "/../x")


def test_cache_key_stable_and_normalized():
    """同一专辑的键稳定且归一化（大小写 / 空格差异同键）；不同内容不同键。"""
    assert cache_key("周杰伦", "叶惠美", "music") == cache_key("周杰伦 ", " 叶惠美", "music")
    assert cache_key("周杰伦", "叶惠美", "music") != cache_key("周杰伦", "范特西", "music")
    assert cache_key("周杰伦", "叶惠美", "music") != cache_key("周杰伦", "叶惠美", "audiobook")


def test_read_meta_corrupt(tmp_path):
    """元信息损坏视为未缓存（None），不抛异常。"""
    key = "b" * 40
    (tmp_path / f"{key}.json").write_text("{bad json", encoding="utf-8")
    assert read_meta(tmp_path, key) is None
    assert read_meta(tmp_path, "c" * 40) is None
    # 正常 JSON 能读回
    (tmp_path / f"{key}.json").write_text(json.dumps({"hit": False}), encoding="utf-8")
    assert read_meta(tmp_path, key) == {"hit": False}
