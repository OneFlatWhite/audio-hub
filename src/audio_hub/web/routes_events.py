"""事件页：DbNotifier 落库的事件流 + 清理动作（v0.4 第二波）。

- GET  /events        按 kind / level 筛选、50 条一页
- POST /events/clear  清理 30 天前的事件（管理员 + CSRF），flash 带清理条数

detail 列存的是 JSON 文本；这里在路由层拆成「一行摘要 + 美化全文」交给模板的
<details> 展开，模板不做 JSON 解析（Jinja 里没有合适的工具，且坏 JSON 不该让页面 500）。
"""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .. import repo
from ..config import NOTIFY_KINDS
from ..timeutil import utcnow
from .deps import get_config, get_db, require_admin_csrf, require_user
from .routes_pages import _common, _flash

router = APIRouter(tags=["events"])

PAGE_SIZE = 50
_LEVELS = ("info", "warning", "error")
#: 清理阈值：比这更早的事件可删。30 天足够回看一轮 H&R（默认 72h）的前因后果
PURGE_AFTER_DAYS = 30
#: detail 摘要长度（表格单元格内一行）
_SUMMARY_LEN = 80

#: 事件种类的下拉选项基线：NOTIFY_KINDS 已覆盖全部实际产出的事件种类
#: （含 adapter_error / round_summary；v0.6.1 起二者并入 NOTIFY_KINDS）
_BASE_KINDS = NOTIFY_KINDS


@router.get("/events", response_class=HTMLResponse)
async def events_page(
    request: Request,
    kind: str | None = Query(None),
    level: str | None = Query(None),
    page: int = Query(1, ge=1),
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    templates = request.app.state.templates
    current_level = level if level in _LEVELS else None
    current_kind = kind or None
    offset = (page - 1) * PAGE_SIZE

    records = await repo.list_events(
        db, kind=current_kind, level=current_level, limit=PAGE_SIZE, offset=offset,
    )
    total = await repo.count_events(db, kind=current_kind, level=current_level)
    kind_counts = await repo.count_events_by_kind(db)

    # 下拉选项 = 基线种类 ∪ 库里实际出现过的种类（含未来模块新增的 kind），
    # 这样筛选器不会出现「表里有、下拉没有」的死角
    kind_options = sorted(set(_BASE_KINDS) | set(kind_counts))

    flash_ok = _flash(request, "ok")
    purged = request.query_params.get("n")
    if flash_ok and request.query_params.get("ok") == "events_cleared" and purged is not None:
        flash_ok = f"{flash_ok}（{purged} 条）"

    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "events",
        "events": [_row(ev) for ev in records],
        "total_count": total,
        "kind_counts": kind_counts,
        "kind_options": kind_options,
        "level_options": _LEVELS,
        "error_count": await repo.count_events(db, level="error"),
        "current_kind": current_kind,
        "current_level": current_level,
        "page": page,
        "page_size": PAGE_SIZE,
        "has_next": offset + len(records) < total,
        "purge_after_days": PURGE_AFTER_DAYS,
        "flash_ok": flash_ok,
        "flash_err": _flash(request, "err"),
    })
    return templates.TemplateResponse(request, "events.html", ctx)


@router.post("/events/clear")
async def events_clear(
    request: Request,
    user: dict = Depends(require_admin_csrf),
    db=Depends(get_db),
):
    """清理 PURGE_AFTER_DAYS 天前的事件。近期事件一律保留 —— 这是回看故障的唯一依据。"""
    before = utcnow() - timedelta(days=PURGE_AFTER_DAYS)
    n = await repo.purge_events_before(db, before)
    logger.info(f"清理 {PURGE_AFTER_DAYS} 天前的事件 {n} 条（by {user['username']}）")
    return RedirectResponse(f"/events?ok=events_cleared&n={n}", status_code=303)


# ── 内部辅助 ────────────────────────────────────────────────

def _row(ev) -> dict:
    """EventRecord → 模板行。detail 拆成一行摘要与美化后的全文。

    不叫 items/keys/values/get：Jinja 的属性访问优先于下标，这几个名字会撞上 dict 方法。
    """
    summary, pretty = _split_detail(ev.detail)
    return {
        "id": ev.id,
        "created_at": ev.created_at,
        "level": ev.level,
        "kind": ev.kind,
        "site": ev.site,
        "title": ev.title,
        "detail": ev.detail,
        "detail_summary": summary,
        "detail_pretty": pretty,
    }


def _split_detail(detail: str | None) -> tuple[str, str]:
    """JSON 文本 → (一行摘要, 缩进全文)。非 JSON 原样当纯文本处理。"""
    if not detail:
        return "", ""
    try:
        obj = json.loads(detail)
    except (TypeError, ValueError):
        text = detail
        pretty = detail
    else:
        if isinstance(obj, dict):
            text = "  ".join(
                f"{k}={v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}"
                for k, v in obj.items()
            )
        else:
            text = json.dumps(obj, ensure_ascii=False)
        pretty = json.dumps(obj, ensure_ascii=False, indent=2)
    text = " ".join(text.split())
    summary = text if len(text) <= _SUMMARY_LEN else text[: _SUMMARY_LEN - 1] + "…"
    return summary, pretty
