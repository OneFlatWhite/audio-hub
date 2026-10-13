"""下载器中心（v0.6 波次 3；v0.8 起支持 qBittorrent / Transmission 双类型）：
多下载器实例的列表、新建 / 编辑、测试连接、默认切换、启用、删除。

- 权威源是 downloaders 表；每次写操作后 settings_store.rebuild_downloaders(app) 原地
  替换注册表，派发 / 完成监听 / 做种链路下一轮即用新配置，不需重启。
- 所有写端点 require_admin_csrf：下载器连接参数是全局配置，普通用户只读。
- 凭据只写不读：页面只显示「已配置 / 未配置」，表单留空 = 不修改
  （repo.upsert_downloader 默认 overwrite_empty_secrets=False）。
- 删除保护（服务端兜底，页面按钮只是提示）：
  · 仍有非终态任务引用 → 拒绝（它们完成后还要靠这台下载器对账 / 归档）
  · 默认下载器 → **自动继任**给优先级最高的其它已启用者（v0.12 起），
    没有可继任者且自己是最后一个已启用 → 拒绝（删了就没地方派发）
  禁用默认同样自动继任；「设为默认」仍要求目标处于启用状态。
- 测试连接走 downloaders.probe.probe_downloader（按类型分流，只读），结果落
  last_test_json 供列表展示版本 / 剩余空间，并作为「状态」列的依据。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .. import registry_store, repo, settings_store
from ..config import DownloaderConfig
from ..downloaders.probe import probe_downloader
from ..models import DownloaderRecord
from ..timeutil import SITE_TZ, utcnow
from .deps import get_config, get_db, require_admin_csrf, require_user
from .routes_pages import _common

router = APIRouter(tags=["downloaders"])

#: 下载器 id 规则：小写字母 / 数字 / 连字符 / 下划线，1–32 位（进 URL 与 tasks.downloader_id）
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_FLASH_OK = {
    "created": "下载器已创建。建议先点「测试连接」确认凭据可用",
    "saved": "下载器已保存，注册表已重建",
    "test_ok": "连接测试通过",
    "default_set": "默认下载器已切换",
    "enabled": "下载器已启用",
    "disabled": "下载器已禁用",
    "deleted": "下载器已删除",
}
_FLASH_ERR = {
    "notfound": "下载器不存在",
    "exists": "该 id 已存在，请直接编辑",
    "bad_id": "id 只能是小写字母 / 数字 / 连字符 / 下划线，1–32 位",
    "bad_name": "名称不能为空",
    "bad_host": "主机不能为空且不能含协议前缀 / 路径（只填主机名或 IP）",
    "bad_port": "端口必须是 1–65535 的整数",
    "bad_priority": "优先级必须是整数",
    "bad_save_path": "保存路径不能为空（下载器所在主机的路径）",
    "bad_type": "类型只支持 qbittorrent / transmission",
    "need_admin": "该操作仅管理员可用",
    "test_failed": "连接测试失败",
    "has_tasks": "仍有在途任务引用该下载器，待任务完成或失败后再删",
    "is_default": "不能删除或禁用默认下载器，请先把其它下载器设为默认",
    "last_enabled": "不能删除最后一个已启用的下载器",
    "disabled_default": "已禁用的下载器不能设为默认，请先启用",
    "secrets_key": "无法加密凭据：密钥文件不存在或不可读。请检查 AUDIO_HUB_SECRETS_KEY。",
}


def _flash(request: Request, kind: str) -> str | None:
    key = request.query_params.get(kind)
    if not key:
        return None
    table = _FLASH_OK if kind == "ok" else _FLASH_ERR
    return table.get(key, key)


def _redirect(path: str, **params) -> RedirectResponse:
    pairs = [f"{k}={quote(str(v), safe='')}" for k, v in params.items() if v is not None]
    url = path + ("?" + "&".join(pairs) if pairs else "")
    return RedirectResponse(url, status_code=303)


# ── 表单解析 ────────────────────────────────────────────────

class _FormError(Exception):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def _parse_form(
    *, downloader_id: str, name: str, host: str, port: str, username: str, password: str,
    api_key: str, save_path: str, category: str, tag: str, priority: str,
    dl_type: str, current: DownloaderRecord | None,
) -> DownloaderConfig:
    """表单 → DownloaderConfig。凭据留空表示不修改；enabled / is_default 沿用库中值。"""
    did = downloader_id.strip().lower()
    if not _ID_RE.match(did):
        raise _FormError("bad_id")
    name = name.strip()
    if not name:
        raise _FormError("bad_name")
    dl_type = (dl_type or "").strip().lower() or "qbittorrent"
    if dl_type not in ("qbittorrent", "transmission"):
        raise _FormError("bad_type")
    host = host.strip()
    if not host or "://" in host or "/" in host or " " in host:
        raise _FormError("bad_host")
    port_raw = (port or "").strip()
    if not port_raw.isdigit() or not 1 <= int(port_raw) <= 65535:
        raise _FormError("bad_port")
    save_path = save_path.strip()
    if not save_path:
        raise _FormError("bad_save_path")
    prio_raw = (priority or "0").strip() or "0"
    try:
        prio = int(prio_raw)
    except ValueError:
        raise _FormError("bad_priority") from None
    return DownloaderConfig(
        id=did, name=name, type=dl_type, host=host, port=int(port_raw),
        username=username.strip(), password=password.strip(), api_key=api_key.strip(),
        save_path=save_path, category=category.strip(), tag=tag.strip(),
        enabled=current.enabled if current is not None else True,
        is_default=current.is_default if current is not None else False,
        priority=prio,
    )


def _cfg_from_record(rec: DownloaderRecord, **update) -> DownloaderConfig:
    """DownloaderRecord → 不含凭据的 DownloaderConfig（凭据留空 = upsert 时保留库中值）。"""
    data = {
        "id": rec.id, "name": rec.name, "type": rec.type, "host": rec.host, "port": rec.port,
        "username": rec.username, "password": "", "api_key": "",
        "save_path": rec.save_path, "category": rec.category, "tag": rec.tag,
        "enabled": rec.enabled, "is_default": rec.is_default, "priority": rec.priority,
    }
    data.update(update)
    return DownloaderConfig(**data)


# ── 列表行 ──────────────────────────────────────────────────

def _state(rec: DownloaderRecord) -> tuple[str, str, str]:
    """(state, css, label)：禁用 / 未配置凭据 / 未测试 / 连接失败 / 正常。"""
    if not rec.enabled:
        return "disabled", "exclude", "已禁用"
    if not rec.has_password and not rec.has_api_key:
        return "unconfigured", "exclude", "未配置凭据"
    if rec.last_test is None:
        return "untested", "medium", "未测试"
    if rec.last_test.get("ok"):
        return "ok", "high", "正常"
    return "failed", "low", "连接失败"


def _fmt_local(dt) -> str | None:
    return dt.astimezone(SITE_TZ).strftime("%Y-%m-%d %H:%M") if dt else None


async def downloader_overview(db) -> list[dict]:
    """列表页 / 总览卡共用的行数据（不含任何凭据）。"""
    active = await repo.count_active_tasks_by_downloader(db)
    rows = []
    for rec in await repo.list_downloaders(db):
        state, css, label = _state(rec)
        test = rec.last_test or {}
        rows.append({
            "id": rec.id, "name": rec.name, "type": rec.type, "host": rec.host, "port": rec.port,
            "username": rec.username, "save_path": rec.save_path,
            "category": rec.category, "tag": rec.tag,
            "enabled": rec.enabled, "is_default": rec.is_default, "priority": rec.priority,
            "has_password": rec.has_password, "has_api_key": rec.has_api_key,
            "state": state, "state_css": css, "state_label": label,
            "last_test_ok": bool(test.get("ok")),
            "last_test_error": test.get("error"),
            "last_test_local": _fmt_local(rec.last_test_at),
            "version": test.get("version"),
            "api_version": test.get("api_version"),
            "free_space_bytes": test.get("free_space_bytes"),
            "default_save_path": test.get("default_save_path"),
            "active_tasks": active.get(rec.id, 0),
        })
    return rows


# ── 页面 ────────────────────────────────────────────────────

@router.get("/downloaders", response_class=HTMLResponse)
async def downloaders_page(
    request: Request, user: dict = Depends(require_user),
    db=Depends(get_db), config=Depends(get_config),
):
    templates = request.app.state.templates
    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "downloaders",
        "downloaders": await downloader_overview(db),
        "is_admin": bool(user.get("is_admin")),
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
        "detail": request.query_params.get("detail"),
    })
    return templates.TemplateResponse(request, "downloaders.html", ctx)


async def _edit_ctx(request: Request, db, config, user: dict, rec: DownloaderRecord | None) -> dict:
    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "downloaders",
        "downloader": rec,
        "is_admin": bool(user.get("is_admin")),
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
        "detail": request.query_params.get("detail"),
    })
    return ctx


@router.get("/downloaders/new", response_class=HTMLResponse)
async def downloader_new_page(
    request: Request, user: dict = Depends(require_user),
    db=Depends(get_db), config=Depends(get_config),
):
    if not user.get("is_admin"):
        return _redirect("/downloaders", err="need_admin")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "downloader_edit.html", await _edit_ctx(request, db, config, user, None),
    )


@router.get("/downloaders/{downloader_id}/edit", response_class=HTMLResponse)
async def downloader_edit_page(
    downloader_id: str, request: Request, user: dict = Depends(require_user),
    db=Depends(get_db), config=Depends(get_config),
):
    rec = await repo.get_downloader(db, downloader_id)
    if rec is None:
        return _redirect("/downloaders", err="notfound")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "downloader_edit.html", await _edit_ctx(request, db, config, user, rec),
    )


# ── 新建 / 保存 ─────────────────────────────────────────────

@router.post("/downloaders")
async def downloader_create(
    request: Request,
    new_id: str = Form("", alias="id"), name: str = Form(""), host: str = Form(""),
    port: str = Form("8085"),
    username: str = Form(""), password: str = Form(""), api_key: str = Form(""),
    save_path: str = Form(""), category: str = Form(""), tag: str = Form(""),
    priority: str = Form("0"), dl_type: str = Form("", alias="type"),
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    try:
        cfg = _parse_form(
            downloader_id=new_id, name=name, host=host, port=port, username=username,
            password=password, api_key=api_key, save_path=save_path, category=category,
            tag=tag, priority=priority, dl_type=dl_type, current=None,
        )
    except _FormError as e:
        return _redirect("/downloaders/new", err=e.key)
    if await repo.get_downloader(db, cfg.id) is not None:
        return _redirect(f"/downloaders/{cfg.id}/edit", err="exists")
    # 表里还没有任何下载器时，新建的这台自动成为默认，保证「有实例就有默认」
    if await repo.get_default_downloader(db) is None:
        cfg = cfg.model_copy(update={"is_default": True})
    try:
        await repo.upsert_downloader(db, cfg)
    except (OSError, RuntimeError) as e:
        logger.error(f"新建下载器 {cfg.id} 失败：密钥文件不可用: {e}")
        return _redirect("/downloaders/new", err="secrets_key")
    await settings_store.rebuild_downloaders(request.app)
    logger.info(f"新建下载器 {cfg.id}（by {user['username']}）")
    return _redirect(f"/downloaders/{cfg.id}/edit", ok="created")


@router.post("/downloaders/{downloader_id}")
async def downloader_save(
    downloader_id: str, request: Request,
    name: str = Form(""), host: str = Form(""), port: str = Form("8085"),
    username: str = Form(""), password: str = Form(""), api_key: str = Form(""),
    save_path: str = Form(""), category: str = Form(""), tag: str = Form(""),
    priority: str = Form("0"), dl_type: str = Form("", alias="type"),
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    current = await repo.get_downloader(db, downloader_id)
    if current is None:
        return _redirect("/downloaders", err="notfound")
    try:
        cfg = _parse_form(
            downloader_id=downloader_id, name=name, host=host, port=port, username=username,
            password=password, api_key=api_key, save_path=save_path, category=category,
            tag=tag, priority=priority, dl_type=dl_type, current=current,
        )
    except _FormError as e:
        return _redirect(f"/downloaders/{downloader_id}/edit", err=e.key)
    try:
        await repo.upsert_downloader(db, cfg)
    except (OSError, RuntimeError) as e:
        logger.error(f"保存下载器 {downloader_id} 失败：密钥文件不可用: {e}")
        return _redirect(f"/downloaders/{downloader_id}/edit", err="secrets_key")
    # 连接参数或凭据变了，上次测试结果不再可信
    changed_conn = (
        cfg.host != current.host or cfg.port != current.port
        or cfg.type != current.type
        or cfg.username != current.username or bool(cfg.password) or bool(cfg.api_key)
    )
    if changed_conn and current.last_test is not None:
        await repo.set_downloader_test(db, downloader_id, {"ok": False, "error": "配置已更改，请重新测试"})
    await settings_store.rebuild_downloaders(request.app)
    logger.info(f"保存下载器 {downloader_id}（by {user['username']}）")
    return _redirect(f"/downloaders/{downloader_id}/edit", ok="saved")


# ── 测试连接 ────────────────────────────────────────────────

async def _load_cfg(db, downloader_id: str) -> DownloaderConfig | None:
    """DB → 含明文凭据的 DownloaderConfig（只在服务端流转，供探测）。"""
    for cfg in await registry_store.load_downloader_configs(db):
        if cfg.id == downloader_id:
            return cfg
    return None


def _back(request: Request, downloader_id: str) -> str:
    ref = request.headers.get("referer") or ""
    return f"/downloaders/{downloader_id}/edit" if "/edit" in ref else "/downloaders"


@router.post("/downloaders/{downloader_id}/test")
async def downloader_test(
    downloader_id: str, request: Request,
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    cfg = await _load_cfg(db, downloader_id)
    if cfg is None:
        return _redirect("/downloaders", err="notfound")
    back = _back(request, downloader_id)
    started = utcnow()
    result = await probe_downloader(cfg)
    result["elapsed_ms"] = int((utcnow() - started).total_seconds() * 1000)
    await repo.set_downloader_test(db, downloader_id, result, at=started)
    if not result.get("ok"):
        logger.warning(f"下载器 {downloader_id} 测试失败: {result.get('error')}")
        return _redirect(back, err="test_failed", detail=(result.get("error") or "")[:300])
    # 测通后客户端缓存可能是失败态（None），重建让下一次派发重试连接
    await settings_store.rebuild_downloaders(request.app)
    is_tr = (cfg.type or "").lower() == "transmission"
    vendor = "Transmission" if is_tr else "qB"
    logger.info(f"下载器 {downloader_id} 测试通过（by {user['username']}）: {vendor} {result.get('version')}")
    detail = f"{vendor} {result.get('version')}"
    if not is_tr:
        detail += f" / API {result.get('api_version')}"
    return _redirect(back, ok="test_ok",
                     detail=f"{detail}，{result['elapsed_ms']} ms")


# ── 默认 / 启用 / 删除 ──────────────────────────────────────

async def _promote_next_default(db, *, exclude: str) -> str | None:
    """把优先级最高的其它已启用下载器提为默认；没有可继任者返回 None。

    v0.12 起「默认」不再是删除 / 禁用的死锁：默认下载器退场时自动继任，
    只有最后一个已启用的下载器才拒绝退场（删了就真没地方派发了）。
    """
    others = [r for r in await repo.list_downloaders(db, enabled_only=True)
              if r.id != exclude]
    if not others:
        return None
    nxt = others[0]          # list_downloaders 已按 is_default DESC, priority DESC, name 排序
    await repo.set_default_downloader(db, nxt.id)
    return nxt.id

@router.post("/downloaders/{downloader_id}/set-default")
async def downloader_set_default(
    downloader_id: str, request: Request,
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    rec = await repo.get_downloader(db, downloader_id)
    if rec is None:
        return _redirect("/downloaders", err="notfound")
    if not rec.enabled:
        return _redirect("/downloaders", err="disabled_default")
    await repo.set_default_downloader(db, downloader_id)
    await settings_store.rebuild_downloaders(request.app)
    logger.info(f"默认下载器切换为 {downloader_id}（by {user['username']}）")
    return _redirect("/downloaders", ok="default_set")


@router.post("/downloaders/{downloader_id}/toggle")
async def downloader_toggle(
    downloader_id: str, request: Request, enabled: str = Form("0"),
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    rec = await repo.get_downloader(db, downloader_id)
    if rec is None:
        return _redirect("/downloaders", err="notfound")
    want = enabled in ("1", "true", "on", "yes")
    back = _back(request, downloader_id)
    promoted = None
    if not want and rec.is_default:
        # 禁用默认 → 自动继任；没有其它已启用下载器才拒绝（最后一个不能关）
        promoted = await _promote_next_default(db, exclude=downloader_id)
        if promoted is None:
            return _redirect(back, err="last_enabled")
    if want != rec.enabled:
        cfg = _cfg_from_record(rec, enabled=want, **({"is_default": False} if promoted else {}))
        await repo.upsert_downloader(db, cfg)
        await settings_store.rebuild_downloaders(request.app)
        logger.info(f"下载器 {downloader_id} {'启用' if want else '禁用'}（by {user['username']}）")
    detail = f"默认下载器已自动切换为 {promoted}" if promoted else None
    return _redirect(back, ok="enabled" if want else "disabled", detail=detail)


@router.post("/downloaders/{downloader_id}/delete")
async def downloader_delete(
    downloader_id: str, request: Request,
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    rec = await repo.get_downloader(db, downloader_id)
    if rec is None:
        return _redirect("/downloaders", err="notfound")
    # 最后一个已启用的下载器不可删（删了就真没地方派发）——通用兜底
    enabled_others = [r for r in await repo.list_downloaders(db, enabled_only=True)
                      if r.id != downloader_id]
    if rec.enabled and not enabled_others:
        return _redirect("/downloaders", err="last_enabled")
    # 默认也可删：先自动继任给优先级最高的其它已启用者
    promoted = None
    if rec.is_default:
        promoted = await _promote_next_default(db, exclude=downloader_id)
    n = await repo.count_tasks_referencing_downloader(db, downloader_id)
    if n:
        return _redirect("/downloaders", err="has_tasks", detail=f"{n} 个在途任务")
    await repo.delete_downloader(db, downloader_id)
    await settings_store.rebuild_downloaders(request.app)
    detail = f"默认下载器已自动切换为 {promoted}" if promoted else None
    logger.info(f"删除下载器 {downloader_id}（by {user['username']}）"
                + (f"，默认已继任为 {promoted}" if promoted else ""))
    return _redirect("/downloaders", ok="deleted", detail=detail)
