"""站点中心（v0.6 波次 2A）：列表、三步向导、认证 / 分类 / RSS / 搜索测试、启用门禁。

- 列表页六态与总览同口径（sites.status.site_overview）。
- 所有写端点 require_admin_csrf：站点凭据与开关是全局配置，普通用户只读。
- 凭据只写不读：页面只显示「已配置 / 未配置」，表单留空 = 不修改。
- 启用门禁在服务端：auth_status != valid 的站点，POST /sites/{id}/toggle 启用会被拒；
  build_adapters 与 search_round 再各挡一道，页面上的灰按钮只是提示。
- 「测试」类端点用 registry.make_adapter_for_test 绕过门禁（目的就是把站验成 valid）。
- flash 文案在本模块映射（不改 routes_pages），结果详情经 ?detail= 回显。
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .. import registry_store, repo, settings_store
from ..adapters.base import AdapterAuthError, AdapterError
from ..adapters.registry import (
    make_adapter_for_test,
    resolve_site_name,
    site_credential_kinds,
    supported_sites,
)
from ..config import SiteConfig, split_ids
from ..models import CATEGORY_KINDS, AuthStatus, SiteCategory
from ..sites.status import site_overview
from ..timeutil import utcnow
from .deps import get_config, get_db, require_admin_csrf, require_user
from .routes_pages import _common

router = APIRouter(tags=["sites"])

#: 「使用常见 Chrome UA」按钮填入的值。与站点登录浏览器一致最稳妥，这里给一个常见默认。
COMMON_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: 站点显示名（向导下拉用）；未列出的按 id 原样显示
_SITE_LABELS = {
    "hhanclub": "憨憨 HHanClub",
    "hdsky": "天空 HDSky",
    "audiences": "观众 Audiences",
    "mteam": "馒头 M-Team",
}

_BRACKET_STYLES = ("【】", "[]")

_FLASH_OK = {
    "created": "站点已创建。第 ② 步：填好凭据后点「测试认证」",
    "saved": "站点已保存，适配器已重建",
    "saved_recheck": "凭据已更新，认证状态已重置，请重新「测试认证」",
    "auth_ok": "认证通过，站点已标记为有效",
    "cats_found": "已发现远端分类，请核对音乐 / 有声书映射后保存",
    "cats_saved": "分类映射已保存",
    "rss_ok": "RSS 测试通过",
    "enabled": "站点已启用，适配器已重建",
    "disabled": "站点已禁用，适配器已重建",
    "deleted": "站点已删除",
}
_FLASH_ERR = {
    "notfound": "站点不存在",
    "bad_name": "请选择一个已支持的站点",
    "exists": "该站点已存在，请直接编辑",
    "bad_rss_url": "RSS 地址必须是 http(s):// 开头的完整 URL",
    "bad_proxy": "代理地址必须是 http(s)://host:port 形式",
    "bad_domain": "站点域名必须是 http(s):// 开头的基址，例如 https://hhanclub.net",
    "bad_rows": "rows 上限必须是 1–200 的整数",
    "bad_priority": "优先级必须是整数",
    "bad_hr": "H&R 最低做种时长必须是整数，最低分享率必须是数字（留空 = 用全局值）",
    "bad_search_url": "搜索地址必须是 http(s):// 开头的完整 URL",
    "need_admin": "该操作仅管理员可用",
    "need_cookie": "NexusPHP 站点认证需要先填 Cookie",
    "need_api_key": "mteam 认证需要先填 API Key",
    "auth_failed": "认证失败：凭据无效或已过期",
    "auth_error": "认证检查未完成（网络 / 解析错误），状态未改变",
    "cats_failed": "发现分类失败",
    "cats_empty": "没有需要保存的分类映射",
    "rss_failed": "RSS 测试失败",
    "search_failed": "搜索测试失败",
    "empty_keyword": "请输入搜索关键字",
    "auth_required": "启用前必须先通过「测试认证」：当前认证状态不是有效，服务端已拒绝启用",
    "secrets_key": "无法加密凭据：密钥文件不存在或不可读。请检查 AUDIO_HUB_SECRETS_KEY。",
}


def _flash(request: Request, kind: str) -> str | None:
    key = request.query_params.get(kind)
    if not key:
        return None
    table = _FLASH_OK if kind == "ok" else _FLASH_ERR
    return table.get(key, key)


def _redirect(path: str, **params) -> RedirectResponse:
    """带 ok / err / detail 的 303 重定向；None 值不拼。"""
    pairs = [f"{k}={quote(str(v), safe='')}" for k, v in params.items() if v is not None]
    url = path + ("?" + "&".join(pairs) if pairs else "")
    return RedirectResponse(url, status_code=303)


def _is_http_url(raw: str) -> bool:
    try:
        parts = urlsplit(raw)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _base_url(raw: str) -> str:
    """域名字段归一：去路径、去尾斜杠，只留 scheme://netloc。"""
    parts = urlsplit(raw.strip())
    return f"{parts.scheme}://{parts.netloc}"


