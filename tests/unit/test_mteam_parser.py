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
        adapter = make_adapter()
        route = respx.post("https://api.m-team.cc/api/torrent/search").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": []})
        )
        await adapter.search("test")
        req = route.calls.last.request
        assert req.headers["Authorization"] == "TEST_MTEAM_KEY"
        assert req.headers["x-api-key"] == "TEST_MTEAM_KEY"

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
