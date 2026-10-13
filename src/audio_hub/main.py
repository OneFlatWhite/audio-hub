"""FastAPI 应用入口 + lifespan 管理 + Web 层装配。

v0.2：在 v0.1 的资源装配基础上接入 Web 界面与认证（PRD 第十二章）。
- 页面路由（/dashboard /watchlist /review ...）需登录，见 web/routes_pages.py
- 认证路由（/login /setup /logout）见 web/routes_auth.py
- 调试用 JSON API 收敛到 /api/ 前缀，需登录；写操作另需 CSRF
- 设置页可编辑（v0.3）：改动存 DB 覆盖层，见 settings_store 与
  web/routes_settings。每项显式标注热生效或需重启，绝不谎报已生效
- /health 保持无认证（健康检查惯例，供容器探针使用）
- v0.4：资料库 / 隔离区 / 连载 / 做种 / 事件五页骨架（只读）；通知改为
  MultiNotifier（日志 + events 表），挂在 app.state.notifier 供后续模块复用
- v0.4 第二波：推送 sink（企微 / Telegram / webhook）按 notify 配置追加到
  notifier；设置页保存通知组后由 rebuild_push_sinks 热重建
- v0.6：站点与下载器的权威源改为 DB（sites / downloaders 表），YAML 只在首启
  做种子（registry_store.seed_from_yaml）；适配器从 DB 加载，多 qb 实例挂在
  app.state.downloaders，app.state.qb_client 保持为默认下载器客户端的别名
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from loguru import logger

from . import __version__, registry_store, repo, settings_store
from .config import load_config
from .db import init_db
from .downloaders import DownloaderRegistry
from .notify import (
    DbNotifier,
    LogNotifier,
    MultiNotifier,
    build_push_sinks,
)
from .notify import rebuild_push_sinks as _rebuild_push_sinks
from .scheduler import PipelineRunner, build_scheduler
from .web import (
    routes_auth,
    routes_discover,
    routes_downloaders,
    routes_events,
    routes_library,
    routes_logs,
    routes_pages,
    routes_quarantine,
    routes_seeding,
    routes_series,
    routes_settings,
    routes_sites,
    routes_subscriptions,
)
from .web.deps import (
    NotAuthenticated,
    get_config,
    get_db,
    not_authenticated_handler,
    require_user,
    verify_csrf,
)
from .web.templates import build_templates, mount_web_assets


def _make_qb_client(qb_config):
    """构造下载器客户端（qbittorrent / Transmission 按 cfg.type 分流）。

    qb 认证：username 非空用用户名 / 密码；否则用 API Key（qb v5.2.0+，避开
    SID→QBIT_SID 改名陷阱，PRD §8.4）。Transmission 用用户名 / 密码（内网
    白名单场景可不填）。也是 DownloaderRegistry 的 client_factory。
    未配凭据或连接失败返回 None。
    """
    label = getattr(qb_config, "id", "qb")
    is_qb = (getattr(qb_config, "type", "") or "qbittorrent").strip().lower() != "transmission"
    if is_qb and not qb_config.username and not qb_config.api_key:
        logger.warning(f"下载器 {label} 未配置凭据；实跑模式将无法派发，dry_run 不受影响")
        return None
    try:
        from .downloaders.client import build_client
        client = build_client(qb_config, login=True)
        if client is None:
            return None
        logger.info(f"下载器 {label} 已连接: {qb_config.host}:{qb_config.port} "
                    f"(v{client.app.version}, API {client.app.web_api_version})")
        return client
    except Exception as e:  # noqa: BLE001 - 单台下载器不可用不应阻止 Web 启动
        logger.error(f"下载器 {label} 连接失败: {e}；实跑派发将不可用，dry_run 仍可运行")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时装配资源，关闭时清理。"""
    config = load_config()

    # DB 必须先于日志初始化：设置覆盖层存在 DB 里，而 log_level 自身
    # 可能被覆盖 —— 若先按 YAML 的 log_level 装配日志再合并覆盖层，
    # 该项改动连重启都不会生效。
    # db_path 刻意不在覆盖层内：让被管理者管理自己会形成鸡生蛋问题
    # （改完路径，下次就读不到覆盖层了），故它只能改 YAML。
    db = await init_db(config.db_path)

    # 合并界面保存的设置（含 secret 解密）。必须在适配器与 qb 客户端
    # 构建之前，否则 dry_run / 站点开关的改动重启后依然不生效。
    await settings_store.hydrate_config(db, config, settings_store.all_specs(config))

    # v0.6：YAML 的 sites / qbittorrent 段首启导入 DB（幂等），并收编 app_settings
    # 里的旧站点覆盖项。必须在 hydrate 之后：qb 段要拿合并过覆盖层的值做种子。
    await registry_store.seed_from_yaml(db, config)
    # 此后 config.sites 反映 DB 现状（含解密凭据）：做种轮询的站点 H&R 规则、
    # 页面凭据状态等仍读 config.sites，站点改动后由 settings_store.rebuild_adapters 同步。
    config.sites = await registry_store.load_site_configs(db)

    # 启动本身即意味着全部覆盖项（含需重启项）已生效，故清除待重启标记。
    # 不清的话横幅会永久挂着，「待重启」提示退化成噪音被无视 ——
    # 那比没有提示更糟，因为它会在真正需要提示时也被忽略。
    await settings_store.set_restart_pending(db, False)

    # 日志管线：v0.11 起可热更新级别 —— settings 页改 log_level 时
    # settings_store.apply_hot 会再次调用 setup_logging，整体重建 sink。
    from . import logsetup
    logsetup.setup_logging(config.log_level, config.db_path)

    # 下载器注册表：多 qb 实例，客户端懒建；dry_run 时一律为 None。
    # qb_client 保持为默认下载器客户端的别名，现有派发 / 监听 / 做种链路不变。
    downloaders = DownloaderRegistry(
        await registry_store.load_downloader_configs(db),
        client_factory=_make_qb_client, dry_run=config.dry_run,
    )
    default_dl = downloaders.default()
    qb_client = default_dl.client if default_dl else None

    # 日志 + DB 事件流两个基础 sink；推送渠道（企微 / Telegram / webhook）按
    # 已合并覆盖层的 notify 配置追加。hydrate 必须在前，否则界面存的凭据重启后不生效。
    notifier = MultiNotifier([LogNotifier(config.log_level), DbNotifier(db)])
    for s in build_push_sinks(config.notify):
        notifier.add(s)
    # runner 持注册表引用：设置页 rebuild_downloaders 原地 replace 后，派发 / 监听 / 做种
    # 链路自动看到新实例；app 传入供站点巡检热重建适配器。
    runner = PipelineRunner(db, config, notifier=notifier, registry=downloaders, app=app)

    # 启动对账（崩溃恢复，PRD §13.6）
    await runner.monitor.reconcile_on_startup()

    scheduler = build_scheduler(runner, config)
    scheduler.start()

    app.state.db = db
    app.state.config = config
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.state.downloaders = downloaders   # v0.6 下载器注册表
    app.state.qb_client = qb_client     # 默认下载器客户端别名；Web 派发待审项时需要（实跑模式）
    app.state.notifier = notifier       # 通知测试端点 / 隔离区动作等后续模块复用

    # 首次启动无管理员时给出醒目提示（引导访问 /setup）
    from .web import auth as web_auth
    if await web_auth.count_users(db) == 0:
        logger.warning("尚未创建管理员账号，请访问 /setup 完成初始化")

    logger.info(
        f"audio-hub v{__version__} 启动 | dry_run={config.dry_run} | "
        f"adapters={list(runner.adapters)} | downloaders={[c.id for c in downloaders.configs]} | "
        f"db={config.db_path} | push={[s.channel for s in notifier.push_sinks]}"
    )

    yield

    scheduler.shutdown(wait=False)
    await db.close()
    logger.info("audio-hub 已关闭")


