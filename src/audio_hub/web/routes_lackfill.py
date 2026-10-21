"""缺集补齐（v0.8 / M1）：/lackfill 页面 + 扫描状态与触发 API。

数据流：页面 / status 只读 config.lackfill 与 app.state.lackfill_report
（上一轮扫描的 ScanReport，进程内暂存，重启即清）；
POST /api/lackfill/scan 在请求内同步执行一轮扫描（service 层已对 TMDB
限速、单剧异常不中断），完成后把报告写回 app.state 供页面渲染。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from .. import __version__
from ..lackfill import LackfillService
from .deps import get_config, get_db, require_user, verify_csrf
from .routes_pages import _common

router = APIRouter(tags=["lackfill"])

_FLASH_ERR = {
    "disabled": "缺集补齐未启用：请在 config.yaml 的 lackfill 段开启。",
    "not_configured": "凭据未配齐：还需要 emby_url / emby_api_key / tmdb_api_key 三项才能扫描。",
}


def _flash(request: Request, kind: str) -> str | None:
    """表单回跳携带的闪信（?ok=scanned&shows=X&lacks=Y / ?err=...）。"""
    key = request.query_params.get(kind)
    if not key:
        return None
    if kind == "ok" and key == "scanned":
        shows = request.query_params.get("shows", "?")
        lacks = request.query_params.get("lacks", "?")
        return f"扫描完成：本轮扫描 {shows} 部，发现缺集 {lacks} 部。"
    if kind == "err":
        return _FLASH_ERR.get(key, key)
    return key


def _last_report(request: Request):
    """上一轮扫描报告（ScanReport 或 None）；进程内暂存，重启即清。"""
    return getattr(request.app.state, "lackfill_report", None)


@router.get("/lackfill", response_class=HTMLResponse)
async def lackfill_page(
    request: Request,
    user: dict = Depends(require_user),
    config=Depends(get_config),
    db=Depends(get_db),
):
    """缺集补齐页：启用状态、上一轮扫描摘要与逐剧缺集明细。"""
    cfg = config.lackfill
    report = _last_report(request)
    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "lackfill",
        "version": __version__,
        "enabled": cfg.enabled,
        "configured": LackfillService(cfg).configured,
        "max_round_shows": cfg.max_round_shows,
        "last_report": report.to_dict() if report is not None else None,
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
    })
    return request.app.state.templates.TemplateResponse(request, "lackfill.html", ctx)


@router.get("/api/lackfill/status")
async def lackfill_status(
    request: Request,
    user: dict = Depends(require_user),
    config=Depends(get_config),
):
    """扫描状态 JSON：enabled / configured / 上一轮报告（无则为 null）。"""
    cfg = config.lackfill
    report = _last_report(request)
    return {
        "enabled": cfg.enabled,
        "configured": LackfillService(cfg).configured,
        "last_report": report.to_dict() if report is not None else None,
    }


@router.post("/api/lackfill/scan")
async def lackfill_scan(
    request: Request,
    user: dict = Depends(verify_csrf),
    config=Depends(get_config),
):
    """手动触发一轮缺集扫描（同步执行，完成后返回报告）。"""
    cfg = config.lackfill
    if not cfg.enabled:
        raise HTTPException(status_code=409, detail="lackfill_disabled")
    service = LackfillService(cfg)
    if not service.configured:
        raise HTTPException(status_code=409, detail="lackfill_not_configured")
    logger.info(f"缺集扫描开始（by {user['username']}，预算 {cfg.max_round_shows} 部）")
    report = await service.scan()
    request.app.state.lackfill_report = report
    logger.info(
        f"缺集扫描完成: 扫描 {report.shows_scanned}/{report.shows_total} 部，"
        f"缺集 {report.lack_show_count} 部 / {report.lack_episode_count} 集，"
        f"错误 {len(report.errors)} 条"
    )
    return report.to_dict()


@router.post("/lackfill/scan")
async def lackfill_scan_form(
    request: Request,
    user: dict = Depends(verify_csrf),
    config=Depends(get_config),
):
    """页面表单入口:扫描后 303 回 /lackfill 并带闪信;程序化调用用 /api/lackfill/scan。"""
    from fastapi.responses import RedirectResponse

    cfg = config.lackfill
    if not cfg.enabled:
        return RedirectResponse("/lackfill?err=disabled", status_code=303)
    service = LackfillService(cfg)
    if not service.configured:
        return RedirectResponse("/lackfill?err=not_configured", status_code=303)
    logger.info(f"缺集扫描开始(页面触发,by {user['username']})")
    report = await service.scan()
    request.app.state.lackfill_report = report
    return RedirectResponse(
        f"/lackfill?ok=scanned&shows={report.shows_scanned}&lacks={report.lack_show_count}",
        status_code=303,
    )
