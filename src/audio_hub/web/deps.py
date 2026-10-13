"""FastAPI 依赖注入：当前用户、CSRF 校验、管理员权限。"""

from __future__ import annotations

from fastapi import Depends, Form, Header, Request
from fastapi.responses import RedirectResponse

from . import auth

LOGIN_PATH = "/login"


def get_db(request: Request):
    return request.app.state.db


def get_config(request: Request):
    return request.app.state.config


async def current_user(
    request: Request,
    db=Depends(get_db),
) -> dict | None:
    """解析会话，返回 user dict 或 None（不强制跳转，由页面自行处理）。"""
    token = request.cookies.get(auth.SESSION_COOKIE)
    result = await auth.resolve_session(db, token)
    if result is None:
        request.state.user = None
        request.state.csrf = None
        return None
    user, csrf = result
    request.state.user = user
    request.state.csrf = csrf
    return user


async def require_user(
    request: Request,
    user: dict | None = Depends(current_user),
) -> dict:
    """要求已登录。未登录抛 401，由异常处理器统一转 /login 重定向。"""
    if user is None:
        raise NotAuthenticated()
    return user


class NotAuthenticated(Exception):
    """未登录。注册全局处理器转为重定向，避免每个页面重复判断。"""


def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    """页面请求重定向到登录页；API 请求返回 401 JSON。"""
    from fastapi.responses import JSONResponse

    wants_json = request.url.path.startswith("/api/")
    if wants_json:
        return JSONResponse({"detail": "not_authenticated"}, status_code=401)
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(f"{LOGIN_PATH}?next={target}", status_code=303)


async def require_admin(user: dict = Depends(require_user)) -> dict:
    if not user.get("is_admin"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


async def verify_csrf(
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias=auth.CSRF_HEADER),
    csrf_token: str | None = Form(default=None, alias=auth.CSRF_FIELD),
    user: dict = Depends(require_user),
) -> dict:
    """所有写操作必须过这一层（PRD §12.3）。

    token 来源优先级：表单字段 > 请求头（fetch 用头，传统表单用字段）。
    """
    provided = csrf_token or x_csrf_token
    expected = getattr(request.state, "csrf", None)
    if not auth.csrf_ok(expected or "", provided):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面重试")
    return user


async def require_admin_csrf(user: dict = Depends(verify_csrf)) -> dict:
    """管理员 + CSRF 双校验，给破坏性写操作用。

    verify_csrf 只保证已登录；改配置、丢弃隔离内容、清除回收区、删连载、
    清理事件这类不可逆动作还需管理员。普通用户能审片 / 放行 / 恢复，
    但不该能删掉东西。
    """
    if not user.get("is_admin"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def client_ip(request: Request) -> str:
    """取客户端 IP。

    host 网络下无代理，直接用 request.client.host。
    若将来前置反代，需改为读 X-Forwarded-For 的第一个地址，
    且必须确认反代可信——否则攻击者可伪造该头绕过限流。
    """
    if request.client and request.client.host:
        return request.client.host
    return "unknown"
