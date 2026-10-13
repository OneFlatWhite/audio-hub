"""做种页：H&R 跟踪记录与手动动作（v0.4 模块 D）。

页面展示 seed_records；四个 POST 动作（暂停 / 恢复 / 标记达标 / 立即检查）全部
经 SeedingMonitor 落地 —— 路由层不直接碰 qb 客户端，保证「本项目只会对种子
pause / resume」这条红线只需在 seeding/lifecycle.py 一处审计。

qb 不可达统一转成 flash 错误而非 500：NAS 上 qb 重启是常态，页面不该因此挂掉。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .. import repo
from ..seeding.lifecycle import QbUnavailable, SeedingMonitor, progress_pct
from .deps import get_config, get_db, require_user, verify_csrf
from .routes_pages import _common, _flash

router = APIRouter(tags=["seeding"])

_PAGE_SIZE = 100
_STATUSES = ("seeding", "satisfied", "paused", "removed")

# 本页专属的 ?ok= / ?err= 文案；routes_pages 的全局表归模块 E 维护，这里不动它。
_SEED_FLASH_OK = {
    "paused": "已暂停做种（种子与文件保留，可随时恢复）",
    "resumed": "已恢复做种",
    "marked": "已手动标记为 H&R 达标",
    "checked": "已完成一次做种检查",
}
_SEED_FLASH_ERR = {
    "notfound": "做种记录不存在",
    "qb_unreachable": "qBittorrent 不可达或未配置 API Key，动作未执行",
    "check_skipped": "做种检查未能连接 qBittorrent，本次跳过",
}


def _seed_flash(request: Request, kind: str) -> str | None:
    """先查本页文案表，查不到再回落到全局 _flash（含「原样显示 key」的兜底）。"""
    key = request.query_params.get(kind)
    if not key:
        return None
    table = _SEED_FLASH_OK if kind == "ok" else _SEED_FLASH_ERR
    return table.get(key) or _flash(request, kind)


def _monitor(request: Request) -> SeedingMonitor:
    """优先复用 runner 持有的监视器（与调度任务共享防重入与 qb 客户端）。

    lifespan 未装配 runner（理论上不会）时按需构造一个，依赖全部来自 app.state。
    """
    state = request.app.state
    runner = getattr(state, "runner", None)
    mon = getattr(runner, "seeding_monitor", None)
    if mon is not None:
        return mon
    return SeedingMonitor(
        state.db, state.config,
        notifier=getattr(state, "notifier", None),
        qb_client_factory=lambda: getattr(state, "qb_client", None),
    )


def _back(status: str | None, **params: str) -> RedirectResponse:
    """跳回列表页并保留当前筛选。"""
    query = [f"{k}={v}" for k, v in params.items()]
    if status in _STATUSES:
        query.append(f"status={status}")
    target = "/seeding" + ("?" + "&".join(query) if query else "")
    return RedirectResponse(target, status_code=303)


# ── 页面 ────────────────────────────────────────────────────

@router.get("/seeding", response_class=HTMLResponse)
async def seeding_page(
    request: Request,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    templates = request.app.state.templates
    current = status if status in _STATUSES else None
    offset = (page - 1) * _PAGE_SIZE

    seed_records = await repo.list_seed_records(
        db, status=current, limit=_PAGE_SIZE, offset=offset,
    )
    total = await repo.count_seed_records(db, status=current)
    status_counts = {st: await repo.count_seed_records(db, status=st) for st in _STATUSES}

    # 标题来自 tasks 表；seed_records 只存 task_id，避免双写标题
    briefs = await repo.get_task_briefs(db, task_ids=[r.task_id for r in seed_records])
    titles = {b["id"]: b["title"] for b in briefs}
    seed_extra = {
        r.task_id: {
            "title": titles.get(r.task_id, ""),
            "pct": progress_pct(r),
            "time_pct": int(min(r.seed_seconds / r.required_seconds, 1.0) * 100)
            if r.required_seconds else 0,
            "ratio_pct": int(min(r.ratio / r.required_ratio, 1.0) * 100)
            if r.required_ratio else 0,
        }
        for r in seed_records
    }

    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "seeding",
        "seed_records": seed_records,
        "seed_extra": seed_extra,
        "total_count": total,
        "status_counts": status_counts,
        "current_status": current,
        "page": page,
        "has_next": offset + len(seed_records) < total,
        "seeding_enabled": config.seeding.enabled,
        "check_interval_minutes": config.seeding.check_interval_minutes,
        "default_min_seed_hours": config.seeding.default_min_seed_hours,
        "default_min_ratio": config.seeding.default_min_ratio,
        "on_satisfied": config.seeding.on_satisfied,
        "flash_ok": _seed_flash(request, "ok"),
        "flash_err": _seed_flash(request, "err"),
    })
    return templates.TemplateResponse(request, "seeding.html", ctx)


# ── 动作 ────────────────────────────────────────────────────

@router.post("/seeding/{task_id}/pause")
async def seeding_pause(
    task_id: str,
    request: Request,
    status: str | None = Query(None),
    user: dict = Depends(verify_csrf),
):
    try:
        rec = await _monitor(request).pause(task_id)
    except QbUnavailable as e:
        logger.warning(f"手动暂停失败 {task_id[:8]}: {e}")
        return _back(status, err="qb_unreachable")
    if rec is None:
        return _back(status, err="notfound")
    logger.info(f"手动暂停做种 {task_id[:8]}（by {user['username']}）")
    return _back(status, ok="paused")


@router.post("/seeding/{task_id}/resume")
async def seeding_resume(
    task_id: str,
    request: Request,
    status: str | None = Query(None),
    user: dict = Depends(verify_csrf),
):
    try:
        rec = await _monitor(request).resume(task_id)
    except QbUnavailable as e:
        logger.warning(f"手动恢复失败 {task_id[:8]}: {e}")
        return _back(status, err="qb_unreachable")
    if rec is None:
        return _back(status, err="notfound")
    logger.info(f"手动恢复做种 {task_id[:8]}（by {user['username']}）")
    return _back(status, ok="resumed")


@router.post("/seeding/{task_id}/mark-satisfied")
async def seeding_mark_satisfied(
    task_id: str,
    request: Request,
    status: str | None = Query(None),
    user: dict = Depends(verify_csrf),
):
    """纯 DB 动作，不碰 qb：站点已确认免除 H&R、或人工核对过做种记录时使用。"""
    rec = await _monitor(request).mark_satisfied(task_id)
    if rec is None:
        return _back(status, err="notfound")
    logger.info(f"手动标记 H&R 达标 {task_id[:8]}（by {user['username']}）")
    return _back(status, ok="marked")


@router.post("/seeding/check-now")
async def seeding_check_now(
    request: Request,
    status: str | None = Query(None),
    user: dict = Depends(verify_csrf),
):
    """立即跑一轮做种轮询。poll_once 自身吞掉 qb 异常，这里只按 skipped 决定文案。"""
    summary = await _monitor(request).poll_once()
    logger.info(f"手动触发做种检查（by {user['username']}）: {summary.as_dict()}")
    if summary.skipped:
        return _back(status, err="check_skipped")
    return _back(status, ok="checked")
