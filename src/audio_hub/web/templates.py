"""模板引擎与静态资源装配。

Jinja2 服务端渲染 + 本地静态文件（无 CDN，NAS 内网离线可用）。

注意：本模块文件名 templates.py 与同级的 templates/ 目录同名。
CPython 的 FileFinder 在解析 `web.templates` 时，文件加载器优先于
命名空间目录，故 `from .web.templates import ...` 命中本文件；
而 Jinja2 用文件系统路径读取 templates/ 下的 .html，不走 Python import。
两者互不干扰，但阅读时需留意这一区分。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__

_HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


def build_templates() -> Jinja2Templates:
    """构造 Jinja2Templates 实例。"""
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    # 全局过滤器：字节数转人类可读
    templates.env.filters["filesize"] = _filesize
    # 版本号单一真源：侧边栏 brand 与 /health 都读 __init__.__version__，
    # 模板里不再写死字面量（v0.2 时曾因此与 /health 不一致）。
    templates.env.globals["version"] = __version__
    return templates


def mount_web_assets(app: FastAPI, templates: Jinja2Templates) -> None:
    """挂载静态资源目录，并把 templates 存入 app.state。"""
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.templates = templates


def _filesize(n: int | None) -> str:
    """1234567 → '1.2 MB'。模板过滤器用。"""
    if not n:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"
