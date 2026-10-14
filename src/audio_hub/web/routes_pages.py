"""页面路由：7 个页面（PRD §12.2）。

渲染策略：服务端渲染完整页面 + 原生 fetch 做局部刷新。
不使用 HTMX（避免额外运行时依赖），也不使用 CDN（NAS 内网需离线可用）。
"""

from __future__ import annotations

import time
from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .. import repo, settings_store
from ..models import HitDecision, SubStatus
from ..timeutil import SITE_TZ, utcnow
from . import auth
from .deps import get_config, get_db, require_user, verify_csrf

router = APIRouter(tags=["pages"])

# 拒绝原因（PRD §13.7：原因分布直接指导规则迭代方向）
REJECT_REASONS = [
    ("wrong_version", "版本不对（Live/再版/合辑）"),
    ("quality_mismatch", "音质不符"),
    ("already_have", "已有"),
    ("not_target", "非目标内容"),
    ("fake_lossless", "疑似假无损/升频"),
    ("other", "其他"),
]


async def _common(request: Request, db, config, user: dict) -> dict:
    """所有页面共享的模板上下文（导航徽标、模式标识等）。

    集中一处，避免 6 个页面路由各自拼凑导致字段漂移。
    """
    return {
        "user": user,
        "csrf": getattr(request.state, "csrf", ""),
        "dry_run": config.dry_run,
        "review_count": await repo.count_pending_review(db),
        # 隔离区待处理数（held）：与待审队列同理，是需要人工介入的积压量
        "quarantine_badge": await repo.count_quarantine_items(db, status="held"),
        # 有待重启改动时全页面挂横幅。放在 _common 而非只在 settings 页，
        # 是因为「界面显示实跑但运行时仍空跑」这种不一致，在任何页面
        # 都可能误导使用者做出错误判断（比如以为不会真下载）。
        "restart_pending": await settings_store.is_restart_pending(db),
        # 新版本检测（v0.15）：update_check job 每日更新；None = 未检测/已关闭
        "update_info": getattr(request.app.state, "update_info", None),
    }


# query 参数 ok/err → 用户可读的 flash 文案
_FLASH_OK = {
    "added": "订阅项已添加",
    "deleted": "订阅项已删除",
    "accepted": "已采纳",
    "rejected": "已拒绝",
    "run_once": "已手动触发一轮，结果见总览与命中记录",
    "synced": "已同步下载器状态，完成的任务已推进归档",
    "password": "密码已修改",
    "revoke": "已登出所有设备",
    "saved_general": "运行参数已保存",
    "saved_qb": "qBittorrent 设置已保存",
    "saved_sites": "站点开关已保存，适配器已重建",
    "saved_paths": "路径设置已保存，重启后生效",
    "saved_validation": "校验设置已保存",
    "saved_scrape": "刮削设置已保存",
    "saved_seeding": "做种设置已保存",
    "saved_notify": "通知设置已保存，推送渠道已重建",
    "notify_test": "测试通知已发送，请到对应渠道查收",
    "events_cleared": "已清理 30 天前的事件",
}
_FLASH_ERR = {
    "empty": "查询词不能为空",
    "notfound": "记录不存在",
    "bad_reason": "请选择有效的拒绝原因",
    "dispatch_failed": "派发失败，详见命中记录的错误信息",
    # 采纳时经站点适配器换取下载直链（馒头 genDlToken）失败，任务已标记 failed
    "resolve_failed": "换取下载直链失败（站点适配器报错），详见命中记录的错误信息",
    "need_admin": "该页面仅管理员可访问",
    "old_password": "当前密码不正确",
    "weak_password": "新密码至少 8 位",
    # 保存凭据时密钥文件缺失 / 不可读（部署问题，不是输入问题）
    "secrets_key": "无法加密凭据：密钥文件不存在或不可读。请检查 AUDIO_HUB_SECRETS_KEY "
                   "指向的文件，或运行 scripts/init_secrets.py 生成密钥后重试。",
    # 通知测试：没有任何渠道可测 / 至少一个渠道发送失败（逐渠道结果在 detail）
    "notify_none": "尚未配置任何推送渠道：请先在「通知推送」保存企微 Webhook、"
                   "Telegram（Token + Chat ID）或通用 Webhook 地址。",
    "notify_test_failed": "测试通知未全部成功",
}


