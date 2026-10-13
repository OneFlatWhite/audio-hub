"""适配器契约层：NormalizedTorrent 数据模型 + BaseAdapter 抽象基类。

所有站点适配器统一输出 NormalizedTorrent，使 pipeline 层无需感知站点差异。
"""

from __future__ import annotations

import hashlib
import re
import time
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime
from enum import Enum

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from ..models import RssTestResult, SiteCategory, SiteUserInfo

#: 适配器统一 HTTP 超时（秒）
HTTP_TIMEOUT = 30.0

#: 未配置 User-Agent 时的默认值（mteam 等站点要求非空 UA）
DEFAULT_USER_AGENT = "audio-hub/0.6"


def synthetic_info_hash(site: str, key: str) -> str:
    """站点给不出真实 info_hash 时的确定性伪哈希（sha1 hex，40 字符）。

    任务幂等（create_task）、命中去重（has_hit）、做种对账都以 info_hash 为身份键；
    空字符串会让「同哈希复用」把所有空哈希种子坍缩成同一条记录。
    用站点内唯一且稳定的键（种子 ID）合成，保证跨轮次一致。
    """
    return hashlib.sha1(f"{site}:{key}".encode()).hexdigest()

# ── 统一分类枚举 ────────────────────────────────────────────

class MediaCategory(str, Enum):
    """中枢内部分类，各站 cat_id 需映射到此枚举。"""
    MUSIC = "music"
    AUDIOBOOK = "audiobook"
    VIDEO = "video"
    UNKNOWN = "unknown"


class DiscountType(str, Enum):
    """优惠状态。v0.1 为 optional，多数 NexusPHP 站 RSS 不提供。"""
    NORMAL = "normal"
    FREE = "free"
    FREE_2UP = "2up"          # 免费 + 双倍上传
    HALF_DOWN = "halfdown"
    PERCENT_30 = "percent30"
    PERCENT_50 = "percent50"
    UNKNOWN = "unknown"


# ── 统一输出模型 ────────────────────────────────────────────

class NormalizedTorrent(BaseModel):
    """适配器统一输出的种子元数据。"""
    info_hash: str = ""
    title: str                              # 原始标题，保留站点格式
    title_normalized: str = ""              # 清洗后（去标记/繁简/全半角）
    site: str                               # hhanclub / hdsky / audiences / mteam
    category: MediaCategory = MediaCategory.UNKNOWN
    #: 站点原始分类名（如「音乐」「Movies」），用于人工排查与判别溯源
    site_category: str = ""
    download_url: str                       # 直链（NexusPHP linktype=dl）或 API 下载端点
    size_bytes: int = 0
    pub_date: datetime | None = None
    discount: DiscountType = DiscountType.UNKNOWN
    seeders: int | None = None
    leechers: int | None = None
    format_tags: list[str] = Field(default_factory=list)   # 如 ["CD", "FLAC分轨", "WEB"]
    torrent_id: str = ""                    # 站内种子 ID
    #: v0.7 发现墙：站点响应自带的封面图 URL。多数 PT 站列表不供封面（恒空），
    #: 封面主要由 discover/covers 层经 iTunes 补齐；此字段只是「站点恰好给了就用」。
    cover_url: str = ""
    #: v0.6：站内搜索降级标记。NexusPHP 无 Cookie 时 search() 退回 RSS search，
    #: 结果逐条置 True —— 订阅引擎据此知道这批结果不可信（RSS search 参数常被站点忽略）。
    degraded: bool = False
    #: 结果来源："html"（站内搜索页）/ "rss" / "api"（mteam REST），便于排查与统计
    source: str = ""


# ── 标题解析工具（各适配器共用）─────────────────────────────

# 【CD】【FLAC分轨】 或 [CD] [FLAC]
_BRACKET_RE = re.compile(r"[【\[]([^】\]]{1,30})[】\]]")

# 体积：1.2 GB / 500 MB / 2.5GiB
_SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(GB|GiB|MB|MiB|KB|KiB|TB|TiB)",
    re.IGNORECASE,
)

_SIZE_UNITS = {
    "KB": 1024, "KIB": 1024,
    "MB": 1024**2, "MIB": 1024**2,
    "GB": 1024**3, "GIB": 1024**3,
    "TB": 1024**4, "TIB": 1024**4,
}

# 上传者：@uploader / [by:xxx]
_UPLOADER_RE = re.compile(r"(?:@|by[:：])\s*[\w\-_.]+", re.IGNORECASE)


def parse_size_text(text: str) -> int:
    """从文本中解析体积，返回字节数。解析失败返回 0。"""
    m = _SIZE_RE.search(text)
    if not m:
        return 0
    value = float(m.group(1))
    unit = m.group(2).upper()
    return int(value * _SIZE_UNITS.get(unit, 0))


