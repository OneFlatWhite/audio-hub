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
    # 全局过滤器：相对时间（发现墙「3 天前」这类展示）
    templates.env.filters["reltime"] = _reltime
    # 全局过滤器：datetime → 本地时区短格式（当年显示 MM-DD HH:mm，往年带年份）
    templates.env.filters["dtfmt"] = _dtfmt
    # 全局过滤器：任务 / 事件状态英文值 → 中文展示（DB 与 API 仍用英文值）
    templates.env.filters["state_zh"] = _state_zh
    # 全局过滤器：事件种类英文 key → 中文（未知名称原样返回）
    templates.env.filters["kind_zh"] = _kind_zh
    # 全局过滤器：置信度英文值 → 中文（high→高 / medium→中 / low→低）
    templates.env.filters["conf_zh"] = _conf_zh
    # 全局过滤器：日志行 → 级别 class（日志页按级别着色，前端 tail 同一套规则）
    templates.env.filters["loglevel"] = _loglevel
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


def _reltime(dt) -> str:
    """datetime → 相对时间（'3 天前'）。naive 输入按 UTC 处理；空值为 '—'。"""
    if not dt:
        return "—"
    from datetime import UTC

    from ..timeutil import utcnow
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=UTC)
    secs = max((utcnow() - dt).total_seconds(), 0)
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{int(secs // 60)} 分钟前"
    if secs < 86400:
        return f"{int(secs // 3600)} 小时前"
    if secs < 30 * 86400:
        return f"{int(secs // 86400)} 天前"
    return dt.strftime("%Y-%m-%d")


def _dtfmt(dt, *, with_time: bool = True) -> str:
    """datetime / ISO 字符串 → 站点时区短格式：当年 'MM-DD HH:mm'，往年带年份。

    DB 里部分时间戳是 TEXT 列（ISO 字符串直出），所以字符串输入先 fromisoformat
    解析；解析失败原样返回。空值为 '—'。
    """
    if not dt:
        return "—"
    from datetime import datetime

    from ..timeutil import SITE_TZ
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    try:
        local = dt.astimezone(SITE_TZ)
    except (ValueError, TypeError, OSError):
        return str(dt)
    now = datetime.now(SITE_TZ)
    day = "%Y-%m-%d" if local.year != now.year else "%m-%d"
    return local.strftime(f"{day} %H:%M" if with_time else day)


#: 任务状态英文值 → 中文。DB / API 层仍用英文值，只有展示层翻译。
TASK_STATE_ZH: dict[str, str] = {
    "discovered": "已发现",
    "scored": "已评分",
    "dispatched": "已派发",
    "downloading": "下载中",
    "completed": "已完成",
    "routed": "已归档",
    "notified": "已通知",
    "rejected": "已拒绝",
    "failed": "失败",
    # 做种状态（seeding 表）与任务状态无值冲突，共用一张表
    "seeding": "做种中",
    "satisfied": "已达标",
    "removed": "已移除",
}

#: 事件级别英文值 → 中文
EVENT_LEVEL_ZH: dict[str, str] = {
    "info": "信息",
    "warning": "警告",
    "error": "错误",
}

#: 事件种类英文 key → 中文。收集自仓内全部 emit(Event(...)) 调用点
#: （scheduler / subscription.engine / completion.monitor / seeding.lifecycle /
#:   series.tracker / sites.auth_check / notify.*）；未知 key 原样返回，
#: 新事件种类上线不至于显示空白。
EVENT_KIND_ZH: dict[str, str] = {
    # 流水线汇总 / 任务链路
    "round_summary": "轮次汇总",
    "dispatched": "已派发",
    "completed": "下载完成",
    "failed": "任务失败",
    "routed": "已归档",
    "archive_skipped": "归档跳过",
    "quarantined": "进入隔离",
    "scrape_failed": "刮削失败",
    # 订阅
    "subscription_hit": "订阅命中",
    "subscription_completed": "订阅完成",
    "subscription_upgrade": "订阅洗版",
    # 站点 / 下载器 / 适配器
    "adapter_error": "适配器错误",
    "site_circuit_open": "站点熔断",
    "site_auth_expired": "站点认证失效",
    "downloader_unreachable": "下载器不可达",
    # 连载 / 做种 / 其它
    "series_completed": "连载完结",
    "hr_satisfied": "H&R 达标",
    "test": "测试通知",
}

#: 置信度英文值 → 中文
CONFIDENCE_ZH: dict[str, str] = {
    "high": "高",
    "medium": "中",
    "low": "低",
}


def _state_zh(value) -> str:
    """状态值 → 中文；未知值原样返回（新状态上线不至于显示空白）。"""
    if value is None:
        return "—"
    key = getattr(value, "value", value)
    return TASK_STATE_ZH.get(key, EVENT_LEVEL_ZH.get(key, str(key)))


def _kind_zh(value) -> str:
    """事件种类 key → 中文；未知 key 原样返回。"""
    if value is None:
        return "—"
    key = getattr(value, "value", value)
    return EVENT_KIND_ZH.get(key, str(key))


def _conf_zh(value) -> str:
    """置信度英文值 → 中文（high→高 / medium→中 / low→低）。"""
    if value is None:
        return "—"
    key = getattr(value, "value", value)
    return CONFIDENCE_ZH.get(key, str(key))


#: 日志级别（loguru 的 level 字段）→ 页面着色 class 后缀
_LOG_LEVEL_CLASS: dict[str, str] = {
    "ERROR": "err",
    "CRITICAL": "err",
    "WARNING": "warn",
    "INFO": "info",
    "DEBUG": "debug",
    "TRACE": "debug",
}


def _loglevel(value) -> str:
    """日志行 → 级别 class（err/warn/info/debug）。

    行格式来自 logsetup 的 sink：`ts | LEVEL | name:func:line | msg`，
    取第二段判定；解析不出管道结构时按行内关键词兜底，
    与 app.js 里 tail 增量渲染保持同一套规则。
    """
    s = str(value or "")
    parts = s.split("|")
    if len(parts) >= 3:
        cls = _LOG_LEVEL_CLASS.get(parts[1].strip().upper())
        if cls:
            return cls
    low = s.lower()
    if "error" in low:
        return "err"
    if "warn" in low:
        return "warn"
    if "debug" in low or "trace" in low:
        return "debug"
    return "info"