def _flash(request: Request, kind: str) -> str | None:
    """把 ?ok=xxx / ?err=xxx 转成可读文案。"""
    key = request.query_params.get(kind)
    if not key:
        return None
    table = _FLASH_OK if kind == "ok" else _FLASH_ERR
    return table.get(key, key)


# ── 首页重定向 ──────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(require_user), db=Depends(get_db)):
    """首次启动无管理员时引导到 /setup。"""
    if await auth.count_users(db) == 0:
        return RedirectResponse("/setup", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


# ── / 总览看板 ──────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    templates = request.app.state.templates
    by_state = await repo.count_tasks_by_state(db)
    entries = await repo.list_watch_entries(db, enabled_only=False)
    sites = await _site_status(db, config)

    ctx = await _common(request, db, config, user)
    # v0.4 卡片：隔离 / H&R / 连载 / 事件。全部是 COUNT / LIMIT 查询，空库返回 0 / []，
    # 模板不需要额外的 None 判断；唯一可为 None 的是「最近完成的连载」。
    last_completed = await repo.list_series(db, status="completed", limit=1)
    ctx.update({
        "active": "dashboard",
        "by_state": by_state,
        "watchlist_count": len(entries),
        "enabled_count": sum(1 for e in entries if e.enabled),
        "failed_count": by_state.get("failed", 0),
        "sites": sites,
        "disk": _disk_usage(),
        # 隔离区待处理（held）：与导航徽标同源，卡片单独命名以免模板误用徽标语义
        "quarantine_held": ctx["quarantine_badge"],
        # H&R 未达标 = 仍在 seeding 状态的做种记录（satisfied / paused / removed 都不算）
        "hr_pending": await repo.count_seed_records(db, status="seeding"),
        "series_active": await repo.count_series(db, status="active"),
        "series_last_completed": last_completed[0] if last_completed else None,
        "recent_events": await repo.list_events(db, limit=10),
        # v0.6 卡：下载器状态（来自 downloaders 表 + 最近测试）与订阅概览
        "downloaders": await _downloader_cards(db),
        "subs": await _subscription_overview(db, config),
        "flash_ok": _flash(request, "ok"),
        # 非管理员访问设置页会被弹回这里，需要能显示原因
        "flash_err": _flash(request, "err"),
    })
    return templates.TemplateResponse(request, "dashboard.html", ctx)


def _disk_usage() -> dict | None:
    """磁盘余量（PRD §12.2 总览要求）。失败不阻断页面。"""
    try:
        import shutil
        from pathlib import Path
        p = Path("/volume1") if Path("/volume1").exists() else Path.cwd()
        total, used, free = shutil.disk_usage(p)
        return {
            "path": str(p),
            "total_gb": round(total / 1024**3, 1),
            "free_gb": round(free / 1024**3, 1),
            "used_pct": round(used / total * 100, 1) if total else 0,
        }
    except Exception as e:  # noqa: BLE001 - 磁盘信息属装饰性，失败即隐藏
        logger.debug(f"磁盘信息获取失败: {e}")
        return None


async def _downloader_cards(db) -> list[dict]:
    """总览「下载器」面板：每台下载器一行状态。

    数据来自 downloaders 表 + 最近一次「测试连接」结果（last_test_json，
    由 /downloaders/{id}/test 落库）；在途任务数取 count_active_tasks_by_downloader。
    不做实时探测——总览页高频打开，不能每次去敲 qb。
    """
    downloaders = await repo.list_downloaders(db)
    active = await repo.count_active_tasks_by_downloader(db)
    cards = []
    for d in downloaders:
        test = d.last_test or {}
        if not d.enabled:
            state, label = "off", "已禁用"
        elif test.get("ok") is True:
            state, label = "ok", "连接正常"
        elif test.get("ok") is False:
            state, label = "err", "连接失败"
        else:
            state, label = "off", "未测试"
        free_gb = test.get("free_space_bytes")
        cards.append({
            "id": d.id, "name": d.name, "endpoint": f"{d.host}:{d.port}",
            "is_default": d.is_default, "state": state, "state_label": label,
            "version": test.get("version") or "",
            "error": test.get("error") or "",
            "free_gb": round(free_gb / 1024**3, 1) if isinstance(free_gb, int | float) else None,
            "active_tasks": active.get(d.id, 0),
        })
    return cards


async def _subscription_overview(db, config) -> dict:
    """总览「订阅概览」卡：订阅中 / 已完成 / 今日命中 / 下次搜索。

    「今日」按站点本地时区（Asia/Shanghai）划界；DB 比较统一用 UTC（timeutil 约定）。
    """
    by_status = await repo.count_subscriptions_by_status(db)
    today_local = utcnow().astimezone(SITE_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    next_at = await repo.next_subscription_search_at(db)
    return {
        "active": by_status.get(SubStatus.ACTIVE.value, 0),
        "completed": by_status.get(SubStatus.COMPLETED.value, 0),
        "today_hits": await repo.count_hits_since(db, today_local.astimezone(UTC)),
        # 有从未搜过的活跃订阅时，下一轮扫描就会搜它，「下次搜索」显示为「即将」
        "pending_search": await repo.has_pending_subscription_search(db),
        "next_search_local": next_at.astimezone(SITE_TZ).strftime("%m-%d %H:%M") if next_at else None,
        "interval_minutes": config.search.subscription_scan_minutes,
    }


# ── /watchlist 订阅：v0.6 起在 routes_subscriptions（列表 / 新建 / 详情 / 立即搜索 / 暂停恢复）──


# ── /records 命中记录 ───────────────────────────────────────

@router.get("/records", response_class=HTMLResponse)
async def records_page(
    request: Request,
    state: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, le=1000),
    limit: int | None = Query(default=None, le=1000),  # 旧参数别名：等价 page_size 且回到第 1 页
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    templates = request.app.state.templates
    if limit is not None:
        page, page_size = 1, limit
    total = await _count_tasks(db, state=state)
    offset = (page - 1) * page_size
    rows = await _query_tasks(db, state=state, limit=page_size, offset=offset)
    by_state = await repo.count_tasks_by_state(db)
    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "records",
        "tasks": rows, "by_state": by_state, "current_state": state,
        "flash_ok": _flash(request, "ok"), "flash_err": _flash(request, "err"),
        "page": page, "page_size": page_size, "total": total,
        "total_pages": max((total + page_size - 1) // page_size, 1),
        "has_next": offset + len(rows) < total,
    })
    return templates.TemplateResponse(request, "records.html", ctx)


@router.post("/records/sync-now")
async def records_sync_now(
    request: Request,
    user: dict = Depends(verify_csrf),
):
    """手动触发一次完成监听轮询（不等 poll_interval_minutes 的下一轮）。

    把「下载器里已完成」立即推进到命中记录 / 归档链路；页面 30s 自动刷新，
    配合本按钮，状态不需要用户手动刷新页面。
    """
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        return RedirectResponse("/records", status_code=303)
    stats = await runner.run_completion_poll_once()
    logger.info(f"手动同步下载器状态（by {user['username']}）: {stats}")
    return RedirectResponse("/records?ok=synced", status_code=303)


# ── 下载进度（命中记录页进度条数据源）────────────────────────

#: 进度聚合缓存：命中记录页 5s 轮询，但下载器 API 查询节流到 8s
_DLPROG: dict[str, Any] = {"at": 0.0, "data": {}}
_DLPROG_TTL = 8.0


@router.get("/api/download-progress")
async def download_progress(request: Request, user: dict = Depends(require_user)):
    """各下载器种子实时进度：{info_hash: {pct, dlspeed, eta, state}}（需登录）。

    多下载器聚合，单台失败跳过；8 秒进程内节流避免多端打开页面打爆 API。
    空结果 / 查询失败同样按窗口缓存：否则下载器全闲（无在途种子）或临时
    不可达时节流失效，每次轮询都真打下载器。失败只缓存一个窗口（8s），
    不把瞬态错误缓存太久。
    """
    now = time.monotonic()
    if now - _DLPROG["at"] < _DLPROG_TTL:
        return _DLPROG["data"]
    data: dict[str, dict] = {}
    registry = getattr(request.app.state, "downloaders", None)
    if registry is not None:
        for handle in registry.all_enabled():
            try:
                client = await handle.ensure_client()
                if client is None:
                    continue
                torrents = await handle.call(client.torrents_info, tag=handle.config.tag)
            except Exception as e:  # noqa: BLE001 - 单台失败不拖垮整体
                logger.debug(f"下载进度查询失败（{handle.id}）: {e}")
                continue
            for t in torrents:
                h = str(getattr(t, "hash", "") or "").lower()
                if not h:
                    continue
                data[h] = {
                    "pct": round(float(getattr(t, "progress", 0) or 0) * 100),
                    "dlspeed": int(getattr(t, "dlspeed", 0) or 0),
                    "eta": int(getattr(t, "eta", -1) if getattr(t, "eta", -1) is not None else -1),
                    "state": str(getattr(t, "state", "") or ""),
                }
    _DLPROG["at"], _DLPROG["data"] = now, data
    return data


# ── /review 待审队列（最高频页面，PRD §12.2）──────────────────

@router.get("/review", response_class=HTMLResponse)
async def review_page(
    request: Request,
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    templates = request.app.state.templates
    items = await _query_tasks(db, state="scored", limit=500, review_only=True)
    # 按置信度倒序（PRD §12.2：high → medium → low）
    rank = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda r: (rank.get(r["confidence"], 3), -(r["score"] or 0)))
    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "review",
        "items": items, "reject_reasons": REJECT_REASONS, "count": len(items),
        "flash_ok": _flash(request, "ok"), "flash_err": _flash(request, "err"),
    })
    return templates.TemplateResponse(request, "review.html", ctx)


