"""NexusPHP 系适配器：HDSky / Hhanclub / Audiences 三站共用。

三站协议相同（torrentrss.php + RSS 2.0；站内页面 index.php / userdetails.php /
torrents.php），差异仅在：
- cat_id 映射（各站分类 ID 不同）
- 标题括号风格（憨憨【】vs HDSky []）
- RSS search 参数是否生效（已知不可靠，故内置 fallback）

v0.6 起 Cookie 认证成为「站内搜索」的前提：
- check_auth：GET index.php 解析用户信息（跳转登录页 / 含 takelogin.php → 失效）
- discover_categories：GET torrents.php 解析分类复选框
- search：有 Cookie 走 torrents.php?search= HTML 解析（最多 2 页，页间 ≥2 秒）；
  无 Cookie 退回 RSS search 并逐条标 degraded=True

已知风险与对策：
- 憨憨 cat410=1 已实测失效 → RSS 分类过滤不可信，一律走本地过滤兜底
- NexusPHP 新版可能忽略 RSS search 参数 → search() 失败自动降级 fetch_rss()
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from loguru import logger
from parsel import Selector

from ..models import SiteCategory, SiteUserInfo
from .base import (
    AdapterAuthError,
    AdapterError,
    AdapterRateLimited,
    BaseAdapter,
    DiscountType,
    MediaCategory,
    NormalizedTorrent,
    extract_bracket_tags,
    normalize_title,
    parse_size_text,
    strip_noise,
    suggest_category_kind,
)

# NexusPHP RSS 默认超时（秒）
_TIMEOUT = 30.0

#: 站内搜索最多翻几页、页间最短间隔（秒）。间隔是账号安全问题：连续翻页像爬虫。
SEARCH_MAX_PAGES = 2
SEARCH_PAGE_DELAY = 2.0

# 类型判别关键词（PRD §6.1 第 2 级信号）
_MUSIC_KEYWORDS = re.compile(
    r"(flac|ape|wav|dsd|分轨|专辑|album|\bcd\b|24bit|hi-?res|无损|mp3|ost|原声)",
    re.IGNORECASE,
)
_AUDIOBOOK_KEYWORDS = re.compile(
    r"(有声书|audiobook|主播|演播|评书|章节|广播剧|听书|m4b|mp3\s*有声)",
    re.IGNORECASE,
)
_EXCLUDE_KEYWORDS = re.compile(
    r"(bluray|blu-ray|bdrip|1080p|2160p|720p|4k|remux|web-?dl|hdtv|"
    r"演唱会|concert|live\s*at|mv|纪录片|documentary|x264|x265|hevc|hdr)",
    re.IGNORECASE,
)

# 优惠标记（若站点在标题中输出）
_DISCOUNT_PATTERNS: list[tuple[re.Pattern, DiscountType]] = [
    (re.compile(r"(2up|双倍上传|2x\s*free)", re.IGNORECASE), DiscountType.FREE_2UP),
    (re.compile(r"(freeleech|免费|free)", re.IGNORECASE), DiscountType.FREE),
    (re.compile(r"(halfdown|50%|半价)", re.IGNORECASE), DiscountType.HALF_DOWN),
    (re.compile(r"(30%)", re.IGNORECASE), DiscountType.PERCENT_30),
]

# 站内搜索页的优惠图标 class → DiscountType。顺序重要：pro_free2up 含 pro_free 子串。
_PRO_CLASS_MAP: list[tuple[str, DiscountType]] = [
    ("pro_free2up", DiscountType.FREE_2UP),
    ("pro_free", DiscountType.FREE),
    ("pro_50pctdown2up", DiscountType.HALF_DOWN),
    ("pro_50pctdown", DiscountType.HALF_DOWN),
    ("pro_30pctdown", DiscountType.PERCENT_30),
]

# 站点 <category> 名 → 内部分类（PRD §6.1 第 1 级信号，权重最高）
# 各站分类名可能是中文或英文，故两种都收录；小写匹配。
_SITE_CATEGORY_HINTS: dict[str, MediaCategory] = {
    # 音乐
    "音乐": MediaCategory.MUSIC,
    "music": MediaCategory.MUSIC,
    "audio": MediaCategory.MUSIC,
    # 有声书
    "有声书": MediaCategory.AUDIOBOOK,
    "有声读物": MediaCategory.AUDIOBOOK,
    "audiobook": MediaCategory.AUDIOBOOK,
    "audiobooks": MediaCategory.AUDIOBOOK,
    # 视频（演唱会蓝光常被归入这些分类）
    "电影": MediaCategory.VIDEO,
    "movies": MediaCategory.VIDEO,
    "电视": MediaCategory.VIDEO,
    "tv": MediaCategory.VIDEO,
    "纪录片": MediaCategory.VIDEO,
    "documentary": MediaCategory.VIDEO,
    "演唱会": MediaCategory.VIDEO,
    "concert": MediaCategory.VIDEO,
    "mv": MediaCategory.VIDEO,
    "动画": MediaCategory.VIDEO,
    "anime": MediaCategory.VIDEO,
    "体育": MediaCategory.VIDEO,
    "sports": MediaCategory.VIDEO,
}

# ── 用户信息解析正则（index.php 信息栏 / userdetails.php）────
_UID_RE = re.compile(r"userdetails\.php\?id=(\d+)")
_SIZE_VALUE = r"(\d[\d.,]*\s*(?:[KMGTPE]i?B|B))"
_UPLOADED_RE = re.compile(r"(?:上[传傳]量?|Uploaded)\s*[:：]?\s*" + _SIZE_VALUE, re.IGNORECASE)
_DOWNLOADED_RE = re.compile(r"(?:下[载載]量?|Downloaded)\s*[:：]?\s*" + _SIZE_VALUE, re.IGNORECASE)
_RATIO_RE = re.compile(r"(?:分享率|Ratio)\s*[:：]?\s*(∞|Inf\.?|---|\d[\d.,]*)", re.IGNORECASE)
_BONUS_RE = re.compile(
    r"(?:魔力值?|Bonus(?:\s*Points?)?|Karma(?:\s*Points?)?)\s*(?:\[[^\]]*\])?\s*[:：]?\s*(\d[\d.,]*)",
    re.IGNORECASE,
)
_INT_RE = re.compile(r"\d+")
_CAT_NAME_RE = re.compile(r"^cat(\d+)$")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")


class NexusPhpAdapter(BaseAdapter):
    """NexusPHP 通用适配器。

    通过 config 参数化站点差异，无需为每站写子类。
    """

    site_name = "nexusphp"

    def __init__(self, config) -> None:
        super().__init__(config)
        self.site_name = config.name
        self._music_cat_id = config.music_cat_id
        self._audiobook_cat_id = config.audiobook_cat_id
        self._max_rows = config.max_rows or 50
        # 憨憨 cat410 已证实失效，故分类过滤仅作"提示"，最终以本地关键词判别为准
        self._trust_cat_filter = config.name not in ("hhanclub",)

    # ── 域名 / 分类 ID 辅助 ─────────────────────────────────

    def _domain(self) -> str:
        """站内页面基址：优先 config.domain，否则从 rss_url / search_url 推导。"""
        dom = (getattr(self.config, "domain", "") or "").strip().rstrip("/")
        if dom:
            return dom if "://" in dom else f"https://{dom}"
        for url in (self.config.rss_url, self.config.search_url):
            if url:
                parts = urlsplit(url)
                if parts.scheme and parts.netloc:
                    return f"{parts.scheme}://{parts.netloc}"
        return ""

    def _require_domain(self) -> str:
        dom = self._domain()
        if not dom:
            raise AdapterError(f"{self.site_name} 未配置站点域名（domain），无法访问站内页面")
        return dom

    def _cat_ids(self, kind: str | None) -> list[str]:
        """按类型取分类 ID：music / audiobook / None（两类并集）。"""
        from ..config import split_ids

        music = split_ids(self._music_cat_id)
        audiobook = split_ids(self._audiobook_cat_id)
        if kind == "music":
            return music
        if kind == "audiobook":
            return audiobook
        return music + [c for c in audiobook if c not in music]

    def _passkey(self) -> str:
        m = re.search(r"[?&]passkey=([^&]+)", self.config.rss_url or "", re.IGNORECASE)
        return m.group(1) if m else ""

    # ── RSS URL 构建 ────────────────────────────────────────

    def build_rss_url(self, keyword: str | None = None, kind: str | None = None) -> str:
        """构建 RSS 请求 URL。

        NexusPHP 分类参数为 cat{id}=1 形式（非 cat[]=id），支持多个分类 ID。
        注意：该参数在部分站点（如憨憨）不生效，因此仅作尽力而为的预过滤。
        kind 省略时沿用旧行为：只带音乐分类（有声书 RSS 由 kind="audiobook" 单独拉）。
        """
        base = self.config.rss_url
        if not base:
            raise AdapterError(f"{self.site_name} 未配置 rss_url")

        sep = "&" if "?" in base else "?"
        params = []

        # rows 上限（NexusPHP 硬编码最大 50）
        rows = min(self._max_rows, 50)
        if "rows=" not in base:
            params.append(f"rows={rows}")

        # 分类过滤（尽力而为，不可信）
        if self._trust_cat_filter:
            for cid in self._cat_ids(kind or "music"):
                params.append(f"cat{cid}=1")

        # 直链下载（passkey 已含在 URL 中）
        if "linktype=" not in base:
            params.append("linktype=dl")

        # 关键词搜索（多数新版 NexusPHP 会忽略此参数，调用方需准备 fallback）
        if keyword:
            params.append(f"search={httpx.QueryParams({'s': keyword})['s']}")
            params.append("search_mode=0")

        url = base + (sep + "&".join(params) if params else "")
        return url

    # ── RSS 拉取 ────────────────────────────────────────────

    async def fetch_rss(self, query: str | None = None) -> list[NormalizedTorrent]:
        """拉取 RSS 并解析。

        query 参数：NexusPHP RSS search 不可靠，故**默认忽略 query**，
        返回全量最新结果，由 pipeline 层本地过滤。这是刻意的设计决策。
        """
        url = self.build_rss_url(keyword=None)  # 不用 RSS search，见上方说明
        xml = await self._http_get(url)
        return self.parse_rss(xml)

    async def search(self, keyword: str, kind: str | None = None) -> list[NormalizedTorrent]:
        """主动搜索。

        有 Cookie：站内 torrents.php HTML 搜索（可靠，这正是认证的价值）。
        无 Cookie：退回 RSS search 参数路径，结果逐条标 degraded=True；若返回空
        （参数被站点忽略的典型症状）抛 AdapterError 让调用方 fallback 到全量 RSS。
        """
        if (self.config.cookie or "").strip() and self._domain():
            return await self.search_html(keyword, kind)

        url = self.build_rss_url(keyword=keyword, kind=kind)
        xml = await self._http_get(url)
        results = self.parse_rss(xml)

        if not results:
            raise AdapterError(
                f"{self.site_name} RSS search 返回空结果，"
                f"疑似 search 参数被站点忽略，需 fallback 到全量 RSS"
            )
        for t in results:
            t.degraded = True
            t.source = "rss"
        logger.info(f"{self.site_name} 无 Cookie，search 已降级为 RSS search（degraded）")
        return results

    # ── HTTP ────────────────────────────────────────────────

    async def _http_get(self, url: str) -> str:
        """执行 RSS GET 请求，统一错误处理。

        RSS 走 passkey，不带 Cookie（避免把登录态暴露给不必要的请求）。
        注意：url 内含 passkey，日志中必须脱敏。
        """
        safe_url = _mask_passkey(url)
        try:
            async with self._http_client(with_cookie=False) as client:
                resp = await client.get(url)
        except httpx.TimeoutException as e:
            raise AdapterError(f"{self.site_name} 请求超时: {safe_url}") from e
        except httpx.HTTPError as e:
            raise AdapterError(f"{self.site_name} 网络错误: {e}") from e

        if resp.status_code == 403:
            raise AdapterAuthError(f"{self.site_name} 返回 403，passkey 可能无效或 IP 被封")
        if resp.status_code == 429:
            raise AdapterRateLimited(f"{self.site_name} 返回 429，触发限速")
        if resp.status_code >= 400:
            raise AdapterError(f"{self.site_name} HTTP {resp.status_code}: {safe_url}")

        text = resp.text
        # NexusPHP 认证失败时可能返回 HTML 登录页而非 XML
        if "passkey" in text.lower() and "<rss" not in text.lower():
            raise AdapterAuthError(f"{self.site_name} 返回登录页，passkey 无效")
        if "<rss" not in text.lower() and "<feed" not in text.lower():
            raise AdapterError(f"{self.site_name} 响应非 RSS 格式，前 200 字符: {text[:200]!r}")

        return text

    async def _page_get(self, client: httpx.AsyncClient, url: str) -> str:
        """带 Cookie 的站内页面 GET：跳转登录页 / 含登录表单 → AdapterAuthError。"""
        try:
            resp = await client.get(url)
        except httpx.TimeoutException as e:
            raise AdapterError(f"{self.site_name} 请求超时: {_mask_passkey(url)}") from e
        except httpx.HTTPError as e:
            raise AdapterError(f"{self.site_name} 网络错误: {e}") from e

        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            if "login" in loc.lower():
                raise AdapterAuthError(f"{self.site_name} 跳转到登录页，Cookie 已失效")
            raise AdapterError(f"{self.site_name} 意外跳转到 {loc!r}")
        if resp.status_code in (401, 403):
            raise AdapterAuthError(f"{self.site_name} HTTP {resp.status_code}，Cookie 无效或 IP 被封")
        if resp.status_code == 429:
            raise AdapterRateLimited(f"{self.site_name} 返回 429，触发限速")
        if resp.status_code >= 400:
            raise AdapterError(f"{self.site_name} HTTP {resp.status_code}: {_mask_passkey(url)}")

        html = resp.text
        if _looks_like_login_page(html):
            raise AdapterAuthError(f"{self.site_name} 返回登录页，Cookie 已失效")
        return html

    # ── v0.6 认证检查 ───────────────────────────────────────

    async def check_auth(self) -> SiteUserInfo:
        """GET index.php 验证 Cookie 并解析用户信息；再取 userdetails.php 补等级 / 做种数。

        userdetails 解析失败只降级字段，不判认证失败。
        """
        domain = self._require_domain()
        if not (self.config.cookie or "").strip():
            raise AdapterAuthError(f"{self.site_name} 未配置 Cookie")

        async with self._http_client(follow_redirects=False) as client:
            html = await self._page_get(client, f"{domain}/index.php")
            info, uid = parse_index_userinfo(html)
            if not uid:
                raise AdapterAuthError(
                    f"{self.site_name} 首页未找到用户信息（userdetails 链接），Cookie 可能已失效"
                )
            try:
                detail_html = await self._page_get(client, f"{domain}/userdetails.php?id={uid}")
            except AdapterError as e:
                logger.warning(f"{self.site_name} userdetails 获取失败，等级/做种数留空: {e}")
            else:
                info = merge_userdetails(info, parse_userdetails(detail_html))
        logger.info(f"{self.site_name} 认证通过: {info.username or '(未解析用户名)'}")
        return info

    # ── v0.6 分类发现 ───────────────────────────────────────

    async def discover_categories(self) -> list[SiteCategory]:
        """GET torrents.php 解析分类复选框 input[name^=cat]（配套 img alt/title 或 label）。"""
        domain = self._require_domain()
        async with self._http_client(follow_redirects=False) as client:
            html = await self._page_get(client, f"{domain}/torrents.php")
        cats = parse_torrents_categories(html, self.site_name)
        if not cats:
            raise AdapterError(f"{self.site_name} torrents.php 未解析到任何分类，页面结构可能不兼容")
        return cats

    # ── v0.6 站内 HTML 搜索 ─────────────────────────────────

    def build_search_url(self, keyword: str, kind: str | None = None, page: int = 0) -> str:
        domain = self._require_domain()
        params: list[tuple[str, str]] = [("search", keyword)]
        for cid in self._cat_ids(kind):
            params.append((f"cat{cid}", "1"))
        params += [("incldead", "0"), ("sort", "4"), ("type", "desc")]
        if page:
            params.append(("page", str(page)))
        return f"{domain}/torrents.php?{httpx.QueryParams(params)}"

    async def search_html(self, keyword: str, kind: str | None = None) -> list[NormalizedTorrent]:
        """Cookie 下的站内搜索：最多 SEARCH_MAX_PAGES 页，页间 ≥SEARCH_PAGE_DELAY 秒。"""
        results: list[NormalizedTorrent] = []
        async with self._http_client(follow_redirects=False) as client:
            for page in range(SEARCH_MAX_PAGES):
                if page:
                    await asyncio.sleep(SEARCH_PAGE_DELAY)
                html = await self._page_get(client, self.build_search_url(keyword, kind, page))
                rows = self.parse_search_html(html)
                results.extend(rows)
                if not rows or not _has_next_page(html, page + 1):
                    break
        logger.debug(f"{self.site_name} 站内搜索 {keyword!r} 解析出 {len(results)} 条")
        return results

    def parse_search_html(self, html: str) -> list[NormalizedTorrent]:
        """解析 torrents.php 种子表。单行失败不中断整批。"""
        sel = Selector(text=html)
        # 只取种子表的直接行：NexusPHP 每行标题格内还嵌了一层 table.torrentname，
        # 用 //tr 会把内层行也当成种子重复解析一遍。
        table = (
            "//table[contains(concat(' ', normalize-space(@class), ' '), ' torrents ')]"
        )
        rows = sel.xpath(
            f"({table}/tr | {table}/tbody/tr)[.//a[contains(@href, 'details.php?id=')]]"
        )
        results: list[NormalizedTorrent] = []
        for row in rows:
            try:
                t = self._parse_search_row(row)
                if t:
                    results.append(t)
            except Exception as e:  # noqa: BLE001 - 单行坏数据不得中断整批
                logger.warning(f"{self.site_name} 解析搜索结果行失败: {e}")
        return results

    def _parse_search_row(self, row: Selector) -> NormalizedTorrent | None:
        link = row.xpath(".//a[contains(@href, 'details.php?id=')]")[0]
        href = link.attrib.get("href", "")
        m = re.search(r"details\.php\?id=(\d+)", href)
        torrent_id = m.group(1) if m else ""
        title = (link.attrib.get("title") or "").strip() or " ".join(
            s.strip() for s in link.xpath(".//text()").getall() if s.strip()
        )
        if not title or not torrent_id:
            return None

        # 分类：行首类型图标的 title/alt
        cat_img = row.xpath(".//a[contains(@href, 'cat=')]//img | .//td[1]//img")
        site_category = ""
        if cat_img:
            site_category = (cat_img[0].attrib.get("title") or cat_img[0].attrib.get("alt") or "").strip()

        # 大小 / 做种 / 下载：NexusPHP 标准列序为 … 大小 | 做种 | 下载 | 完成 …，
        # 以「首个匹配体积的单元格」定位，比写死列号更能跨站复用。
        cells = row.xpath("./td")
        texts = [" ".join(s.strip() for s in c.xpath(".//text()").getall() if s.strip()) for c in cells]
        size = 0
        seeders = leechers = None
        for i, txt in enumerate(texts):
            if i == 0 or not re.fullmatch(r"\d[\d.,]*\s*[KMGTPE]i?B", txt, re.IGNORECASE):
                continue
            size = parse_size_text(txt.replace(",", ""))
            seeders = _first_int(texts[i + 1]) if i + 1 < len(texts) else None
            leechers = _first_int(texts[i + 2]) if i + 2 < len(texts) else None
            break
        if size == 0:
            size = parse_size_text(title)

        # 发布时间：<span title="YYYY-MM-DD HH:MM:SS">，否则单元格文本里的日期
        pub_date = None
        date_attr = row.xpath(".//span[@title]/@title").getall()
        for cand in date_attr + texts:
            dm = _DATE_RE.search(cand or "")
            if dm:
                pub_date = _parse_site_date(dm.group(0))
                if pub_date:
                    break

        # 优惠图标
        discount = DiscountType.UNKNOWN
        classes = " ".join(row.xpath(".//img/@class").getall()).lower()
        for cls, dtype in _PRO_CLASS_MAP:
            if cls in classes:
                discount = dtype
                break

        parsed = self.parse_title(title)
        if discount is DiscountType.UNKNOWN:
            discount = parsed["discount"]
        category = self._resolve_category(parsed["category"], site_category)

        return NormalizedTorrent(
            title=title,
            title_normalized=normalize_title(title),
            site=self.site_name,
            category=category,
            site_category=site_category,
            download_url=self._download_url(torrent_id),
            size_bytes=size,
            pub_date=pub_date,
            discount=discount,
            seeders=seeders,
            leechers=leechers,
            format_tags=parsed["format_tags"],
            torrent_id=torrent_id,
            source="html",
        )

    def _download_url(self, torrent_id: str) -> str:
        """下载直链：优先 passkey（qb 不带 Cookie 也能下），否则 Cookie 直下地址。"""
        domain = self._domain()
        pk = self._passkey()
        if pk:
            return f"{domain}/download.php?id={torrent_id}&passkey={pk}"
        return f"{domain}/download.php?id={torrent_id}"

    # ── RSS 解析 ────────────────────────────────────────────

    def parse_rss(self, xml: str) -> list[NormalizedTorrent]:
        """解析 RSS 2.0 XML 为 NormalizedTorrent 列表。

        NexusPHP RSS 结构：
        - item/guid = info_hash 的 hex 编码（isPermaLink=false）
        - item/enclosure@url = download.php?id=X&passkey=Y
        - item/enclosure@length = 体积字节数
        - item/category = 分类名
        - item/pubDate = RFC822 日期
        """
        sel = Selector(text=xml)
        items = sel.xpath("//item")
        results: list[NormalizedTorrent] = []

        for item in items:
            try:
                t = self._parse_item(item)
                if t:
                    results.append(t)
            except Exception as e:  # noqa: BLE001 - 单条坏数据不得中断整批解析
                # 单条解析失败不应中断整批
                logger.warning(f"{self.site_name} 解析单条 RSS item 失败: {e}")

        logger.debug(f"{self.site_name} RSS 解析出 {len(results)} 条")
        return results

    def _parse_item(self, item: Selector) -> NormalizedTorrent | None:
        """解析单个 <item>。"""
        title = (item.xpath("./title/text()").get() or "").strip()
        if not title:
            return None

        guid = (item.xpath("./guid/text()").get() or "").strip()
        enclosure = item.xpath("./enclosure")
        enclosure_url = enclosure.attrib.get("url", "") if enclosure else ""
        enclosure_len = enclosure.attrib.get("length", "") if enclosure else ""

        # info_hash：优先 guid，其次从 enclosure url 解析
        info_hash = guid or self._hash_from_url(enclosure_url)

        # 体积：优先 enclosure@length，其次从标题解析
        size = 0
        if enclosure_len.isdigit():
            size = int(enclosure_len)
        if size == 0:
            size = parse_size_text(title)

        category_name = (item.xpath("./category/text()").get() or "").strip()
        pub_date_str = (item.xpath("./pubDate/text()").get() or "").strip()

        parsed = self.parse_title(title)
        category = self._resolve_category(parsed["category"], category_name)

        return NormalizedTorrent(
            info_hash=info_hash,
            title=title,
            title_normalized=normalize_title(title),
            site=self.site_name,
            category=category,
            site_category=category_name,
            download_url=enclosure_url,
            size_bytes=size,
            pub_date=_parse_pubdate(pub_date_str),
            discount=parsed["discount"],
            format_tags=parsed["format_tags"],
            torrent_id=self._id_from_url(enclosure_url),
            source="rss",
        )

    # ── 分类解析 ────────────────────────────────────────────

    @staticmethod
    def _resolve_category(
        title_category: MediaCategory, category_name: str
    ) -> MediaCategory:
        """融合站点分类与标题判别（PRD §6.1 三级信号的前两级）。

        优先级：
        1. 标题判为 VIDEO 时无条件胜出——排除优先，宁漏勿错。
           站点把演唱会蓝光归在「音乐」分类下是常见情况。
        2. 站点分类名命中已知词 → 采用（权重最高的信号）。
        3. 否则用标题判别结果。
        4. 两者都有信号但冲突（站点=音乐、标题=有声书）→ 降级 UNKNOWN 进待审，
           不擅自二选一。
        """
        if title_category == MediaCategory.VIDEO:
            return MediaCategory.VIDEO

        site_cat = _SITE_CATEGORY_HINTS.get(category_name.strip().lower())

        if site_cat is None:
            return title_category
        if title_category == MediaCategory.UNKNOWN:
            return site_cat
        if site_cat == title_category:
            return title_category
        # 站点分类与标题判别给出不同的非 UNKNOWN 结论 → 冲突
        return MediaCategory.UNKNOWN

    # ── 标题解析 ────────────────────────────────────────────

    def parse_title(self, raw: str) -> dict:
        """解析标题，返回结构化字段。

        判别优先级（PRD §6.1）：
        1. 排除关键词命中 → VIDEO（宁漏勿错，演唱会/蓝光一律排除）
        2. 有声书关键词命中 → AUDIOBOOK
        3. 音乐关键词命中 → MUSIC
        4. 都不命中 → UNKNOWN（进待审，不自动下载）
        """
        cleaned = strip_noise(raw)
        tags = extract_bracket_tags(raw)

        # 排除优先：含视频特征一律判 VIDEO
        if _EXCLUDE_KEYWORDS.search(cleaned):
            category = MediaCategory.VIDEO
        elif _AUDIOBOOK_KEYWORDS.search(cleaned):
            category = MediaCategory.AUDIOBOOK
        elif _MUSIC_KEYWORDS.search(cleaned):
            category = MediaCategory.MUSIC
        else:
            category = MediaCategory.UNKNOWN

        # 标签本身也是信号（憨憨的【FLAC分轨】等）
        tag_joined = " ".join(tags)
        if category is MediaCategory.UNKNOWN and tag_joined:
            if _EXCLUDE_KEYWORDS.search(tag_joined):
                category = MediaCategory.VIDEO
            elif _MUSIC_KEYWORDS.search(tag_joined):
                category = MediaCategory.MUSIC

        discount = DiscountType.UNKNOWN
        for pattern, dtype in _DISCOUNT_PATTERNS:
            if pattern.search(cleaned) or pattern.search(tag_joined):
                discount = dtype
                break

        return {
            "category": category,
            "format_tags": tags,
            "discount": discount,
            "cleaned": cleaned,
        }

    # ── 辅助 ────────────────────────────────────────────────

    @staticmethod
    def _hash_from_url(url: str) -> str:
        """从 download.php?id=X 无法直接得 hash，返回空由上层处理。"""
        return ""

    @staticmethod
    def _id_from_url(url: str) -> str:
        m = re.search(r"[?&]id=(\d+)", url)
        return m.group(1) if m else ""


# ── 页面解析（模块级纯函数，便于用 fixture 单测）────────────

def _looks_like_login_page(html: str) -> bool:
    low = html.lower()
    return "takelogin.php" in low


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _first_int(text: str | None) -> int | None:
    if not text:
        return None
    m = _INT_RE.search(text.replace(",", ""))
    return int(m.group(0)) if m else None


def parse_index_userinfo(html: str) -> tuple[SiteUserInfo, str]:
    """解析 index.php 的用户信息栏。返回 (SiteUserInfo, uid)；找不到用户链接时 uid 为空。

    缺哪个字段就留空，不抛异常——各站信息栏文案差异大，解析不到不代表没登录。
    """
    sel = Selector(text=html)
    links = sel.xpath("//a[contains(@href, 'userdetails.php?id=')]")
    uid = ""
    username = ""
    for a in links:
        m = _UID_RE.search(a.attrib.get("href", ""))
        name = _collapse(" ".join(a.xpath(".//text()").getall()))
        if m and name:
            uid, username = m.group(1), name
            break
    if not uid and links:
        m = _UID_RE.search(links[0].attrib.get("href", ""))
        uid = m.group(1) if m else ""

    # 信息栏优先（避免把种子列表里的数字误当成上传量），否则全页文本
    block = sel.xpath("string(//*[@id='info_block'])").get() or ""
    text = _collapse(block) or _collapse(sel.xpath("string(//body)").get() or "")

    def grab(rx: re.Pattern) -> str:
        m = rx.search(text)
        return m.group(1).strip() if m else ""

    info = SiteUserInfo(
        username=username,
        uploaded=grab(_UPLOADED_RE),
        downloaded=grab(_DOWNLOADED_RE),
        ratio=grab(_RATIO_RE),
        bonus=grab(_BONUS_RE),
        seeding=_first_int(sel.xpath(
            "//img[contains(@class,'arrowup')]/following-sibling::text()[1]").get()),
        leeching=_first_int(sel.xpath(
            "//img[contains(@class,'arrowdown')]/following-sibling::text()[1]").get()),
    )
    return info, uid


def parse_userdetails(html: str) -> dict:
    """解析 userdetails.php：等级、当前做种 / 下载数（以及信息栏缺失时的上传 / 下载 / 分享率）。"""
    sel = Selector(text=html)
    out: dict = {}

    level_cell = sel.xpath(
        "//td[contains(@class,'rowhead')][contains(normalize-space(.),'等级') "
        "or contains(normalize-space(.),'等級') or contains(normalize-space(.),'Class')]"
        "/following-sibling::td[1]"
    )
    if level_cell:
        img = level_cell.xpath(".//img")
        level = ""
        if img:
            level = (img[0].attrib.get("title") or img[0].attrib.get("alt") or "").strip()
        if not level:
            level = _collapse(" ".join(level_cell.xpath(".//text()").getall()))
        if level:
            out["level"] = level

    seeding = _first_int(sel.xpath(
        "//img[contains(@class,'arrowup')]/following-sibling::text()[1]").get())
    leeching = _first_int(sel.xpath(
        "//img[contains(@class,'arrowdown')]/following-sibling::text()[1]").get())
    if seeding is not None:
        out["seeding"] = seeding
    if leeching is not None:
        out["leeching"] = leeching

    text = _collapse(sel.xpath("string(//body)").get() or "")
    for key, rx in (("uploaded", _UPLOADED_RE), ("downloaded", _DOWNLOADED_RE),
                    ("ratio", _RATIO_RE), ("bonus", _BONUS_RE)):
        m = rx.search(text)
        if m:
            out[key] = m.group(1).strip()
    return out


def merge_userdetails(info: SiteUserInfo, details: dict) -> SiteUserInfo:
    """userdetails 只补信息栏缺的字段（等级 / 做种数必来自这里）。"""
    data = info.model_dump()
    for k, v in details.items():
        if k not in data:
            continue
        if data[k] in ("", None):
            data[k] = v
    return SiteUserInfo(**data)


def parse_torrents_categories(html: str, site_id: str) -> list[SiteCategory]:
    """解析 torrents.php 的分类复选框：input[name^=cat] + 配套 img title/alt 或 label 文本。"""
    sel = Selector(text=html)
    cats: list[SiteCategory] = []
    seen: set[str] = set()
    for inp in sel.xpath("//input[starts-with(@name, 'cat')]"):
        name_attr = inp.attrib.get("name", "")
        m = _CAT_NAME_RE.match(name_attr)
        remote_id = m.group(1) if m else (inp.attrib.get("value") or "").strip()
        if not remote_id.isdigit() or remote_id in seen:
            continue
        seen.add(remote_id)

        label = ""
        parent = inp.xpath("..")
        img = parent.xpath(".//img") if parent else []
        if img:
            label = (img[0].attrib.get("title") or img[0].attrib.get("alt") or "").strip()
        if not label:
            lab = sel.xpath(f"//label[@for='{name_attr}' or @for='cat{remote_id}']")
            if lab:
                label = _collapse(" ".join(lab[0].xpath(".//text()").getall()))
        if not label and parent:
            label = _collapse(" ".join(parent[0].xpath(".//text()").getall()))
        cats.append(SiteCategory(
            site_id=site_id, remote_id=remote_id, remote_name=label,
            local_kind=suggest_category_kind(label),
        ))
    return cats


def _has_next_page(html: str, next_page: int) -> bool:
    """是否存在指向下一页的分页链接（NexusPHP page 参数 0 起）。"""
    return re.search(rf"[?&]page={next_page}(?:&|\"|'|\s)", html) is not None


def _parse_site_date(s: str) -> datetime | None:
    """站内页面的时间是北京时间无时区标记，按 Asia/Shanghai 解释。"""
    from ..timeutil import parse_site_datetime

    try:
        return parse_site_datetime(s.replace("T", " "))
    except Exception:  # noqa: BLE001 - 日期解析失败不阻断
        return None


def _parse_pubdate(s: str) -> datetime | None:
    """解析 RFC822 日期。失败返回 None（不阻断）。"""
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def _mask_passkey(url: str) -> str:
    """脱敏 URL 中的 passkey，用于日志输出。"""
    return re.sub(r"(passkey=)[^&]+", r"\1***", url, flags=re.IGNORECASE)