async def _load_cfg(db, site_id: str) -> SiteConfig | None:
    """DB → 含明文凭据的 SiteConfig（只在服务端流转，供适配器构建）。"""
    for cfg in await registry_store.load_site_configs(db):
        if resolve_site_name(cfg.name) == site_id:
            return cfg
    return None


def _needed_kind(site_id: str) -> str:
    """认证所需凭据：mteam 要 api_key，其余 NexusPHP 要 cookie。"""
    return "api_key" if resolve_site_name(site_id) == "mteam" else "cookie"


# ── 表单解析 ────────────────────────────────────────────────

class _FormError(Exception):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def _parse_site_form(
    *, name: str, domain: str, rss_url: str, api_key: str, cookie: str, user_agent: str,
    proxy: str, search_url: str, music_cat_ids: str, audiobook_cat_ids: str,
    max_rows: str, title_bracket_style: str, priority: str,
    hr_min_seed_hours: str, hr_min_ratio: str, current: SiteConfig | None,
) -> SiteConfig:
    """表单 → SiteConfig。凭据留空表示不修改（upsert_site 自己保留库中值）。"""
    sid = resolve_site_name(name)
    if sid not in supported_sites():
        raise _FormError("bad_name")

    domain = domain.strip()
    if domain and not _is_http_url(domain):
        raise _FormError("bad_domain")
    rss_url = rss_url.strip()
    if rss_url and not _is_http_url(rss_url):
        raise _FormError("bad_rss_url")
    proxy = proxy.strip()
    if proxy and not _is_http_url(proxy):
        raise _FormError("bad_proxy")
    search_url = search_url.strip()
    if search_url and not _is_http_url(search_url):
        raise _FormError("bad_search_url")

    rows_raw = (max_rows or "").strip()
    if not rows_raw.isdigit() or not 1 <= int(rows_raw) <= 200:
        raise _FormError("bad_rows")
    prio_raw = (priority or "0").strip() or "0"
    try:
        prio = int(prio_raw)
    except ValueError:
        raise _FormError("bad_priority") from None

    hr_hours = (hr_min_seed_hours or "").strip()
    hr_ratio = (hr_min_ratio or "").strip()
    try:
        hr_hours_v = int(hr_hours) if hr_hours else None
        hr_ratio_v = float(hr_ratio) if hr_ratio else None
    except ValueError:
        raise _FormError("bad_hr") from None

    style = title_bracket_style if title_bracket_style in _BRACKET_STYLES else "【】"
    return SiteConfig(
        name=sid,
        enabled=current.enabled if current is not None else False,
        rss_url=rss_url, search_url=search_url, api_key=api_key.strip(),
        cookie=cookie.strip(), user_agent=user_agent.strip(), proxy=proxy,
        domain=_base_url(domain) if domain else "",
        music_cat_id=",".join(split_ids(music_cat_ids)),
        audiobook_cat_id=",".join(split_ids(audiobook_cat_ids)),
        max_rows=int(rows_raw), title_bracket_style=style, priority=prio,
        hr_min_seed_hours=hr_hours_v, hr_min_ratio=hr_ratio_v,
        auth_status=current.auth_status if current is not None else AuthStatus.UNCONFIGURED.value,
    )


