"""NexusPHP 签名下载链接：海豚 2026 起 download.php 必须带 sign，否则 404。

resolve_download_url 在派发前抓详情页抠签名链接；已签名 / 非 download.php /
抠取失败 → 原样返回（回落旧行为）。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from audio_hub.adapters.base import MediaCategory, NormalizedTorrent
from audio_hub.adapters.nexusphp import NexusPhpAdapter, extract_signed_download
from audio_hub.config import SiteConfig

DETAILS = "https://hdsky.me/details.php?id=497211"
SIGNED = "https://hdsky.me/download.php?id=497211&passkey=PK&sign=ab12cd34"
UNSIGNED = "https://hdsky.me/download.php?id=497211&passkey=PK"

DETAILS_HTML = f"""<html><body>
<a href="{SIGNED}">下载</a>
<a href="https://hdsky.me/download.php?id=497211&amp;type=zip&amp;sign=zz99">打包下载</a>
</body></html>"""


def make(**kw) -> NexusPhpAdapter:
    base = {
        "name": "hdsky", "enabled": True, "domain": "https://hdsky.me",
        "rss_url": "https://hdsky.me/torrentrss.php?passkey=PK",
        "cookie": "uid=1; pass=2",
    }
    base.update(kw)
    return NexusPhpAdapter(SiteConfig(**base))


def make_torrent(download_url=UNSIGNED) -> NormalizedTorrent:
    return NormalizedTorrent(
        info_hash="a" * 40, title="某专辑 FLAC", site="hdsky",
        category=MediaCategory.MUSIC, download_url=download_url,
        torrent_id="497211", size_bytes=500 * 1024**2,
    )


class TestExtractSignedDownload:
    def test_prefers_passkey_link_over_zip(self):
        assert extract_signed_download(DETAILS_HTML, "https://hdsky.me") == SIGNED

    def test_unescapes_amp(self):
        page = f'<a href="{SIGNED.replace("&", "&amp;")}">x</a>'
        assert extract_signed_download(page, "https://hdsky.me") == SIGNED

    def test_relative_url_prefixed(self):
        page = '<a href="/download.php?id=1&passkey=PK&sign=s1">x</a>'
        assert extract_signed_download(page, "https://hdsky.me") == \
            "https://hdsky.me/download.php?id=1&passkey=PK&sign=s1"

    def test_no_signed_link_returns_none(self):
        assert extract_signed_download("<html>无链接</html>", "https://hdsky.me") is None
        assert extract_signed_download("", "https://hdsky.me") is None


class TestResolveDownloadUrl:
    @pytest.mark.asyncio
    @respx.mock
    async def test_unsigned_url_gets_signed(self):
        respx.get(DETAILS).mock(return_value=httpx.Response(200, text=DETAILS_HTML))
        url = await make().resolve_download_url(make_torrent())
        assert url == SIGNED

    @pytest.mark.asyncio
    @respx.mock
    async def test_already_signed_passthrough(self):
        route = respx.get(DETAILS)
        url = await make().resolve_download_url(make_torrent(download_url=SIGNED))
        assert url == SIGNED
        assert not route.called, "已带 sign 不该再请求详情页"

    @pytest.mark.asyncio
    @respx.mock
    async def test_details_failure_falls_back(self):
        respx.get(DETAILS).mock(return_value=httpx.Response(404, text="gone"))
        url = await make().resolve_download_url(make_torrent())
        assert url == UNSIGNED

    @pytest.mark.asyncio
    @respx.mock
    async def test_details_without_signed_link_falls_back(self):
        respx.get(DETAILS).mock(return_value=httpx.Response(200, text="<html>无</html>"))
        url = await make().resolve_download_url(make_torrent())
        assert url == UNSIGNED

    @pytest.mark.asyncio
    async def test_no_cookie_passthrough(self):
        url = await make(cookie="").resolve_download_url(make_torrent())
        assert url == UNSIGNED

    @pytest.mark.asyncio
    @respx.mock
    async def test_string_input_extracts_id(self):
        """review 采纳路径只传 URL 字符串：从 id= 参数推出详情页地址。"""
        respx.get(DETAILS).mock(return_value=httpx.Response(200, text=DETAILS_HTML))
        url = await make().resolve_download_url(UNSIGNED)
        assert url == SIGNED

    @pytest.mark.asyncio
    async def test_non_download_url_passthrough(self):
        url = await make().resolve_download_url("https://hdsky.me/other.php?id=1")
        assert url == "https://hdsky.me/other.php?id=1"
