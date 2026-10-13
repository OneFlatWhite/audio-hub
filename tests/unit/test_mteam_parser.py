"""M-Team（馒头）适配器单元测试：JSON 解析、分类映射、域名 failover、认证错误。

测试数据：tests/fixtures/mteam_search.json（合成样本，字段名据 SDK/MCP 文档推断，
阶段 4 需用真实 API 响应校准）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from audio_hub.adapters.base import DiscountType, MediaCategory
from audio_hub.adapters.mteam import MTeamAdapter
from audio_hub.config import SiteConfig

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def make_adapter(**kw) -> MTeamAdapter:
    cfg = SiteConfig(
        name="mteam",
        enabled=True,
        search_url="https://api.m-team.cc/api/torrent/search",
        rss_url="https://rss.m-team.cc/api/rss/fetch?dl=1&pageSize=50",
        api_key="TEST_MTEAM_KEY",
        max_rows=50,
        **kw,
    )
    return MTeamAdapter(cfg)


def load_search_fixture() -> dict:
    return json.loads((FIXTURES / "mteam_search.json").read_text(encoding="utf-8"))


# ── 搜索响应解析 ────────────────────────────────────────────

class TestSearchParsing:
    def test_parses_all_items(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        assert len(results) == 4

    def test_music_category_mapped(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        jay = next(r for r in results if "七里香" in r.title)
        assert jay.category == MediaCategory.MUSIC

    def test_audiobook_category_mapped(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        book = next(r for r in results if "三体" in r.title)
        assert book.category == MediaCategory.AUDIOBOOK

    def test_movie_mapped_to_video(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        movie = next(r for r in results if "Oppenheimer" in r.title)
        assert movie.category == MediaCategory.VIDEO

    def test_size_string_coerced(self):
        """size 为字符串时应转为 int。"""
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        jay = next(r for r in results if "七里香" in r.title)
        assert jay.size_bytes == 734003200
        assert isinstance(jay.size_bytes, int)

    def test_size_int_preserved(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        taylor = next(r for r in results if "Taylor" in r.title)
        assert taylor.size_bytes == 2147483648

    def test_discount_enum_mapped(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        jay = next(r for r in results if "七里香" in r.title)
        assert jay.discount == DiscountType.FREE
        book = next(r for r in results if "三体" in r.title)
        assert book.discount == DiscountType.PERCENT_50

    def test_labels_as_format_tags(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        movie = next(r for r in results if "Oppenheimer" in r.title)
        assert "4k" in movie.format_tags
        assert "DoVi" in movie.format_tags

    def test_seeders_leechers_parsed(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        jay = next(r for r in results if "七里香" in r.title)
        assert jay.seeders == 12
        assert jay.leechers == 1

    def test_info_hash_parsed(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        assert results[0].info_hash == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    def test_download_url_constructed_when_empty(self):
        """馒头 downloadUrl 为空时应构造 genDlToken 端点。"""
        adapter = make_adapter()
        adapter._active_base = "https://api.m-team.cc"
        results = adapter._parse_search_response(load_search_fixture())
        jay = next(r for r in results if "七里香" in r.title)
        assert "genDlToken" in jay.download_url
        assert jay.torrent_id in jay.download_url

    def test_date_parsed(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        assert results[0].pub_date is not None
        assert results[0].pub_date.year == 2026

    def test_normalized_title_populated(self):
        """归一化应：去掉【】标记、转小写（中文无大小写，故断言标记移除与非空）。"""
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        jay = next(r for r in results if "七里香" in r.title)
        assert jay.title_normalized                     # 非空
        assert jay.title_normalized != jay.title        # 已被处理
        assert "[FLAC 分轨]" not in jay.title_normalized  # 方括号标记已移除
        assert "周杰伦" in jay.title_normalized           # 中文内容保留

    def test_english_title_lowercased(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        taylor = next(r for r in results if "Taylor" in r.title)
        assert "taylor swift" in taylor.title_normalized

    def test_cover_url_multi_field_fallback(self):
        """v0.7：image/cover/poster 多字段兜底（字符串或数组），取不到为空串，绝不报错。"""
        adapter = make_adapter()
        base = {"name": "周杰伦 - 叶惠美 [FLAC]", "size": 100, "id": 1}
        for raw, expected in [
            ({**base, "image": "https://img.example/a.jpg"}, "https://img.example/a.jpg"),
            ({**base, "image": ["https://img.example/b.jpg"]}, "https://img.example/b.jpg"),
            ({**base, "cover": "https://img.example/c.jpg"}, "https://img.example/c.jpg"),
            ({**base, "poster": "https://img.example/d.jpg"}, "https://img.example/d.jpg"),
            ({**base, "image": ["", "https://img.example/e.jpg"]}, "https://img.example/e.jpg"),
            ({**base, "image": 123, "cover": None}, ""),
            (base, ""),
        ]:
            t = adapter._parse_torrent(raw)
            assert t is not None and t.cover_url == expected, raw

    def test_cover_url_empty_by_default(self):
        adapter = make_adapter()
        results = adapter._parse_search_response(load_search_fixture())
        assert all(r.cover_url == "" for r in results)

    def test_empty_response_returns_empty(self):
        adapter = make_adapter()
        assert adapter._parse_search_response({}) == []
        assert adapter._parse_search_response({"code": 0, "data": None}) == []

    def test_list_payload_variant(self):
        """兼容 data 直接是 list 的响应变体。"""
        adapter = make_adapter()
        payload = {"code": 0, "data": [{"name": "Test FLAC", "id": "1", "size": 100}]}
        results = adapter._parse_search_response(payload)
        assert len(results) == 1


# ── HTTP 与认证 ─────────────────────────────────────────────

class TestHttpAndAuth:
    @pytest.mark.asyncio
    @respx.mock
    async def test_search_success(self):
        adapter = make_adapter()
        route = respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(200, json=load_search_fixture())
        )
        results = await adapter.search("周杰伦")
        assert route.called
        assert len(results) == 4

    @pytest.mark.asyncio
    @respx.mock
    async def test_auth_header_sent(self):
        """只发 x-api-key：并发 Authorization 会被馒头拒为「無效的請求」（2026-10 实测）。"""
        adapter = make_adapter()
        route = respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": []})
        )
        await adapter.search("test")
        req = route.calls.last.request
        assert req.headers["x-api-key"] == "TEST_MTEAM_KEY"
        assert "Authorization" not in req.headers

    @pytest.mark.asyncio
    @respx.mock
    async def test_string_zero_code_accepted(self):
        """馒头部分端点返回 code="0"（字符串），不应被当成业务错误。"""
        adapter = make_adapter()
        respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(200, json={"code": "0", "data": []})
        )
        assert await adapter.search("test") == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_raises_auth_error(self):
        from audio_hub.adapters.base import AdapterAuthError
        adapter = make_adapter()
        respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(401)
        )
        with pytest.raises(AdapterAuthError):
            await adapter.search("test")

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_raises_rate_limited(self):
        from audio_hub.adapters.base import AdapterRateLimited
        adapter = make_adapter()
        respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(429)
        )
        with pytest.raises(AdapterRateLimited):
            await adapter.search("test")

    @pytest.mark.asyncio
    @respx.mock
    async def test_business_error_code_raises(self):
        """API 返回 code != 0 时应抛错。"""
        from audio_hub.adapters.base import AdapterError
        adapter = make_adapter()
        respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(200, json={"code": 4001, "message": "参数错误"})
        )
        with pytest.raises(AdapterError, match="业务错误"):
            await adapter.search("test")

    def test_missing_api_key_raises(self):
        from audio_hub.adapters.base import AdapterError
        with pytest.raises(AdapterError, match="api_key"):
            MTeamAdapter(SiteConfig(name="mteam", api_key="", search_url="https://api.m-team.cc/api/torrent/search"))


# ── 域名 failover ───────────────────────────────────────────

class TestDomainFailover:
    def test_derive_base_urls_from_search_url(self):
        adapter = make_adapter()
        assert adapter._base_urls[0] == "https://api.m-team.cc"
        assert "https://api.m-team.io" in adapter._base_urls

    @pytest.mark.asyncio
    @respx.mock
    async def test_failover_to_secondary_domain(self):
        """主域名超时时应切换备用域名。"""
        adapter = make_adapter()
        respx.post("https://api.m-team.cc/api/torrent/search").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        respx.post("https://api.m-team.io/api/torrent/search").mock(
            return_value=httpx.Response(200, json=load_search_fixture())
        )
        results = await adapter.search("test")
        assert len(results) == 4
        assert adapter._active_base == "https://api.m-team.io"

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_domains_fail_raises(self):
        from audio_hub.adapters.base import AdapterError
        adapter = make_adapter()
        respx.post("https://api.m-team.cc/api/torrent/search").mock(
            side_effect=httpx.ConnectError("fail")
        )
        respx.post("https://api.m-team.io/api/torrent/search").mock(
            side_effect=httpx.ConnectError("fail")
        )
        with pytest.raises(AdapterError, match="所有域名均不可用"):
            await adapter.search("test")

    @pytest.mark.asyncio
    @respx.mock
    async def test_cached_active_base_preferred(self):
        """上次成功的域名应优先使用。"""
        adapter = make_adapter()
        adapter._active_base = "https://api.m-team.io"
        route_io = respx.post("https://api.m-team.io/api/torrent/search").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": []})
        )
        await adapter.search("test")
        assert route_io.called


# ── 标题解析兜底 ────────────────────────────────────────────

class TestParseTitleFallback:
    def test_audiobook_keyword(self):
        adapter = make_adapter()
        assert adapter.parse_title("某有声书 全集 MP3")["category"] == MediaCategory.AUDIOBOOK

    def test_video_keyword(self):
        adapter = make_adapter()
        assert adapter.parse_title("Movie 2023 2160p BluRay")["category"] == MediaCategory.VIDEO

    def test_music_keyword(self):
        adapter = make_adapter()
        assert adapter.parse_title("Album FLAC 无损")["category"] == MediaCategory.MUSIC

    def test_unknown(self):
        adapter = make_adapter()
        assert adapter.parse_title("some random title")["category"] == MediaCategory.UNKNOWN


# ── 真实响应形态（2026-10 实测）──────────────────────────────

class TestRealResponseShape:
    """实测：category 是字符串 ID；seeders/discount 嵌在 status 子对象；封面字段是 imageList。"""

    @staticmethod
    def _item() -> dict:
        return {
            "id": "1269467",
            "name": "Dou Luo Da Lu Zhi Zhui Xu 2021 Complete WEB-DL M4A",
            "category": "442",
            "size": "46572298654",
            "imageList": ["https://img.example/cover.jpg"],
            "infoHash": "ab" * 20,
            "createdDate": "2026-10-12 20:43:27",
            "status": {"discount": "FREE", "seeders": "16", "leechers": "1"},
        }

    def test_category_string_id_mapped(self):
        t = make_adapter()._parse_torrent(self._item())
        assert t.category == MediaCategory.AUDIOBOOK
        assert t.site_category == "有声书"

    def test_status_nested_fields(self):
        t = make_adapter()._parse_torrent(self._item())
        assert t.seeders == 16 and t.leechers == 1
        assert t.discount is DiscountType.FREE

    def test_cover_from_image_list(self):
        t = make_adapter()._parse_torrent(self._item())
        assert t.cover_url == "https://img.example/cover.jpg"

    def test_music_and_mv_ids_mapped(self):
        item = self._item()
        item["category"] = "434"
        assert make_adapter()._parse_torrent(item).category == MediaCategory.MUSIC
        item["category"] = "406"
        assert make_adapter()._parse_torrent(item).category == MediaCategory.VIDEO

    def test_real_infohash_passthrough(self):
        t = make_adapter()._parse_torrent(self._item())
        assert t.info_hash == "ab" * 20

    def test_null_infohash_synthesized_from_id(self):
        """实测 search 响应 infoHash=null：用种子 ID 合成稳定伪哈希。

        空哈希会让 create_task 幂等把不同种子坍缩成同一任务（v0.7.6 修复）。
        """
        item = self._item()
        item["infoHash"] = None
        t = make_adapter()._parse_torrent(item)
        assert len(t.info_hash) == 40
        int(t.info_hash, 16)                         # 合法 hex
        # 确定性：同 id 必同哈希（去重/幂等依赖）；不同 id 不同哈希
        same = self._item()
        same["infoHash"] = None
        assert make_adapter()._parse_torrent(same).info_hash == t.info_hash
        other = self._item()
        other.update({"infoHash": None, "id": "999"})
        assert make_adapter()._parse_torrent(other).info_hash != t.info_hash
        # 键缺失（不只是 null）也要兜底
        missing = self._item()
        del missing["infoHash"]
        assert make_adapter()._parse_torrent(missing).info_hash == t.info_hash


class TestSearchKindMapping:
    @pytest.mark.asyncio
    @respx.mock
    async def test_audiobook_kind_uses_normal_mode_plus_category(self):
        """馒头无 audiobook 模式：有声书 = mode=normal + categories=[442]（或配置值）。"""
        adapter = make_adapter()
        route = respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": []})
        )
        await adapter.search("", kind="audiobook")
        body = json.loads(route.calls.last.request.content)
        assert body["mode"] == "normal" and body["categories"] == ["442"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_kind_merges_music_and_audiobook(self):
        """all = 音乐 + 有声书各搜一遍（不带 mode 的全站最新几乎全是影视，两类都捞不到）。"""
        adapter = make_adapter()
        route = respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": []})
        )
        await adapter.search("", kind="all")
        bodies = [json.loads(c.request.content) for c in route.calls]
        assert len(bodies) == 2
        assert {b.get("mode") for b in bodies} == {"music", "normal"}
        normal = next(b for b in bodies if b.get("mode") == "normal")
        assert normal["categories"] == ["442"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_kind_dedupes_overlap(self):
        """两路搜索结果有重叠时按 info_hash/torrent_id/title 去重。"""
        item = {
            "name": "周杰伦 - 最伟大的作品 (2022) FLAC",
            "category": "110",
            "size": "465722986",
            "id": "12345",
            "infoHash": "ab" * 20,
            "status": {"seeders": "3", "leechers": "0"},
        }
        adapter = make_adapter()
        respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"data": [item]}})
        )
        results = await adapter.search("", kind="all")
        assert len(results) == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_default_kind_is_music(self):
        adapter = make_adapter()
        route = respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": []})
        )
        await adapter.search("周杰伦")
        body = json.loads(route.calls.last.request.content)
        assert body["mode"] == "music"


class TestFetchRssPrimaryPath:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_rss_uses_search_empty_keyword(self):
        """最新上架主路径 = search 空关键词（RSS 端点需单独 RSS key，仅兜底）。"""
        adapter = make_adapter()
        route = respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"data": []}})
        )
        assert await adapter.fetch_rss(kind="all") == []
        assert json.loads(route.calls.last.request.content)["keyword"] == ""

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_rss_falls_back_to_rss_endpoint(self):
        """search 故障且配置了 rss_url 时退回 RSS 端点；RSS 业务码非 0 应报错。"""
        from audio_hub.adapters.base import AdapterError
        adapter = make_adapter()
        respx.post("https://api.m-team.cc/api/torrent/search").mock(
            side_effect=httpx.ConnectError("down")
        )
        respx.post("https://api.m-team.io/api/torrent/search").mock(
            side_effect=httpx.ConnectError("down")
        )
        respx.get("https://rss.m-team.cc/api/rss/fetch").mock(
            return_value=httpx.Response(200, json={"code": 1, "message": "禁止訪問"},
                                        headers={"content-type": "application/json"})
        )
        with pytest.raises(AdapterError, match="RSS 端点拒绝"):
            await adapter.fetch_rss()
