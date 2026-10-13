"""资料库页：已入库成品 + 回收区（恢复 / 彻底删除）。

回收区动作只碰 recycle_dir 与 library 目录，所有删除都过 archive.fs 的路径围栏。
恢复可逆（任何已登录用户），彻底删除不可逆（仅管理员）。
flash 文案表放在本文件（routes_pages._FLASH_* 属模块 E，不动）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .. import repo
from ..archive.fs import move_tree, path_within, remove_tree
from ..completion.monitor import recycle_slot
from ..models import RecycleEntry
from .deps import get_config, get_db, require_admin_csrf, require_user, verify_csrf
from .routes_pages import _common

router = APIRouter(tags=["library"])

_PAGE_SIZE = 100

_FLASH_OK = {
    "restored": "已从回收区恢复到原路径",
    "purged": "已彻底删除回收区条目",
}
_FLASH_ERR = {
    "notfound": "回收区条目不存在",
    "already_restored": "该条目已恢复过，无需重复操作",
    "missing": "回收区目录已不存在，无法恢复（可直接彻底删除条目）",
    "outside": "路径不在回收区目录之下，拒绝操作",
    "io": "文件操作失败，详见日志",
}


def _flash(request: Request, kind: str) -> str | None:
    key = request.query_params.get(kind)
    if not key:
        return None
    table = _FLASH_OK if kind == "ok" else _FLASH_ERR
    return table.get(key, key)


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    kind: str | None = Query(None),
    page: int = Query(1, ge=1),
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    templates = request.app.state.templates
    kind = kind if kind in ("music", "audiobook") else None
    offset = (page - 1) * _PAGE_SIZE

    library_items = await repo.list_library_items(
        db, kind=kind, limit=_PAGE_SIZE, offset=offset,
    )
    total = await repo.count_library_items(db, kind=kind)
    recycle_count = await repo.count_recycle_entries(db, restored=False)
    # 回收区只列未恢复的：已恢复条目的 recycle_path 已不存在，再列出只会误导
    recycle_entries = await repo.list_recycle_entries(db, restored=False, limit=_PAGE_SIZE)

    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "library",
        "library_items": library_items,
        "total_count": total,
        "music_count": await repo.count_library_items(db, kind="music"),
        "audiobook_count": await repo.count_library_items(db, kind="audiobook"),
        "recycle_count": recycle_count,
        "recycle_entries": recycle_entries,
        "current_kind": kind,
        "page": page,
        "has_next": offset + len(library_items) < total,
        "library_music": config.paths.library_music,
        "library_audiobook": config.paths.library_audiobook,
        "recycle_dir": config.paths.recycle_dir,
        "recycle_retention_days": config.paths.recycle_retention_days,
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
    })
    return templates.TemplateResponse(request, "library.html", ctx)


def _back(key: str, ok: bool = True) -> RedirectResponse:
    return RedirectResponse(f"/library?{'ok' if ok else 'err'}={key}", status_code=303)


@router.post("/library/recycle/{entry_id}/restore")
async def recycle_restore(
    entry_id: int,
    user: dict = Depends(verify_csrf),
    db=Depends(get_db),
    config=Depends(get_config),
):
    """回滚升级：把回收区目录移回原路径。

    原路径若已被（通常是升级版）占用，先把占用者移入回收区并建条目，
    这样回滚本身也可回滚 —— 不会出现"恢复旧版把新版删了"的单向操作。
    """
    entry = await repo.get_recycle_entry(db, entry_id)
    if entry is None:
        return _back("notfound", ok=False)
    if entry.restored_at is not None:
        return _back("already_restored", ok=False)

    recycle_root = Path(config.paths.recycle_dir)
    src = Path(entry.recycle_path)
    if not path_within(src, recycle_root):
        return _back("outside", ok=False)
    if not src.exists():
        return _back("missing", ok=False)

    dst = Path(entry.original_path)
    try:
        if dst.exists():
            displaced_to = recycle_slot(config, entry.library_item_id)
            await move_tree(dst, displaced_to)
            await repo.add_recycle_entry(db, RecycleEntry(
                library_item_id=entry.library_item_id, original_path=str(dst),
                recycle_path=str(displaced_to), reason=f"displaced_by_restore:{entry.id}",
            ))
            logger.info(f"恢复前让位：{dst} → {displaced_to}")
        await move_tree(src, dst)
    except OSError as e:
        logger.error(f"回收区恢复失败 {src} → {dst}: {e}")
        return _back("io", ok=False)

    await repo.mark_recycle_restored(db, entry.id)
    if entry.library_item_id is not None:
        await repo.update_library_item(db, entry.library_item_id, path=str(dst))
    logger.info(f"回收区恢复 (by {user['username']}): {src} → {dst}")
    return _back("restored")


@router.post("/library/recycle/{entry_id}/purge")
async def recycle_purge(
    entry_id: int,
    user: dict = Depends(require_admin_csrf),
    db=Depends(get_db),
    config=Depends(get_config),
):
    """彻底删除回收区条目（目录 + DB 行）。目录不存在时只删行。"""
    entry = await repo.get_recycle_entry(db, entry_id)
    if entry is None:
        return _back("notfound", ok=False)

    recycle_root = Path(config.paths.recycle_dir)
    rp = Path(entry.recycle_path)
    if not path_within(rp, recycle_root):
        return _back("outside", ok=False)
    try:
        await remove_tree(rp, root=recycle_root)
    except OSError as e:
        logger.error(f"回收区删除失败 {rp}: {e}")
        return _back("io", ok=False)

    await repo.delete_recycle_entry(db, entry.id)
    logger.info(f"回收区彻底删除 (by {user['username']}): {rp}")
    return _back("purged")