def extract_bracket_tags(title: str) -> list[str]:
    """提取【】或 [] 内的标记，如 ['CD', 'FLAC分轨', 'WEB']。"""
    return [t.strip() for t in _BRACKET_RE.findall(title) if t.strip()]


def strip_noise(title: str) -> str:
    """去除标题中的非内容噪声（体积、上传者），保留【】标记供类型判别。"""
    t = _SIZE_RE.sub(" ", title)
    t = _UPLOADER_RE.sub(" ", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


# opencc 转换器缓存：normalize_title 是热路径（每个候选都调），
# 不应每次重建实例。None 表示尚未初始化，False 表示初始化失败。
_opencc_t2s: object | None = None
_opencc_warned = False


def _get_opencc():
    """惰性加载并缓存 OpenCC t2s 转换器。

    失败时返回 False 并**只警告一次**：静默降级会让繁体标题匹配不上，
    用户无从得知漏抓原因，故必须留下可观测的痕迹。
    """
    global _opencc_t2s, _opencc_warned
    if _opencc_t2s is not None:
        return _opencc_t2s

    try:
        from opencc import OpenCC
        _opencc_t2s = OpenCC("t2s")
    except Exception as e:  # noqa: BLE001 - 依赖缺失不应中断流程
        if not _opencc_warned:
            _opencc_warned = True
            logger.warning(
                f"opencc 不可用（{type(e).__name__}: {e}），繁简转换已跳过。"
                f"繁体标题可能导致漏抓，请安装 opencc-python-reimplemented"
            )
        _opencc_t2s = False
    return _opencc_t2s


def normalize_title(title: str) -> str:
    """标题归一化：去【】标记 → 繁转简 → 全角转半角 → 压缩空格 → 小写。

    繁简转换失败时降级跳过该步（不阻断流程），但会警告一次。
    """
    t = strip_noise(title)
    t = _BRACKET_RE.sub(" ", t)

    converter = _get_opencc()
    if converter:
        t = converter.convert(t)

    # 全角 → 半角
    t = _fullwidth_to_halfwidth(t)

    t = re.sub(r"\s{2,}", " ", t).strip()
    return t.lower()


def _fullwidth_to_halfwidth(text: str) -> str:
    """全角字符转半角。"""
    result = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:                    # 全角空格
            code = 0x20
        elif 0xFF01 <= code <= 0xFF5E:        # 全角 ASCII 区
            code -= 0xFEE0
        result.append(chr(code))
    return "".join(result)


# ── 抽象基类 ────────────────────────────────────────────────

class AdapterError(Exception):
    """适配器层异常基类。"""


class AdapterAuthError(AdapterError):
    """认证失败（passkey/API Key 无效或过期）。"""


class AdapterRateLimited(AdapterError):
    """触发站点限速。"""


class BaseAdapter(ABC):
    """站点适配器抽象基类。

    子类需实现 fetch_rss 与 search。
    search 在 NexusPHP 站点可能失效，基类约定：
    失败或返回空时应 fallback 到 fetch_rss + 本地过滤。
    """

    #: 站点标识，子类覆写
    site_name: str = ""

    def __init__(self, config) -> None:
        """
        Args:
            config: SiteConfig 实例（凭据已解密）
        """
        self.config = config

    @abstractmethod
    async def fetch_rss(
        self, query: str | None = None, kind: str | None = None,
    ) -> list[NormalizedTorrent]:
        """拉取 RSS。

        Args:
            query: 可选关键词；若站点 RSS search 不可靠，实现应忽略此参数
                   返回全量并交由 pipeline 本地过滤。
            kind: "music" / "audiobook" / "all" / None；支持分类过滤的实现按其预过滤
                  （v0.7.1 发现墙依赖它——不过滤时最新 30 条可能被无关分类刷满；
                  "all" = music+audiobook 并集，发现墙一次拉取两个 tab 共用）。
        """

    @abstractmethod
    async def search(self, keyword: str, kind: str | None = None) -> list[NormalizedTorrent]:
        """主动搜索。失败时应抛 AdapterError，由调用方决定 fallback。

        kind（v0.6）："music" / "audiobook" / None，用于选择站点分类过滤；
        旧实现只接 keyword 仍兼容（调用方传位置参数 keyword 即可）。
        """

    @abstractmethod
    def parse_title(self, raw: str) -> dict:
        """解析标题，提取结构化字段。

        Returns:
            {"format_tags": [...], "category_hint": MediaCategory, ...}
        """

    async def resolve_download_url(self, torrent_or_url: NormalizedTorrent | str) -> str:
        """把候选的 download_url 解析为可直接交给下载器的直链。

        多数站点（NexusPHP linktype=dl）的 download_url 本身就是直链，默认原样返回。
        需要先换取临时凭据的站点（如馒头 genDlToken）在子类覆写；
        派发层在实跑前调用本方法，dry-run 不调用。

        Args:
            torrent_or_url: NormalizedTorrent（取其 download_url）或 URL 字符串

        Raises:
            AdapterError: 解析失败（子类实现决定）
        """
        if isinstance(torrent_or_url, NormalizedTorrent):
            return torrent_or_url.download_url
        return torrent_or_url or ""

    async def health_check(self) -> tuple[bool, str]:
        """连通性检查。返回 (是否正常, 说明)。"""
        try:
            results = await self.fetch_rss()
            if not results:
                return False, "RSS 返回 0 条结果"
            return True, f"OK，返回 {len(results)} 条"
        except AdapterAuthError as e:
            return False, f"认证失败: {e}"
        except AdapterError as e:
            return False, f"适配器错误: {e}"
        except Exception as e:  # noqa: BLE001 - 诊断函数须捕获一切，返回而非抛出
            return False, f"未预期异常: {type(e).__name__}: {e}"

    # ── HTTP 客户端（v0.6 统一注入 UA / Cookie / 代理 / 超时）──

    def _user_agent(self) -> str:
        return (getattr(self.config, "user_agent", "") or "").strip() or DEFAULT_USER_AGENT

    def _http_client(
        self, *, with_cookie: bool = True, follow_redirects: bool = True,
    ) -> httpx.AsyncClient:
        """构造带站点配置的 httpx 客户端。

        - User-Agent：站点配置值，否则 DEFAULT_USER_AGENT
        - Cookie：仅在配置了 Cookie 且 with_cookie=True 时加头（RSS 走 passkey，不需要）
        - 代理：config.proxy 非空时启用
        - 超时：HTTP_TIMEOUT
        调用方用 `async with` 管理生命周期。
        """
        headers = {"User-Agent": self._user_agent()}
        cookie = (getattr(self.config, "cookie", "") or "").strip()
        if with_cookie and cookie:
            headers["Cookie"] = cookie
        proxy = (getattr(self.config, "proxy", "") or "").strip() or None
        return httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, follow_redirects=follow_redirects,
            headers=headers, proxy=proxy,
        )

    # ── v0.6 站点中心契约 ────────────────────────────────────

    async def check_auth(self) -> SiteUserInfo:
        """验证凭据并返回用户信息。失败抛 AdapterAuthError（凭据失效）或 AdapterError。"""
        raise AdapterError(f"{self.site_name} 适配器不支持认证检查")

    async def discover_categories(self) -> list[SiteCategory]:
        """抓取站点远端分类列表并给出本地类型建议。"""
        raise AdapterError(f"{self.site_name} 适配器不支持分类发现")

    async def test_rss(self) -> RssTestResult:
        """拉一次 RSS，汇总条数 / 样本标题 / 分类分布 / 耗时 / 解析警告。永不抛出。"""
        started = time.monotonic()
        try:
            torrents = await self.fetch_rss()
        except AdapterError as e:
            return RssTestResult(ok=False, error=str(e),
                                 elapsed_ms=int((time.monotonic() - started) * 1000))
        except Exception as e:  # noqa: BLE001 - 诊断函数须返回而非抛出
            return RssTestResult(ok=False, error=f"{type(e).__name__}: {e}",
                                 elapsed_ms=int((time.monotonic() - started) * 1000))
        elapsed = int((time.monotonic() - started) * 1000)
        counts = Counter(t.category.value for t in torrents)
        warnings: list[str] = []
        if not torrents:
            warnings.append("RSS 返回 0 条，检查 passkey / 分类参数")
        if torrents and counts.get(MediaCategory.UNKNOWN.value, 0) == len(torrents):
            warnings.append("全部条目无法判别类型，分类映射可能未配置")
        no_size = sum(1 for t in torrents if not t.size_bytes)
        if no_size:
            warnings.append(f"{no_size} 条缺少体积信息")
        return RssTestResult(
            ok=bool(torrents), count=len(torrents),
            sample_titles=[t.title for t in torrents[:5]],
            category_counts=dict(counts), elapsed_ms=elapsed, warnings=warnings,
        )


def suggest_category_kind(name: str) -> str:
    """远端分类名 → 本地类型建议（music / audiobook / other）。

    有声书关键词先判："Audio Book" 含 "Audio"，若先判音乐会把有声书归错。
    """
    low = (name or "").lower()
    if any(k in low for k in ("有声", "有聲", "audio book", "audiobook", "听书", "聽書", "audio-book")):
        return "audiobook"
    if any(k in low for k in ("音乐", "music", "flac", "专辑")):
        return "music"
    return "other"
