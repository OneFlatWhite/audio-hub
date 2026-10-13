"""领域模型：WatchEntry / Task / TorrentRecord + v0.4 归档/隔离/连载/做种/事件。

v0.4 新增模型只是 DB 行的类型化镜像（与 db.py 的 _MIGRATION_V4 一一对应），
业务逻辑（归档决策、校验、连载解析、做种轮询）由各自模块在第二波实现。

v0.6 站点 / 下载器中心与订阅引擎（与 _MIGRATION_V5 一一对应）：
SiteRecord / SiteCategory / SiteUserInfo / RssTestResult / DownloaderRecord /
SubscriptionHit / ParsedTitle，以及 WatchEntry 的订阅引擎字段。
凭据列（cookie / rss_url / api_key / password）**绝不**随模型返回明文，
只暴露 has_* 布尔 —— 页面与日志都拿不到可泄露的值。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .timeutil import utcnow

# ── WatchEntry ──────────────────────────────────────────────

class WatchKind(str, Enum):
    MUSIC = "music"
    AUDIOBOOK = "audiobook"


class SubType(str, Enum):
    """订阅类型（v0.6）。

    artist：歌手型，持续追踪、专辑级去重、永不自动完成；
    album：歌手+专辑，首次命中即完成，可开洗版；
    audiobook：书名/作者，命中即完成（勾选连载则按卷追）；
    keyword：关键词/正则（高级），行为同歌手型。
    """
    ARTIST = "artist"
    ALBUM = "album"
    AUDIOBOOK = "audiobook"
    KEYWORD = "keyword"


class SubStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"


#: 音质偏好的合法值（watchlist.preferred_quality）
PREFERRED_QUALITIES = ("any", "lossless_only", "hires_only")

#: 音质档位排序（洗版判定「更高档」用）。标签与 matcher.detect_quality 的
#: 输出一致；未识别的标签按 0 处理。放这里而非 matcher：repo 的
#: best_quality_for_album 与 2B 的 decide() 都要用同一张表。
QUALITY_RANK: dict[str, int] = {
    "unknown": 0,
    "mp3": 1,
    "alac": 2,
    "ape": 3,
    "wav": 3,
    "flac_16": 4,
    "flac_24": 5,
    "dsd": 6,
}


def quality_rank(tag: str | None) -> int:
    """音质标签 → 档位；None / 未知标签为 0。"""
    return QUALITY_RANK.get((tag or "").lower(), 0)


class WatchEntry(BaseModel):
    """订阅项。v0.6 起带订阅引擎字段，全部有默认值以兼容旧行。"""
    id: str = Field(default_factory=lambda: "")
    kind: WatchKind = WatchKind.MUSIC
    query: str                          # 艺术家名 / 书名 / 关键词
    aliases: list[str] = Field(default_factory=list)
    regex: str | None = None
    sites: list[str] = Field(default_factory=list)  # 空 = 全部
    min_confidence: Literal["high", "medium", "low"] = "high"
    auto_download: bool = True
    priority: int = 0
    enabled: bool = True
    #: v0.4：订阅项绑定的连载（series.id）；非连载为 None
    series_id: int | None = None
    # ── v0.6 订阅引擎 ──
    #: 未显式给出时按 kind 推导：music → artist，audiobook → audiobook
    sub_type: SubType = SubType.ARTIST
    artist: str = ""
    album: str = ""
    author: str = ""
    status: SubStatus = SubStatus.ACTIVE
    search_interval_hours: int = 6
    last_search_at: datetime | None = None
    next_search_at: datetime | None = None
    completed_at: datetime | None = None
    upgrade_enabled: bool = False
    min_seeders: int = 0
    hit_count: int = 0
    #: 指定下载器；None = 默认下载器
    downloader_id: str | None = None
    #: 任务级落盘覆盖；None = 沿用下载器默认
    save_path: str | None = None
    qb_category: str | None = None
    qb_tags: list[str] = Field(default_factory=list)
    preferred_quality: Literal["any", "lossless_only", "hires_only"] = "any"
    min_size_mb: int | None = None
    max_size_mb: int | None = None
    free_only: bool = False
    exclude_keywords: list[str] = Field(default_factory=list)
    include_keywords: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _derive_sub_type(self):
        """旧调用方只给 kind：有声书订阅应落成 audiobook 型而非默认的 artist。"""
        if "sub_type" not in self.model_fields_set and self.kind == WatchKind.AUDIOBOOK:
            self.sub_type = SubType.AUDIOBOOK
        return self


# ── Task（全链路状态机）──────────────────────────────────────

class TaskState(str, Enum):
    DISCOVERED = "discovered"
    SCORED = "scored"
    DISPATCHED = "dispatched"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    ROUTED = "routed"
    NOTIFIED = "notified"
    REJECTED = "rejected"
    FAILED = "failed"
    #: v0.4：校验不通过被扣在隔离区，等待人工放行 / 丢弃
    QUARANTINED = "quarantined"
    #: v0.4：归档决策为 skip（库内已有同等或更优版本），内容未入库
    ARCHIVED_SKIPPED = "archived_skipped"


class ArchiveDecision(str, Enum):
    """归档决策：库内无同名 → new；库内更差 → upgrade；库内不差 → skip。"""
    NEW = "new"
    UPGRADE = "upgrade"
    SKIP = "skip"


class Task(BaseModel):
    """一次获取任务的全链路记录。"""
    id: str = Field(default_factory=lambda: "")
    #: 归属的订阅项；硬排除候选与人工手动任务可为 None
    watch_entry_id: str | None = None
    info_hash: str
    site: str
    title: str
    state: TaskState = TaskState.DISCOVERED
    score: float = 0.0
    confidence: Literal["high", "medium", "low"] = "high"
    reject_reason: str | None = None
    qb_category: str | None = None
    qb_save_path: str | None = None
    archived_path: str | None = None
    error_message: str | None = None
    #: v2 新增：待审采纳后需真实派发，而 TorrentMeta 只存在于内存轮次中，
    #: 故下载链接与质量信息必须持久化，否则 /review 采纳无法转化为下载。
    download_url: str | None = None
    size_bytes: int = 0
    quality: str | None = None
    format_tags: list[str] = Field(default_factory=list)
    site_category: str | None = None
    #: v0.4：连载归属 / 分卷号 / 校验结果（JSON 文本）/ 归档决策（ArchiveDecision.value）
    series_id: int | None = None
    part_no: int | None = None
    validation_result: str | None = None
    archive_decision: str | None = None
    #: v0.6：归属下载器（None = 旧数据，迁移时回填 default）/ 订阅引擎归属
    downloader_id: str | None = None
    watch_id: str | None = None
    album_key: str | None = None
    quality_tag: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ── TorrentRecord（已入库的种子指纹）─────────────────────────

class TorrentRecord(BaseModel):
    """去重表：记录已处理过的种子，防止重复派发。"""
    info_hash: str
    site: str
    title_normalized: str
    first_seen_at: datetime = Field(default_factory=utcnow)
    task_id: str | None = None


# ── v0.4：资料库 / 回收 / 隔离 ───────────────────────────────

class LibraryItem(BaseModel):
    """已入库成品。(watch_id, title_norm) 用于归档决策查旧版本。"""
    id: int | None = None
    watch_id: str | None = None
    task_id: str | None = None
    title_norm: str
    kind: WatchKind = WatchKind.MUSIC
    quality: str | None = None
    quality_score: int = 0
    path: str
    created_at: datetime = Field(default_factory=utcnow)


class RecycleEntry(BaseModel):
    """升级替换时旧版本的去向；restored_at 非空表示已恢复。"""
    id: int | None = None
    library_item_id: int | None = None
    original_path: str
    recycle_path: str
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    restored_at: datetime | None = None


QUARANTINE_STATUSES = ("held", "released", "discarded")


class QuarantineItem(BaseModel):
    """校验不通过的内容。reasons 为失败原因列表。"""
    id: int | None = None
    task_id: str | None = None
    scratch_path: str
    reasons: list[str] = Field(default_factory=list)
    status: Literal["held", "released", "discarded"] = "held"
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


# ── v0.4：连载 ──────────────────────────────────────────────

class SeriesEntry(BaseModel):
    """连载（多部有声书 / 分卷合辑）。total_parts 未知时为 None。"""
    id: int | None = None
    watch_id: str | None = None
    name: str
    total_parts: int | None = None
    part_pattern: str | None = None
    last_part: int = 0
    status: Literal["active", "completed", "paused"] = "active"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class PartEntry(BaseModel):
    """连载中的一部；(series_id, part_no) 唯一。"""
    id: int | None = None
    series_id: int
    part_no: int
    task_id: str | None = None
    title: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


# ── v0.4：做种 / H&R ────────────────────────────────────────

class SeedRecord(BaseModel):
    """做种跟踪：一个任务一条，task_id 为主键。"""
    task_id: str
    info_hash: str
    site: str
    started_at: datetime = Field(default_factory=utcnow)
    seed_seconds: int = 0
    ratio: float = 0.0
    required_seconds: int | None = None
    required_ratio: float | None = None
    status: Literal["seeding", "satisfied", "paused", "removed"] = "seeding"
    hr_satisfied_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow)


# ── v0.4：事件流 ────────────────────────────────────────────

class EventRecord(BaseModel):
    """events 表的一行（DbNotifier 落库，/events 页展示）。detail 为 JSON 文本。

    有意不叫 Event：notify.base.Event 是进程内事件 dataclass，两者同名会在
    导入时互相遮蔽。DbNotifier 负责把前者转成本模型再写库。
    """
    id: int | None = None
    kind: str
    level: str = "info"
    site: str | None = None
    title: str | None = None
    detail: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


# ── v0.6：站点中心 ──────────────────────────────────────────

class AuthStatus(str, Enum):
    """站点认证状态。只有 valid 才允许启用 / 参与搜索（门禁由 2A 在路由层强制）。"""
    UNCONFIGURED = "unconfigured"
    VALID = "valid"
    EXPIRED = "expired"
    ERROR = "error"


#: 适配器类型的合法值（sites.adapter_type）
ADAPTER_TYPES = ("nexusphp", "mteam")

#: 分类本地映射的合法值（site_categories.local_kind）
CATEGORY_KINDS = ("music", "audiobook", "other")


class SiteUserInfo(BaseModel):
    """认证成功后从站点解析到的用户信息（sites.user_info_json）。解析不到的字段留空。"""
    username: str = ""
    level: str = ""
    uploaded: str = ""
    downloaded: str = ""
    ratio: str = ""
    bonus: str = ""
    seeding: int | None = None
    leeching: int | None = None


class RssTestResult(BaseModel):
    """「测试 RSS」的结果（sites.last_rss_test_json）。"""
    ok: bool = False
    count: int = 0
    sample_titles: list[str] = Field(default_factory=list)
    category_counts: dict[str, int] = Field(default_factory=dict)
    elapsed_ms: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    tested_at: datetime = Field(default_factory=utcnow)


class SiteRecord(BaseModel):
    """sites 表的一行，**不含任何凭据明文**。

    cookie / rss_url / api_key 只以 has_* 布尔形式出现。需要明文的唯一场景是
    构建适配器，走 registry_store.load_site_configs / repo.get_site_secret。
    """
    id: str
    name: str
    adapter_type: str = "nexusphp"
    enabled: bool = False
    domain: str = ""
    user_agent: str = ""
    search_url: str = ""
    proxy: str = ""
    priority: int = 0
    music_cat_ids: list[str] = Field(default_factory=list)
    audiobook_cat_ids: list[str] = Field(default_factory=list)
    max_rows: int = 50
    title_bracket_style: str = "【】"
    hr_min_seed_hours: int | None = None
    hr_min_ratio: float | None = None
    auth_status: AuthStatus = AuthStatus.UNCONFIGURED
    auth_checked_at: datetime | None = None
    auth_error: str | None = None
    user_info: SiteUserInfo | None = None
    last_rss_test: RssTestResult | None = None
    has_cookie: bool = False
    has_api_key: bool = False
    has_rss: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SiteCategory(BaseModel):
    """站点远端分类 → 本地类型映射；(site_id, remote_id) 唯一。"""
    id: int | None = None
    site_id: str
    remote_id: str
    remote_name: str = ""
    local_kind: Literal["music", "audiobook", "other"] = "other"
    discovered_at: datetime = Field(default_factory=utcnow)


# ── v0.6：下载器中心 ────────────────────────────────────────

class DownloaderRecord(BaseModel):
    """downloaders 表的一行，不含密码 / API Key 明文。"""
    id: str
    name: str
    type: str = "qbittorrent"
    host: str = "localhost"
    port: int = 8085
    username: str = ""
    save_path: str = ""
    category: str = ""
    tag: str = ""
    enabled: bool = True
    is_default: bool = False
    priority: int = 0
    has_password: bool = False
    has_api_key: bool = False
    last_test: dict | None = None
    last_test_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ── v0.6：订阅引擎 ──────────────────────────────────────────

class HitDecision(str, Enum):
    DISPATCHED = "dispatched"
    REVIEW = "review"
    REJECTED = "rejected"
    SKIPPED_DUP = "skipped_dup"
    UPGRADE = "upgrade"


class FoundVia(str, Enum):
    RSS = "rss"
    SEARCH = "search"


class SubscriptionHit(BaseModel):
    """subscription_hits 表的一行：一次候选命中及其决策。"""
    id: int | None = None
    watch_id: str
    site: str
    torrent_title: str
    info_hash: str = ""
    #: 归一化「歌手|专辑」或「书名|卷」，专辑级去重的键
    album_key: str = ""
    quality_tag: str | None = None
    discount: str | None = None
    size_bytes: int = 0
    seeders: int = 0
    score: float = 0.0
    found_via: FoundVia = FoundVia.RSS
    decision: HitDecision = HitDecision.REVIEW
    reject_reason: str | None = None
    task_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ParsedTitle(BaseModel):
    """title_parser.parse() 的输出（2B 实现解析，这里只定形状）。

    全部可空：解析不出的维度留 None / 空串，raw 永远保留原标题以便回落到全文匹配。
    """
    artist: str = ""
    album: str = ""
    title: str = ""
    year: int | None = None
    quality_tag: str | None = None
    bitrate: str | None = None
    volume: int | None = None
    edition: str | None = None
    raw: str = ""
