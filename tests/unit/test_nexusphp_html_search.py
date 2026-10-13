"""v0.6 NexusPHP 站内 HTML 搜索：URL 构建、分页、结果解析、无 Cookie 的 RSS 降级。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from audio_hub.adapters import nexusphp as nx
from audio_hub.adapters.base import AdapterError, DiscountType, MediaCategory
from audio_hub.adapters.nexusphp import NexusPhpAdapter, _has_next_page
from audio_hub.config import SiteConfig

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make(**kw) -> NexusPhpAdapter:
    base = {
        "name": "hdsky", "enabled": True, "domain": "https://hdsky.me",
        "rss_url": "https://hdsky.me/torrentrss.php?passkey=PK",
        "music_cat_id": "408,409", "audiobook_cat_id": "410", "cookie": "uid=1; pass=2",
    }
    base.update(kw)
    return NexusPhpAdapter(SiteConfig(**base))


class TestBuildSearchUrl:
    def test_music_kind_uses_all_music_cats(self):
        url = make().build_search_url("周杰伦", "music")
        assert url.startswith("https://hdsky.me/torrents.php?")
        assert "cat408=1" in url and "cat409=1" in url and "cat410" not in url
        assert "search=%E5%91%A8%E6%9D%B0%E4%BC%A6" in url
        assert "page=" not in url

    def test_none_kind_is_union(self):
        url = make().build_search_url("x", None, page=1)
        assert "cat408=1" in url and "cat410=1" in url
        assert "page=1" in url

    def test_domain_derived_from_rss_when_missing(self):
        url = make(domain="").build_search_url("x")
        assert url.startswith("https://hdsky.me/torrents.php")

    def test_no_domain_at_all_raises(self):
        adapter = NexusPhpAdapter(SiteConfig(name="hdsky", cookie="c"))
        with pytest.raises(AdapterError):
            adapter.build_search_url("x")


class TestParseSearchHtml:
    def test_rows_parsed(self):
        rows = make().parse_search_html(load("nexusphp_search_result.html"))
        assert [t.torrent_id for t in rows] == ["100001", "100002", "100003"]
        first = rows[0]
        assert first.title == "周杰伦 - 叶惠美 (2003) [FLAC 分轨]"
        assert first.size_bytes == int(412.5 * 1024 * 1024)
        assert first.seeders == 38 and first.leechers == 2
        assert first.discount is DiscountType.FREE
        assert first.category is MediaCategory.MUSIC
        assert first.site_category == "音乐 Music"
        assert first.source == "html" and first.degraded is False
        assert first.pub_date is not None and first.pub_date.year == 2024

    def test_discount_and_video_exclusion(self):
        rows = make().parse_search_html(load("nexusphp_search_result.html"))
        assert rows[1].discount is DiscountType.HALF_DOWN
        assert rows[2].category is MediaCategory.VIDEO     # 演唱会 / Blu-ray 排除优先

    def test_download_url_prefers_passkey(self):
        rows = make().parse_search_html(load("nexusphp_search_result.html"))
        assert rows[0].download_url == "https://hdsky.me/download.php?id=100001&passkey=PK"
        rows2 = make(rss_url="").parse_search_html(load("nexusphp_search_result.html"))
        assert rows2[0].download_url == "https://hdsky.me/download.php?id=100001"

    def test_info_hash_synthesized_from_torrent_id(self):
        """HTML 搜索页无 info_hash 字段：用种子 ID 合成确定性伪哈希。

        空哈希会让 create_task 的幂等查询把所有空哈希种子坍缩成同一任务
        （v0.7.6 前真实发生：14 条命中共享 1 个任务，批量下载只剩 1 个）。
        """
        rows = make().parse_search_html(load("nexusphp_search_result.html"))
        for t in rows:
            assert len(t.info_hash) == 40
            int(t.info_hash, 16)                     # 合法 hex
        again = make().parse_search_html(load("nexusphp_search_result.html"))
        assert [t.info_hash for t in rows] == [t.info_hash for t in again]   # 确定性
        assert len({t.info_hash for t in rows}) == len(rows)                 # 唯一性

    def test_has_next_page(self):
        html = load("nexusphp_search_result.html")
        assert _has_next_page(html, 1) is True
        assert _has_next_page(html, 2) is False


class TestSearchHtmlFlow:
    @respx.mock
    async def test_two_pages_max_with_delay(self, monkeypatch):
        """fixture 第 1 页有下一页链接 → 拉第 2 页；第 2 页也有链接但到达上限停止。"""
        slept: list[float] = []

        async def fake_sleep(sec):
            slept.append(sec)
        monkeypatch.setattr(nx.asyncio, "sleep", fake_sleep)

        route = respx.get(url__startswith="https://hdsky.me/torrents.php").mock(
            return_value=httpx.Response(200, text=load("nexusphp_search_result.html")))
        rows = await make().search("周杰伦", "music")
        assert route.call_count == 2
        assert len(rows) == 6
        assert slept == [nx.SEARCH_PAGE_DELAY]
        assert "page=1" in str(route.calls[1].request.url)
        assert "page=" not in str(route.calls[0].request.url)

    @respx.mock
    async def test_stops_when_no_next_page(self, monkeypatch):
        async def fake_sleep(sec):
            pass
        monkeypatch.setattr(nx.asyncio, "sleep", fake_sleep)
        html = load("nexusphp_search_result.html").replace("page=1", "page=9")
        route = respx.get(url__startswith="https://hdsky.me/torrents.php").mock(
            return_value=httpx.Response(200, text=html))
        rows = await make().search("x", "music")
        assert route.call_count == 1 and len(rows) == 3

    @respx.mock
    async def test_login_redirect_raises_auth_error(self):
        from audio_hub.adapters.base import AdapterAuthError
        respx.get(url__startswith="https://hdsky.me/torrents.php").mock(
            return_value=httpx.Response(302, headers={"location": "https://hdsky.me/login.php"}))
        with pytest.raises(AdapterAuthError):
            await make().search("x", "music")

    @respx.mock
    async def test_sends_cookie(self):
        route = respx.get(url__startswith="https://hdsky.me/torrents.php").mock(
            return_value=httpx.Response(200, text="<html><table class='torrents'></table></html>"))
        rows = await make().search("x")
        assert rows == []
        assert route.calls[0].request.headers["cookie"] == "uid=1; pass=2"


class TestRssFallback:
    @respx.mock
    async def test_without_cookie_degrades_to_rss(self):
        route = respx.get(url__startswith="https://hdsky.me/torrentrss.php").mock(
            return_value=httpx.Response(200, text=load("hdsky_rss.xml")))
        rows = await make(cookie="").search("周杰伦", "music")
        assert rows
        assert all(t.degraded is True and t.source == "rss" for t in rows)
        url = str(route.calls[0].request.url)
        assert "search=" in url and "cat408=1" in url
        # RSS 走 passkey，不带 Cookie
        assert "cookie" not in route.calls[0].request.headers

    @respx.mock
    async def test_without_cookie_empty_rss_raises(self):
        respx.get(url__startswith="https://hdsky.me/torrentrss.php").mock(
            return_value=httpx.Response(
                200, text='<?xml version="1.0"?><rss><channel></channel></rss>'))
        with pytest.raises(AdapterError):
            await make(cookie="").search("周杰伦")

    @respx.mock
    async def test_cookie_with_rss_domain_goes_html(self):
        """没填 domain 但有 rss_url：域名从 rss_url 推出，有 Cookie 就走 HTML 而非降级。"""
        adapter = NexusPhpAdapter(SiteConfig(
            name="hdsky", cookie="c", rss_url="https://hdsky.me/torrentrss.php?passkey=PK",
        ))
        respx.get(url__startswith="https://hdsky.me/torrents.php").mock(
            return_value=httpx.Response(200, text="<html></html>"))
        assert await adapter.search("x") == []