# ── 页面 ────────────────────────────────────────────────────

@router.get("/sites", response_class=HTMLResponse)
async def sites_page(
    request: Request, user: dict = Depends(require_user),
    db=Depends(get_db), config=Depends(get_config),
):
    templates = request.app.state.templates
    rows = await site_overview(db, config)
    existing = {r["id"] for r in rows}
    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "sites",
        "sites": rows,
        "addable": [s for s in supported_sites() if s not in existing],
        "site_labels": _SITE_LABELS,
        "is_admin": bool(user.get("is_admin")),
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
        "detail": request.query_params.get("detail"),
    })
    return templates.TemplateResponse(request, "sites.html", ctx)


@router.get("/sites/new", response_class=HTMLResponse)
async def site_new_page(
    request: Request, user: dict = Depends(require_user),
    db=Depends(get_db), config=Depends(get_config),
):
    if not user.get("is_admin"):
        return _redirect("/sites", err="need_admin")
    templates = request.app.state.templates
    existing = {r.id for r in await repo.list_sites(db)}
    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "sites",
        "site": None, "cats": [], "step": 1,
        "addable": [s for s in supported_sites() if s not in existing],
        "site_labels": _SITE_LABELS,
        "bracket_styles": _BRACKET_STYLES,
        "common_ua": COMMON_CHROME_UA,
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
        "detail": request.query_params.get("detail"),
    })
    return templates.TemplateResponse(request, "site_edit.html", ctx)


async def _edit_ctx(request: Request, db, config, user: dict, site_id: str, **extra) -> dict | None:
    rows = {r["id"]: r for r in await site_overview(db, config)}
    row = rows.get(site_id)
    if row is None:
        return None
    rec = await repo.get_site(db, site_id)
    cats = await repo.list_site_categories(db, site_id)
    needed = _needed_kind(site_id)
    # 向导步骤：凭据没齐 → ①；认证未过 → ②；否则 ③（RSS / 搜索测试与启用）
    if not row.get(f"has_{needed}"):
        step = 1
    elif row["auth_status"] != AuthStatus.VALID.value:
        step = 2
    else:
        step = 3
    ctx = await _common(request, db, config, user)
    ctx.update({
        "active": "sites",
        "site": row, "record": rec, "cats": cats, "step": step,
        "needed_kind": needed,
        "credential_kinds": site_credential_kinds(site_id),
        "category_kinds": CATEGORY_KINDS,
        "site_labels": _SITE_LABELS,
        "bracket_styles": _BRACKET_STYLES,
        "common_ua": COMMON_CHROME_UA,
        "is_admin": bool(user.get("is_admin")),
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
        "detail": request.query_params.get("detail"),
        "search_result": None,
    })
    ctx.update(extra)
    return ctx


@router.get("/sites/{site_id}/edit", response_class=HTMLResponse)
async def site_edit_page(
    site_id: str, request: Request, user: dict = Depends(require_user),
    db=Depends(get_db), config=Depends(get_config),
):
    templates = request.app.state.templates
    ctx = await _edit_ctx(request, db, config, user, site_id)
    if ctx is None:
        return _redirect("/sites", err="notfound")
    return templates.TemplateResponse(request, "site_edit.html", ctx)


# ── 新建 / 保存 ─────────────────────────────────────────────

async def _save(db, cfg: SiteConfig, *, site_id: str, request: Request) -> None:
    await repo.upsert_site(
        db, cfg, site_id=site_id, adapter_type=registry_store.adapter_type_for(site_id),
    )
    await settings_store.rebuild_adapters(request.app)


