"""日志中心（v0.10）：/logs 页实时 tail + /api/logs/tail 供前端轮询。

背景：启动后的 INFO 日志此前只进 stdout（容器一收就没了），排障只能 docker logs。
v0.10 起主流程日志同步写 /config/logs/hub.log（10MB×5 轮转，见 main.lifespan），
本页面在 UI 里直接 tail 该文件：

- 服务端渲染最近 N 行（默认 400，最多 1000）
- 级别筛选（全部 / INFO / WARNING / ERROR）按 loguru 行内的级别字段匹配
- 前端每 3s 拉 /api/logs/tail 增量重绘（仅「跟随」开启且页面可见时）；
  无 WebSocket——内网 SSR 场景下轮询足够，少一条长连接的状态管理

只读：不提供任何删除 / 下载端点，清理交给轮转。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from .deps import get_config, get_db, require_user
from .routes_pages import _common

router = APIRouter(tags=["logs"])

#: 单次 tail 的读取窗口上限：日志行可能超长（异常栈），窗口封顶防内存尖峰
_TAIL_WINDOW_BYTES = 768 * 1024

#: 级别筛选 → loguru 行内的级别字段（格式里 level 左对齐补空格到 8 位）
_LEVEL_FIELDS = {"info": "| INFO", "warning": "| WARNING", "error": "| ERROR"}


def log_file_path(config) -> Path:
    """与 main.lifespan 的文件 sink 同源：db_path 同级 logs/hub.log。"""
    return Path(config.db_path).parent / "logs" / "hub.log"


def tail_lines(path: Path, n: int = 400) -> list[str]:
    """读文件末尾 n 行；文件不存在 / 不可读返回空表（页面显示空态）。

    从窗口起点 seek 后整体 decode，按行切分取尾部——避免逐行回扫。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            start = max(0, size - _TAIL_WINDOW_BYTES)
            f.seek(start)
            blob = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = blob.splitlines()
    if start > 0 and lines:
        # 窗口起点大概率切在行中间，第一行是残行，丢弃
        lines = lines[1:]
    return lines[-n:]


def filter_level(lines: list[str], level: str | None) -> list[str]:
    """按级别过滤 loguru 行；level 为空 / 未知时原样返回。"""
    field = _LEVEL_FIELDS.get((level or "").lower())
    if not field:
        return lines
    return [ln for ln in lines if field in ln]


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    level: str | None = Query(default=None),
    n: int = Query(default=400, le=1000),
    user: dict = Depends(require_user),
    config=Depends(get_config),
    db=Depends(get_db),
):
    templates = request.app.state.templates
    ctx = await _common(request, db, config, user)
    lines = filter_level(tail_lines(log_file_path(config), n=n), level)
    ctx.update({
        "active": "logs",
        "lines": lines,
        "current_level": level or "",
        "line_count": len(lines),
        "log_exists": log_file_path(config).exists(),
    })
    return templates.TemplateResponse(request, "logs.html", ctx)


@router.get("/api/logs/tail")
async def logs_tail_api(
    level: str | None = Query(default=None),
    n: int = Query(default=200, le=1000),
    user: dict = Depends(require_user),
    config=Depends(get_config),
):
    """前端轮询端点：返回过滤后的行与是否被窗口截断（JSON，需登录）。"""
    lines = await asyncio.to_thread(tail_lines, log_file_path(config), n)
    # 级别过滤后行数会变少，但截断语义按「文件尾巴是否在窗口内」判断：
    # 行数等于窗口能装下的极限时视为截断，前端提示「日志过长，请缩小范围」
    return {
        "lines": filter_level(lines, level),
        "level": level or "",
        "count": len(lines),
        "truncated": len(lines) >= n,
    }
