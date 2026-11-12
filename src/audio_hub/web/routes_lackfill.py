"""缺集扫描、AY API 优先编排与 audio-hub 自有 Re0 油猴回填桥。

缺集持久化到 lackfill_queue；AY 管理员预览/真实执行与 Re0 回填复用同一套
115/ed2k 分流、转存预留、CD2 文件验证和 Emby 核销链。
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger
from pydantic import BaseModel, Field

from .. import __version__, settings_store
from ..lackfill import (
    LackfillService,
    ay,
    cloud,
    ledger,
    queue,
    re0_browser,
    re0_status,
    reservations,
)
from ..lackfill.refill import ed2k_package_name, normalize_resource_links, resource_identity
from ..notify.base import Event
from . import routes_settings as settings_routes
from .deps import get_config, get_db, require_admin, require_admin_csrf, require_user, verify_csrf
from .routes_pages import _common

router = APIRouter(tags=["lackfill"])


class RefillSubmit(BaseModel):
    tmdb_id: int = Field(gt=0)
    title: str = Field(default="", max_length=300)
    links: str | list[str]
    source: str = Field(default="audio_hub_re0_userscript", max_length=80)


class RefillStatus(BaseModel):
    status: Literal[
        "running", "waiting", "success", "challenge", "failed", "quota", "manual", "not_found"
    ]
    tmdb_id: int | None = Field(default=None, gt=0)
    title: str = Field(default="", max_length=300)
    reason: str = Field(default="", max_length=500)
    stage: str = Field(default="", max_length=40)
    attempts: int | None = Field(default=None, ge=0)
    auto_running: bool = False
    script_version: str = Field(default="", max_length=20)
    queue_count: int = Field(default=0, ge=0)


class RefillPoints(BaseModel):
    """油猴每周从 /manager/account 页解析出的积分快照；数值解析失败允许为空。"""

    balance: int | None = Field(default=None, ge=0)
    rollover: int | None = Field(default=None, ge=0)
    weekly_used_percent: int | None = Field(default=None, ge=0, le=100)
    weekly_quota_raw: str = Field(default="", max_length=40)
    vip: str = Field(default="", max_length=20)


async def _require_refill_key(
    config=Depends(get_config),
    provided: str | None = Header(default=None, alias="X-AudioHub-Refill-Key"),
) -> None:
    expected = config.lackfill.refill_api_key
    if not expected:
        raise HTTPException(status_code=503, detail="refill_token_not_configured")
    if not provided or not secrets.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid_refill_token")


_FLASH_ERR = {
    "disabled": "缺集补齐未启用：请先在设置页的缺集补齐分组启用。",
    "not_configured": "凭据未配齐：还需要 emby_url / emby_api_key / tmdb_api_key 三项才能扫描。",
    "secrets_key": "密钥文件不可用，无法保存回填令牌；请先检查部署的 secrets.key。",
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
    if kind == "ok" and key == "cloud_checked":
        ready = request.query_params.get("ready", "0")
        waiting = request.query_params.get("waiting", "0")
        failed = request.query_params.get("failed", "0")
        return f"CD2 检查完成：新确认 {ready}，等待文件 {waiting}，失败 {failed}。"
    if key == "probe":
        detail = request.query_params.get("detail", "")
        return f"连通性自检：{detail or key}"
    if kind == "err":
        return _FLASH_ERR.get(key, key)
    return key


def _last_report(request: Request):
    """上一轮扫描报告（ScanReport 或 None）；进程内暂存，重启即清。"""
    return getattr(request.app.state, "lackfill_report", None)


async def _re0_context(request: Request, db, cfg) -> dict:
    now = datetime.now().isoformat()
    return {
        "enabled": getattr(cfg, "re0_browser_enabled", False),
        "browser": getattr(request.app.state, "re0_browser_health", {}) or {},
        "worker": await re0_status.worker_status(db, now),
        "ui_url": re0_browser.public_ui_url(
            getattr(cfg, "re0_browser_ui_url", "https://127.0.0.1:3001")
        ),
    }


async def _pipeline_rows(db) -> list[dict]:
    """缺集任务流水：待找(Re0 队列)/转存预留/台账(待核销·部分到位)/CD2 落盘
    合并成一屏进度行。只读总览——操作入口留在各通道详情里(FRONTEND-RULES §1.4)。
    """
    rows: list[dict] = []
    for item in await queue.list_pending(db):
        rows.append({
            "title": item["title"],
            "sub": f"tmdb:{item['tmdb_id']}",
            "missing": item.get("missing"),
            "stage": "待找资源", "kind": "warn",
            "source": "Re0 / 爱影",
            "updated": (item.get("updated_at") or item.get("created_at") or "")[:19],
        })
    for r in await reservations.list_active(db):
        attempted = r.get("stage") == "attempted"
        rows.append({
            "title": r["show_key"], "sub": "",
            "missing": "",
            "stage": "转存待核对" if attempted else "等待转存",
            "kind": "err" if attempted else "warn",
            "source": "115 / SA",
            "updated": (r.get("updated_at") or "")[:19],
        })
    for entry in await ledger.list_by_status(db, "pending", "partial"):
        rows.append({
            "title": entry.get("package_name") or entry["show_key"],
            "sub": entry.get("show_key") or "",
            "missing": "",
            "stage": "待核销" if entry["status"] == "pending" else "部分到位",
            "kind": "warn", "source": "115 转存",
            "updated": (entry.get("updated_at") or "")[:19],
        })
    cloud_stage = {
        "waiting": ("CD2 确认中", "warn"),
        "ready": ("CD2 已确认", "ok"),
        "failed": ("CD2 失败", "err"),
    }
    for c in await cloud.list_by_status(db, "waiting", "ready", "failed"):
        stage, kind = cloud_stage.get(c.get("status"), (str(c.get("status")), "warn"))
        rows.append({
            "title": c.get("package_name") or "（未命名包）",
            "sub": "", "missing": "",
            "stage": stage, "kind": kind, "source": "CD2",
            "updated": (c.get("updated_at") or "")[:19],
        })
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return rows


async def _render_lackfill(
    request: Request,
    user: dict,
    config,
    db,
    *,
    new_refill_token: str | None = None,
):
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
        "refill_items": await queue.list_pending(db),
        "refill_reservations": await reservations.list_active(db),
        "refill_configured": bool(cfg.refill_api_key),
        "re0": await _re0_context(request, db, cfg),
        "ay_enabled": cfg.ay_api_enabled,
        "ay_configured": bool(
            cfg.ay_api_url and cfg.ay_api_token and cfg.ay_tg_id and cfg.tmdb_api_key
        ),
        "ay_status": await ay.quota_status(db),
        "cloud_items": await cloud.list_by_status(db, "waiting", "ready", "failed"),
        "cloud_enabled": cfg.cloud_verify_enabled,
        "pipeline": await _pipeline_rows(db),
        # 设置抽屉:配置跟使用走(FRONTEND-RULES §1.3),主链展开/通道折叠
        "settings_sections": settings_routes.lackfill_settings_sections(config),
        "new_refill_token": new_refill_token,
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
    })
    return request.app.state.templates.TemplateResponse(request, "lackfill.html", ctx)


@router.get("/lackfill", response_class=HTMLResponse)
async def lackfill_page(
    request: Request,
    user: dict = Depends(require_user),
    config=Depends(get_config),
    db=Depends(get_db),
):
    return await _render_lackfill(request, user, config, db)


@router.post("/lackfill/refill-token", response_class=HTMLResponse)
async def generate_refill_token(
    request: Request,
    user: dict = Depends(require_admin_csrf),
    config=Depends(get_config),
    db=Depends(get_db),
):
    token = secrets.token_urlsafe(32)
    spec = next(s for s in settings_store.LACKFILL_SPECS if s.key == "lackfill_refill_api_key")
    try:
        encoded = settings_store.encode_for_storage(spec, token)
    except (OSError, RuntimeError):
        return RedirectResponse("/lackfill?err=secrets_key", status_code=303)
    await settings_store.save_override(db, spec.key, encoded)
    await settings_store.apply_hot(request.app, {spec.key: token})
    logger.info(f"Re0 回填令牌已生成/重置（by {user['username']}）")
    response = await _render_lackfill(request, user, config, db, new_refill_token=token)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/lackfill/reservation/clear")
async def clear_refill_reservation(
    request: Request,
    user: dict = Depends(require_admin_csrf),
    db=Depends(get_db),
):
    show_key = str((await request.form()).get("show_key", "")).strip()
    if not show_key:
        raise HTTPException(status_code=400, detail="show_key_required")
    await reservations.release_attempted(db, show_key)
    logger.info(f"Re0 转存预留已人工核对并清除[{show_key}]（by {user['username']}）")
    return RedirectResponse("/lackfill", status_code=303)


@router.get("/api/lackfill/status")
async def lackfill_status(
    request: Request,
    user: dict = Depends(require_user),
    config=Depends(get_config),
    db=Depends(get_db),
):
    """扫描、AY 额度与 Re0 状态；不返回 Token、cookies 或调试地址。"""
    cfg = config.lackfill
    report = _last_report(request)
    return {
        "enabled": cfg.enabled,
        "configured": LackfillService(cfg).configured,
        "last_report": report.to_dict() if report is not None else None,
        "ay": {
            **await ay.quota_status(db),
            "enabled": cfg.ay_api_enabled,
            "configured": bool(
                cfg.ay_api_url and cfg.ay_api_token and cfg.ay_tg_id and cfg.tmdb_api_key
            ),
        },
        "re0": await _re0_context(request, db, cfg),
    }


@router.get("/api/lackfill/re0_browser/health")
async def re0_browser_health(
    user: dict = Depends(require_admin),
    config=Depends(get_config),
    db=Depends(get_db),
):
    cfg = config.lackfill
    if not cfg.enabled or not getattr(cfg, "re0_browser_enabled", False):
        raise HTTPException(status_code=409, detail="Re0 NAS 浏览器主通道未启用")
    now = datetime.now().isoformat()
    health = await re0_browser.inspect(cfg.re0_browser_cdp_url)
    worker = await re0_status.worker_status(db, now)
    return {
        **health,
        "primary_mode": re0_browser.primary_mode(health, worker),
        "worker": worker,
        "checked_at": now,
        "ui_url": re0_browser.public_ui_url(cfg.re0_browser_ui_url),
    }


async def _run_ay_api_test(config, db, tmdb_id: int, *, execute: bool) -> dict:
    if tmdb_id <= 0:
        raise HTTPException(status_code=422, detail="tmdb_id 必须是正整数")
    cfg = config.lackfill
    if not cfg.enabled or not cfg.ay_api_enabled:
        raise HTTPException(status_code=409, detail="爱影 API 限额补充未启用")
    queued = await queue.get(db, tmdb_id)
    if queued is None:
        raise HTTPException(status_code=404, detail="该剧不在缺集队列中，请先扫描")
    result = await LackfillService(cfg).fill_ay(
        db,
        queued,
        cfg.ay_round_clicks,
        now=datetime.now().isoformat(),
        execute=execute,
    )
    if result["status"] in {"disabled", "unconfigured"}:
        raise HTTPException(status_code=409, detail=result["reason"])
    return {
        "success": result["status"] not in {"failed", "budget"},
        "message": result["reason"],
        "data": result,
    }


@router.get("/api/lackfill/ay_api_test")
async def ay_api_preview(
    user: dict = Depends(require_admin),
    config=Depends(get_config),
    db=Depends(get_db),
    tmdb_id: int = 0,
    save: Literal["0", "1"] = "0",
):
    """管理员只读预览：查询 AY、计算选包，但不调用 115/SA。"""
    if save != "0":
        raise HTTPException(status_code=405, detail="真实提交必须使用 POST + CSRF")
    logger.info(f"AY API 查询预览[tmdb:{tmdb_id}]（by {user['username']}）")
    return await _run_ay_api_test(config, db, tmdb_id, execute=False)


@router.post("/api/lackfill/ay_api_test")
async def ay_api_execute(
    user: dict = Depends(require_admin_csrf),
    config=Depends(get_config),
    db=Depends(get_db),
    tmdb_id: int = 0,
    save: Literal["0", "1"] = "1",
    form_tmdb_id: int = Form(default=0, alias="tmdb_id"),
):
    """管理员真实执行：支持 API query 或页面表单 tmdb_id，进入现有核销链。"""
    if save != "1":
        raise HTTPException(status_code=400, detail="查询预览请使用 GET + save=0")
    tmdb_id = tmdb_id or form_tmdb_id
    logger.info(f"AY API 真实转存[tmdb:{tmdb_id}]（by {user['username']}）")
    return await _run_ay_api_test(config, db, tmdb_id, execute=True)


@router.get("/api/lackfill/ledger")
async def lackfill_ledger(
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """补齐台账 JSON：全部台账行（含需人工确认的 manual）。"""
    rows = await ledger.list_by_status(
        db, "pending", "partial", "complete", "timeout", "failed", "manual"
    )
    return {"rows": rows}


@router.post("/api/lackfill/scan")
async def lackfill_scan(
    request: Request,
    user: dict = Depends(verify_csrf),
    config=Depends(get_config),
    db=Depends(get_db),
):
    """手动触发一轮缺集扫描，并把结果写入持久化回填队列。"""
    cfg = config.lackfill
    if not cfg.enabled:
        raise HTTPException(status_code=409, detail="lackfill_disabled")
    service = LackfillService(cfg)
    if not service.configured:
        raise HTTPException(status_code=409, detail="lackfill_not_configured")
    logger.info(f"缺集扫描开始（by {user['username']}，预算 {cfg.max_round_shows} 部）")
    report = await service.scan(conn=db)
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
    db=Depends(get_db),
):
    """页面表单入口:扫描后 303 回 /lackfill 并带闪信;程序化调用用 /api/lackfill/scan。"""
    cfg = config.lackfill
    if not cfg.enabled:
        return RedirectResponse("/lackfill?err=disabled", status_code=303)
    service = LackfillService(cfg)
    if not service.configured:
        return RedirectResponse("/lackfill?err=not_configured", status_code=303)
    logger.info(f"缺集扫描开始(页面触发,by {user['username']})")
    report = await service.scan(conn=db)
    request.app.state.lackfill_report = report
    return RedirectResponse(
        f"/lackfill?ok=scanned&shows={report.shows_scanned}&lacks={report.lack_show_count}",
        status_code=303,
    )


@router.post("/lackfill/cloud/check")
async def check_cloud_files(
    request: Request,
    _user: dict = Depends(require_admin_csrf),
    config=Depends(get_config),
    db=Depends(get_db),
):
    stats = await cloud.poll_cloud_once(db, config.lackfill, datetime.now().isoformat())
    return RedirectResponse(
        f"/lackfill?ok=cloud_checked&ready={stats['ready']}"
        f"&waiting={stats['waiting']}&failed={stats['failed']}",
        status_code=303,
    )


def _wait_days(created_at: str) -> int:
    try:
        return max(0, (datetime.now() - datetime.fromisoformat(created_at)).days)
    except (TypeError, ValueError):
        return 0


@router.get("/api/lackfill/refill_queue")
async def refill_queue(
    _auth: None = Depends(_require_refill_key),
    db=Depends(get_db),
):
    items = []
    for row in await queue.list_pending(db):
        items.append({
            "tmdb_id": row["tmdb_id"],
            "title": row["title"],
            "year": row["year"],
            "seasons": row["seasons"],
            "missing": row["missing"],
            "wait_days": _wait_days(row["created_at"]),
        })
    return {"success": True, "message": "", "data": {"count": len(items), "items": items}}


@router.post("/api/lackfill/refill_submit")
async def refill_submit(
    body: RefillSubmit,
    _auth: None = Depends(_require_refill_key),
    config=Depends(get_config),
    db=Depends(get_db),
):
    cfg = config.lackfill
    if not cfg.enabled:
        raise HTTPException(status_code=409, detail="lackfill_disabled")
    queued = await queue.get(db, body.tmdb_id)
    if queued is None:
        raise HTTPException(status_code=404, detail="refill_show_not_queued")
    try:
        urls = normalize_resource_links(body.links)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    service = LackfillService(cfg)
    show_key = f"tmdb:{body.tmdb_id}"
    missing = {int(season): list(eps) for season, eps in queued["seasons"].items()}
    verify_round = await ledger.latest_round(db, show_key)
    results = []
    accepted = duplicates = failed = 0
    for kind, url in urls:
        share_code = resource_identity(url)
        reservation = await reservations.reserve(db, show_key, share_code, datetime.now().isoformat())
        if reservation != "reserved":
            duplicates += 1
            results.append({
                "status": "duplicate",
                "share_code": share_code,
                "reason": "已有活动台账或转存预留" if reservation == "ledger" else "转存状态待核对",
            })
            break
        attempt_now = datetime.now().isoformat()

        async def mark_attempted(now=attempt_now):
            await reservations.mark_attempted(db, show_key, now)

        try:
            if kind == "share":
                outcome = await service.transfer_one(
                    db,
                    show_key,
                    None,
                    missing,
                    url,
                    queued["title"],
                    now=attempt_now,
                    emby_id=queued["emby_id"],
                    before_receive=mark_attempted,
                    reservation=True,
                    verify_round=verify_round,
                )
            else:
                outcome = await service.offline_one(
                    db,
                    show_key,
                    None,
                    missing,
                    url,
                    ed2k_package_name(url),
                    now=attempt_now,
                    emby_id=queued["emby_id"],
                    sa=None,
                    before_receive=mark_attempted,
                    reservation=True,
                    verify_round=verify_round,
                )
        except Exception as exc:  # noqa: BLE001 —— 单条失败可尝试下一备选包
            logger.error(f"Re0 回填转存异常[{show_key}]: {type(exc).__name__}")
            outcome = {"outcome": "failed", "reason": "转存链异常"}
        finally:
            await reservations.release(db, show_key)
        if outcome.get("outcome") == "ok":
            accepted += 1
            results.append({
                "status": "accepted", "share_code": share_code, "entry_id": outcome["entry_id"],
            })
            break
        failed += 1
        results.append({
            "status": "failed",
            "share_code": share_code,
            "reason": str(outcome.get("reason") or "转存失败")[:300],
        })
    logger.info(
        f"Re0 回填完成[{show_key}]: 接受 {accepted}，重复 {duplicates}，失败 {failed}"
    )
    success = accepted > 0 or duplicates > 0
    return {
        "success": success,
        "message": "" if accepted else ("已有活动转存" if duplicates else "没有链接转存成功"),
        "data": {
            "submitted": len(urls),
            "accepted": accepted,
            "duplicates": duplicates,
            "failed": failed,
            "results": results,
        },
    }


@router.post("/api/lackfill/refill_status")
async def refill_status(
    request: Request,
    body: RefillStatus,
    _auth: None = Depends(_require_refill_key),
    db=Depends(get_db),
):
    await re0_status.report_status(db, body.model_dump(), datetime.now().isoformat())
    if body.status in {"running", "waiting", "success", "not_found"}:
        return {"success": True}
    reason = body.reason or "油猴自动任务已暂停，请检查 Re0 页面"
    await request.app.state.notifier.emit(Event(
        kind="lackfill_refill",
        site="re0.me",
        title=body.title or (f"TMDB {body.tmdb_id}" if body.tmdb_id else "Re0 回填桥"),
        detail={"status": body.status, "reason": reason, "tmdb_id": body.tmdb_id},
        level="warning",
    ))
    return {"success": True}


def _hours_between(start_iso: str, end_iso: str) -> float:
    try:
        delta = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
    except (TypeError, ValueError):
        return 0.0
    return delta.total_seconds() / 3600


@router.post("/api/lackfill/probe")
async def lackfill_probe(
    request: Request,
    target: str = Form(...),
    _admin: None = Depends(require_admin_csrf),
    config=Depends(get_config),
):
    """连通性自检:凡与外部程序打通的通道必须可一键验证(FRONTEND-RULES §2)。"""
    from urllib.parse import quote

    from ..lackfill import probe as probe_mod

    try:
        result = await probe_mod.probe(target.strip(), config.lackfill)
    except ValueError as exc:
        return RedirectResponse(f"/lackfill?err=probe&detail={quote(str(exc)[:120])}", 303)
    flag = "ok" if result["ok"] else "err"
    return RedirectResponse(
        f"/lackfill?{flag}=probe&detail={quote(result['detail'])}", 303
    )


@router.post("/api/lackfill/re0_points")
async def refill_points_report(
    request: Request,
    body: RefillPoints,
    _auth: None = Depends(_require_refill_key),
    db=Depends(get_db),
):
    """油猴每周积分上报：落库留痕；距上一条 ≥20h 才发通知（周报节流，手点重试不刷屏）。"""
    now = datetime.now().isoformat()
    saved = await re0_status.save_points(db, body.model_dump(), now)
    entry, prev, delta = saved["entry"], saved["prev"], saved["delta"]
    fresh = prev is None or _hours_between(str(prev.get("at") or ""), now) >= 20
    if fresh:
        parts = []
        if entry["balance"] is not None:
            parts.append(f"可用 {entry['balance']}")
        if delta is not None:
            parts.append(f"较上次 {'+' if delta >= 0 else ''}{delta}")
        if entry["rollover"] is not None:
            parts.append(f"累积结转 {entry['rollover']}")
        if entry["weekly_quota_raw"]:
            parts.append(f"每周额度 {entry['weekly_quota_raw']}")
        if entry["weekly_used_percent"] is not None:
            parts.append(f"已用 {entry['weekly_used_percent']}%")
        await request.app.state.notifier.emit(Event(
            kind="lackfill_refill",
            site="re0.me",
            title="Re0 积分周报" if prev is not None else "Re0 积分基线",
            detail={
                "status": "points",
                "summary": "；".join(parts) or "已记录快照",
                "balance": entry["balance"],
                "delta": delta,
                "rollover": entry["rollover"],
                "weekly_quota_raw": entry["weekly_quota_raw"],
                "weekly_used_percent": entry["weekly_used_percent"],
                "at": entry["at"],
            },
            level="info",
        ))
    return {"success": True, "data": {"delta": delta, "notified": fresh}}


@router.get("/api/lackfill/re0_points")
async def refill_points_view(_user: None = Depends(require_user), db=Depends(get_db)):
    """最近积分快照与历史（登录用户可看，便于页面/排查）。"""
    return {"success": True, "data": await re0_status.points_history(db)}
