"""激活页（/activate）：授权码的查看、粘贴与状态展示。

特殊页面：不受授权门禁限制（强制模式下它是唯一的入口页），也不要求登录——
门禁生效时用户可能还没登录，激活本身就是登录前的动作。

流程（详见 docs/LICENSE-ACTIVATION.md）：
1. 给 GitHub 仓库点 Star；
2. 打开发签页（作者部署的 Cloudflare Worker），GitHub OAuth 登录；
3. Worker 校验你确实 Star 过，签出授权码；
4. 回到本页粘贴授权码 → 保存即激活。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from .. import __version__
from .. import license as license_mod
from .deps import get_db

router = APIRouter(tags=["license"])

_FLASH_OK = {"activated": "授权码已激活，感谢支持 🎉"}
_FLASH_ERR = {
    "invalid": "授权码无效或已过期：请确认粘贴完整（AH1. 开头），且 Star 过仓库后再重新获取",
    "readonly": "无法保存授权码：数据库不可写",
}


def _flash(request: Request, kind: str) -> str | None:
    key = request.query_params.get(kind)
    if not key:
        return None
    table = _FLASH_OK if kind == "ok" else _FLASH_ERR
    return table.get(key, key)


@router.get("/activate", response_class=HTMLResponse)
async def activate_page(
    request: Request,
    db=Depends(get_db),
):
    state = await license_mod.check_state(db)
    templates = request.app.state.templates
    # 独立布局：不要求登录；base.html 的未定义变量（user/dry_run 等）按假值渲染
    ctx = {
        "request": request,
        "version": __version__,
        "csrf": "",
        "state": state,
        "flash_ok": _flash(request, "ok"),
        "flash_err": _flash(request, "err"),
        "repo_url": f"https://github.com/{license_mod.DEFAULT_REPO}",
    }
    return templates.TemplateResponse(request, "activate.html", ctx)


@router.post("/activate")
async def activate_save(
    request: Request,
    db=Depends(get_db),
):
    form = await request.form()
    key = (form.get("license_key") or "").strip()
    payload = license_mod.verify_license(key) if key else None
    if key and payload is None and license_mod.public_key_b64():
        # 公钥已配置但验签失败：拒绝保存，避免用户以为激活成功
        return RedirectResponse("/activate?err=invalid", status_code=303)
    try:
        await license_mod.set_stored_key(db, key)
    except OSError as e:
        logger.warning(f"授权码保存失败: {e}")
        return RedirectResponse("/activate?err=readonly", status_code=303)
    if payload:
        logger.info(f"授权码已激活（user={payload.get('user')}）")
        return RedirectResponse("/login?ok=activated", status_code=303)
    return RedirectResponse(
        "/activate?ok=activated&detail=" + quote("已保存；当前未配置验证公钥，保存后暂不校验"),
        status_code=303,
    )
