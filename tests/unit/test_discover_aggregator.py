"""发现墙聚合层单元测试：分组去重 / 代表选择 / kind 过滤 / 单站失败隔离 / TTL / 每站刷新下限。

适配器全是内存假实现（list[NormalizedTorrent]），不触网。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from audio_hub.adapters.base import DiscountType, MediaCategory, NormalizedTorrent
from audio_hub.discover.aggregator import WallCache, collect_wall


class FakeAdapter:
    """内存假适配器：fetch_rss 返回固定列表或抛固定异常。"""

    def __init__(self, name: str, torrents=None, error: Exception | None = None):
        self.site_name = name
        self._torrents = list(torrents or [])
        self.error = error
        self.fetch_calls = 0

    async def fetch_rss(self, query: str | None = None):
        self.fetch_calls += 1
        if self.error is not None:
            raise self.error
        return list(self._torrents)


_DEFAULT_PUB = datetime(2026, 10, 10, tzinfo=UTC)


def _t(title: str, *, site: str, category=MediaCategory.MUSIC, site_category="",
       discount=DiscountType.NORMAL, seeders=10, pub: datetime | None = _DEFAULT_PUB,
       size_mb: int = 500) -> NormalizedTorrent:
    return NormalizedTorrent(
        info_hash=f"{site}-{abs(hash(title)) & 0xffffffff:08x}" + "0" * 8,
        title=title, title_normalized=title.lower(),
        site=site, category=category, site_category=site_category,
        download_url=f"https://{site}/dl/1",
        size_bytes=size_mb * 1024 ** 2,
        pub_date=pub,
        discount=discount, seeders=seeders,
    )


async def test_group_same_album_across_sites():
    """同专辑多站 / 多版本合一卡，sites 记全部来源站。"""
    a = FakeAdapter("siteA", [_t("周杰伦 - 叶惠美 (2003) [FLAC]", site="siteA")])
    b = FakeAdapter("siteB", [_t("周杰伦 - 叶惠美 (2003) [FLAC 24-96]", site="siteB"),
                              _t("周杰伦 - 范特西 [FLAC]", site="siteB")])
    wall = await collect_wall({"siteA": a, "siteB": b}, "music", cache=WallCache(), ttl=900)
    assert len(wall.cards) == 2
    card = next(c for c in wall.cards if c.title == "叶惠美")
    assert card.sites == ["siteA", "siteB"]
    assert card.artist == "周杰伦"
    # 代表选高音质：flac_24 胜出
    assert card.quality_tag == "flac_24"
    assert not wall.site_errors and not wall.from_cache


async def test_representative_priority_quality_then_discount_then_seeders():
    """代表选择：音质档 > 优惠 > 做种数。"""
    # 音质优先于优惠：flac_24 + NORMAL 胜过 flac_16 + FREE
    a = FakeAdapter("s", [_t("周杰伦 - 叶惠美 [FLAC]", site="s",
                             discount=DiscountType.FREE, seeders=100),
                          _t("周杰伦 - 叶惠美 [FLAC 24-96]", site="s",
                             discount=DiscountType.NORMAL, seeders=1)])
    wall = await collect_wall({"s": a}, "music", cache=WallCache(), ttl=900)
    assert len(wall.cards) == 1
    assert wall.cards[0].quality_tag == "flac_24"
    assert wall.cards[0].discount == DiscountType.NORMAL

    # 同音质比优惠：FREE 胜 NORMAL
    a2 = FakeAdapter("s", [_t("周杰伦 - 范特西 [FLAC]", site="s", discount=DiscountType.NORMAL),
                           _t("周杰伦 - 范特西 [FLAC分轨]", site="s", discount=DiscountType.FREE)])
    wall2 = await collect_wall({"s": a2}, "music", cache=WallCache(), ttl=900)
    assert len(wall2.cards) == 1
    assert wall2.cards[0].discount == DiscountType.FREE

    # 同音质同优惠比做种数
    a3 = FakeAdapter("s", [_t("周杰伦 - 八度空间 [FLAC]", site="s", seeders=3),
                           _t("周杰伦 - 八度空间 [FLAC分轨]", site="s", seeders=50)])
    wall3 = await collect_wall({"s": a3}, "music", cache=WallCache(), ttl=900)
    assert len(wall3.cards) == 1
    assert wall3.cards[0].seeders == 50


async def test_kind_filter():
    """music 墙只留音乐：有声书 / 视频被排除；UNKNOWN 用分类名与标题兜底。"""
    a = FakeAdapter("s", [
        _t("周杰伦 - 叶惠美 [FLAC]", site="s", category=MediaCategory.MUSIC),
        _t("鬼吹灯 第一卷 周建龙演播", site="s", category=MediaCategory.AUDIOBOOK),
        _t("某演唱会 BluRay", site="s", category=MediaCategory.VIDEO),
        # UNKNOWN：靠 site_category / 标题判别
        _t("林俊杰 - 幸存者 [FLAC]", site="s", category=MediaCategory.UNKNOWN,
           site_category="音乐"),
        _t("三体 有声书 第一部", site="s", category=MediaCategory.UNKNOWN,
           site_category="有声小说"),
        _t("乱七八糟无法判别", site="s", category=MediaCategory.UNKNOWN, site_category="其他"),
    ])
    wall = await collect_wall({"s": a}, "music", cache=WallCache(), ttl=900)
    titles = {c.title for c in wall.cards}
    assert "叶惠美" in titles and "幸存者" in titles
    assert len(wall.cards) == 2

    wall_ab = await collect_wall({"s": a}, "audiobook", cache=WallCache(), ttl=900)
    ab_titles = {c.title for c in wall_ab.cards}
    assert "鬼吹灯" in ab_titles and "三体" in ab_titles
    assert len(wall_ab.cards) == 2


async def test_audiobook_volume_dedup():
    """有声书按 书名|卷 分键：同书不同卷是两卡，同卷多站合一卡。"""
    a = FakeAdapter("s1", [_t("鬼吹灯 第一卷 周建龙演播", site="s1", category=MediaCategory.AUDIOBOOK)])
    b = FakeAdapter("s2", [_t("鬼吹灯 第一卷 有声书", site="s2", category=MediaCategory.AUDIOBOOK),
                           _t("鬼吹灯 第二卷", site="s2", category=MediaCategory.AUDIOBOOK)])
    wall = await collect_wall({"s1": a, "s2": b}, "audiobook", cache=WallCache(), ttl=900)
    assert len(wall.cards) == 2
    vol1 = next(c for c in wall.cards if "第一卷" in c.title or "卷一" in c.title or c.title == "鬼吹灯")
    by_sites = sorted(len(c.sites) for c in wall.cards)
    assert by_sites == [1, 2]
    assert all(c.kind == "audiobook" for c in wall.cards)
    _ = vol1


async def test_single_site_failure_isolated():
    """单站异常隔离：记入 site_errors，其余站照常出卡，不抛出。"""
    bad = FakeAdapter("bad", error=ConnectionError("timeout"))
    good = FakeAdapter("good", [_t("周杰伦 - 叶惠美 [FLAC]", site="good")])
    wall = await collect_wall({"bad": bad, "good": good}, "music",
                              cache=WallCache(), ttl=900)
    assert len(wall.cards) == 1
    assert wall.cards[0].sites == ["good"]
    assert "bad" in wall.site_errors
    assert "ConnectionError" in wall.site_errors["bad"]


async def test_ttl_hit_and_expiry():
    """TTL 期内命中缓存（不再拉站，from_cache=True）；过期后重拉。"""
    a = FakeAdapter("s", [_t("周杰伦 - 叶惠美 [FLAC]", site="s")])
    cache = WallCache()
    t0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

    wall1 = await collect_wall({"s": a}, "music", cache=cache, ttl=900, now=t0)
    assert a.fetch_calls == 1 and not wall1.from_cache

    wall2 = await collect_wall({"s": a}, "music", cache=cache, ttl=900,
                               now=t0 + timedelta(minutes=10))
    assert a.fetch_calls == 1                      # TTL 内不重拉
    assert wall2.from_cache
    assert wall2.fetched_at == wall1.fetched_at    # 缓存时间保留首次拉取时刻

    wall3 = await collect_wall({"s": a}, "music", cache=cache, ttl=900,
                               now=t0 + timedelta(minutes=16))
    assert a.fetch_calls == 2 and not wall3.from_cache


async def test_cache_key_includes_kind_and_sites():
    """缓存键 = kind + 启用站指纹：不同 kind / 不同站点集合互不影响。"""
    a = FakeAdapter("s", [_t("周杰伦 - 叶惠美 [FLAC]", site="s")])
    cache = WallCache()
    await collect_wall({"s": a}, "music", cache=cache, ttl=900)
    await collect_wall({"s": a}, "audiobook", cache=cache, ttl=900)
    assert a.fetch_calls == 2                        # kind 不同 → 各拉一次
    b = FakeAdapter("s2", [])
    await collect_wall({"s": a, "s2": b}, "music", cache=cache, ttl=900)
    assert a.fetch_calls == 3                        # 站点指纹变了 → 缓存不命中


async def test_refresh_per_site_min_interval():
    """手动刷新：5 分钟内拉过的站沿用旧数据并标注 stale；到期的站真重拉。"""
    a = FakeAdapter("fast", [_t("周杰伦 - 叶惠美 [FLAC]", site="fast")])
    b = FakeAdapter("slow", [_t("林俊杰 - 幸存者 [FLAC]", site="slow")])
    cache = WallCache()
    t0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

    await collect_wall({"fast": a}, "music", cache=cache, ttl=0, now=t0)
    await collect_wall({"slow": b}, "music", cache=cache, ttl=0,
                       now=t0 - timedelta(minutes=10))
    assert a.fetch_calls == 1 and b.fetch_calls == 1

    # 手动刷新两站：fast 1 分钟前刚拉过 → 沿用；slow 10 分钟前 → 重拉
    wall = await collect_wall({"fast": a, "slow": b}, "music", cache=cache, ttl=0,
                              force_refresh=True, refresh_min_interval=300,
                              now=t0 + timedelta(minutes=1))
    assert a.fetch_calls == 1 and b.fetch_calls == 2
    assert wall.stale_sites == ["fast"]
    assert {c.title for c in wall.cards} == {"叶惠美", "幸存者"}


async def test_refresh_failure_falls_back_to_old_data():
    """手动刷新时某站失败但有旧数据：沿用旧数据 + 记 site_errors，墙不塌。"""
    a = FakeAdapter("s", [_t("周杰伦 - 叶惠美 [FLAC]", site="s")])
    cache = WallCache()
    t0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    await collect_wall({"s": a}, "music", cache=cache, ttl=0, now=t0)

    a.error = ConnectionError("boom")
    wall = await collect_wall({"s": a}, "music", cache=cache, ttl=0,
                              force_refresh=True, refresh_min_interval=0,
                              now=t0 + timedelta(minutes=10))
    assert len(wall.cards) == 1                      # 旧数据顶上
    assert "s" in wall.site_errors


async def test_pub_date_desc_and_none_last():
    """卡片按发布时间倒序；无发布时间的沉底。"""
    a = FakeAdapter("s", [
        _t("周杰伦 - 叶惠美 [FLAC]", site="s", pub=datetime(2026, 10, 1, tzinfo=UTC)),
        _t("林俊杰 - 幸存者 [FLAC]", site="s", pub=datetime(2026, 10, 12, tzinfo=UTC)),
        _t("孙燕姿 - 跳舞的梵谷 [FLAC]", site="s", pub=None),
    ])
    wall = await collect_wall({"s": a}, "music", cache=WallCache(), ttl=900)
    assert [c.title for c in wall.cards] == ["幸存者", "叶惠美", "跳舞的梵谷"]
