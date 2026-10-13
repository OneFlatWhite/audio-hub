"""M-Team（馒头）适配器：独立实现，不复用 NexusPHP 基类。

馒头已从 NexusPHP 迁移到自研 mTorrent 架构，关键差异（2026-10 实测校准）：
- 搜索：POST api.m-team.cc/api/torrent/search（JSON REST；空关键词=最新上架列表）
- 认证：API Key 只走 x-api-key header——并发 Authorization 会被拒为「無效的請求」
- RSS 端点：rss.m-team.cc/api/rss/fetch 需单独 RSS key（与 API key 不通用），仅作兜底
- 分类：响应是字符串 ID（110/434=音乐、442=有声书、406=演唱会/MV），非 dict.mode
- 做种数/优惠：嵌在 status 子对象（字段均为字符串）；封面字段是 imageList
- 域名：api.m-team.cc 与 api.m-team.io 均可用，需 failover

⚠️ 认证凭据为「存取令牌」，获取路径：登录 → 控制台 → 实验室 → 存取令牌生成
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

import httpx
from loguru import logger
from pydantic import ValidationError

from ..models import SiteCategory, SiteUserInfo
from .base import (
    AdapterAuthError,
    AdapterError,
    AdapterRateLimited,
    BaseAdapter,
    DiscountType,
    MediaCategory,
    NormalizedTorrent,
    normalize_title,
    parse_size_text,
    suggest_category_kind,
)

_TIMEOUT = 30.0

# 双域名 failover（Prowlarr issue #2159：馒头公告两域名均可用）
_DEFAULT_BASE_URLS = ("https://api.m-team.cc", "https://api.m-team.io")

# 下载直链换取端点：搜索结果里的 download_url 是此占位 URL，派发前需 POST 换取真正的直链
_GEN_DL_TOKEN_PATH = "/api/torrent/genDlToken"

# v0.6 站点中心：认证检查与分类发现端点
_PROFILE_PATH = "/api/member/profile"
_CATEGORY_LIST_PATH = "/api/torrent/categoryList"

# 馒头业务成功码：search 返回整数 0，genDlToken 返回字符串 "0"，统一按 str 比较
_OK_CODES = {"0", "200"}

# 馒头 discount 枚举 → 中枢 DiscountType
_DISCOUNT_MAP = {
    "NORMAL": DiscountType.NORMAL,
    "FREE": DiscountType.FREE,
    "_2X_FREE": DiscountType.FREE_2UP,
    "PERCENT_50": DiscountType.PERCENT_50,
    "PERCENT_30": DiscountType.PERCENT_30,
}

# 馒头 category.mode → 中枢 MediaCategory
_MODE_MAP = {
    "music": MediaCategory.MUSIC,
    "audiobook": MediaCategory.AUDIOBOOK,
    "movie": MediaCategory.VIDEO,
    "tvshow": MediaCategory.VIDEO,
    "adult": MediaCategory.VIDEO,
}

#: 主站真实分类 ID → (展示名, 中枢类别)。2026-10 实测 categoryList：
#: 响应里 category 是字符串 ID（不是早期文档假设的 dict），按此映射；
#: 未收录的 ID 回退标题关键词判别。
_CATEGORY_ID_MAP: dict[str, tuple[str, MediaCategory]] = {
    "110": ("Music", MediaCategory.MUSIC),
    "434": ("Music(无损)", MediaCategory.MUSIC),
    "442": ("有声书", MediaCategory.AUDIOBOOK),
    "406": ("演唱会/MV", MediaCategory.VIDEO),
}

#: 有声书搜索分类兜底 ID（馒头无独立 audiobook 模式，用 mode=normal + 分类 ID）
_DEFAULT_AUDIOBOOK_CAT = "442"


class MTeamAdapter(BaseAdapter):
    """馒头 REST API 适配器。"""

    site_name = "mteam"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._api_key = config.api_key
        if not self._api_key:
            raise AdapterError("mteam 未配置 api_key（存取令牌）")
        # search_url 形如 https://api.m-team.cc/api/torrent/search
        self._base_urls = self._derive_base_urls(config.search_url)
        self._active_base: str | None = None   # 域名可用性缓存

    @staticmethod
    def _derive_base_urls(search_url: str) -> tuple[str, ...]:
        """从配置的 search_url 推导 base URL，并补全备用域名。"""
        if not search_url:
            return _DEFAULT_BASE_URLS
        m = re.match(r"(https?://[^/]+)", search_url)
        primary = m.group(1) if m else _DEFAULT_BASE_URLS[0]
        others = tuple(u for u in _DEFAULT_BASE_URLS if u != primary)
        return (primary, *others)

    def _headers(self, *, json_body: bool = True) -> dict[str, str]:
        # 只发 x-api-key：2026-10 实测，并发 Authorization（无论是否 Bearer）
        # 都会被馒头拒为「無效的請求/非法用戶端」，这不是兼容是投毒。
        headers = {
            "x-api-key": self._api_key,
            "Accept": "application/json",
            "User-Agent": self._user_agent(),
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    # ── v0.6 认证检查 / 分类发现 ────────────────────────────

    async def check_auth(self) -> SiteUserInfo:
        """POST /api/member/profile：API Key 有效则返回用户资料。

        401/403 由 _post 转 AdapterAuthError；业务码非 0 也视为凭据问题。
        """
        try:
            data = await self._post(_PROFILE_PATH, {})
        except AdapterError as e:
            if isinstance(e, (AdapterAuthError, AdapterRateLimited)):
                raise
            if "业务错误" in str(e):
                raise AdapterAuthError(f"mteam 存取令牌被拒: {e}") from e
            raise
        return parse_profile(data)

    async def discover_categories(self) -> list[SiteCategory]:
        """POST /api/torrent/categoryList：兼容 nameChs / nameCht / nameEng 与嵌套 list 结构。"""
        data = await self._post(_CATEGORY_LIST_PATH, {})
        cats = parse_category_list(data, self.site_name)
        if not cats:
            raise AdapterError("mteam categoryList 未返回任何分类")
        return cats

    # ── 搜索（馒头的主力接口）──────────────────────────────

    async def search(self, keyword: str, kind: str | None = None) -> list[NormalizedTorrent]:
        """POST /api/torrent/search。

        馒头有完整 JSON API，搜索是可靠的主力路径（不同于 NexusPHP 的 RSS search）。
        kind 映射（2026-10 实测）：
        - "music" → mode=music（含 110/434/406，演唱会/MV 由本地分类判别剔除）
        - "audiobook" → 无独立模式，用 mode=normal + categories=[有声书分类 ID]
        - "all" → 音乐 + 有声书各拉一遍再合并（不带 mode 的全站最新几乎全是影视，
          两类都捞不到；发现墙聚合用）
        - None → mode=music（旧默认）
        空关键词 = 最新上架列表（发现墙数据源）。
        """
        if kind == "all":
            music, books = await asyncio.gather(
                self.search(keyword, kind="music"),
                self.search(keyword, kind="audiobook"),
            )
            seen: set[str] = set()
            merged: list[NormalizedTorrent] = []
            for t in (*music, *books):
                k = t.info_hash or t.torrent_id or t.title
                if k not in seen:
                    seen.add(k)
                    merged.append(t)
            return merged

        payload: dict = {
            "keyword": keyword,
            "pageNumber": 1,
            "pageSize": min(self.config.max_rows or 50, 100),
        }
        if kind == "audiobook":
            from ..config import split_ids

            payload["mode"] = "normal"
            payload["categories"] = (
                split_ids(getattr(self.config, "audiobook_cat_id", "") or "")
                or [_DEFAULT_AUDIOBOOK_CAT]
            )
        else:
            payload["mode"] = "music"
        data = await self._post("/api/torrent/search", payload)
        return self._parse_search_response(data)

    async def fetch_rss(self, query: str | None = None, kind: str | None = None) -> list[NormalizedTorrent]:
        """馒头「最新上架」主路径是 search API（空关键词 = 最新列表）。

        RSS 端点（rss.m-team.cc/api/rss/fetch）需要单独的 RSS key，与 API key
        不通用（2026-10 实测只带 x-api-key 也返回「禁止訪問」），仅作兜底。
        """
        try:
            return await self.search(query or "", kind=kind)
        except (AdapterAuthError, AdapterRateLimited):
            raise
        except AdapterError:
            if not self.config.rss_url:
                raise
            logger.warning("mteam search 不可用，退回 RSS 端点")

        try:
            async with self._http_client(with_cookie=False) as client:
                resp = await client.get(
                    self.config.rss_url,
                    headers={"x-api-key": self._api_key},
                )
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                body = resp.json()
                if isinstance(body, dict) and str(body.get("code")) not in _OK_CODES:
                    raise AdapterError(
                        f"mteam RSS 端点拒绝: {body.get('message')}"
                        "（RSS 需在站点 RSS 页生成带 key 的链接）"
                    )
                return self._parse_search_response(body)
            # XML RSS：复用 parsel 解析（结构与 NexusPHP 不同，故单独处理）
            return self._parse_rss_xml(resp.text)
        except httpx.HTTPStatusError as e:
            self._classify_http_error(e.response.status_code)
            raise
        except httpx.HTTPError as e:
            raise AdapterError(f"mteam RSS 请求失败: {e}") from e

    # ── 下载直链换取 ────────────────────────────────────────

    async def resolve_download_url(self, torrent_or_url: NormalizedTorrent | str) -> str:
        """换取馒头下载直链。

        搜索结果里的 download_url 是 genDlToken 占位 URL（见 _parse_torrent），
        不能直接交给 qb；需 POST /api/torrent/genDlToken 换取带临时凭据的直链。
        已是直链（非占位）的 URL 原样返回，不发请求。

        Raises:
            AdapterError: 无法确定 torrent_id，或 API 返回非 0 码 / 空 data
        """
        if isinstance(torrent_or_url, NormalizedTorrent):
            url = torrent_or_url.download_url or ""
            torrent_id = torrent_or_url.torrent_id
        else:
            url = torrent_or_url or ""
            torrent_id = ""

        if url and _GEN_DL_TOKEN_PATH not in url:
            return url

        torrent_id = self._extract_torrent_id(url) or torrent_id
        if not torrent_id:
            raise AdapterError("mteam 无法确定 torrent_id，不能换取下载直链")

        data = await self._post(_GEN_DL_TOKEN_PATH, form={"id": torrent_id})
        direct = data.get("data") if isinstance(data, dict) else None
        if not isinstance(direct, str) or not direct.strip():
            raise AdapterError(f"mteam genDlToken 未返回直链 (id={torrent_id}): {data!r}")

        logger.debug(f"mteam 已换取直链 id={torrent_id}")
        return direct.strip()

    @staticmethod
    def _extract_torrent_id(url: str) -> str:
        """从占位 URL 的 id= 参数解析种子 ID。"""
        if not url:
            return ""
        m = re.search(r"[?&]id=(\d+)", url)
        return m.group(1) if m else ""

    # ── HTTP 与域名 failover ────────────────────────────────

    async def _post(
        self, path: str, payload: dict | None = None, *, form: dict | None = None,
    ) -> dict:
        """POST 请求，带双域名 failover。

        优先使用上次成功的域名（缓存在 _active_base）；
        连续失败则切换备用域名。

        Args:
            payload: JSON 请求体（search 等端点）
            form: 表单请求体（genDlToken 等端点）；与 payload 二选一
        """
        candidates = list(self._base_urls)
        if self._active_base and self._active_base in candidates:
            candidates.remove(self._active_base)
            candidates.insert(0, self._active_base)

        last_error: Exception | None = None
        for base in candidates:
            url = base + path
            try:
                async with self._http_client(with_cookie=False) as client:
                    if form is not None:
                        resp = await client.post(
                            url, data=form, headers=self._headers(json_body=False),
                        )
                    else:
                        resp = await client.post(
                            url, json=payload or {}, headers=self._headers(),
                        )
                if resp.status_code >= 400:
                    self._classify_http_error(resp.status_code, base)
                data = resp.json()
                self._active_base = base          # 缓存可用域名
                if isinstance(data, dict):
                    code = data.get("code")
                    if code is not None and str(code) not in _OK_CODES:
                        raise AdapterError(f"mteam API 业务错误: {code} {data.get('message')}")
                return data
            except (AdapterAuthError, AdapterRateLimited):
                raise                              # 认证/限速问题换域名无用，直接上抛
            except (httpx.TimeoutException, httpx.HTTPError, ValueError) as e:
                last_error = e
                logger.warning(f"mteam {base} 请求失败，尝试下一域名: {e}")
                continue

        raise AdapterError(f"mteam 所有域名均不可用: {last_error}")

    @staticmethod
    def _classify_http_error(status: int, base: str = "") -> None:
        if status in (401, 403):
            raise AdapterAuthError(f"mteam {base} HTTP {status}：API Key（存取令牌）无效或已过期")
        if status == 429:
            raise AdapterRateLimited(f"mteam {base} HTTP 429：触发限速，请降低请求频率")
        if status >= 500:
            raise AdapterError(f"mteam {base} HTTP {status}：服务端错误")
        if status >= 400:
            raise AdapterError(f"mteam {base} HTTP {status}")

    # ── 响应解析 ────────────────────────────────────────────

    def _parse_search_response(self, data: dict) -> list[NormalizedTorrent]:
        """解析馒头搜索 API 响应。

        响应结构（据 SDK 与 MCP Server 文档，字段名待实测校准）：
        { "code": 0, "data": { "total": N, "data": [ {...torrent...} ] } }
        """
        if not isinstance(data, dict):
            return []

        payload = data.get("data") or {}
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("data") or payload.get("list") or []
        else:
            items = []

        results: list[NormalizedTorrent] = []
        for raw in items:
            t = self._parse_torrent(raw)
            if t:
                results.append(t)

        logger.debug(f"mteam search 解析出 {len(results)} 条")
        return results

    def _parse_torrent(self, raw: dict) -> NormalizedTorrent | None:
        """解析单条馒头种子记录。字段名做多重兜底，因未实测确认。"""
        if not isinstance(raw, dict):
            return None

        title = raw.get("name") or raw.get("title") or ""
        if not title:
            return None

        # 体积可能是字符串（"1.2 GB"）或整数（字节）
        size_raw = raw.get("size")
        size = self._coerce_size(size_raw, title)

        info_hash = raw.get("infoHash") or raw.get("info_hash") or raw.get("hash") or ""
        torrent_id = str(raw.get("id") or raw.get("torrentId") or "")

        # 分类：真实响应是字符串 ID（如 "442"），早期文档假设的 dict.mode 结构保留兼容
        category = self._resolve_category(raw, title)
        # 站点原始分类名，供人工排查与判别溯源（与 NexusPHP 的 site_category 对齐）
        raw_cat = raw.get("category")
        if isinstance(raw_cat, dict):
            site_category = str(raw_cat.get("name") or "")
        elif raw_cat is not None:
            site_category = _CATEGORY_ID_MAP.get(str(raw_cat), (str(raw_cat), None))[0]
        else:
            site_category = ""

        # 优惠/做种数：真实响应嵌在 status 子对象里（且字段均为字符串）
        status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
        discount_raw = status.get("discount") or raw.get("discount") or ""
        discount = _DISCOUNT_MAP.get(str(discount_raw).upper(), DiscountType.UNKNOWN)

        # 标签：labels 数组（中字/4k/hdr10/DoVi 等）
        labels = raw.get("labels") or []
        format_tags = [str(x) for x in labels] if isinstance(labels, list) else []

        download_url = raw.get("downloadUrl") or raw.get("download_url") or ""
        if not download_url and torrent_id:
            # 馒头下载需先取临时 token，此处记占位 URL，派发前由 resolve_download_url 换取直链
            download_url = f"{self._active_base or self._base_urls[0]}{_GEN_DL_TOKEN_PATH}?id={torrent_id}"

        pub_date = self._parse_date(raw.get("createdDate") or raw.get("created_date"))

        try:
            return NormalizedTorrent(
                info_hash=info_hash,
                title=title,
                title_normalized=normalize_title(title),
                site=self.site_name,
                category=category,
                site_category=site_category,
                download_url=download_url,
                size_bytes=size,
                pub_date=pub_date,
                discount=discount,
                seeders=_coerce_int(status.get("seeders") or raw.get("seeders")),
                leechers=_coerce_int(status.get("leechers") or raw.get("leechers")),
                format_tags=format_tags,
                torrent_id=torrent_id,
                cover_url=_coerce_cover_url(raw),
                source="api",
            )
        except ValidationError as e:
            logger.warning(f"mteam 种子字段校验失败，已跳过: {title[:60]!r} — {e}")
            return None

    @staticmethod
    def _coerce_size(value, title: str) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            if value.isdigit():
                return int(value)
            parsed = parse_size_text(value)
            if parsed:
                return parsed
        return parse_size_text(title)

    @staticmethod
    def _resolve_category(raw: dict, title: str) -> MediaCategory:
        """分类解析：分类 ID 静态映射（实测形态）→ dict.mode（旧文档形态）→ 标题关键词兜底。"""
        cat = raw.get("category")
        if isinstance(cat, str) and cat in _CATEGORY_ID_MAP:
            return _CATEGORY_ID_MAP[cat][1]
        if isinstance(cat, dict):
            mode = str(cat.get("mode") or "").lower()
            if mode in _MODE_MAP:
                return _MODE_MAP[mode]
            parent = str(cat.get("name") or "").lower()
            if "music" in parent or "音乐" in parent:
                return MediaCategory.MUSIC
            if "audiobook" in parent or "有声" in parent or "有聲" in parent:
                return MediaCategory.AUDIOBOOK

        # 兜底：标题关键词
        t = title.lower()
        if re.search(r"(有声书|有聲書|audiobook|演播|主播)", t):
            return MediaCategory.AUDIOBOOK
        if re.search(r"(flac|ape|\bcd\b|专辑|album|无损)", t):
            return MediaCategory.MUSIC
        if re.search(r"(1080p|2160p|bluray|remux|4k)", t):
            return MediaCategory.VIDEO
        return MediaCategory.UNKNOWN

    @staticmethod
    def _parse_date(value) -> datetime | None:
        """解析馒头 createdDate。

        馒头返回的是**北京时间但无时区标记**（如 "2026-09-30 08:00:00"），
        故走 parse_site_datetime（按 Asia/Shanghai 解释），不能当 UTC。
        """
        from ..timeutil import from_timestamp, parse_site_datetime

        if not value:
            return None
        if isinstance(value, (int, float)):
            # 可能是秒或毫秒时间戳
            ts = value / 1000 if value > 1e11 else value
            return from_timestamp(ts)
        if isinstance(value, str):
            return parse_site_datetime(value)
        return None

    # ── RSS XML 解析（备用路径）─────────────────────────────

    def _parse_rss_xml(self, xml: str) -> list[NormalizedTorrent]:
        from parsel import Selector

        if "<rss" not in xml.lower() and "<feed" not in xml.lower():
            raise AdapterError("mteam RSS 响应非 XML 格式，结构未确认，需实测")

        sel = Selector(text=xml)
        results: list[NormalizedTorrent] = []
        for item in sel.xpath("//item"):
            title = (item.xpath("./title/text()").get() or "").strip()
            if not title:
                continue
            enclosure = item.xpath("./enclosure")
            url = enclosure.attrib.get("url", "") if enclosure else ""
            length = enclosure.attrib.get("length", "") if enclosure else ""
            id_match = re.search(r"[?&]id=(\d+)", url) if url else None
            results.append(NormalizedTorrent(
                info_hash=(item.xpath("./guid/text()").get() or "").strip(),
                title=title,
                title_normalized=normalize_title(title),
                site=self.site_name,
                category=self._resolve_category({}, title),
                download_url=url,
                size_bytes=int(length) if length.isdigit() else parse_size_text(title),
                format_tags=[],
                torrent_id=id_match.group(1) if id_match else "",
            ))
        return results

    # ── 标题解析（契约要求实现）─────────────────────────────

    def parse_title(self, raw: str) -> dict:
        """馒头标题信息主要在 labels 数组而非标题文本，此处仅做兜底判别。"""
        t = raw.lower()
        if re.search(r"(有声书|有聲書|audiobook|演播|主播)", t):
            category = MediaCategory.AUDIOBOOK
        elif re.search(r"(bluray|1080p|2160p|remux|4k|演唱会|concert)", t):
            category = MediaCategory.VIDEO
        elif re.search(r"(flac|ape|\bcd\b|专辑|album|无损|24bit)", t):
            category = MediaCategory.MUSIC
        else:
            category = MediaCategory.UNKNOWN
        return {"category": category, "format_tags": [], "discount": DiscountType.UNKNOWN}


def _coerce_int(value) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _coerce_cover_url(raw: dict) -> str:
    """从馒头响应里挑封面 URL（v0.7 发现墙）。

    实测字段为 imageList（字符串数组，2026-10）；image / cover / poster 等
    字段名做多重兜底。取不到返回空串，绝不报错。
    """
    if not isinstance(raw, dict):
        return ""
    for key in ("image", "cover", "poster", "imageList"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return ""


# ── v0.6 响应解析（模块级纯函数，便于 fixture 单测）─────────────────

_BYTES_UNITS = ((1024**4, "TB"), (1024**3, "GB"), (1024**2, "MB"), (1024, "KB"))


def _human_bytes(value) -> str:
    """馒头 profile 的 uploaded/downloaded 是字节数字符串，转成可读文案；非数字原样返回。"""
    if value in (None, ""):
        return ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    for unit_size, label in _BYTES_UNITS:
        if n >= unit_size:
            return f"{n / unit_size:.2f} {label}"
    return f"{int(n)} B"


def parse_profile(data) -> SiteUserInfo:
    """解析 /api/member/profile 响应。字段做多重兜底，缺字段留空。

    已知结构：{ code: 0, data: { username, role, memberCount: { uploaded, downloaded,
    shareRate, bonus, ... }, memberStatus?, ... } }；部分字段可能平铺在 data 下。
    """
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        raise AdapterAuthError("mteam profile 未返回用户数据，存取令牌可能无效")
    counts = payload.get("memberCount") or {}
    if not isinstance(counts, dict):
        counts = {}

    def pick(*keys):
        for src in (counts, payload):
            for k in keys:
                v = src.get(k)
                if v not in (None, ""):
                    return v
        return None

    role = payload.get("role")
    if isinstance(role, dict):
        role = role.get("nameChs") or role.get("nameEng") or role.get("name") or ""
    ratio = pick("shareRate", "ratio")
    bonus = pick("bonus", "karma")
    return SiteUserInfo(
        username=str(payload.get("username") or payload.get("name") or ""),
        level=str(role or payload.get("roleName") or ""),
        uploaded=_human_bytes(pick("uploaded", "upload")),
        downloaded=_human_bytes(pick("downloaded", "download")),
        ratio="" if ratio is None else str(ratio),
        bonus="" if bonus is None else str(bonus),
        seeding=_coerce_int(pick("seeding", "seedingCount", "seeder")),
        leeching=_coerce_int(pick("leeching", "leechingCount", "leecher")),
    )


def parse_category_list(data, site_id: str) -> list[SiteCategory]:
    """解析 /api/torrent/categoryList。

    兼容两种形态：data 直接是分类数组；或 data.list 是数组（并可能有 data.music 等分模式子列表）。
    每条：{ id, nameChs, nameCht, nameEng, parent?, mode? }。名字取 nameChs → nameCht → nameEng → name。
    """
    payload = data.get("data") if isinstance(data, dict) else data
    items: list = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("list"), list):
            items = payload["list"]
        else:
            for v in payload.values():
                if isinstance(v, list):
                    items.extend(x for x in v if isinstance(x, dict))

    cats: list[SiteCategory] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        rid = str(raw.get("id") or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        name = str(raw.get("nameChs") or raw.get("nameCht") or raw.get("nameEng")
                   or raw.get("name") or "").strip()
        # mode 字段（music / audiobook）比名字更可靠，有则优先
        mode = str(raw.get("mode") or "").lower()
        kind = mode if mode in ("music", "audiobook") else suggest_category_kind(
            f"{name} {raw.get('nameEng') or ''}")
        cats.append(SiteCategory(site_id=site_id, remote_id=rid, remote_name=name, local_kind=kind))
    return cats
