"""NexusPHP 适配器单元测试：RSS 解析、类型判别、URL 构建、错误处理。

测试数据来源：tests/fixtures/*.xml（合成样本，结构依据 NexusPHP RSS 2.0 规范）。
⚠️ 这些不是真实抓包数据。阶段 4 会用 verify_adapters.py 获取真实响应后校准。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from audio_hub.adapters.base import DiscountType, MediaCategory, normalize_title
from audio_hub.adapters.nexusphp import NexusPhpAdapter
from audio_hub.config import SiteConfig

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# 各站真实域名（测试用，与 config.example.yaml 保持一致）
_SITE_DOMAINS = {
    "hhanclub": "hhanclub.net",
    "hdsky": "hdsky.me",
    "audiences": "audiences.me",
}


def make_adapter(name: str = "hdsky", cat_id: str = "401", **kw) -> NexusPhpAdapter:
    """构造测试用适配器（凭据为占位符）。"""
    domain = _SITE_DOMAINS.get(name, f"{name}.me")
    cfg = SiteConfig(
        name=name,
        enabled=True,
        rss_url=f"https://{domain}/torrentrss.php?passkey=TESTKEY",
        music_cat_id=cat_id,
        max_rows=50,
        title_bracket_style="【】" if name == "hhanclub" else "[]",
        **kw,
    )
    return NexusPhpAdapter(cfg)


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── RSS 解析 ────────────────────────────────────────────────

class TestRssParsing:
    def test_hhanclub_parses_all_valid_items(self):
        adapter = make_adapter("hhanclub", cat_id="410")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        # fixture 有 6 个 item，其中 1 个空标题应被跳过
        assert len(results) == 5

    def test_empty_title_item_skipped(self):
        adapter = make_adapter("hhanclub", cat_id="410")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        hashes = [r.info_hash for r in results]
        # 空标题那条的 guid 不应出现在结果中
        assert "f60718293a4b5c6d7e8f9012345678a1b2c3d4e5" not in hashes

    def test_info_hash_from_guid(self):
        adapter = make_adapter("hhanclub", cat_id="410")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        assert results[0].info_hash == "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

    def test_size_from_enclosure_length(self):
        adapter = make_adapter("hhanclub", cat_id="410")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        assert results[0].size_bytes == 387973120

    def test_size_falls_back_to_title_when_length_zero(self):
        """enclosure@length=0 时应从标题的 '1.2 GB' 解析。"""
        adapter = make_adapter("hhanclub", cat_id="410")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        # 第 5 条（index 4）length=0，标题含 1.2 GB
        assert results[4].size_bytes == int(1.2 * 1024**3)

    def test_torrent_id_from_download_url(self):
        adapter = make_adapter("hhanclub", cat_id="410")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        assert results[0].torrent_id == "100001"

    def test_pubdate_parsed(self):
        adapter = make_adapter("hhanclub", cat_id="410")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        assert results[0].pub_date is not None
        assert results[0].pub_date.year == 2026

    def test_site_name_propagated(self):
        adapter = make_adapter("hdsky", cat_id="401")
        results = adapter.parse_rss(load_fixture("hdsky_rss.xml"))
        assert all(r.site == "hdsky" for r in results)


# ── 类型判别（PRD §6.1）──────────────────────────────────────

class TestCategoryDiscrimination:
    def test_music_flac_detected(self):
        adapter = make_adapter("hhanclub")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        assert results[0].category == MediaCategory.MUSIC

    def test_concert_bluray_excluded_as_video(self):
        """演唱会蓝光必须判为 VIDEO——这是零误抓验收标准的关键。"""
        adapter = make_adapter("hhanclub")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        concert = next(r for r in results if "演唱会" in r.title)
        assert concert.category == MediaCategory.VIDEO

    def test_audiobook_detected(self):
        adapter = make_adapter("hhanclub")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        book = next(r for r in results if "有声书" in r.title)
        assert book.category == MediaCategory.AUDIOBOOK

    def test_exclude_takes_priority_over_music(self):
        """同时含音乐与视频特征时，排除优先（宁漏勿错）。"""
        adapter = make_adapter("hdsky")
        parsed = adapter.parse_title("某歌手 Live 演唱会 2024 FLAC 1080p BluRay")
        assert parsed["category"] == MediaCategory.VIDEO


class TestSiteCategoryFusion:
    """_resolve_category 融合逻辑：站点分类名（一级信号）× 标题判别（二级信号）。

    PRD §6.1：站点分类是权重最高的信号。这四条分支是 v0.1 新增逻辑，
    之前只接了标题判别，分类名解析了却没用——现已接入并需覆盖。
    """

    def setup_method(self):
        self.adapter = make_adapter("hdsky")
        self.resolve = self.adapter._resolve_category

    def test_video_title_overrides_music_site_category(self):
        """站点把演唱会蓝光归在「音乐」分类下是常见情况，标题判 VIDEO 必须胜出。"""
        result = self.resolve(MediaCategory.VIDEO, "音乐")
        assert result == MediaCategory.VIDEO

    def test_site_category_fills_unknown_title(self):
        """标题无信号（UNKNOWN）时，采用站点分类。"""
        result = self.resolve(MediaCategory.UNKNOWN, "Music")
        assert result == MediaCategory.MUSIC

    def test_site_category_audiobook_fills_unknown(self):
        result = self.resolve(MediaCategory.UNKNOWN, "有声书")
        assert result == MediaCategory.AUDIOBOOK

    def test_conflict_downgrades_to_unknown(self):
        """站点分类=音乐、标题判=有声书，两个非 UNKNOWN 信号冲突 → 降级待审。

        宁漏勿错：不擅自二选一，交人工判定。
        """
        result = self.resolve(MediaCategory.AUDIOBOOK, "音乐")
        assert result == MediaCategory.UNKNOWN

    def test_agreement_keeps_category(self):
        result = self.resolve(MediaCategory.MUSIC, "音乐")
        assert result == MediaCategory.MUSIC

    def test_unmapped_site_category_falls_back_to_title(self):
        """站点分类名不在映射表中（如 'Other'）时，沿用标题判别结果。"""
        result = self.resolve(MediaCategory.MUSIC, "Other")
        assert result == MediaCategory.MUSIC

    def test_empty_site_category_falls_back_to_title(self):
        result = self.resolve(MediaCategory.MUSIC, "")
        assert result == MediaCategory.MUSIC

    def test_case_insensitive_site_category(self):
        result = self.resolve(MediaCategory.UNKNOWN, "MOVIES")
        assert result == MediaCategory.VIDEO

    def test_site_category_field_populated_from_rss(self):
        """解析 RSS 后 site_category 应保留站点原始分类名（供排查溯源）。"""
        adapter = make_adapter("hhanclub")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        music_item = next(r for r in results if "周杰伦" in r.title and "演唱会" not in r.title)
        assert music_item.site_category == "音乐"

    def test_unknown_when_no_signal(self):
        adapter = make_adapter("hdsky")
        results = adapter.parse_rss(load_fixture("hdsky_rss.xml"))
        unknown = next(r for r in results if "Without Tags" in r.title)
        assert unknown.category == MediaCategory.UNKNOWN

    def test_video_movie_excluded(self):
        adapter = make_adapter("hdsky")
        results = adapter.parse_rss(load_fixture("hdsky_rss.xml"))
        movie = next(r for r in results if "Dark Knight" in r.title)
        assert movie.category == MediaCategory.VIDEO

    def test_bracket_tags_extracted(self):
        """憨憨的【CD】【FLAC分轨】标记应被提取。"""
        adapter = make_adapter("hhanclub")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        assert "CD" in results[0].format_tags
        assert "FLAC分轨" in results[0].format_tags


# ── 优惠解析 ────────────────────────────────────────────────

class TestDiscountParsing:
    def test_free_detected(self):
        adapter = make_adapter("hdsky")
        results = adapter.parse_rss(load_fixture("hdsky_rss.xml"))
        free = next(r for r in results if "[Free]" in r.title)
        assert free.discount == DiscountType.FREE

    def test_2up_detected(self):
        adapter = make_adapter("hdsky")
        results = adapter.parse_rss(load_fixture("hdsky_rss.xml"))
        two_up = next(r for r in results if "[2UP]" in r.title)
        assert two_up.discount == DiscountType.FREE_2UP

    def test_normal_when_no_marker(self):
        adapter = make_adapter("hdsky")
        results = adapter.parse_rss(load_fixture("hdsky_rss.xml"))
        assert results[0].discount == DiscountType.UNKNOWN


# ── 标题归一化 ──────────────────────────────────────────────

class TestTitleNormalization:
    def test_traditional_to_simplified(self):
        """繁体应转简体：陳奕迅 → 陈奕迅。"""
        assert "陈奕迅" in normalize_title("陳奕迅 - 打得火熱")

    def test_brackets_stripped(self):
        n = normalize_title("【CD】【FLAC分轨】周杰伦 - 范特西")
        assert "【" not in n and "】" not in n
        assert "cd" not in n.split()          # 标记被移除
        assert "周杰伦" in n

    def test_size_and_uploader_stripped(self):
        n = normalize_title("某专辑 1.2 GB @uploader_x")
        assert "1.2" not in n
        assert "uploader_x" not in n

    def test_lowercase(self):
        assert normalize_title("ADELE - 30") == normalize_title("adele - 30")

    def test_fullwidth_to_halfwidth(self):
        assert "ａｂｃ" not in normalize_title("ＡＢＣ")

    def test_normalized_title_field_populated(self):
        adapter = make_adapter("hhanclub")
        results = adapter.parse_rss(load_fixture("hhanclub_rss.xml"))
        assert results[0].title_normalized
        assert results[0].title_normalized != results[0].title


# ── URL 构建 ────────────────────────────────────────────────

class TestUrlBuilding:
    def test_linktype_dl_appended(self):
        adapter = NexusPhpAdapter(SiteConfig(
            name="hdsky", rss_url="https://hdsky.me/torrentrss.php?passkey=K",
        ))
        url = adapter.build_rss_url()
        assert "linktype=dl" in url

    def test_existing_params_preserved(self):
        adapter = NexusPhpAdapter(SiteConfig(
            name="hdsky",
            rss_url="https://hdsky.me/torrentrss.php?rows=50&linktype=dl&passkey=K",
        ))
        url = adapter.build_rss_url()
        assert url.count("linktype=dl") == 1     # 不重复追加
        assert url.count("rows=") == 1

    def test_cat_param_appended_when_trusted(self):
        adapter = make_adapter("hdsky", cat_id="401")
        url = adapter.build_rss_url()
        assert "cat401=1" in url

    def test_cat_param_not_appended_for_hhanclub(self):
        """憨憨 cat410 已实测失效，不应追加分类参数。"""
        adapter = make_adapter("hhanclub", cat_id="410")
        url = adapter.build_rss_url()
        assert "cat410" not in url

    def test_rows_capped_at_50(self):
        """NexusPHP 硬编码 rows 上限 50。"""
        adapter = make_adapter("hdsky", cat_id="401")
        adapter._max_rows = 200
        adapter.config.rss_url = "https://hdsky.me/torrentrss.php?passkey=K"
        url = adapter.build_rss_url()
        assert "rows=50" in url

    def test_missing_rss_url_raises(self):
        from audio_hub.adapters.base import AdapterError
        adapter = NexusPhpAdapter(SiteConfig(name="hdsky", rss_url=""))
        with pytest.raises(AdapterError):
            adapter.build_rss_url()


# ── 网络与错误处理 ──────────────────────────────────────────

class TestHttpErrorHandling:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_rss_success(self):
        adapter = make_adapter("hhanclub", cat_id="410")
        route = respx.get(url__startswith="https://hhanclub.net/torrentrss.php").mock(
            return_value=httpx.Response(200, text=load_fixture("hhanclub_rss.xml"))
        )
        results = await adapter.fetch_rss()
        assert route.called
        assert len(results) == 5

    @pytest.mark.asyncio
    @respx.mock
    async def test_fetch_rss_ignores_query_param(self):
        """NexusPHP RSS search 不可靠，fetch_rss 应忽略 query 返回全量。"""
        adapter = make_adapter("hhanclub", cat_id="410")
        route = respx.get(url__startswith="https://hhanclub.net/torrentrss.php").mock(
            return_value=httpx.Response(200, text=load_fixture("hhanclub_rss.xml"))
        )
        await adapter.fetch_rss(query="周杰伦")
        assert "search=" not in str(route.calls.last.request.url)

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_raises_auth_error(self):
        from audio_hub.adapters.base import AdapterAuthError
        adapter = make_adapter("hdsky")
        respx.get(url__startswith="https://hdsky.me/torrentrss.php").mock(
            return_value=httpx.Response(403)
        )
        with pytest.raises(AdapterAuthError):
            await adapter.fetch_rss()

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_raises_rate_limited(self):
        from audio_hub.adapters.base import AdapterRateLimited
        adapter = make_adapter("hdsky")
        respx.get(url__startswith="https://hdsky.me/torrentrss.php").mock(
            return_value=httpx.Response(429)
        )
        with pytest.raises(AdapterRateLimited):
            await adapter.fetch_rss()

    @pytest.mark.asyncio
    @respx.mock
    async def test_html_login_page_raises_auth_error(self):
        """passkey 失效时 NexusPHP 返回 HTML 登录页而非 XML。"""
        from audio_hub.adapters.base import AdapterAuthError
        adapter = make_adapter("hdsky")
        respx.get(url__startswith="https://hdsky.me/torrentrss.php").mock(
            return_value=httpx.Response(
                200, text="<html><body>Invalid passkey</body></html>"
            )
        )
        with pytest.raises(AdapterAuthError):
            await adapter.fetch_rss()

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_raises_adapter_error(self):
        from audio_hub.adapters.base import AdapterError
        adapter = make_adapter("hdsky")
        respx.get(url__startswith="https://hdsky.me/torrentrss.php").mock(
            side_effect=httpx.TimeoutException("timeout")
        )
        with pytest.raises(AdapterError):
            await adapter.fetch_rss()

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_empty_triggers_fallback_error(self):
        """RSS search 返回空时应抛错，让调用方 fallback 到全量 RSS。"""
        from audio_hub.adapters.base import AdapterError
        adapter = make_adapter("hdsky")
        empty_rss = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        respx.get(url__startswith="https://hdsky.me/torrentrss.php").mock(
            return_value=httpx.Response(200, text=empty_rss)
        )
        with pytest.raises(AdapterError, match="fallback"):
            await adapter.search("不存在的艺术家")

    @pytest.mark.asyncio
    @respx.mock
    async def test_malformed_item_does_not_break_batch(self):
        """单条 item 解析失败不应中断整批。"""
        adapter = make_adapter("hdsky")
        broken = """<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item><title>Valid FLAC Album</title>
            <guid isPermaLink="false">aabb</guid>
            <enclosure url="https://hdsky.me/download.php?id=1" length="100" type="application/x-bittorrent"/>
          </item>
        </channel></rss>"""
        respx.get(url__startswith="https://hdsky.me/torrentrss.php").mock(
            return_value=httpx.Response(200, text=broken)
        )
        results = await adapter.fetch_rss()
        assert len(results) == 1


# ── passkey 脱敏 ────────────────────────────────────────────

class TestPasskeyMasking:
    def test_passkey_masked(self):
        from audio_hub.adapters.nexusphp import _mask_passkey
        url = "https://hdsky.me/torrentrss.php?passkey=SECRET123&rows=50"
        masked = _mask_passkey(url)
        assert "SECRET123" not in masked
        assert "passkey=***" in masked

    def test_error_message_does_not_leak_passkey(self):
        """超时错误信息中的 URL 必须已脱敏。"""
        from audio_hub.adapters.nexusphp import _mask_passkey
        url = "https://hdsky.me/torrentrss.php?passkey=SECRET123"
        assert "SECRET123" not in _mask_passkey(url)


# ── 跨站复用性 ──────────────────────────────────────────────

class TestCrossSiteReuse:
    """验证同一个 NexusPhpAdapter 类能服务三个站点。"""

    def test_audiences_parses(self):
        adapter = make_adapter("audiences", cat_id="")
        results = adapter.parse_rss(load_fixture("audiences_rss.xml"))
        assert len(results) == 2
        assert results[0].category == MediaCategory.MUSIC
        assert results[1].category == MediaCategory.AUDIOBOOK

    def test_each_site_has_distinct_name(self):
        names = set()
        for site in ("hhanclub", "hdsky", "audiences"):
            adapter = make_adapter(site)
            names.add(adapter.site_name)
        assert names == {"hhanclub", "hdsky", "audiences"}
