"""调度层：把订阅引擎（RSS 捕获 / 主动搜索）→ 派发 → 完成监听 → 做种串成周期任务。

设计要点：
- 单进程内嵌 APScheduler（PRD §8.1：v0.1 不引入 Redis/broker）
- 每类任务串行执行，**上一轮未结束不启动下一轮**（防重入导致重复派发）
- 请求预算：站点请求频率由订阅引擎的站点预算 / 熔断 / 认证门禁约束（账号安全，非性能）

v0.6 job 清单（间隔键在 config.SearchConfig / poll_interval_minutes，设置页热生效）：
- rss_capture         每 search.rss_interval_minutes      engine.run_rss_capture_once → 派发
- subscription_search 每 search.subscription_scan_minutes engine.run_subscription_search_once → 派发
- completion_poll     每 poll_interval_minutes            monitor.poll_once（取代旧 pipeline_round）
- site_auth_check     每 search.auth_check_hours 小时     sites.auth_check.run_auth_check_once
- seeding_check       每 seeding.check_interval_minutes   seeding_monitor.poll_once

旧「search_round → match_candidates → dispatch」轮次已由订阅引擎取代：引擎只把命中建成
DISCOVERED 任务（不碰 qb），本模块把 created_tasks 交给 Dispatcher.dispatch_tasks 派发。
run_once() 保留为「RSS 捕获 + 派发 + 完成轮询」的手动触发入口（/run-once、CLI）。
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from . import repo
from .adapters.registry import build_adapters
from .completion.monitor import CompletionMonitor
from .downloaders.registry import DownloaderRegistry
from .models import HitDecision, SubStatus, Task, TaskState
from .notify.base import Event
from .notify.log_notify import LogNotifier
from .pipeline.dispatcher import Dispatcher
from .seeding.lifecycle import SeedingMonitor
from .subscription import engine as sub_engine
from .timeutil import utcnow

# 默认搜索并发上限（PRD §8.5：每站独立预算）
DEFAULT_MAX_CONCURRENCY = 2

#: 调度间隔安全下限（分钟）：触网站点的任务不得低于此值，防误配打爆 PT 站
MIN_SITE_INTERVAL_MINUTES = 5


class PipelineRunner:
    """各周期任务的执行器：持有适配器、下载器注册表、派发器与两个监听器。"""

    def __init__(
        self, db, config, qb_client=None, notifier=None, *,
        registry: DownloaderRegistry | None = None, app=None,
    ):
        """
        Args:
            db: aiosqlite 连接
            config: AppConfig
            qb_client: （旧式单实例）默认 qb 客户端；registry 为 None 时包成单实例注册表
            notifier: 事件接收端；None 用 LogNotifier
            registry: 下载器注册表（v0.6，main 传 app.state.downloaders）
            app: FastAPI app；站点巡检需要它热重建适配器（run_auth_check_once(app=...)）
        """
        self.db = db
        self.config = config
        self.notifier = notifier or LogNotifier()
        self.app = app
        if registry is None:
            # 旧式单实例：注册表 dry_run 固定 False，空跑由 Dispatcher / Monitor 自身守卫
            registry = DownloaderRegistry(
                [config.qbittorrent], client_factory=lambda _cfg: qb_client, dry_run=False,
            )
        self.registry = registry
        self.adapters = build_adapters(config)
        self.dispatcher = Dispatcher(
            dry_run=config.dry_run, registry=registry, db=db,
            adapter_lookup=lambda site: self.adapters.get(site),
        )
        # notifier 必须传给监听器：归档链路（routed / quarantined / archive_skipped /
        # scrape_failed）的事件从这里发出，不传则这些事件只写日志、不进 events 表与推送。
        self.monitor = CompletionMonitor(
            db, None, config, dry_run=config.dry_run, notifier=self.notifier, registry=registry,
        )
        # 做种 H&R 跟踪（v0.4 模块 D）：按注册表逐台只读轮询
        self.seeding_monitor = SeedingMonitor(
            db, config, notifier=self.notifier, registry=registry,
        )
        self._running = False
        self._searching = False

    # ── 旧接口兼容 ──

    @property
    def qb_client(self):
        """默认下载器的客户端（旧调用方 / 测试仍读此属性）。"""
        h = self.registry.default()
        return h.client if h else None

    def on_downloaders_rebuilt(self) -> None:
        """settings_store.rebuild_downloaders 在 registry.replace 后调用：清掉派生缓存。"""
        self.seeding_monitor.reset_clients()

    # ── 派发 ──

    async def dispatch_created(self, tasks: list[Task]) -> dict:
        """公开入口：路由层「立即搜索 / 一键订阅」建完任务后立刻派发。

        否则 DISCOVERED 任务要等调度器下一轮（最长一个 subscription_scan 周期），
        用户在界面上点了「立即搜索」却看不到下载，会以为功能坏了。
        dry_run 下引擎建的是 SCORED 任务，这里自然空转。
        """
        return await self._dispatch_created(tasks)

    async def _dispatch_created(self, tasks: list[Task]) -> dict:
        """把引擎建好的 DISCOVERED 任务交给派发器；待审（SCORED）等不动。"""
        pending = [t for t in tasks if t.state == TaskState.DISCOVERED]
        if not pending:
            return {"dispatched": 0, "dispatch_failed": 0}
        result = await self.dispatcher.dispatch_tasks(pending)
        for task in result.dispatched:
            await self.notifier.dispatched(
                task.site, task.title, score=task.score, quality=task.quality,
                downloader_id=result.downloader_ids.get(task.info_hash),
                dry_run=self.config.dry_run,
            )
            await self._complete_one_shot_subscription(task)
        for task, reason in result.failed:
            await self.notifier.failed(task.site, task.title, reason)
            await self._rollback_failed_dispatch(task, reason)
        return {"dispatched": result.ok_count, "dispatch_failed": len(result.failed)}

    async def _complete_one_shot_subscription(self, task: Task) -> None:
        """一次性订阅的完结标记：实跑下延后到派发确认成功才置 completed。

        引擎 decide 时任务只是 DISCOVERED（没碰 qb）；若那时就标 completed，
        qb 不在线 / 直链换取失败会把订阅永远卡在「已完成但没下到」。
        dry_run 无实发，引擎已在 decide 当场完结，这里直接跳过。
        """
        if self.config.dry_run or not task.watch_id:
            return
        watch = await repo.get_watch_entry(self.db, task.watch_id)
        if watch is None or watch.status == SubStatus.COMPLETED:
            return
        if not sub_engine.is_one_shot(watch):
            return
        await repo.set_sub_status(self.db, watch.id, SubStatus.COMPLETED,
                                  completed_at=utcnow())
        await self.notifier.emit(Event(
            kind=sub_engine.EVENT_SUBSCRIPTION_COMPLETED, site=task.site,
            title=f"订阅已完成：{watch.query}",
            detail={"watch_id": watch.id, "sub_type": watch.sub_type.value,
                    "album_key": task.album_key or "", "task_id": task.id},
        ))

    async def _rollback_failed_dispatch(self, task: Task, reason: str) -> None:
        """派发失败的订阅任务回滚：撤回命中流水，任务重置回 DISCOVERED 等下轮重试。

        命中留着会让该专辑被 has_album 视为「已拥有」、同种子被 has_hit 挡住，
        订阅保持 active 却永远等不到重试。回滚后下一轮引擎重新决策重新派发；
        候选若已从 RSS 消失，订阅停在 active、任务停在 DISCOVERED —— 诚实，
        且候选再次出现时能继续。失败本身有 tasks.error_message 与 failed 事件记账。
        """
        if self.config.dry_run or not task.watch_id:
            return
        removed = await repo.delete_hits_for_task(self.db, task.id)
        await repo.set_task_state(self.db, task.id, TaskState.DISCOVERED,
                                  error_message=None)
        if removed:
            logger.warning(
                f"派发失败，订阅保持活跃等待下轮重试: {task.title[:60]!r} | {reason}"
            )

    # ── 各 job 入口 ──

    async def run_rss_capture_once(self) -> dict:
        """rss_capture job：RSS 捕获 → 派发命中。"""
        if not self.adapters:
            logger.error("无可用适配器，检查站点配置")
            return {"no_adapters": True}
        summary = await sub_engine.run_rss_capture_once(
            self.db, self.adapters, config=self.config, notifier=self.notifier,
            dry_run=self.config.dry_run,
        )
        for site, err in summary.site_errors.items():
            await self.notifier.adapter_error(site, err)
        out = self._capture_summary_dict(summary)
        out.update(await self._dispatch_created(summary.created_tasks))
        return out

    async def run_subscription_search_once(self, only_watch_id: str | None = None) -> dict:
        """subscription_search job：到点订阅主动搜索 → 派发命中。自带防重入。"""
        if self._searching:
            logger.warning("上一轮订阅搜索尚未结束，本轮跳过")
            return {"skipped": True}
        if not self.adapters:
            logger.error("无可用适配器，检查站点配置")
            return {"no_adapters": True}
        self._searching = True
        try:
            summary = await sub_engine.run_subscription_search_once(
                self.db, self.adapters, config=self.config, notifier=self.notifier,
                dry_run=self.config.dry_run, only_watch_id=only_watch_id,
            )
        except Exception as e:  # noqa: BLE001 - 单轮异常不得让调度器停摆
            logger.exception(f"订阅搜索轮次异常: {e}")
            await self.notifier.failed("", "", f"subscription_search_exception: {type(e).__name__}: {e}")
            return {"error": str(e)}
        finally:
            self._searching = False
        for site, err in summary.site_errors.items():
            await self.notifier.adapter_error(site, err)
        by = _count_decisions(summary.hits)
        out = {
            "due": summary.due,
            "searched": len(summary.searched),
            "deferred": len(summary.deferred),
            "skipped_no_sites": len(summary.skipped_no_sites),
            "no_sites": summary.no_sites,
            "requests": summary.requests,
            "site_errors": summary.site_errors,
            "skipped_sites": summary.skipped_sites,
            "hits": len(summary.hits),
            "decisions": by,
            "dry_run": summary.dry_run,
        }
        out.update(await self._dispatch_created(summary.created_tasks))
        return out

    async def run_site_auth_check_once(self) -> dict:
        """site_auth_check job：站点认证巡检；状态变化时经 app 热重建适配器。"""
        from .sites.auth_check import run_auth_check_once

        try:
            return await run_auth_check_once(
                self.db, self.config, notifier=self.notifier, app=self.app,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"站点巡检异常: {e}")
            return {"error": str(e)}

    async def run_completion_poll_once(self) -> dict:
        """completion_poll job：轮询各下载器，推进完成任务进归档链路。"""
        try:
            return await self.monitor.poll_once()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"完成轮询异常: {e}")
            return {"error": str(e)}

    async def run_discover_auto_refresh_once(self) -> dict:
        """discover_auto_refresh job：后台静默重拉发现墙缓存（music + audiobook）。

        复用 routes 层的墙缓存（app.state.discover_cache），页面下次打开即是
        新数据；每站最短间隔照常生效（刚拉过的站沿用），单 kind 失败不影响另一个。
        """
        from .discover.aggregator import WallCache, collect_wall

        app = self.app
        cache: WallCache = getattr(app.state, "discover_cache", None) or WallCache()
        app.state.discover_cache = cache
        cfg = self.config.discover
        summary: dict = {}
        for kind in ("music", "audiobook"):
            try:
                wall = await collect_wall(
                    dict(self.adapters), kind, cache=cache,
                    ttl=cfg.cache_ttl_minutes * 60, force_refresh=True,
                    refresh_min_interval=cfg.refresh_min_interval_minutes * 60,
                )
                summary[kind] = {"cards": len(wall.cards), "errors": len(wall.site_errors)}
            except Exception as e:  # noqa: BLE001 - 单 kind 失败不影响另一个
                logger.warning(f"发现墙后台刷新失败（kind={kind}）: {e}")
                summary[kind] = {"error": str(e)}
        logger.info(f"发现墙后台刷新完成: {summary}")
        return summary

    async def run_license_revalidate_once(self) -> dict:
        """license_revalidate job：在线复核授权码是否被吊销（取消 Star）。"""
        from . import license as license_mod

        try:
            return await license_mod.revalidate(self.db)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"授权复核异常: {e}")
            return {"error": str(e)}

    async def run_update_check_once(self) -> dict | None:
        """update_check job：拉 GitHub 最新 release，结果挂 app.state 供横幅显示。"""
        from . import updates

        try:
            return await updates.check(self.app)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"新版本检测异常: {e}")
            return None

    async def run_seeding_once(self) -> dict:
        """seeding_check job（与 /seeding 页手动触发共用）。

        与 run_once 独立：做种轮询不触网站点，只读 qb，没必要被搜索轮次的
        防重入闸门挡住。自身的防重入在 SeedingMonitor.poll_once 内。
        """
        summary = await self.seeding_monitor.poll_once()
        return summary.as_dict()

    # ── 手动触发入口（/run-once、CLI run-once） ──

    async def run_once(self) -> dict:
        """执行一轮：RSS 捕获 → 派发 → 轮询完成。

        Returns:
            本轮统计摘要
        """
        # 防重入：上一轮未结束则跳过（PRD §13.6 幂等要求）
        if self._running:
            logger.warning("上一轮流水线尚未结束，本轮跳过（防重复派发）")
            return {"skipped": True}

        self._running = True
        try:
            return await self._run_inner()
        except Exception as e:  # noqa: BLE001
            # 单轮异常不得让调度器停摆
            logger.exception(f"流水线轮次异常: {e}")
            await self.notifier.failed("", "", f"pipeline_exception: {type(e).__name__}: {e}")
            return {"error": str(e)}
        finally:
            self._running = False

    async def _run_inner(self) -> dict:
        entries = await repo.list_watch_entries(self.db, enabled_only=True)
        if not entries:
            logger.info("watchlist 为空，本轮无操作")
            return {"watchlist_empty": True}

        if not self.adapters:
            logger.error("无可用适配器，检查 config.yaml sites 配置")
            return {"no_adapters": True}

        # ── ① RSS 捕获 + 匹配 + 决策（订阅引擎）──
        capture = await sub_engine.run_rss_capture_once(
            self.db, self.adapters, config=self.config, notifier=self.notifier,
            dry_run=self.config.dry_run,
        )
        for site, err in capture.site_errors.items():
            await self.notifier.adapter_error(site, err)

        # ── ② 派发引擎建好的 DISCOVERED 任务 ──
        dispatch = await self._dispatch_created(capture.created_tasks)

        # ── ③ 完成监听轮询 ──
        poll_stats = await self.monitor.poll_once()

        summary = self._capture_summary_dict(capture)
        summary.update(dispatch)
        summary["poll"] = poll_stats
        await self.notifier.round_summary(
            candidates=summary["candidates"],
            accepted=summary["accepted"],
            review=summary["review"],
            rejected=summary["rejected"],
            site_errors=summary["site_errors"],
            skipped_circuit=summary["skipped_circuit"],
            dry_run=self.config.dry_run,
        )
        return summary

    def _capture_summary_dict(self, capture: sub_engine.CaptureSummary) -> dict:
        by = _count_decisions(capture.hits)
        skipped_circuit = [
            s for s, why in capture.skipped_sites.items() if why == "circuit_open"
        ]
        return {
            "candidates": capture.candidates,
            "matched": capture.matched,
            "accepted": by.get(HitDecision.DISPATCHED.value, 0) + by.get(HitDecision.UPGRADE.value, 0),
            "review": by.get(HitDecision.REVIEW.value, 0),
            "rejected": by.get(HitDecision.REJECTED.value, 0),
            "skipped_dup": by.get(HitDecision.SKIPPED_DUP.value, 0),
            "sites_polled": list(capture.sites_polled),
            "site_errors": dict(capture.site_errors),
            "skipped_sites": dict(capture.skipped_sites),
            "skipped_circuit": skipped_circuit,
            "completed_subscriptions": list(capture.completed),
            "dry_run": capture.dry_run,
        }


def _count_decisions(hits) -> dict[str, int]:
    out: dict[str, int] = {}
    for h in hits:
        key = h.decision.value if hasattr(h.decision, "value") else str(h.decision)
        out[key] = out.get(key, 0) + 1
    return out


def _add(scheduler: AsyncIOScheduler, func, trigger: IntervalTrigger, job_id: str, name: str) -> None:
    scheduler.add_job(
        func, trigger=trigger, id=job_id, name=name,
        max_instances=1,          # 与 runner 内部防重入双重保险
        coalesce=True,            # 错过的多次触发合并为一次
        misfire_grace_time=300,
    )


def build_scheduler(runner: PipelineRunner, config) -> AsyncIOScheduler:
    """构造 APScheduler，注册周期任务。

    getattr 守卫：测试里的 DummyRunner / 简易 config 可能只有部分方法 / 段。
    """
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    search = getattr(config, "search", None)

    # ── completion_poll：下载器轮询（取代旧 pipeline_round 的「每轮尾部轮询」）──
    poll_interval = max(int(config.poll_interval_minutes), MIN_SITE_INTERVAL_MINUTES)
    if poll_interval != config.poll_interval_minutes:
        logger.warning(
            f"poll_interval_minutes={config.poll_interval_minutes} 低于安全下限，"
            f"已提升到 {poll_interval} 分钟"
        )
    poll_fn = getattr(runner, "run_completion_poll_once", None) or runner.run_once
    _add(scheduler, poll_fn, IntervalTrigger(minutes=poll_interval), "completion_poll", "下载完成轮询")
    logger.info(f"完成轮询已配置：每 {poll_interval} 分钟")

    # ── rss_capture：触网站点，受 5 分钟下限约束 ──
    if hasattr(runner, "run_rss_capture_once"):
        rss_interval = max(int(getattr(search, "rss_interval_minutes", 10)), MIN_SITE_INTERVAL_MINUTES)
        _add(
            scheduler, runner.run_rss_capture_once, IntervalTrigger(minutes=rss_interval),
            "rss_capture", "RSS 捕获与派发",
        )
        logger.info(f"RSS 捕获已配置：每 {rss_interval} 分钟")

    # ── subscription_search：单条订阅另受自身回退间隔与站点预算约束，job 下限 1 分钟 ──
    if hasattr(runner, "run_subscription_search_once"):
        scan_interval = max(int(getattr(search, "subscription_scan_minutes", 15)), 1)
        _add(
            scheduler, runner.run_subscription_search_once, IntervalTrigger(minutes=scan_interval),
            "subscription_search", "订阅主动搜索",
        )
        logger.info(f"订阅搜索已配置：每 {scan_interval} 分钟")

    # ── site_auth_check ──
    if hasattr(runner, "run_site_auth_check_once"):
        auth_hours = max(int(getattr(search, "auth_check_hours", 24)), 1)
        _add(
            scheduler, runner.run_site_auth_check_once, IntervalTrigger(hours=auth_hours),
            "site_auth_check", "站点认证巡检",
        )
        logger.info(f"站点巡检已配置：每 {auth_hours} 小时")

    # ── seeding_check：只读 qb、不触网站点，至少 1 分钟 ──
    seeding = getattr(config, "seeding", None)
    if seeding is not None and seeding.enabled and hasattr(runner, "run_seeding_once"):
        seed_interval = max(int(seeding.check_interval_minutes), 1)
        _add(
            scheduler, runner.run_seeding_once, IntervalTrigger(minutes=seed_interval),
            "seeding_check", "做种 H&R 轮询",
        )
        logger.info(f"做种轮询已配置：每 {seed_interval} 分钟检查一次 H&R")

    # ── discover_auto_refresh：后台静默重拉发现墙缓存，0 = 关闭 ──
    # NAS 场景下页面打开应当永远即时；新鲜度由这个后台间隔保证。
    discover = getattr(config, "discover", None)
    auto_min = int(getattr(discover, "auto_refresh_minutes", 0) or 0)
    if auto_min > 0 and hasattr(runner, "run_discover_auto_refresh_once"):
        _add(
            scheduler, runner.run_discover_auto_refresh_once,
            IntervalTrigger(minutes=auto_min), "discover_auto_refresh", "发现墙后台刷新",
        )
        logger.info(f"发现墙后台刷新已配置：每 {auto_min} 分钟")

    # ── license_revalidate：授权码在线复核（取消 Star 自动吊销的执行端）──
    from . import license as _license
    if _license.license_required() and _license.verify_url() \
            and hasattr(runner, "run_license_revalidate_once"):
        _add(
            scheduler, runner.run_license_revalidate_once,
            IntervalTrigger(hours=_license.REVALIDATE_INTERVAL_HOURS),
            "license_revalidate", "授权在线复核",
        )
        logger.info(f"授权复核已配置：每 {_license.REVALIDATE_INTERVAL_HOURS} 小时")

    # ── update_check：每日拉一次 GitHub 最新 release，仅提示不自动升级 ──
    from . import updates as _updates
    if not _updates.opt_out() and hasattr(runner, "run_update_check_once"):
        _add(
            scheduler, runner.run_update_check_once,
            IntervalTrigger(hours=24), "update_check", "新版本检测",
        )
        logger.info("新版本检测已配置：每 24 小时（AUDIO_HUB_NO_UPDATE_CHECK=1 可关）")
    return scheduler
