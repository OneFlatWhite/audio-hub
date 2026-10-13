"""隔离区页：校验不通过的内容，支持放行（重新归档）与丢弃。

放行把隔离目录移回 scratch_inbox 后直接走 `archive_from_scratch`（不再校验 ——
人已经看过原因并明确放行）。丢弃只删 quarantine_dir 之下的目录，原种永不碰；
丢弃不可逆，仅管理员可操作，放行任何已登录用户都可。
flash 文案表放在本文件（routes_pages._FLASH_* 属模块 E，不动）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .. import repo
from ..archive.fs import move_tree, path_within, remove_tree, unique_path
from ..completion.monitor import archive_from_scratch
from ..models import QUARANTINE_STATUSES, TaskState
from .deps import get_config, get_db, require_admin_csrf, require_user, verify_csrf
from .routes_pages import _common

router = APIRouter(tags=["quarantine"])

_PAGE_SIZE = 100

_FLASH_OK = {
    "released": "已放行并归档入库",
    "released_skip": "已放行，但归档决策为 skip（库内已有同等或更高音质版本）",
    "discarded": "已丢弃隔离内容",
}
_FLASH_ERR = {
    "notfound": "隔离条目不存在",
    "not_held": "该条目已处理过，无需重复操作",
    "missing": "隔离目录已不存在（可直接丢弃条目）",
    "outside": "路径不在隔离目录之下，拒绝操作",
    "task_missing": "关联任务不存在，无法放行",
    "io": "文件操作失败，详见日志",
    "archive_failed": "归档失败，详见日志",
}


def _flash(request: Request, kind: str) -> str | None:
    key = request.query_params.get(kind)
    if not key:
        return None
    table = _FLASH_OK if kind == "ok" else _FLASH_ERR
    return table.get(key, key)


def _back(key: str, ok: bool = True, status: str = "held") -> RedirectResponse:
    return RedirectResponse(
        f"/quarantine?status={status}&{'ok' if ok else 'err'}={key}", status_code=303,
    )


@router.get("/quarantine", response_class=HTMLResponse)
async def quarantine_page(
    request: Request,
    status: str = Query("held"),
    page: int = Query(1, ge=1),
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    templates = request.app.state.templates
    # "all" 显示全部；其他非法值回落到 held
    current = status if status in (*QUARANTINE_STATUSES, "all") else "held"
    filter_status = None if current == "all" else current
    offset = (page - 1) * _PAGE_SIZE

    quarantine_items = await repo.list_quarantine_items(
        db, status=filter_status, limit=_PAGE_SIZE, offset=offset,
    )
    total = await repo.count_quarantine_items(db, status=filter_status)
    status_counts = {
        st: await repo.count_quarantine_items(db, status=st) for st in QUARANTINE_STATUSES
    }

    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "quarantine",
        "quarantine_items": quarantine_items,
        "total_count": total,
        "status_counts": status_counts,
        "current_status": current,
        "page": page,
        "has_next": offset + len(quarantine_items) < total,
        "quarantine_dir": config.paths.quarantine_dir,
        "validation_on_fail": config.validation.on_fail,
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
    })
    return templates.TemplateResponse(request, "quarantine.html", ctx)


@router.post("/quarantine/{item_id}/release")
async def quarantine_release(
    item_id: int,
    request: Request,
    user: dict = Depends(verify_csrf),
    db=Depends(get_db),
    config=Depends(get_config),
):
    """放行：隔离目录 → scratch_inbox → decide → 归档。校验跳过（人已确认）。"""
    item = await repo.get_quarantine_item(db, item_id)
    if item is None:
        return _back("notfound", ok=False)
    if item.status != "held":
        return _back("not_held", ok=False)

    q_root = Path(config.paths.quarantine_dir)
    src = Path(item.scratch_path)
    if not path_within(src, q_root):
        return _back("outside", ok=False)
    if not src.exists():
        return _back("missing", ok=False)

    task = await repo.get_task(db, item.task_id) if item.task_id else None
    if task is None:
        return _back("task_missing", ok=False)

    dst = unique_path(Path(config.paths.scratch_inbox) / src.name, hint=task.id[:8])
    try:
        await move_tree(src, dst)
    except OSError as e:
        logger.error(f"隔离放行移动失败 {src} → {dst}: {e}")
        return _back("io", ok=False)

    notifier = getattr(request.app.state, "notifier", None)
    try:
        outcome = await archive_from_scratch(db, config, task, dst, notifier=notifier)
    except Exception as e:  # noqa: BLE001 - 归档失败不能让隔离条目悬空
        logger.exception(f"隔离放行后归档失败 task={task.id}: {e}")
        # 条目保持 held 以便重试：尽量把目录移回隔离位；移不回就把条目路径指到 scratch
        try:
            await move_tree(dst, src)
        except OSError:
            await repo.update_quarantine_path(db, item.id, str(dst))
        return _back("archive_failed", ok=False)

    await repo.set_quarantine_status(db, item.id, "released")
    logger.info(
        f"隔离放行 (by {user['username']}): item={item.id} task={task.id} "
        f"→ {outcome.status}/{outcome.decision}"
    )
    key = "released_skip" if outcome.status == "skipped" else "released"
    return _back(key)


@router.post("/quarantine/{item_id}/discard")
async def quarantine_discard(
    item_id: int,
    user: dict = Depends(require_admin_csrf),
    db=Depends(get_db),
    config=Depends(get_config),
):
    """丢弃：删隔离目录（围栏 quarantine_dir），任务 → REJECTED，条目 → discarded。"""
    item = await repo.get_quarantine_item(db, item_id)
    if item is None:
        return _back("notfound", ok=False)
    if item.status != "held":
        return _back("not_held", ok=False)

    q_root = Path(config.paths.quarantine_dir)
    src = Path(item.scratch_path)
    if not path_within(src, q_root):
        return _back("outside", ok=False)
    try:
        await remove_tree(src, root=q_root)
    except OSError as e:
        logger.error(f"隔离丢弃删除失败 {src}: {e}")
        return _back("io", ok=False)

    if item.task_id:
        await repo.set_task_state(
            db, item.task_id, TaskState.REJECTED, reject_reason="quarantine_discarded",
        )
    await repo.set_quarantine_status(db, item.id, "discarded")
    logger.info(f"隔离丢弃 (by {user['username']}): item={item.id} task={item.task_id}")
    return _back("discarded")