@router.post("/review/{task_id}/accept")
async def review_accept(
    task_id: str,
    request: Request,
    user: dict = Depends(verify_csrf),
    db=Depends(get_db),
    config=Depends(get_config),
):
    """采纳：dry_run 下仅标记；实跑则用持久化的 download_url 真实派发。"""
    row = await _get_task(db, task_id)
    if row is None:
        return RedirectResponse("/review?err=notfound", status_code=303)

    if config.dry_run:
        await repo.set_task_state(db, task_id, "scored", reject_reason="accepted_dry_run")
        logger.info(f"[DRY-RUN] 采纳待审项: {row['title'][:60]!r}")
        # 订阅引擎送进待审的命中也要回写：否则详情页永远停在 review
        await repo.set_hit_task(db, row["info_hash"], row["id"], HitDecision.DISPATCHED)
    else:
        # 派发走 runner.dispatcher（注册表解析下载器 + 与流水线同一套适配器直链换取），
        # 不再读 app.state.qb_client —— 那只是默认实例别名，且落盘参数要按任务归属实例取。
        # runner 未就绪时传 None → _dispatch_task 以「派发器未就绪」失败而非 500。
        dispatcher = getattr(getattr(request.app.state, "runner", None), "dispatcher", None)
        err = await _dispatch_task(db, config, row, dispatcher)
        if err:
            return RedirectResponse(f"/review?err={err}", status_code=303)

    await _record_decision(db, row, "accept", None)
    return RedirectResponse("/review?ok=accepted", status_code=303)


