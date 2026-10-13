"""认证路由：首次初始化、登录、登出、改密。

首次启动无管理员时，所有页面重定向到 /setup 强制创建账号
（不预设默认密码，PRD §12.3）。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from . import auth
from .deps import (
    client_ip,
    current_user,
    get_config,
    get_db,
    require_user,
    verify_csrf,
)

router = APIRouter(tags=["auth"])

# 不需要登录即可访问的路径（含静态资源）
PUBLIC_PATHS = {"/login", "/setup", "/health", "/static"}


def _cookie_kwargs(config, max_age: int | None) -> dict:
    """统一的 cookie 参数。

    Secure 默认 false：本机多为内网 HTTP，若强开 Secure 浏览器不会回传 cookie，
    表现为「登录后立刻被踢回登录页」。走 HTTPS/Tailscale 时应在 config 开启。
    SameSite=Strict：从外链跳入不带 cookie，降低 CSRF 面。
    """
    kw = {
        "key": auth.SESSION_COOKIE,
        "httponly": True,          # JS 读不到，XSS 拿不走
        "samesite": "strict",
        "secure": config.web.cookie_secure,
        "path": "/",
    }
    if max_age is not None:
        kw["max_age"] = max_age
    return kw


def _safe_next(value: str | None) -> str:
    """只允许站内相对路径，防开放重定向。"""
    if not value:
        return "/"
    if value.startswith("//") or "://" in value:
        return "/"
    if not value.startswith("/"):
        return "/"
    return value


# ── 首次初始化 ──────────────────────────────────────────────

@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, db=Depends(get_db)):
    """首次启动创建管理员。已有用户则拒绝访问。"""
    if await auth.count_users(db) > 0:
        return RedirectResponse("/", status_code=303)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "setup.html", {
        "error": None,
        "hint_username": "不要用 admin，避开扫描字典第一批",
    })


@router.post("/setup")
async def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    db=Depends(get_db),
    config=Depends(get_config),
):
    templates = request.app.state.templates

    if await auth.count_users(db) > 0:
        return RedirectResponse("/", status_code=303)

    if password != password2:
        return templates.TemplateResponse(request, "setup.html", {
            "error": "两次输入的密码不一致", "hint_username": "",
        }, status_code=400)

    try:
        await auth.create_admin(db, username, password)
    except auth.AuthError as e:
        return templates.TemplateResponse(request, "setup.html", {
            "error": str(e), "hint_username": "",
        }, status_code=400)

    # 创建后直接登录，免去再输一次
    user = await auth.get_user_by_username(db, username)
    token, _ = await auth.create_session(db, user["id"], config.web.session_days)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(**_cookie_kwargs(config, config.web.session_days * 86400), value=token)
    logger.info("管理员账号已创建并完成首次登录")
    return resp


# ── 登录 ────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: str | None = None,
    db=Depends(get_db),
    config=Depends(get_config),
    user: dict | None = Depends(current_user),
):
    if await auth.count_users(db) == 0:
        return RedirectResponse("/setup", status_code=303)
    if user is not None:
        return RedirectResponse(_safe_next(next), status_code=303)

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {
        "error": None, "next": _safe_next(next),
        # 传入真实配置值，避免改了 auth_request_limit 后页面仍显示硬编码默认
        "limit": config.web.auth_request_limit,
        "window": config.web.auth_window_minutes,
    })


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
    db=Depends(get_db),
    config=Depends(get_config),
):
    templates = request.app.state.templates
    ip = client_ip(request)
    target = _safe_next(next)

    # 限流检查（DB 持久化，容器重启不重置计数）
    try:
        await auth.check_rate_limit(
            db, ip,
            limit=config.web.auth_request_limit,
            window_minutes=config.web.auth_window_minutes,
        )
    except auth.RateLimited as e:
        logger.warning(f"登录限流触发: ip={ip} user={username!r}")
        await auth.record_attempt(db, ip, username, success=False)
        return templates.TemplateResponse(request, "login.html", {
            "error": str(e), "next": target,
        }, status_code=429)

    user = await auth.authenticate(db, username, password)
    if user is None:
        await auth.record_attempt(db, ip, username, success=False)
        # 统一话术，不区分用户名/密码错误，避免枚举
        return templates.TemplateResponse(request, "login.html", {
            "error": "用户名或密码错误", "next": target,
        }, status_code=401)

    await auth.record_attempt(db, ip, username, success=True)
    token, _csrf = await auth.create_session(db, user["id"], config.web.session_days)

    logger.info(f"登录成功: user={username} ip={ip}")
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(**_cookie_kwargs(config, config.web.session_days * 86400), value=token)
    return resp


# ── 登出 ────────────────────────────────────────────────────

@router.post("/logout")
async def logout(
    request: Request,
    _user: dict = Depends(verify_csrf),
    db=Depends(get_db),
    config=Depends(get_config),
):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        await auth.revoke_session(db, token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(**{k: v for k, v in _cookie_kwargs(config, None).items()
                          if k != "max_age"})
    return resp


# ── 改密 / 登出所有设备 ─────────────────────────────────────

@router.post("/account/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    if not auth.verify_password(
        (await auth.get_user_by_id(db, user["id"]))["password_hash"], current_password
    ):
        return RedirectResponse("/settings?err=old_password", status_code=303)
    try:
        await auth.change_password(db, user["id"], new_password)
    except auth.AuthError:
        return RedirectResponse("/settings?err=weak_password", status_code=303)

    # 改密后全部会话已吊销，重新签一个当前会话
    token, _ = await auth.create_session(db, user["id"], config.web.session_days)
    resp = RedirectResponse("/settings?ok=password", status_code=303)
    resp.set_cookie(**_cookie_kwargs(config, config.web.session_days * 86400), value=token)
    return resp


@router.post("/account/revoke-all")
async def revoke_all(
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    n = await auth.revoke_all_sessions(db, user["id"])
    logger.info(f"用户 {user['username']} 登出所有设备（{n} 个会话）")
    return RedirectResponse(f"/settings?ok=revoke&n={quote(str(n))}", status_code=303)
