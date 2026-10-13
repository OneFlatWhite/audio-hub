"""订阅中心（v0.6 波次 2B）：/watchlist 列表 / 新建 / 详情 / 编辑 / 立即搜索 / 暂停恢复 / 删除。

接替 routes_pages 里的旧 watchlist 段。表单分「基础 + 高级」两层；
新建成功后立即对该订阅跑一次主动搜索（run_subscription_search_once(only_watch_id=...)），
没有可用站点时给明确提示而不是静默。flash 文案在本模块内映射（不改 routes_pages）。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .. import repo
from ..models import (
    PREFERRED_QUALITIES,
    AuthStatus,
    HitDecision,
    SeriesEntry,
    SubStatus,
    SubType,
    WatchEntry,
    WatchKind,
)
from ..subscription.engine import run_subscription_search_once
from .deps import get_config, get_db, require_admin_csrf, require_user, verify_csrf
from .routes_pages import _common

router = APIRouter(tags=["subscriptions"])

_FLASH_OK = {
    "added": "订阅已添加",
    "added_searched": "订阅已添加，并已完成首次搜索（结果见命中记录）",
    "added_no_sites": "订阅已添加，但当前没有可用站点，首次搜索未执行 —— 请到「站点中心」启用并通过认证",
    "added_deferred": "订阅已添加，本轮站点请求预算已用完，稍后自动搜索",
    "updated": "订阅已更新",
    "deleted": "订阅已删除",
    "paused": "订阅已暂停",
    "resumed": "订阅已恢复",
    "searched": "已完成一次搜索（结果见命中记录）",
    "search_no_sites": "没有可用站点，未发出任何请求 —— 请到「站点中心」启用并通过认证",
    "search_deferred": "本轮站点请求预算已用完，该订阅已顺延到下一轮",
}
_FLASH_ERR = {
    "empty": "名称不能为空",
    "notfound": "订阅不存在",
    "bad_type": "订阅类型无效",
    "bad_interval": "搜索间隔必须是不小于 1 的整数（小时）",
    "bad_size": "体积范围必须是非负整数，且下限不大于上限",
    "bad_seeders": "最低做种数必须是非负整数",
    "bad_downloader": "所选下载器不存在或未启用",
    "bad_regex": "正则表达式无效",
    "bad_quality": "音质偏好无效",
    "search_failed": "搜索执行出错，详见日志",
}

#: 列表 / 表单共用的类型元数据（值、名称、徽标色）
SUB_TYPES = [
    (SubType.ARTIST.value, "歌手", "artist"),
    (SubType.ALBUM.value, "专辑", "album"),
    (SubType.AUDIOBOOK.value, "有声书", "audiobook"),
    (SubType.KEYWORD.value, "关键词", "keyword"),
]
QUALITY_LABELS = {
    "any": "不限", "lossless_only": "仅无损（FLAC/APE/WAV/ALAC 及以上）", "hires_only": "仅 Hi-Res / DSD",
}
QUALITY_SHORT = {"lossless_only": "无损+", "hires_only": "Hi-Res"}
STATUS_LABELS = {"active": "进行中", "paused": "已暂停", "completed": "已完成"}
DECISION_LABELS = {
    "dispatched": "已派发", "review": "待审", "rejected": "已拒绝",
    "skipped_dup": "重复跳过", "upgrade": "洗版",
}


def _flash(request: Request, kind: str) -> str | None:
    key = request.query_params.get(kind)
    if not key:
        return None
    table = _FLASH_OK if kind == "ok" else _FLASH_ERR
    return table.get(key, key)


def _redirect(path: str, **params: str) -> RedirectResponse:
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v)
    return RedirectResponse(f"{path}?{qs}" if qs else path, status_code=303)


# ── 表单解析 ────────────────────────────────────────────────

def _split(raw: str | None, sep: str = ",") -> list[str]:
    out: list[str] = []
    for part in re.split(rf"[{re.escape(sep)}，\n]", raw or ""):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def _bool(form, name: str) -> bool:
    v = form.get(name)
    return v is not None and str(v).lower() not in ("", "0", "false", "off", "no")


def _int_or_none(raw: str | None) -> tuple[bool, int | None]:
    raw = (raw or "").strip()
    if not raw:
        return True, None
    if not raw.isdigit():
        return False, None
    return True, int(raw)


async def _valid_sites(db) -> list:
    """已启用且认证通过的站点：订阅「限定站点」只允许从这里选。"""
    return [s for s in await repo.list_sites(db, enabled_only=True)
            if s.auth_status == AuthStatus.VALID]


async def _build_entry(db, form, base: WatchEntry | None) -> tuple[WatchEntry | None, str | None]:
    """表单 → WatchEntry。base 非空为编辑（保留运行态列）。返回 (entry, 错误键)。"""
    sub_type_raw = (form.get("sub_type") or "").strip()
    kind_raw = (form.get("kind") or "").strip()
    if sub_type_raw:
        if sub_type_raw not in SubType._value2member_map_:
            return None, "bad_type"
        sub_type = SubType(sub_type_raw)
    elif kind_raw == WatchKind.AUDIOBOOK.value:
        sub_type = SubType.AUDIOBOOK
    elif base is not None:
        sub_type = base.sub_type
    else:
        # 旧表单只给 query / kind：按原语义做关键词匹配
        sub_type = SubType.KEYWORD
    kind = WatchKind.AUDIOBOOK if sub_type == SubType.AUDIOBOOK else WatchKind.MUSIC
    if kind_raw in WatchKind._value2member_map_ and sub_type == SubType.KEYWORD:
        kind = WatchKind(kind_raw)

    query = (form.get("query") or "").strip()
    artist = (form.get("artist") or "").strip()
    album = (form.get("album") or "").strip()
    author = (form.get("author") or "").strip()
    if sub_type == SubType.ARTIST:
        artist = artist or query
        query = query or artist
    elif sub_type == SubType.ALBUM:
        album = album or query
        query = query or album
    if not query:
        return None, "empty"

    ok, interval = _int_or_none(form.get("search_interval_hours"))
    if not ok or (interval is not None and interval < 1):
        return None, "bad_interval"
    ok1, min_mb = _int_or_none(form.get("min_size_mb"))
    ok2, max_mb = _int_or_none(form.get("max_size_mb"))
    if not (ok1 and ok2) or (min_mb is not None and max_mb is not None and min_mb > max_mb):
        return None, "bad_size"
    ok, min_seeders = _int_or_none(form.get("min_seeders"))
    if not ok:
        return None, "bad_seeders"
    ok, priority = _int_or_none((form.get("priority") or "").lstrip("-"))
    if not ok:
        priority = 0
    elif (form.get("priority") or "").strip().startswith("-"):
        priority = -(priority or 0)

    regex = (form.get("regex") or "").strip() or None
    if regex:
        try:
            re.compile(regex, re.IGNORECASE)
        except re.error:
            return None, "bad_regex"

    preferred = (form.get("preferred_quality") or "any").strip()
    if preferred not in PREFERRED_QUALITIES:
        return None, "bad_quality"

    downloader_id = (form.get("downloader_id") or "").strip() or None
    if downloader_id:
        rec = await repo.get_downloader(db, downloader_id)
        if rec is None or not rec.enabled:
            return None, "bad_downloader"

    # 限定站点：多选框（sites）或旧式逗号串；只留 valid 站点，其他静默丢弃
    valid = await _valid_sites(db)
    valid_ids = {s.id for s in valid} | {s.name for s in valid}
    picked: list[str] = []
    for v in form.getlist("sites") if hasattr(form, "getlist") else [form.get("sites")]:
        picked.extend(_split(v))
    sites = [s for s in picked if s in valid_ids] if valid_ids else []

    min_confidence = (form.get("min_confidence") or "high").strip()
    if min_confidence not in ("high", "medium", "low"):
        min_confidence = "high"

    data = {
        "kind": kind, "sub_type": sub_type, "query": query,
        "artist": artist, "album": album, "author": author,
        "aliases": _split(form.get("aliases")),
        "regex": regex, "sites": sites,
        "min_confidence": min_confidence,
        "auto_download": _bool(form, "auto_download"),
        "priority": priority or 0,
        "search_interval_hours": interval if interval is not None else 6,
        "upgrade_enabled": _bool(form, "upgrade_enabled"),
        "min_seeders": min_seeders or 0,
        "downloader_id": downloader_id,
        "save_path": (form.get("save_path") or "").strip() or None,
        "qb_category": (form.get("qb_category") or "").strip() or None,
        "qb_tags": _split(form.get("qb_tags")),
        "preferred_quality": preferred,
        "min_size_mb": min_mb, "max_size_mb": max_mb,
        "free_only": _bool(form, "free_only"),
        "exclude_keywords": _split(form.get("exclude_keywords")),
        "include_keywords": _split(form.get("include_keywords")),
    }
    if base is not None:
        entry = base.model_copy(update=data)
    else:
        entry = WatchEntry(**data)
    return entry, None


# ── 上下文 ──────────────────────────────────────────────────

def _filter_summary(e: WatchEntry) -> list[str]:
    """列表页「过滤摘要」：只列非默认项。"""
    out: list[str] = []
    if e.preferred_quality and e.preferred_quality != "any":
        out.append(QUALITY_SHORT.get(e.preferred_quality, e.preferred_quality))
    if e.free_only:
        out.append("仅免费")
    if e.min_size_mb or e.max_size_mb:
        lo = f"{e.min_size_mb}MB" if e.min_size_mb else ""
        hi = f"{e.max_size_mb}MB" if e.max_size_mb else ""
        out.append(f"{lo}~{hi}" if (lo and hi) else (f"≥{lo}" if lo else f"≤{hi}"))
    if e.min_seeders:
        out.append(f"做种≥{e.min_seeders}")
    if e.include_keywords:
        out.append("含:" + "/".join(e.include_keywords[:3]))
    if e.exclude_keywords:
        out.append("排:" + "/".join(e.exclude_keywords[:3]))
    if e.upgrade_enabled:
        out.append("洗版")
    if e.sites:
        out.append("站点:" + ",".join(e.sites))
    return out


async def _form_ctx(db) -> dict:
    sites = await _valid_sites(db)
    downloaders = await repo.list_downloaders(db, enabled_only=True)
    default_dl = await repo.get_default_downloader(db)
    return {
        "sub_types": SUB_TYPES,
        "sub_type_names": {v: n for v, n, _ in SUB_TYPES},
        "valid_sites": sites,
        "downloaders": downloaders,
        "default_downloader_id": default_dl.id if default_dl else None,
        "downloader_names": {d.id: d.name for d in await repo.list_downloaders(db)},
        "quality_options": [(q, QUALITY_LABELS.get(q, q)) for q in PREFERRED_QUALITIES],
        "status_labels": STATUS_LABELS,
        "decision_labels": DECISION_LABELS,
        "no_valid_sites": not sites,
        "no_downloaders": not downloaders,
    }


def _adapters_of(request: Request) -> dict:
    runner = getattr(request.app.state, "runner", None)
    return dict(getattr(runner, "adapters", None) or {})


async def _search_one(request: Request, db, config, watch_id: str) -> str:
    """对单条订阅立即搜索一次，返回 flash 键（不抛异常）。"""
    try:
        summary = await run_subscription_search_once(
            db, _adapters_of(request), config=config,
            notifier=getattr(request.app.state, "notifier", None),
            dry_run=config.dry_run, only_watch_id=watch_id,
        )
    except Exception as e:  # noqa: BLE001 - 搜索失败不能让新建 / 页面请求 500
        logger.exception(f"订阅 {watch_id[:8]} 立即搜索失败: {e}")
        return "search_failed"
    if summary.no_sites:
        return "search_no_sites"
    if watch_id in summary.deferred:
        return "search_deferred"
    return "searched"


# ── 页面 ────────────────────────────────────────────────────

@router.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(
    request: Request, user: dict = Depends(require_user),
    db=Depends(get_db), config=Depends(get_config),
):
    templates = request.app.state.templates
    entries = await repo.list_watch_entries(db, enabled_only=False)
    ctx = await _common(request, db, config, user)
    ctx.update(await _form_ctx(db))
    ctx.update({
        "active": "watchlist",
        "entries": entries,
        "summaries": {e.id: _filter_summary(e) for e in entries},
        "status_counts": {
            st: sum(1 for e in entries if e.status.value == st) for st in STATUS_LABELS
        },
        "edit_entry": None,
        "flash_ok": _flash(request, "ok"), "flash_err": _flash(request, "err"),
    })
    return templates.TemplateResponse(request, "watchlist.html", ctx)


@router.get("/watchlist/{entry_id}", response_class=HTMLResponse)
async def subscription_detail(
    request: Request, entry_id: str, user: dict = Depends(require_user),
    db=Depends(get_db), config=Depends(get_config),
):
    entry = await repo.get_watch_entry(db, entry_id)
    if entry is None:
        return _redirect("/watchlist", err="notfound")
    templates = request.app.state.templates
    decision = request.query_params.get("decision") or None
    if decision not in HitDecision._value2member_map_:
        decision = None
    hits = await repo.list_hits(db, entry_id, decision=decision, limit=200)
    all_hits = hits if decision is None else await repo.list_hits(db, entry_id, limit=1000)
    decision_counts: dict[str, int] = {}
    for h in all_hits:
        decision_counts[h.decision.value] = decision_counts.get(h.decision.value, 0) + 1
    series = await repo.get_series(db, entry.series_id) if entry.series_id is not None else None

    ctx = await _common(request, db, config, user)
    ctx.update(await _form_ctx(db))
    ctx.update({
        "active": "watchlist",
        "entry": entry,
        "edit_entry": entry,
        "hits": hits,
        "hit_filter": decision,
        "decision_counts": decision_counts,
        "owned_albums": await repo.list_owned_albums(db, entry_id),
        "series_entry": series,
        "filter_summary": _filter_summary(entry),
        "flash_ok": _flash(request, "ok"), "flash_err": _flash(request, "err"),
    })
    return templates.TemplateResponse(request, "subscription_detail.html", ctx)


# ── 写操作 ──────────────────────────────────────────────────

@router.post("/watchlist")
async def watchlist_add(
    request: Request, user: dict = Depends(verify_csrf),
    db=Depends(get_db), config=Depends(get_config),
):
    form = await request.form()
    entry, err = await _build_entry(db, form, None)
    if err:
        return _redirect("/watchlist", err=err)
    await repo.add_watch_entry(db, entry)

    # 有声书勾了「连载」：建一个 SeriesEntry 并绑定，分卷登记走现有 series 流程
    if entry.kind == WatchKind.AUDIOBOOK and _bool(form, "series"):
        sid = await repo.add_series(db, SeriesEntry(watch_id=entry.id, name=entry.query))
        await repo.set_watch_series(db, entry.id, sid)

    key = await _search_one(request, db, config, entry.id)
    ok = {
        "searched": "added_searched", "search_no_sites": "added_no_sites",
        "search_deferred": "added_deferred",
    }.get(key, "added")
    return _redirect("/watchlist", ok=ok)


@router.post("/watchlist/{entry_id}")
async def watchlist_edit(
    request: Request, entry_id: str, user: dict = Depends(verify_csrf), db=Depends(get_db),
):
    base = await repo.get_watch_entry(db, entry_id)
    if base is None:
        return _redirect("/watchlist", err="notfound")
    form = await request.form()
    entry, err = await _build_entry(db, form, base)
    if err:
        return _redirect(f"/watchlist/{entry_id}", err=err)
    entry.id = entry_id
    await repo.update_watch_entry(db, entry)
    return _redirect(f"/watchlist/{entry_id}", ok="updated")


@router.post("/watchlist/{entry_id}/search-now")
async def watchlist_search_now(
    request: Request, entry_id: str, user: dict = Depends(verify_csrf),
    db=Depends(get_db), config=Depends(get_config),
):
    if await repo.get_watch_entry(db, entry_id) is None:
        return _redirect("/watchlist", err="notfound")
    key = await _search_one(request, db, config, entry_id)
    back = request.headers.get("referer") or ""
    target = f"/watchlist/{entry_id}" if f"/watchlist/{entry_id}" in back else "/watchlist"
    if key == "search_failed":
        return _redirect(target, err=key)
    return _redirect(target, ok=key)


@router.post("/watchlist/{entry_id}/pause")
async def watchlist_pause(entry_id: str, user: dict = Depends(verify_csrf), db=Depends(get_db)):
    if not await repo.set_sub_status(db, entry_id, SubStatus.PAUSED):
        return _redirect("/watchlist", err="notfound")
    return _redirect("/watchlist", ok="paused")


@router.post("/watchlist/{entry_id}/resume")
async def watchlist_resume(entry_id: str, user: dict = Depends(verify_csrf), db=Depends(get_db)):
    """恢复：暂停 / 已完成的订阅都回到 active（完成态恢复 = 继续追该歌手的新专辑 / 洗版）。"""
    if not await repo.set_sub_status(db, entry_id, SubStatus.ACTIVE):
        return _redirect("/watchlist", err="notfound")
    await repo.set_watch_enabled(db, entry_id, True)
    return _redirect("/watchlist", ok="resumed")


@router.post("/watchlist/{entry_id}/toggle")
async def watchlist_toggle(entry_id: str, user: dict = Depends(verify_csrf), db=Depends(get_db)):
    """启用 / 停用开关（沿用旧端点：enabled 是比 status 更粗的总闸）。"""
    entry = await repo.get_watch_entry(db, entry_id)
    if entry is None:
        return _redirect("/watchlist", err="notfound")
    await repo.set_watch_enabled(db, entry_id, not entry.enabled)
    return _redirect("/watchlist")


@router.post("/watchlist/{entry_id}/delete")
async def watchlist_delete(
    entry_id: str, user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    """删订阅不可逆（命中流水一并清除，已建任务保留），仅管理员。"""
    if not await repo.delete_subscription(db, entry_id):
        return _redirect("/watchlist", err="notfound")
    return _redirect("/watchlist", ok="deleted")
