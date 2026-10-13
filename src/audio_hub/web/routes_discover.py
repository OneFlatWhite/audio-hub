"""发现墙（v0.7）：/discover 卡片墙 + 一键订阅 + /covers/{key} 封面代理。

页面数据流：runner.adapters（已启用 + 认证通过）→ discover.aggregator.collect_wall
（进程内 TTL 缓存，手动刷新有每站最短间隔）→ discover.covers.fill_covers 补封面。
一键订阅只是订阅引擎的快捷入口：构造 WatchEntry 后复用 routes_subscriptions
的 _search_one 立即搜一次。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from loguru import logger

from .. import repo
from ..discover.aggregator import WallCache, collect_wall, search_wall
from ..discover.covers import covers_dir, fill_covers, is_valid_key, read_meta
from ..models import SubType, WatchEntry, WatchKind
from ..pipeline.title_parser import album_key, normalize_key
from .deps import get_config, get_db, require_user, verify_csrf
from .routes_pages import _common
from .routes_subscriptions import _search_one

router = APIRouter(tags=["discover"])

KIND_TABS = [("music", "音乐"), ("audiobook", "有声书")]

#: 卡片徽章文案（模板不写死映射，与订阅页的路由侧 labels 惯例一致）
QUALITY_LABELS = {
    "dsd": "DSD", "flac_24": "FLAC 24bit", "flac_16": "FLAC", "ape": "APE",
    "wav": "WAV", "alac": "ALAC", "mp3": "MP3",
}
DISCOUNT_LABELS = {
    "free": "Free", "2up": "2xFree", "halfdown": "半价",
    "percent50": "50%", "percent30": "30%",
}

#: 徽章悬停说明（用户对 FLAC/Free 等术语不熟，鼠标悬停即见解释）
QUALITY_TITLES = {
    "dsd": "DSD：SACD 直扒的无损格式，体积最大",
    "flac_24": "FLAC 24bit：高解析度无损音频（音质最高的一档）",
    "flac_16": "FLAC：无损压缩音频，CD 级音质（16bit/44.1kHz）",
    "ape": "APE：无损压缩音频，CD 级音质",
    "wav": "WAV：无压缩原始音频，CD 级音质",
    "alac": "ALAC：Apple 无损格式，CD 级音质",
    "mp3": "MP3：有损压缩，体积最小",
}
DISCOUNT_TITLES = {
    "free": "Free（免费）：下载不计入下载量，不影响分享率",
    "2up": "2xFree：不计下载量，且上传量按 2 倍计入",
    "halfdown": "半价：下载量按 50% 计入",
    "percent50": "50%：下载量按 50% 计入",
    "percent30": "30%：下载量按 30% 计入",
}

_FLASH_OK = {
    "added": "订阅已添加",
    "added_searched": "订阅已添加，并已完成首次搜索（结果见命中记录）",
    "added_no_sites": "订阅已添加，但当前没有可用站点，首次搜索未执行 —— 请到「站点中心」启用并通过认证",
    "added_deferred": "订阅已添加，本轮站点请求预算已用完，稍后自动搜索",
    "refreshed": "已刷新；5 分钟内刚拉过的站点沿用了旧数据（卡片上方标注）",
}
_FLASH_ERR = {
    "empty": "订阅目标为空，无法创建",
    "exists": "相同订阅已存在且启用中，未重复创建",
    "bad_kind": "类型无效",
    "bad_type": "订阅类型无效",
    "search_failed": "订阅已添加，但首次搜索执行出错，详见日志",
}


def _flash(request: Request, kind: str) -> str | None:
    key = request.query_params.get(kind)
    if not key:
        return None
    table = _FLASH_OK if kind == "ok" else _FLASH_ERR
    return table.get(key, key)


def _redirect(kind: str, **params: str) -> RedirectResponse:
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v)
    base = f"/discover?kind={kind}" if kind else "/discover"
    return RedirectResponse(f"{base}&{qs}" if qs else base, status_code=303)


def _adapters_of(request: Request) -> dict:
    """当前生效的站点适配器（runner.adapters 已是启用 + 认证通过的子集）。"""
    runner = getattr(request.app.state, "runner", None)
    return dict(getattr(runner, "adapters", None) or {})


def _wall_cache(request: Request) -> WallCache:
    """进程内墙缓存，惰性创建（单用户工具，无需锁）。"""
    cache = getattr(request.app.state, "discover_cache", None)
    if cache is None:
        cache = WallCache()
        request.app.state.discover_cache = cache
    return cache


def _subscribed_index(entries: list[WatchEntry], kind: str) -> tuple[set[str], set[str]]:
    """已订阅判定索引：(歌手/作者键集合, 专辑/书键集合)，只收启用中的订阅。

    键的算法与 aggregator 的 album_key 对齐：音乐专辑是 album_key(歌手, 专辑)，
    有声书是 normalize_key(书名)（卡片的键带「|volN」后缀，比对时取「|」前段）。
    """
    artists: set[str] = set()
    albums: set[str] = set()
    for e in entries:
        if not e.enabled or e.kind.value != kind:
            continue
        if e.sub_type == SubType.ARTIST:
            key = normalize_key(e.artist or e.query)
            if key:
                artists.add(key)
        elif e.sub_type == SubType.ALBUM:
            key = album_key(e.artist, e.album or e.query)
            if key:
                albums.add(key)
        elif e.sub_type == SubType.AUDIOBOOK:
            if e.album:
                key = normalize_key(e.album)
                if key:
                    albums.add(key)
            else:
                key = normalize_key(e.author or e.query)
                if key:
                    artists.add(key)
    return artists, albums


def _card_states(cards, artists: set[str], albums: set[str]) -> dict[str, dict[str, bool]]:
    """每张卡的已订阅状态：{album_key: {artist_subbed, album_subbed}}。"""
    states: dict[str, dict[str, bool]] = {}
    for c in cards:
        book_base = c.album_key.split("|")[0] if c.kind == "audiobook" else ""
        states[c.album_key] = {
            "artist_subbed": bool(c.artist) and normalize_key(c.artist) in artists,
            "album_subbed": c.album_key in albums or bool(book_base) and book_base in albums,
        }
    return states


# ── 页面 ────────────────────────────────────────────────────

@router.get("/discover", response_class=HTMLResponse)
async def discover_page(
    request: Request,
    kind: str = Query(default="music"),
    site: str | None = Query(default=None),
    q: str | None = Query(default=None),
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    if kind not in ("music", "audiobook"):
        kind = "music"
    q = (q or "").strip()
    templates = request.app.state.templates
    adapters = _adapters_of(request)
    if q:
        # 搜索模式：跨站主动搜索，不读墙缓存（结果随关键词变化，缓存无意义）
        wall = await search_wall(adapters, kind, q)
    else:
        wall = await collect_wall(
            adapters, kind, cache=_wall_cache(request),
            ttl=config.discover.cache_ttl_minutes * 60,
        )

    all_sites = sorted({s for c in wall.cards for s in c.sites})
    cards = wall.cards
    if site:
        cards = [c for c in cards if site in c.sites]

    await fill_covers(
        cards, cache_dir=covers_dir(config), provider=config.discover.cover_provider,
    )

    entries = await repo.list_watch_entries(db, enabled_only=True)
    artists, albums = _subscribed_index(entries, kind)

    # 限速与真失败分列：限速（如 audiences RSS 2 分钟间隔）是预期内的临时状态，
    # 且旧数据已沿用，标红「失败」会误导用户以为站点挂了
    rate_limited = {s: e for s, e in wall.site_errors.items()
                    if e.startswith("AdapterRateLimited")}
    failed = {s: e for s, e in wall.site_errors.items()
              if not e.startswith("AdapterRateLimited")}

    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "discover",
        "kind": kind,
        "kind_tabs": KIND_TABS,
        "wall": wall,
        "cards": cards,
        "card_states": _card_states(cards, artists, albums),
        "all_sites": all_sites,
        "current_site": site or "",
        "q": q,
        "rate_limited": rate_limited,
        "failed": failed,
        "no_sites": not adapters,
        "site_count": len(adapters),
        "quality_labels": QUALITY_LABELS,
        "discount_labels": DISCOUNT_LABELS,
        "quality_titles": QUALITY_TITLES,
        "discount_titles": DISCOUNT_TITLES,
        "flash_ok": _flash(request, "ok"), "flash_err": _flash(request, "err"),
    })
    return templates.TemplateResponse(request, "discover.html", ctx)


# ── 写操作（与 /watchlist 同档：登录 + CSRF）─────────────────

@router.post("/discover/refresh")
async def discover_refresh(
    request: Request,
    user: dict = Depends(verify_csrf),
    config=Depends(get_config),
):
    """手动刷新：跳过墙级 TTL 逐站重拉，但遵守每站最短间隔（discover.refresh_min_interval_minutes）。"""
    form = await request.form()
    kind = (form.get("kind") or "music").strip()
    if kind not in ("music", "audiobook"):
        kind = "music"
    wall = await collect_wall(
        _adapters_of(request), kind, cache=_wall_cache(request),
        ttl=config.discover.cache_ttl_minutes * 60,
        force_refresh=True,
        refresh_min_interval=config.discover.refresh_min_interval_minutes * 60,
    )
    logger.info(
        f"发现墙手动刷新（by {user['username']}）: kind={kind} "
        f"cards={len(wall.cards)} errors={list(wall.site_errors)} stale={wall.stale_sites}"
    )
    return _redirect(kind, ok="refreshed")


@router.post("/discover/subscribe")
async def discover_subscribe(
    request: Request,
    user: dict = Depends(verify_csrf),
    db=Depends(get_db),
    config=Depends(get_config),
):
    """一键订阅：卡片 → WatchEntry → 立即搜索一次（复用订阅中心的 search-now 调用方式）。"""
    form = await request.form()
    kind = (form.get("kind") or "").strip()
    if kind not in ("music", "audiobook"):
        return _redirect("music", err="bad_kind")
    sub_type = (form.get("sub_type") or "").strip()
    if sub_type not in (SubType.ARTIST.value, SubType.ALBUM.value):
        return _redirect(kind, err="bad_type")
    artist = (form.get("artist") or "").strip()
    album = (form.get("album") or form.get("title") or "").strip()

    entry = _build_entry(kind, sub_type, artist, album)
    if entry is None:
        return _redirect(kind, err="empty")

    # 重复订阅（同 artist / 同 album_key 且启用中）不重复建
    entries = await repo.list_watch_entries(db, enabled_only=True)
    artists, albums = _subscribed_index(entries, kind)
    if sub_type == SubType.ARTIST.value:
        dup = normalize_key(artist) in artists
    elif kind == "audiobook":
        dup = normalize_key(album) in albums
    else:
        dup = album_key(artist, album) in albums
    if dup:
        return _redirect(kind, err="exists")

    await repo.add_watch_entry(db, entry)
    logger.info(f"发现墙一键订阅（by {user['username']}）: {entry.sub_type.value} {entry.query!r}")
    key = await _search_one(request, db, config, entry.id)
    ok = {
        "searched": "added_searched", "search_no_sites": "added_no_sites",
        "search_deferred": "added_deferred",
    }.get(key, "added")
    if key == "search_failed":
        return _redirect(kind, err=key)
    return _redirect(kind, ok=ok)


def _build_entry(kind: str, sub_type: str, artist: str, album: str) -> WatchEntry | None:
    """卡片表单 → WatchEntry（复用 _derive_sub_type 语义）。

    artist 型：query=歌手/作者（持续追踪）；album 型：query="歌手 专辑"、
    sub_type=album（仅此一张，一次性）。有声书同理：作者追踪 / 仅此书。
    """
    wk = WatchKind.MUSIC if kind == "music" else WatchKind.AUDIOBOOK
    if sub_type == SubType.ARTIST.value:
        if not artist:
            return None
        if wk == WatchKind.AUDIOBOOK:
            # 不显式给 sub_type：由 _derive_sub_type 落成 AUDIOBOOK
            return WatchEntry(kind=wk, query=artist, author=artist)
        return WatchEntry(kind=wk, sub_type=SubType.ARTIST, query=artist, artist=artist)
    if not album:
        return None
    query = f"{artist} {album}".strip()
    if wk == WatchKind.AUDIOBOOK:
        return WatchEntry(kind=wk, sub_type=SubType.AUDIOBOOK,
                          query=query, author=artist, album=album)
    return WatchEntry(kind=wk, sub_type=SubType.ALBUM,
                      query=query, artist=artist, album=album)


# ── 封面代理（免登录：img 标签带不了 CSRF，内容非敏感）──────────

@router.get("/covers/{key}")
async def cover_proxy(key: str, config=Depends(get_config)):
    """只服务已缓存的键（sha1 hex 格式校验 + 文件存在），不接受任意 URL（防 SSRF）。"""
    if not is_valid_key(key):
        raise HTTPException(status_code=404, detail="bad key")
    cache_dir = covers_dir(config)
    img = cache_dir / f"{key}.img"
    if not img.is_file():
        raise HTTPException(status_code=404, detail="not cached")
    meta = read_meta(cache_dir, key) or {}
    media_type = meta.get("content_type") or "image/jpeg"
    return FileResponse(img, media_type=media_type,
                        headers={"Cache-Control": "max-age=86400"})