@router.post("/review/{task_id}/reject")
async def review_reject(
    task_id: str,
    reason: str = Form(...),
    note: str = Form(default=""),
    user: dict = Depends(verify_csrf),
    db=Depends(get_db),
):
    """拒绝：必须选原因（PRD §12.2），原因回流供规则迭代。"""
    valid = {r[0] for r in REJECT_REASONS}
    if reason not in valid:
        return RedirectResponse("/review?err=bad_reason", status_code=303)

    row = await _get_task(db, task_id)
    if row is None:
        return RedirectResponse("/review?err=notfound", status_code=303)

    await repo.set_task_state(db, task_id, "rejected",
                             reject_reason=f"{reason}:{note}".rstrip(":") if note else reason)
    await repo.set_hit_task(db, row["info_hash"], row["id"], HitDecision.REJECTED)
    await _record_decision(db, row, "reject", reason)
    logger.info(f"拒绝待审项: reason={reason} title={row['title'][:60]!r}")
    return RedirectResponse("/review?ok=rejected", status_code=303)


# ── /sites 站点中心：v0.6 起在 routes_sites（列表 / 向导 / 测试 / 门禁）──


# ── /settings 设置 ──────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    ok: str | None = None, detail: str | None = None,
    user: dict = Depends(require_user),
    db=Depends(get_db), config=Depends(get_config),
):
    """设置页（仅管理员）。

    字段清单由 settings_store 的 SPEC 驱动，模板不写死字段名 ——
    以后加设置项只需加一条声明，不会出现「后端支持但界面没有」的漂移。

    这里用 require_user 而非 require_admin：deps.require_admin 抛
    HTTPException 会返回裸 JSON，对浏览器请求是糟糕体验。改为重定向
    回总览并说明原因；写端点仍严格要求管理员（见 routes_settings）。
    """
    from . import routes_settings

    if not user.get("is_admin"):
        return RedirectResponse("/dashboard?err=need_admin", status_code=303)

    # 默认下载器的只读摘要（指引卡用）：v0.6 起权威源是 downloaders 表，
    # 只暴露连接参数与凭据形态，绝不回显凭据本身。
    registry = getattr(request.app.state, "downloaders", None)
    default_dl = registry.default() if registry is not None else None
    dl_info = None
    if default_dl is not None:
        c = default_dl.config
        dl_info = {
            "id": c.id, "name": c.name, "host": c.host, "port": c.port,
            "save_path": c.save_path, "category": c.category, "tag": c.tag,
            "auth_label": "账密" if c.username else ("API Key" if c.api_key else "❌ 未配置"),
        }

    templates = request.app.state.templates
    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "settings",
        "config": config, "detail": detail,
        # ok/err 经文案表翻译；表里没有的键（如 SettingsError 的原文）原样展示
        "ok": ok, "flash_ok": _flash(request, "ok"),
        "err": _flash(request, "err"),
        "groups": routes_settings.build_settings_groups(config),
        "default_downloader": dl_info,
        # 凭据只写不读：仅暴露是否已配置，绝不回显明文或密文。
        # 由 settings_store.credential_status 按站点类型判断所需凭据 ——
        # 原实现 `bool(s.rss_url or s.api_key)` 对 NexusPHP 站测的是
        # rss_url 有无（与 passkey 无关），对 mteam 也判断错了必需项。
        # qb 状态读下载器注册表（downloaders 表），不是 config.qbittorrent 种子值。
        "secrets_status": settings_store.credential_status(config, registry),
    })
    return templates.TemplateResponse(request, "settings.html", ctx)