@router.post("/sites")
async def site_create(
    request: Request,
    name: str = Form(""),
    domain: str = Form(""), rss_url: str = Form(""), api_key: str = Form(""),
    cookie: str = Form(""), user_agent: str = Form(""), proxy: str = Form(""),
    search_url: str = Form(""),
    music_cat_ids: str = Form(""), audiobook_cat_ids: str = Form(""),
    max_rows: str = Form("50"), title_bracket_style: str = Form("【】"),
    priority: str = Form("0"), hr_min_seed_hours: str = Form(""), hr_min_ratio: str = Form(""),
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    sid = resolve_site_name(name)
    if sid not in supported_sites():
        return _redirect("/sites/new", err="bad_name")
    if await repo.site_exists(db, sid):
        return _redirect(f"/sites/{sid}/edit", err="exists")
    try:
        cfg = _parse_site_form(
            name=sid, domain=domain, rss_url=rss_url, api_key=api_key, cookie=cookie,
            user_agent=user_agent, proxy=proxy, search_url=search_url,
            music_cat_ids=music_cat_ids, audiobook_cat_ids=audiobook_cat_ids,
            max_rows=max_rows, title_bracket_style=title_bracket_style, priority=priority,
            hr_min_seed_hours=hr_min_seed_hours, hr_min_ratio=hr_min_ratio, current=None,
        )
    except _FormError as e:
        return _redirect("/sites/new", err=e.key)
    try:
        await _save(db, cfg, site_id=sid, request=request)
    except (OSError, RuntimeError) as e:
        # 凭据加密在 upsert_site 内完成；密钥文件缺失 / 不可读是部署问题，给可操作提示而非 500
        logger.error(f"新建站点 {sid} 失败：密钥文件不可用: {e}")
        return _redirect("/sites/new", err="secrets_key")
    logger.info(f"新建站点 {sid}（by {user['username']}）")
    return _redirect(f"/sites/{sid}/edit", ok="created")


@router.post("/sites/{site_id}")
async def site_save(
    site_id: str, request: Request,
    domain: str = Form(""), rss_url: str = Form(""), api_key: str = Form(""),
    cookie: str = Form(""), user_agent: str = Form(""), proxy: str = Form(""),
    search_url: str = Form(""),
    music_cat_ids: str = Form(""), audiobook_cat_ids: str = Form(""),
    max_rows: str = Form("50"), title_bracket_style: str = Form("【】"),
    priority: str = Form("0"), hr_min_seed_hours: str = Form(""), hr_min_ratio: str = Form(""),
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    current = await _load_cfg(db, site_id)
    if current is None:
        return _redirect("/sites", err="notfound")
    try:
        cfg = _parse_site_form(
            name=site_id, domain=domain, rss_url=rss_url, api_key=api_key, cookie=cookie,
            user_agent=user_agent, proxy=proxy, search_url=search_url,
            music_cat_ids=music_cat_ids, audiobook_cat_ids=audiobook_cat_ids,
            max_rows=max_rows, title_bracket_style=title_bracket_style, priority=priority,
            hr_min_seed_hours=hr_min_seed_hours, hr_min_ratio=hr_min_ratio, current=current,
        )
    except _FormError as e:
        return _redirect(f"/sites/{site_id}/edit", err=e.key)

    # 认证凭据变了 → 之前的 valid 不再可信：重置为 unconfigured，门禁随之生效
    needed = _needed_kind(site_id)
    recheck = bool(getattr(cfg, needed))
    try:
        await repo.upsert_site(
            db, cfg, site_id=site_id, adapter_type=registry_store.adapter_type_for(site_id),
        )
    except (OSError, RuntimeError) as e:
        logger.error(f"保存站点 {site_id} 失败：密钥文件不可用: {e}")
        return _redirect(f"/sites/{site_id}/edit", err="secrets_key")
    if recheck:
        await repo.set_site_auth(db, site_id, AuthStatus.UNCONFIGURED, error=None,
                                 checked_at=utcnow())
    await settings_store.rebuild_adapters(request.app)
    logger.info(f"保存站点 {site_id}（by {user['username']}，凭据{'已更新' if recheck else '未变'}）")
    return _redirect(f"/sites/{site_id}/edit", ok="saved_recheck" if recheck else "saved")


# ── 测试动作（绕过门禁：make_adapter_for_test）──────────────

@router.post("/sites/{site_id}/test-auth")
async def site_test_auth(
    site_id: str, request: Request,
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    cfg = await _load_cfg(db, site_id)
    if cfg is None:
        return _redirect("/sites", err="notfound")
    needed = _needed_kind(site_id)
    if not getattr(cfg, needed):
        return _redirect(f"/sites/{site_id}/edit", err=f"need_{needed}")
    now = utcnow()
    try:
        info = await make_adapter_for_test(cfg).check_auth()
    except AdapterAuthError as e:
        await repo.set_site_auth(db, site_id, AuthStatus.EXPIRED, error=str(e), checked_at=now)
        await settings_store.rebuild_adapters(request.app)
        return _redirect(f"/sites/{site_id}/edit", err="auth_failed", detail=str(e)[:300])
    except Exception as e:  # noqa: BLE001 - 网络 / 解析错误不改状态
        msg = str(e) if isinstance(e, AdapterError) else f"{type(e).__name__}: {e}"
        await repo.set_site_auth(db, site_id, cfg.auth_status, error=msg, checked_at=now)
        return _redirect(f"/sites/{site_id}/edit", err="auth_error", detail=msg[:300])
    await repo.set_site_auth(db, site_id, AuthStatus.VALID, error=None, user_info=info,
                             checked_at=now)
    await settings_store.rebuild_adapters(request.app)
    logger.info(f"站点 {site_id} 认证通过（by {user['username']}）: {info.username or '?'}")
    return _redirect(f"/sites/{site_id}/edit", ok="auth_ok")


@router.post("/sites/{site_id}/discover-categories")
async def site_discover_categories(
    site_id: str, request: Request,
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    cfg = await _load_cfg(db, site_id)
    if cfg is None:
        return _redirect("/sites", err="notfound")
    try:
        cats = await make_adapter_for_test(cfg).discover_categories()
    except Exception as e:  # noqa: BLE001
        msg = str(e) if isinstance(e, AdapterError) else f"{type(e).__name__}: {e}"
        return _redirect(f"/sites/{site_id}/edit", err="cats_failed", detail=msg[:300])
    n = await repo.replace_site_categories(
        db, site_id, [c.model_copy(update={"site_id": site_id}) for c in cats],
    )
    return _redirect(f"/sites/{site_id}/edit", ok="cats_found", detail=f"{n} 个分类")


@router.post("/sites/{site_id}/categories")
async def site_save_categories(
    site_id: str, request: Request,
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    cfg = await _load_cfg(db, site_id)
    if cfg is None:
        return _redirect("/sites", err="notfound")
    form = await request.form()
    existing = {c.remote_id: c for c in await repo.list_site_categories(db, site_id)}
    updated: list[SiteCategory] = []
    for key, value in form.multi_items():
        if not key.startswith("kind_"):
            continue
        rid = key[len("kind_"):]
        kind = str(value) if str(value) in CATEGORY_KINDS else "other"
        if rid in existing:
            await repo.set_category_kind(db, site_id, rid, kind)
            updated.append(existing[rid].model_copy(update={"local_kind": kind}))
    if not updated:
        return _redirect(f"/sites/{site_id}/edit", err="cats_empty")
    # 映射同步到适配器实际使用的分类 ID 列表，RSS / 站内搜索按此过滤
    music = [c.remote_id for c in updated if c.local_kind == "music"]
    books = [c.remote_id for c in updated if c.local_kind == "audiobook"]
    await repo.upsert_site(db, cfg.model_copy(update={
        "music_cat_id": ",".join(music), "audiobook_cat_id": ",".join(books),
        "cookie": "", "rss_url": "", "api_key": "",   # 空 = 保留库中凭据
    }), site_id=site_id)
    await settings_store.rebuild_adapters(request.app)
    return _redirect(f"/sites/{site_id}/edit", ok="cats_saved")


@router.post("/sites/{site_id}/test-rss")
async def site_test_rss(
    site_id: str, request: Request,
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    cfg = await _load_cfg(db, site_id)
    if cfg is None:
        return _redirect("/sites", err="notfound")
    try:
        result = await make_adapter_for_test(cfg).test_rss()
    except Exception as e:  # noqa: BLE001 - test_rss 自身不抛，这里兜构造失败
        msg = str(e) if isinstance(e, AdapterError) else f"{type(e).__name__}: {e}"
        return _redirect(f"/sites/{site_id}/edit", err="rss_failed", detail=msg[:300])
    await repo.set_site_rss_test(db, site_id, result)
    if not result.ok:
        return _redirect(f"/sites/{site_id}/edit", err="rss_failed",
                         detail=(result.error or "")[:300])
    return _redirect(f"/sites/{site_id}/edit", ok="rss_ok",
                     detail=f"{result.count} 条，{result.elapsed_ms} ms")


@router.post("/sites/{site_id}/test-search", response_class=HTMLResponse)
async def site_test_search(
    site_id: str, request: Request,
    keyword: str = Form(""), kind: str = Form("music"),
    user: dict = Depends(require_admin_csrf), db=Depends(get_db), config=Depends(get_config),
):
    """搜索测试结果不落库，直接渲染编辑页（含 degraded 标记与样本标题）。"""
    cfg = await _load_cfg(db, site_id)
    if cfg is None:
        return _redirect("/sites", err="notfound")
    keyword = keyword.strip()
    if not keyword:
        return _redirect(f"/sites/{site_id}/edit", err="empty_keyword")
    kind = kind if kind in ("music", "audiobook") else "music"
    search_result: dict = {"keyword": keyword, "kind": kind, "ok": False,
                           "count": 0, "degraded": False, "source": "", "samples": [], "error": None}
    try:
        torrents = await make_adapter_for_test(cfg).search(keyword, kind)
        search_result.update({
            "ok": True, "count": len(torrents),
            "degraded": any(t.degraded for t in torrents),
            "source": torrents[0].source if torrents else "",
            "samples": [
                {"title": t.title, "size": t.size_bytes, "category": t.category.value,
                 "seeders": t.seeders, "discount": t.discount.value, "degraded": t.degraded}
                for t in torrents[:10]
            ],
        })
    except Exception as e:  # noqa: BLE001
        search_result["error"] = str(e) if isinstance(e, AdapterError) \
            else f"{type(e).__name__}: {e}"
    templates = request.app.state.templates
    ctx = await _edit_ctx(request, db, config, user, site_id, search_result=search_result)
    return templates.TemplateResponse(request, "site_edit.html", ctx)


# ── 启用 / 禁用 / 删除 ───────────────────────────────────────

@router.post("/sites/{site_id}/toggle")
async def site_toggle(
    site_id: str, request: Request, enabled: str = Form("0"),
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    rec = await repo.get_site(db, site_id)
    if rec is None:
        return _redirect("/sites", err="notfound")
    want = enabled in ("1", "true", "on", "yes")
    back = request.headers.get("referer") or "/sites"
    back = f"/sites/{site_id}/edit" if "/edit" in back else "/sites"
    if want and rec.auth_status != AuthStatus.VALID:
        logger.warning(f"拒绝启用站点 {site_id}：认证状态 {rec.auth_status.value}（by {user['username']}）")
        return _redirect(back, err="auth_required", detail=f"当前状态：{rec.auth_status.value}")
    await repo.set_site_enabled(db, site_id, want)
    await settings_store.rebuild_adapters(request.app)
    logger.info(f"站点 {site_id} {'启用' if want else '禁用'}（by {user['username']}）")
    return _redirect(back, ok="enabled" if want else "disabled")


@router.post("/sites/{site_id}/delete")
async def site_delete(
    site_id: str, request: Request,
    user: dict = Depends(require_admin_csrf), db=Depends(get_db),
):
    if not await repo.delete_site(db, site_id):
        return _redirect("/sites", err="notfound")
    await settings_store.rebuild_adapters(request.app)
    logger.info(f"删除站点 {site_id}（by {user['username']}）")
    return _redirect("/sites", ok="deleted")
