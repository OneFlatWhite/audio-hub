"""发现墙聚合层：并发拉取各启用站 RSS → 标题解析 → 专辑级分组去重 → 卡片墙。

与订阅引擎的边界：发现墙只读 RSS（站点真实最新上架），不做主动搜索、
**不写 site_stats**——用户手动浏览不触发轮询熔断统计，失败只在页面展示。

代表选择优先级（同专辑多站 / 多版本合一卡时）：音质档 > 优惠 > 做种数，
与 matcher 的权重表共用同一份数据，避免两处漂移。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from loguru import logger
from pydantic import BaseModel, Field

from ..adapters.base import (
    BaseAdapter,
    DiscountType,
    MediaCategory,
    NormalizedTorrent,
    suggest_category_kind,
)
from ..models import ParsedTitle, quality_rank
from ..pipeline.matcher import _DISCOUNT_WEIGHTS
from ..pipeline.title_parser import album_key_for, detect_quality_tag, parse
from ..timeutil import utcnow


class DiscoverCard(BaseModel):
    """墙上一张卡：同一专辑（或同书同卷）跨站合并后的展示单元。"""
    album_key: str
    title: str                            # 专辑名 / 书名
    artist: str = ""                      # 歌手 / 作者（拆不出时为空）
    kind: str = "music"
    quality_tag: str | None = None
    discount: DiscountType = DiscountType.UNKNOWN
    seeders: int | None = None
    size_bytes: int = 0
    pub_date: datetime | None = None
    sites: list[str] = Field(default_factory=list)   # 全部来源站
    cover: str = ""                       # /covers/{key}，由 covers 层补；空则前端占位


class DiscoverWall(BaseModel):
    """一面墙：某个 kind 下全部卡片的聚合结果。"""
    cards: list[DiscoverCard] = Field(default_factory=list)
    #: {站名: 错误摘要}；单站失败不抛出，只在页面展示
    site_errors: dict[str, str] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=utcnow)
    from_cache: bool = False
    #: 手动刷新时因「每站最短间隔」被跳过、沿用旧数据的站
    stale_sites: list[str] = Field(default_factory=list)


class _SiteEntry(BaseModel):
    """单站最近一次抓取的原始数据（手动刷新的每站下限要复用它）。"""
    fetched_at: datetime
    torrents: list[NormalizedTorrent] = Field(default_factory=list)
    error: str = ""


class WallCache:
    """发现墙的进程内缓存。

    墙级键 = kind + 启用站指纹，TTL 期内直接复用；另按 (kind, site) 记各站
    最近抓取结果，供手动刷新的「每站最短间隔」判定与旧数据复用。
    进程重启即清空——TTL 只有分钟级，没必要落盘。
    """

    def __init__(self) -> None:
        self._walls: dict[str, DiscoverWall] = {}
        self._sites: dict[tuple[str, str], _SiteEntry] = {}

    @staticmethod
    def wall_key(kind: str, adapters: dict) -> str:
        return f"{kind}|{','.join(sorted(adapters))}"

    def get_wall(self, key: str) -> DiscoverWall | None:
        return self._walls.get(key)

    def set_wall(self, key: str, wall: DiscoverWall) -> None:
        self._walls[key] = wall

    def get_site(self, kind: str, site: str) -> _SiteEntry | None:
        return self._sites.get((kind, site))

    def set_site(
        self, kind: str, site: str, fetched_at: datetime,
        torrents: list[NormalizedTorrent], error: str = "",
    ) -> None:
        self._sites[(kind, site)] = _SiteEntry(
            fetched_at=fetched_at, torrents=list(torrents), error=error,
        )


def _kind_match(t: NormalizedTorrent, kind: str) -> bool:
    """该条种子是否属于目标类型。

    优先用适配器判别好的 category；UNKNOWN 时用站点原始分类名 + 标题关键词兜底
    （与站点中心「分类建议」同一套 suggest_category_kind）；判别为其他类型则排除。
    """
    target = MediaCategory.MUSIC if kind == "music" else MediaCategory.AUDIOBOOK
    if t.category == target:
        return True
    if t.category != MediaCategory.UNKNOWN:
        return False
    return suggest_category_kind(f"{t.site_category} {t.title}") == kind


def _rep_rank(item: tuple[str, NormalizedTorrent, ParsedTitle, str | None]) -> tuple:
    """组内代表排序键：音质档 > 优惠 > 做种数（越大越优）。"""
    _site, t, _parsed, qtag = item
    return (
        quality_rank(qtag),
        _DISCOUNT_WEIGHTS.get(t.discount, 0.0),
        t.seeders or 0,
    )


def build_cards(items: list[tuple[str, NormalizedTorrent]], kind: str) -> list[DiscoverCard]:
    """(站名, 种子) 列表 → 分组去重后的卡片，按发布时间倒序。

    同专辑多站 / 多版本合一卡：album_key_for 分组，代表按 _rep_rank 选，
    sites 记全部来源站。拆不出结构的条目退化为全文键（粒度变粗，不误并）。
    """
    groups: dict[str, list[tuple[str, NormalizedTorrent, ParsedTitle, str | None]]] = {}
    for site, t in items:
        parsed = parse(t.title, kind)
        qtag = parsed.quality_tag or detect_quality_tag(
            f"{t.title} {' '.join(t.format_tags)}"
        )
        key = album_key_for(parsed, kind)
        if not key:
            # 无法识别：按 (站, hash/标题) 各自成卡，绝不把不同内容并成一张
            key = f"raw|{site}|{t.info_hash or t.title}"
        groups.setdefault(key, []).append((site, t, parsed, qtag))

    cards: list[DiscoverCard] = []
    for key, members in groups.items():
        _site, rep_t, rep_parsed, rep_qtag = max(members, key=_rep_rank)
        sites = sorted({m[0] for m in members})
        cards.append(DiscoverCard(
            album_key=key,
            title=rep_parsed.title or rep_parsed.album or rep_t.title,
            artist=rep_parsed.artist,
            kind=kind,
            quality_tag=rep_qtag,
            discount=rep_t.discount,
            seeders=rep_t.seeders,
            size_bytes=rep_t.size_bytes,
            pub_date=rep_t.pub_date,
            sites=sites,
        ))
    # 发布时间倒序；无时间的沉底（None 不能参与比较，分两趟）
    with_date = [c for c in cards if c.pub_date is not None]
    without_date = [c for c in cards if c.pub_date is None]
    with_date.sort(key=lambda c: c.pub_date, reverse=True)
    return with_date + without_date


async def collect_wall(
    adapters: dict[str, BaseAdapter],
    kind: str,
    *,
    cache: WallCache,
    ttl: float,
    force_refresh: bool = False,
    refresh_min_interval: float = 0,
    now: datetime | None = None,
) -> DiscoverWall:
    """聚合一类（music / audiobook）发现墙。

    Args:
        adapters: 已启用的站点适配器（runner.adapters，调用方已按启用 + 认证过滤）
        kind: "music" / "audiobook"
        cache: 进程内缓存
        ttl: 墙级缓存 TTL（秒）
        force_refresh: True 时跳过墙级 TTL，逐站重拉（手动刷新）
        refresh_min_interval: 手动刷新的每站最短间隔（秒）；期内该站沿用
            缓存数据并记入 wall.stale_sites
        now: 测试注入用；默认 utcnow()

    Returns:
        DiscoverWall；单站异常隔离进 site_errors，绝不抛出。
    """
    now = now or utcnow()
    kind = str(getattr(kind, "value", kind) or "music").lower()
    key = cache.wall_key(kind, adapters)

    if not force_refresh:
        wall = cache.get_wall(key)
        if wall is not None and (now - wall.fetched_at).total_seconds() < ttl:
            return wall.model_copy(update={"from_cache": True})

    # ── 逐站决定：真拉 or 沿用旧数据 ──
    results: dict[str, list[NormalizedTorrent]] = {}
    errors: dict[str, str] = {}
    stale: list[str] = []
    to_fetch: list[tuple[str, BaseAdapter]] = []
    for name, adapter in sorted(adapters.items()):
        entry = cache.get_site(kind, name)
        if (force_refresh and entry is not None and refresh_min_interval > 0
                and (now - entry.fetched_at).total_seconds() < refresh_min_interval):
            if entry.torrents:
                results[name] = entry.torrents
            if entry.error:
                errors[name] = entry.error
            stale.append(name)
        else:
            to_fetch.append((name, adapter))

    if to_fetch:
        fetched = await asyncio.gather(
            *(a.fetch_rss() for _, a in to_fetch), return_exceptions=True,
        )
        for (name, _adapter), res in zip(to_fetch, fetched, strict=True):
            if isinstance(res, BaseException):
                logger.warning(f"发现墙：站点 {name} RSS 拉取失败: {type(res).__name__}: {res}")
                errors[name] = f"{type(res).__name__}: {res}"[:200]
                # 刷新失败但手里有旧数据：沿用旧数据，让墙不因单站故障塌掉一角
                old = cache.get_site(kind, name)
                if old and old.torrents:
                    results[name] = old.torrents
                    cache.set_site(kind, name, old.fetched_at, old.torrents, errors[name])
                else:
                    cache.set_site(kind, name, now, [], errors[name])
            else:
                results[name] = res
                cache.set_site(kind, name, now, res, "")

    items = [
        (site, t) for site, torrents in results.items() for t in torrents
        if _kind_match(t, kind)
    ]
    wall = DiscoverWall(
        cards=build_cards(items, kind),
        site_errors=errors,
        fetched_at=now,
        from_cache=False,
        stale_sites=sorted(stale),
    )
    cache.set_wall(key, wall)
    return wall
