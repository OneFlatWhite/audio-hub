"""连载页：多部有声书 / 分卷合辑的追踪进度（v0.4 模块 C）。

列表 / 详情 / 建立 / 编辑 / 删除。分集号解析与完结判定在 series.tracker，
这里只做表单校验与 repo 调用。flash 文案在本模块内映射（不改 routes_pages）。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import repo
from ..models import SeriesEntry, WatchKind
from ..series.tracker import DEFAULT_PART_PATTERN, refresh_status
from .deps import get_config, get_db, require_admin_csrf, require_user, verify_csrf
from .routes_pages import _common

router = APIRouter(tags=["series"])

_PAGE_SIZE = 100
_STATUSES = ("active", "completed", "paused")
#: 编辑表单可手动设置的状态；completed 由 refresh_status 按进度判定，不手选
_EDITABLE_STATUSES = ("active", "paused")

_FLASH_OK = {
    "created": "连载已建立，并已绑定到订阅项",
    "updated": "连载已更新",
    "deleted": "连载已删除，订阅项已解绑",
}
_FLASH_ERR = {
    "notfound": "连载不存在",
    "empty_name": "连载名称不能为空",
    "bad_watch": "请选择一个有声书订阅项",
    "watch_bound": "该订阅项已绑定其他连载，请先删除旧连载",
    "bad_total": "总部数必须是正整数，或留空表示未知",
    "bad_pattern": "分集规则不是有效的正则，或缺少捕获组",
    "bad_status": "状态只能是 active 或 paused",
}


def _flash(request: Request, kind: str) -> str | None:
    key = request.query_params.get(kind)
    if not key:
        return None
    table = _FLASH_OK if kind == "ok" else _FLASH_ERR
    return table.get(key, key)


def _parse_total(raw: str) -> tuple[bool, int | None]:
    """总部数：空 → None（未知）；正整数 → 值；其他 → 无效。"""
    raw = (raw or "").strip()
    if not raw:
        return True, None
    if not raw.isdigit() or int(raw) <= 0:
        return False, None
    return True, int(raw)


def _parse_pattern(raw: str) -> tuple[bool, str | None]:
    """分集规则：空 → None（用默认）；有效且带捕获组 → 原样；其他 → 无效。"""
    raw = (raw or "").strip()
    if not raw or raw == DEFAULT_PART_PATTERN:
        return True, None
    try:
        compiled = re.compile(raw, re.IGNORECASE)
    except re.error:
        return False, None
    if compiled.groups == 0:
        return False, None
    return True, raw


async def _page_ctx(request: Request, db, config, user: dict, *, status: str | None,
                    page: int, watch_id: str | None, detail_series: SeriesEntry | None) -> dict:
    """列表页与详情页共用的上下文。"""
    current = status if status in _STATUSES else None
    offset = (page - 1) * _PAGE_SIZE

    series_rows = await repo.list_series(db, status=current, limit=_PAGE_SIZE, offset=offset)
    total = await repo.count_series(db, status=current)
    part_counts = await repo.count_parts_by_series(db)
    status_counts = {st: await repo.count_series(db, status=st) for st in _STATUSES}

    # 订阅项：名称映射 + 可绑定的有声书订阅（未绑定连载的排前面）
    all_entries = await repo.list_watch_entries(db, enabled_only=False)
    watch_names = {e.id: e.query for e in all_entries}
    book_entries = sorted(
        (e for e in all_entries if e.kind == WatchKind.AUDIOBOOK),
        key=lambda e: (e.series_id is not None, e.query),
    )

    # ?watch_id= 预填：订阅名作为默认连载名
    prefill_watch = None
    if watch_id:
        prefill_watch = next((e for e in book_entries if e.id == watch_id), None)

    detail_parts = []
    if detail_series is not None:
        detail_parts = await repo.list_series_parts(db, detail_series.id)

    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "series",
        "series_rows": series_rows,
        "part_counts": part_counts,
        "total_count": total,
        "status_counts": status_counts,
        "current_status": current,
        "page": page,
        "has_next": offset + len(series_rows) < total,
        "watch_names": watch_names,
        "book_entries": book_entries,
        "prefill_watch": prefill_watch,
        "default_pattern": DEFAULT_PART_PATTERN,
        "detail_series": detail_series,
        "detail_parts": detail_parts,
        "editable_statuses": _EDITABLE_STATUSES,
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
    })
    return ctx


@router.get("/series", response_class=HTMLResponse)
async def series_page(
    request: Request,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    watch_id: str | None = Query(None),
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    templates = request.app.state.templates
    ctx = await _page_ctx(request, db, config, user, status=status, page=page,
                          watch_id=watch_id, detail_series=None)
    return templates.TemplateResponse(request, "series.html", ctx)


@router.get("/series/{series_id}", response_class=HTMLResponse)
async def series_detail(
    request: Request,
    series_id: int,
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    """详情：在列表页上方展开该连载的分卷表与编辑表单。"""
    series = await repo.get_series(db, series_id)
    if series is None:
        return RedirectResponse("/series?err=notfound", status_code=303)
    templates = request.app.state.templates
    ctx = await _page_ctx(request, db, config, user, status=None, page=1,
                          watch_id=None, detail_series=series)
    return templates.TemplateResponse(request, "series.html", ctx)


@router.post("/series")
async def series_create(
    request: Request,
    watch_id: str = Form(default=""),
    name: str = Form(default=""),
    total_parts: str = Form(default=""),
    part_pattern: str = Form(default=""),
    user: dict = Depends(verify_csrf),
    db=Depends(get_db),
):
    watch_id = watch_id.strip()
    entry = await repo.get_watch_entry(db, watch_id) if watch_id else None
    if entry is None or entry.kind != WatchKind.AUDIOBOOK:
        return RedirectResponse("/series?err=bad_watch", status_code=303)
    if entry.series_id is not None and await repo.get_series(db, entry.series_id) is not None:
        return RedirectResponse(f"/series?err=watch_bound&watch_id={watch_id}", status_code=303)

    name = name.strip() or entry.query
    if not name:
        return RedirectResponse("/series?err=empty_name", status_code=303)
    ok, total = _parse_total(total_parts)
    if not ok:
        return RedirectResponse(f"/series?err=bad_total&watch_id={watch_id}", status_code=303)
    ok, pattern = _parse_pattern(part_pattern)
    if not ok:
        return RedirectResponse(f"/series?err=bad_pattern&watch_id={watch_id}", status_code=303)

    sid = await repo.add_series(db, SeriesEntry(
        watch_id=watch_id, name=name, total_parts=total, part_pattern=pattern,
    ))
    await repo.set_watch_series(db, watch_id, sid)
    return RedirectResponse(f"/series/{sid}?ok=created", status_code=303)


@router.post("/series/{series_id}/edit")
async def series_edit(
    request: Request,
    series_id: int,
    name: str = Form(default=""),
    total_parts: str = Form(default=""),
    part_pattern: str = Form(default=""),
    status: str = Form(default="active"),
    user: dict = Depends(verify_csrf),
    db=Depends(get_db),
):
    series = await repo.get_series(db, series_id)
    if series is None:
        return RedirectResponse("/series?err=notfound", status_code=303)

    back = f"/series/{series_id}"
    name = name.strip()
    if not name:
        return RedirectResponse(f"{back}?err=empty_name", status_code=303)
    ok, total = _parse_total(total_parts)
    if not ok:
        return RedirectResponse(f"{back}?err=bad_total", status_code=303)
    ok, pattern = _parse_pattern(part_pattern)
    if not ok:
        return RedirectResponse(f"{back}?err=bad_pattern", status_code=303)
    if status not in _EDITABLE_STATUSES:
        return RedirectResponse(f"{back}?err=bad_status", status_code=303)

    await repo.update_series(
        db, series_id, name=name, total_parts=total, part_pattern=pattern, status=status,
    )
    # 总部数变化可能让连载刚好集齐 / 不再集齐，由 refresh_status 统一判定
    notifier = getattr(request.app.state, "notifier", None)
    await refresh_status(db, series_id, notifier=notifier)
    return RedirectResponse(f"{back}?ok=updated", status_code=303)


@router.post("/series/{series_id}/delete")
async def series_delete(
    series_id: int,
    user: dict = Depends(require_admin_csrf),
    db=Depends(get_db),
):
    """删连载会连带删分卷登记并解绑订阅项，不可逆，仅管理员。"""
    deleted = await repo.delete_series(db, series_id)
    if not deleted:
        return RedirectResponse("/series?err=notfound", status_code=303)
    return RedirectResponse("/series?ok=deleted", status_code=303)