# ── 手动触发 ────────────────────────────────────────────────

@router.post("/run-once")
async def run_once(
    request: Request,
    user: dict = Depends(verify_csrf),
):
    runner = request.app.state.runner
    summary = await runner.run_once()
    logger.info(f"手动触发一轮（by {user['username']}）: {summary}")
    return RedirectResponse("/dashboard?ok=run_once", status_code=303)


# ── 内部辅助 ────────────────────────────────────────────────

async def _site_status(db, config) -> list[dict]:
    """总览站点健康区块的数据：v0.6 起与 /sites 同走 sites.status.site_overview（六态）。

    保留这个包装是为了不改 dashboard 调用点；返回字段向后兼容
    （name / enabled / healthy / circuit_* / 计数），并新增 state / state_label / state_css。
    """
    from ..sites.status import site_overview

    return await site_overview(db, config)


async def _query_tasks(db, *, state: str | None, limit: int,
                       offset: int = 0, review_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM tasks"
    conds, params = [], []
    if state:
        conds.append("state = ?")
        params.append(state)
    if review_only:
        conds.append(
            "(reject_reason IS NULL OR reject_reason LIKE 'pending_review%' "
            "OR reject_reason LIKE 'confidence_below%' OR reject_reason='auto_download_disabled')"
        )
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    async with db.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _count_tasks(db, *, state: str | None) -> int:
    """命中记录分页用的总数（与 _query_tasks 同过滤条件）。"""
    sql = "SELECT COUNT(*) FROM tasks"
    params: list = []
    if state:
        sql += " WHERE state = ?"
        params.append(state)
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _get_task(db, task_id: str) -> dict | None:
    async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _record_decision(db, row: dict, decision: str, reason: str | None) -> None:
    import uuid as _uuid

    from ..timeutil import iso_utc
    await db.execute(
        """INSERT INTO review_decisions (id, task_id, info_hash, decision, reason, decided_at)
           VALUES (?,?,?,?,?,?)""",
        (str(_uuid.uuid4()), row["id"], row["info_hash"], decision, reason, iso_utc()),
    )
    await db.commit()


async def _dispatch_task(db, config, row: dict, dispatcher) -> str | None:
    """实跑模式下派发单个已采纳任务。成功返回 None，失败返回 _FLASH_ERR 的键。

    download_url 已在流水线轮次持久化到 tasks 表，故此处可直接真实派发，
    不必等下一轮。下载器与落盘参数全部经 dispatcher 的注册表解析
    （task.downloader_id → 回落默认实例）：config.qbittorrent 只是首启种子，
    运行时的权威源是 downloaders 表，直接读它会错过 /downloaders 页的改动。

    派发前经 dispatcher.resolve_download_url 换取直链：馒头持久化的是
    genDlToken 占位 URL，直接交给 qb 必失败；其他站点 / 无适配器时原样返回。

    幂等：Conflict409Error（相同 info_hash 已在 qb）视为成功。
    """
    from ..adapters.base import AdapterError
    from ..models import TaskState

    url = row.get("download_url")
    if not url:
        logger.error(f"任务 {row['id']} 无 download_url，无法派发（旧数据？）")
        await repo.set_task_state(db, row["id"], TaskState.FAILED,
                                  error_message="采纳时缺 download_url")
        return "dispatch_failed"
    if dispatcher is None:
        logger.error("派发器尚未就绪（runner 未装配），无法真实派发")
        await repo.set_task_state(db, row["id"], TaskState.FAILED,
                                  error_message="派发器未就绪")
        return "dispatch_failed"

    # 落点解析与流水线同一套：task 级覆盖 → 默认下载器的 save_path / category / tag
    target = dispatcher.resolve_target(
        site=row["site"],
        task_downloader_id=row.get("downloader_id"),
        task_save_path=row.get("qb_save_path"),
        task_category=row.get("qb_category"),
    )
    client = await target.handle.ensure_client() if target is not None else None
    if client is None:
        logger.error("下载器客户端不可用（dry_run 或凭据无效），无法真实派发")
        await repo.set_task_state(db, row["id"], TaskState.FAILED,
                                  error_message="下载器客户端不可用")
        return "dispatch_failed"

    try:
        url = await dispatcher.resolve_download_url(url, site=row["site"])
    except AdapterError as e:
        logger.error(f"采纳派发换取直链失败: site={row['site']} {e}")
        await repo.set_task_state(db, row["id"], TaskState.FAILED,
                                  error_message=f"换取下载直链失败: {e}")
        return "resolve_failed"

    from qbittorrentapi import Conflict409Error, LoginFailed
    from qbittorrentapi.exceptions import APIConnectionError

    try:
        await target.handle.call(
            client.torrents_add,
            urls=url,
            save_path=target.save_path,
            category=target.category,
            tags=target.tags,
            is_paused=False,
        )
    except Conflict409Error:
        logger.info(f"种子已存在（幂等跳过）: {row['title'][:60]!r}")
    except LoginFailed as e:
        logger.critical(f"采纳派发 qb 认证失败: {e}")
        await repo.set_task_state(db, row["id"], TaskState.FAILED,
                                  error_message=f"qb 认证失败: {e}")
        return "dispatch_failed"
    except APIConnectionError as e:
        logger.error(f"采纳派发 qb 连接失败: {e}")
        await repo.set_task_state(db, row["id"], TaskState.FAILED,
                                  error_message=f"qb 连接失败: {e}")
        return "dispatch_failed"
    except Exception as e:  # noqa: BLE001 - 单个派发失败不得让请求 500
        logger.exception(f"采纳派发未预期异常: {e}")
        await repo.set_task_state(db, row["id"], TaskState.FAILED,
                                  error_message=f"{type(e).__name__}: {e}")
        return "dispatch_failed"

    await repo.set_task_state(db, row["id"], TaskState.DISPATCHED, reject_reason=None)
    # 订阅引擎送进待审的命中：review → dispatched 并挂上 task_id（非订阅任务影响 0 行）
    await repo.set_hit_task(db, row["info_hash"], row["id"], HitDecision.DISPATCHED)
    logger.info(f"待审采纳并已派发: {row['site']} {row['title'][:60]!r}")
    return None