app = FastAPI(title="audio-hub", version=__version__, lifespan=lifespan)


def rebuild_push_sinks(app_or_notifier, notify_cfg):
    """按当前 notify 配置重建推送 sink（设置页保存通知组后调用）。

    实现在 notify.webhook_notify；这里的入口让调用方只需认 main 一处。
    接受 app（取 app.state.notifier）或 MultiNotifier 本身。
    """
    return _rebuild_push_sinks(app_or_notifier, notify_cfg)


# ── Web 层装配（模块级，不依赖运行时状态）──
mount_web_assets(app, build_templates())
app.add_exception_handler(NotAuthenticated, not_authenticated_handler)
app.include_router(routes_auth.router)
app.include_router(routes_pages.router)
app.include_router(routes_discover.router)
app.include_router(routes_settings.router)
app.include_router(routes_library.router)
app.include_router(routes_quarantine.router)
app.include_router(routes_series.router)
app.include_router(routes_seeding.router)
app.include_router(routes_events.router)
app.include_router(routes_logs.router)
app.include_router(routes_sites.router)
app.include_router(routes_subscriptions.router)
app.include_router(routes_downloaders.router)


# ── 无认证端点 ──────────────────────────────────────────────

@app.get("/health")
async def health():
    """健康检查（容器探针用，不含敏感信息）。"""
    config = getattr(app.state, "config", None)
    runner = getattr(app.state, "runner", None)
    return {
        "status": "ok",
        "version": __version__,
        "dry_run": config.dry_run if config else None,
        "adapters": list(runner.adapters) if runner else [],
    }


# ── 调试用 JSON API（需登录，收敛到 /api/ 前缀）──────────────

@app.get("/api/status")
async def api_status(
    user: dict = Depends(require_user),
    db=Depends(get_db),
    config=Depends(get_config),
):
    """任务状态统计（JSON）。"""
    by_state = await repo.count_tasks_by_state(db)
    entries = await repo.list_watch_entries(db, enabled_only=False)
    return {
        "tasks_by_state": by_state,
        "watchlist_count": len(entries),
        "review_count": await repo.count_pending_review(db),
        "dry_run": config.dry_run,
    }


@app.post("/api/run-once")
async def api_run_once(
    request: Request,
    user: dict = Depends(verify_csrf),
):
    """手动触发一轮流水线（JSON，调试用）。

    与页面版 POST /run-once 的区别：本端点返回 JSON 而非重定向，
    供脚本/监控调用。会向站点发起真实请求，故与页面版一致要求
    登录 + CSRF（PRD §12.3：所有写操作必须过 CSRF）。
    脚本调用请带 X-CSRF-Token 请求头。
    """
    summary = await request.app.state.runner.run_once()
    return summary
