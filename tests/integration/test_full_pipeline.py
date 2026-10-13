"""全链路装配集成测试：真实 DB + 真实 repo + 真实 PipelineRunner + 真实 dispatcher。

只有适配器是注入的假实现——这样验证的是 scheduler.py / repo.py / monitor.py /
dispatcher.py 的真实装配与持久化，且**不向任何 PT 站点发起网络请求**。

与 test_pipeline_dryrun.py 的区别：
- 那个测试验证 pipeline 三个模块的逻辑正确性（内存态）
- 本测试验证「一轮流水线后数据真的落库了」（持久化 + 幂等 + 重启恢复）

v0.6：生产路径是订阅引擎（subscription.engine），rss_capture /
subscription_search 两个 job 分别以 runner.run_rss_capture_once /
run_subscription_search_once 为入口；runner.run_once() 是「RSS 捕获 + 派发 +
完成轮询」的手动触发入口。dry_run 下引擎把命中建成 scored 任务（不派发），
summary 里「应派发」的计数是 accepted，dispatched 恒为 0。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from audio_hub import repo
from audio_hub.adapters.base import BaseAdapter, MediaCategory, NormalizedTorrent
from audio_hub.completion.monitor import CompletionMonitor, route_completed_task
from audio_hub.config import AppConfig, QbittorrentConfig, SiteConfig
from audio_hub.db import init_db
from audio_hub.models import WatchEntry, WatchKind
from audio_hub.notify.log_notify import LogNotifier
from audio_hub.scheduler import PipelineRunner


class InjectedAdapter(BaseAdapter):
    """注入式假适配器：返回预设候选，不发网络请求。"""

    def __init__(self, name: str, torrents: list[NormalizedTorrent]):
        self.config = None
        self.site_name = name
        self._torrents = torrents
        self.call_count = 0

    async def fetch_rss(self, query=None):
        self.call_count += 1
        return self._torrents

    async def search(self, keyword):
        # 模拟 NexusPHP：search 参数被忽略 → 返回空 → 触发 fallback
        return []

    def parse_title(self, raw):
        return {"category": MediaCategory.MUSIC, "format_tags": [], "discount": None}


def make_torrent(title, category=MediaCategory.MUSIC, site="fakesite",
                 size=500*1024**2, info_hash=None, **kw):
    return NormalizedTorrent(
        info_hash=info_hash or uuid.uuid4().hex,
        title=title, title_normalized=title.lower(), site=site, category=category,
        download_url=f"https://{site}.me/download.php?id=1", size_bytes=size, **kw,
    )


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        dry_run=True,
        db_path=str(tmp_path / "hub.db"),
        log_level="INFO",
        poll_interval_minutes=30,
        qbittorrent=QbittorrentConfig(
            host="localhost", port=8085, api_key="",
            category="audio-hub", tag="hub",
            save_path=str(tmp_path / "seeds"),
        ),
        sites=[SiteConfig(name="fakesite", enabled=True, rss_url="https://x/torrentrss.php")],
    )


@pytest.fixture
async def db(app_config):
    conn = await init_db(app_config.db_path)
    yield conn
    await conn.close()


def build_runner(app_config, db, torrents) -> tuple[PipelineRunner, InjectedAdapter]:
    """构造 runner 并注入假适配器（绕过 registry 的真实构造）。"""
    adapter = InjectedAdapter("fakesite", torrents)
    runner = PipelineRunner(db, app_config, qb_client=None, notifier=LogNotifier())
    runner.adapters = {"fakesite": adapter}
    return runner, adapter


# ── 全链路持久化 ────────────────────────────────────────────

class TestFullPipelinePersistence:
    @pytest.mark.asyncio
    async def test_round_persists_task_and_fingerprint(self, app_config, db):
        """一轮 RSS 捕获后，task 与去重指纹必须真的落库。"""
        entry = WatchEntry(id=str(uuid.uuid4()), query="周杰伦",
                           kind=WatchKind.MUSIC, min_confidence="medium")
        await repo.add_watch_entry(db, entry)

        torrents = [make_torrent("周杰伦 范特西 FLAC")]
        runner, _ = build_runner(app_config, db, torrents)
        summary = await runner.run_rss_capture_once()

        assert summary["candidates"] == 1
        assert summary["accepted"] == 1, "dry_run 下的「应派发」计数"
        assert summary["dispatched"] == 0, "dry_run 不真派发（v0.6 引擎只建 scored 任务）"
        assert summary["dry_run"] is True

        # DB 侧验证：task 已写入
        by_state = await repo.count_tasks_by_state(db)
        assert sum(by_state.values()) == 1
        # dry_run 下状态为 scored（不是 dispatched，因为没真派发）
        assert "scored" in by_state

        # 去重指纹已写入
        known = await repo.get_known_hashes(db)
        assert len(known) == 1

    @pytest.mark.asyncio
    async def test_second_round_dedupes(self, app_config, db):
        """第二轮同一候选不应重复派发（幂等）：订阅引擎按 subscription_hits 去重。"""
        entry = WatchEntry(id=str(uuid.uuid4()), query="周杰伦",
                           kind=WatchKind.MUSIC, min_confidence="medium")
        await repo.add_watch_entry(db, entry)

        t = make_torrent("周杰伦 范特西 FLAC", info_hash="fixed_hash")
        runner, _ = build_runner(app_config, db, [t])

        s1 = await runner.run_rss_capture_once()
        s2 = await runner.run_rss_capture_once()

        assert s1["accepted"] == 1
        assert s2["candidates"] == 1
        assert s2["accepted"] == 0, "第二轮应被 subscription_hits 去重"
        assert len(await repo.list_hits(db, entry.id)) == 1, "已决策过的种子不再入账"

        by_state = await repo.count_tasks_by_state(db)
        assert sum(by_state.values()) == 1, "不应产生重复 task"

    @pytest.mark.asyncio
    async def test_video_never_persisted_as_accepted(self, app_config, db):
        """v0.1 核心验收：视频绝不进入采纳路径，也不落 task。"""
        entry = WatchEntry(id=str(uuid.uuid4()), query="周杰伦",
                           kind=WatchKind.MUSIC, min_confidence="medium")
        await repo.add_watch_entry(db, entry)

        torrents = [
            make_torrent("周杰伦 范特西 FLAC"),
            make_torrent("周杰伦 演唱会 BluRay 1080p", category=MediaCategory.VIDEO),
            make_torrent("周杰伦 演唱会 2024 REMUX", category=MediaCategory.MUSIC),
        ]
        runner, _ = build_runner(app_config, db, torrents)
        summary = await runner.run_rss_capture_once()

        assert summary["accepted"] == 1, "只有真正的音乐专辑被采纳"
        assert summary["rejected"] == 2, "两条视频均被拒绝"
        # 被拒绝的候选不落 task、不进指纹表
        by_state = await repo.count_tasks_by_state(db)
        assert sum(by_state.values()) == 1
        assert len(await repo.get_known_hashes(db)) == 1

    @pytest.mark.asyncio
    async def test_review_items_persisted_without_dispatch(self, app_config, db):
        """待审项落库但不派发（high 门槛下 medium 置信度）。"""
        entry = WatchEntry(id=str(uuid.uuid4()), query="周杰伦",
                           kind=WatchKind.MUSIC, min_confidence="high")
        await repo.add_watch_entry(db, entry)

        # 命中词但无音质标记 → medium → 低于 high 门槛 → 待审
        torrents = [make_torrent("周杰伦 某专辑")]
        runner, _ = build_runner(app_config, db, torrents)
        summary = await runner.run_once()

        assert summary["dispatched"] == 0
        assert summary["review"] == 1
        known = await repo.get_known_hashes(db)
        assert len(known) == 1, "待审项也应记入去重表，避免下轮重复提示"

    @pytest.mark.asyncio
    async def test_search_fallback_recorded(self, app_config, db):
        """RSS 兜底（v0.6 的降级形态）：search 空返回的站，由独立的 rss_capture 兜住。

        v0.6 把旧「search 失败 → 轮内 fallback 到 RSS」拆成两个 job：
        subscription_search 只做主动搜索，rss_capture 持续拉 RSS——NexusPHP
        search 参数被忽略（返回空）的站点，订阅命中由 RSS 捕获保证。
        """
        entry = WatchEntry(id=str(uuid.uuid4()), query="周杰伦",
                           kind=WatchKind.MUSIC, min_confidence="medium")
        await repo.add_watch_entry(db, entry)

        torrents = [make_torrent("周杰伦 范特西 FLAC")]
        runner, adapter = build_runner(app_config, db, torrents)

        # 主动搜索：适配器 search 返回空（模拟 NexusPHP 忽略参数）→ 无命中
        search = await runner.run_subscription_search_once()
        assert search["due"] == 1 and search["searched"] == 1
        assert search["hits"] == 0

        # RSS 捕获兜住同一候选
        capture = await runner.run_rss_capture_once()
        assert adapter.call_count == 1, "fetch_rss 应被调用一次"
        assert capture["sites_polled"] == ["fakesite"]
        assert capture["accepted"] == 1
        assert len(await repo.get_known_hashes(db)) == 1

    @pytest.mark.asyncio
    async def test_empty_watchlist_noop(self, app_config, db):
        runner, adapter = build_runner(app_config, db, [make_torrent("x FLAC")])
        summary = await runner.run_once()
        assert summary.get("watchlist_empty") is True
        assert adapter.call_count == 0, "无订阅项时不应发起请求"

    @pytest.mark.asyncio
    async def test_reentrancy_guard(self, app_config, db, monkeypatch):
        """防重入：运行中再次调用应跳过。"""
        entry = WatchEntry(id=str(uuid.uuid4()), query="周杰伦",
                           kind=WatchKind.MUSIC, min_confidence="medium")
        await repo.add_watch_entry(db, entry)
        runner, _ = build_runner(app_config, db, [make_torrent("周杰伦 FLAC")])

        runner._running = True          # 模拟上一轮未结束
        summary = await runner.run_once()
        assert summary.get("skipped") is True


# ── 实跑派发：直链换取 ────────────────────────────────────────

class TokenAdapter(InjectedAdapter):
    """模拟馒头：download_url 是占位 URL，派发前需换取直链。"""

    def __init__(self, name, torrents, fail=False):
        super().__init__(name, torrents)
        self.fail = fail
        self.resolved: list[str] = []

    async def resolve_download_url(self, torrent_or_url):
        from audio_hub.adapters.base import AdapterError
        url = getattr(torrent_or_url, "download_url", torrent_or_url)
        if self.fail:
            raise AdapterError("genDlToken 业务错误: 1")
        self.resolved.append(url)
        return url.replace("/genDlToken?id=", "/rss/dl?credential=tok-")


class RecordingQb:
    def __init__(self):
        self.calls: list[dict] = []

    def torrents_add(self, **kw):
        self.calls.append(kw)

    def torrents_info(self, **kw):
        return []


class TestRealDispatchResolvesDownloadUrl:
    @pytest.mark.asyncio
    async def test_runner_dispatches_direct_link_to_qb(self, app_config, db):
        """实跑：runner 注入的适配器在派发前换取直链，qb 收到直链，task 落库为 dispatched。"""
        cfg = app_config.model_copy(update={"dry_run": False})
        entry = WatchEntry(id=str(uuid.uuid4()), query="周杰伦",
                           kind=WatchKind.MUSIC, min_confidence="medium")
        await repo.add_watch_entry(db, entry)

        t = make_torrent("周杰伦 范特西 FLAC", site="mteam")
        t = t.model_copy(update={
            "download_url": "https://api.m-team.cc/api/torrent/genDlToken?id=12345",
            "torrent_id": "12345",
        })
        adapter = TokenAdapter("mteam", [t])
        qb = RecordingQb()
        runner = PipelineRunner(db, cfg, qb_client=qb, notifier=LogNotifier())
        runner.adapters = {"mteam": adapter}      # 构造后替换，验证 lookup 是活查找

        summary = await runner.run_once()

        assert summary["dispatched"] == 1
        assert summary["dispatch_failed"] == 0
        assert adapter.resolved == [t.download_url], "应恰调用一次 resolve_download_url"
        assert len(qb.calls) == 1
        assert qb.calls[0]["urls"] == "https://api.m-team.cc/api/torrent/rss/dl?credential=tok-12345"
        assert "genDlToken" not in qb.calls[0]["urls"]

        by_state = await repo.count_tasks_by_state(db)
        assert by_state.get("dispatched") == 1

    @pytest.mark.asyncio
    async def test_resolve_failure_retries_next_round(self, app_config, db):
        """换取直链失败 → 不碰 qb，订阅保持活跃，下一轮重试。

        v0.6.0：一次性订阅若因派发失败留在 failed 态就永远完不成
        （hit 已记录、任务已 failed，引擎不会再推它），所以派发失败
        必须回滚——删 hit、任务退回 discovered，下轮引擎重新决策。
        """
        cfg = app_config.model_copy(update={"dry_run": False})
        entry = WatchEntry(id=str(uuid.uuid4()), query="周杰伦",
                           kind=WatchKind.MUSIC, min_confidence="medium")
        await repo.add_watch_entry(db, entry)

        t = make_torrent("周杰伦 范特西 FLAC", site="mteam")
        adapter = TokenAdapter("mteam", [t], fail=True)
        qb = RecordingQb()
        runner = PipelineRunner(db, cfg, qb_client=qb, notifier=LogNotifier())
        runner.adapters = {"mteam": adapter}

        summary = await runner.run_once()

        assert summary["dispatched"] == 0
        assert summary["dispatch_failed"] == 1
        assert qb.calls == []
        async with db.execute("SELECT id, state, error_message FROM tasks") as cur:
            row = await cur.fetchone()
        assert row["state"] == "discovered", "失败应回滚回 discovered 以便下轮重试"
        assert row["error_message"] is None
        async with db.execute(
            "SELECT COUNT(*) AS n FROM subscription_hits WHERE task_id = ?",
            (row["id"],),
        ) as cur:
            assert (await cur.fetchone())["n"] == 0, "hit 未删，下轮会被 has_hit 挡住"
        watch = await repo.get_watch_entry(db, entry.id)
        assert watch.status.value == "active", "订阅不应被标完成"

        # 下一轮：适配器恢复，任务被重新派发成功
        adapter.fail = False
        summary2 = await runner.run_once()
        assert summary2["dispatched"] == 1
        assert summary2["dispatch_failed"] == 0
        assert len(qb.calls) == 1
        async with db.execute("SELECT state FROM tasks WHERE id = ?", (row["id"],)) as cur:
            assert (await cur.fetchone())["state"] == "dispatched"

    @pytest.mark.asyncio
    async def test_dry_run_never_resolves(self, app_config, db):
        """dry-run 不应调用 resolve_download_url（不发 genDlToken 请求），也不真派发。"""
        entry = WatchEntry(id=str(uuid.uuid4()), query="周杰伦",
                           kind=WatchKind.MUSIC, min_confidence="medium")
        await repo.add_watch_entry(db, entry)

        adapter = TokenAdapter("mteam", [make_torrent("周杰伦 范特西 FLAC", site="mteam")])
        runner = PipelineRunner(db, app_config, qb_client=None, notifier=LogNotifier())
        runner.adapters = {"mteam": adapter}

        summary = await runner.run_once()

        assert summary["accepted"] == 1, "dry_run 下决策为「应派发」"
        assert summary["dispatched"] == 0, "dry_run 不进入派发链路"
        assert adapter.resolved == []


# ── 崩溃恢复 ────────────────────────────────────────────────

class TestCrashRecovery:
    @pytest.mark.asyncio
    async def test_reconcile_in_dry_run_does_not_crash(self, app_config, db):
        """dry-run 下对账应安全跳过 qb 查询。"""
        monitor = CompletionMonitor(db, None, app_config, dry_run=True)
        stats = await monitor.reconcile_on_startup()
        assert stats["recovered"] == 0
        assert stats["orphaned"] == 0

    @pytest.mark.asyncio
    async def test_reconcile_cleans_upgrading_tmp(self, app_config, db, tmp_path):
        """归档中途崩溃留下的 .upgrading 应被清理（PRD §13.6）——扫的是成品库。"""
        music = tmp_path / "library" / "music"
        books = tmp_path / "library" / "audiobook"
        music.mkdir(parents=True)
        (books / "deep" / "nested").mkdir(parents=True)
        left_music = music / "album.flac.upgrading"
        left_book = books / "deep" / "nested" / "ch01.m4b.upgrading"
        left_music.write_bytes(b"partial")
        left_book.write_bytes(b"partial")
        keep = music / "album.flac"
        keep.write_bytes(b"fLaC")

        cfg = app_config.model_copy(deep=True)
        cfg.paths.library_music = str(music)
        cfg.paths.library_audiobook = str(books)

        monitor = CompletionMonitor(db, None, cfg, dry_run=True)
        stats = await monitor.reconcile_on_startup()
        assert stats["cleaned_tmp"] == 2
        assert not left_music.exists() and not left_book.exists()
        assert keep.exists()

    @pytest.mark.asyncio
    async def test_reconcile_does_not_touch_qb_save_path(self, app_config, db, tmp_path):
        """原种目录只读：即便里面有 .upgrading 也不在清理范围内。"""
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        leftover = seeds / "album.flac.upgrading"
        leftover.write_bytes(b"partial")

        cfg = app_config.model_copy(deep=True)
        cfg.qbittorrent.save_path = str(seeds)
        cfg.paths.library_music = str(tmp_path / "library" / "music")
        cfg.paths.library_audiobook = str(tmp_path / "library" / "audiobook")

        monitor = CompletionMonitor(db, None, cfg, dry_run=True)
        assert Path(seeds) not in monitor._library_roots()
        stats = await monitor.reconcile_on_startup()
        assert stats["cleaned_tmp"] == 0
        assert leftover.exists()

    @pytest.mark.asyncio
    async def test_library_roots_dedupe_and_skip_empty(self, app_config, db, tmp_path):
        """两个库指向同一目录只扫一次；空路径不进入清单。"""
        cfg = app_config.model_copy(deep=True)
        cfg.paths.library_music = str(tmp_path / "lib")
        cfg.paths.library_audiobook = str(tmp_path / "lib")
        monitor = CompletionMonitor(db, None, cfg, dry_run=True)
        assert monitor._library_roots() == [tmp_path / "lib"]

        cfg.paths.library_audiobook = ""
        assert monitor._library_roots() == [tmp_path / "lib"]

    @pytest.mark.asyncio
    async def test_cleanup_skips_missing_library_roots(self, app_config, db, tmp_path):
        """库目录尚未创建（首次启动 / 挂载未就）时安全跳过，不报错。"""
        cfg = app_config.model_copy(deep=True)
        cfg.paths.library_music = str(tmp_path / "nope" / "music")
        cfg.paths.library_audiobook = str(tmp_path / "nope" / "books")
        monitor = CompletionMonitor(db, None, cfg, dry_run=True)
        assert monitor._cleanup_upgrading_tmp() == 0
        stats = await monitor.reconcile_on_startup()
        assert stats["cleaned_tmp"] == 0

    @pytest.mark.asyncio
    async def test_orphan_task_marked_failed(self, app_config, db):
        """qb 中不存在的 in-flight 任务应标记 failed（非 dry-run 路径）。"""
        from audio_hub.models import Task, TaskState

        task = Task(id=str(uuid.uuid4()), watch_entry_id=None, info_hash="ghost",
                    site="fakesite", title="已被人工删除的种子", state=TaskState.DISPATCHED)
        await repo.create_task(db, task)

        class FakeQb:
            def torrents_info(self, tag=None):
                return []      # qb 里没有这个种子

        cfg = app_config.model_copy(update={"dry_run": False})
        monitor = CompletionMonitor(db, FakeQb(), cfg, dry_run=False)
        stats = await monitor.reconcile_on_startup()

        assert stats["orphaned"] == 1
        async with db.execute("SELECT state, error_message FROM tasks WHERE id=?",
                              (task.id,)) as cur:
            row = await cur.fetchone()
        assert row["state"] == "failed"
        assert "人工删除" in row["error_message"]


# ── 归档（v0.1 简化版）──────────────────────────────────────

class TestArchiving:
    @pytest.mark.asyncio
    async def test_route_copies_not_moves(self, tmp_path):
        """必须用 cp 而非 mv：移动原种会破坏 PT 做种哈希（PRD §8.2 红线）。"""
        src_dir = tmp_path / "seeds" / "Album"
        src_dir.mkdir(parents=True)
        (src_dir / "01.flac").write_bytes(b"audio")

        inbox = tmp_path / "scratch" / "00_inbox"
        result = await route_completed_task(None, str(src_dir), inbox)

        assert result is not None
        assert result.exists()
        # 源文件必须仍然存在（做种不能断）
        assert (src_dir / "01.flac").exists(), "归档不得移动原种文件"

    @pytest.mark.asyncio
    async def test_route_idempotent(self, tmp_path):
        src_dir = tmp_path / "seeds" / "Album"
        src_dir.mkdir(parents=True)
        (src_dir / "01.flac").write_bytes(b"audio")
        inbox = tmp_path / "scratch" / "00_inbox"

        r1 = await route_completed_task(None, str(src_dir), inbox)
        r2 = await route_completed_task(None, str(src_dir), inbox)
        assert r1 == r2, "重复归档应幂等返回同一路径"

    @pytest.mark.asyncio
    async def test_route_missing_source_returns_none(self, tmp_path):
        result = await route_completed_task(
            None, str(tmp_path / "nonexistent"), tmp_path / "inbox"
        )
        assert result is None


# ── 调度器配置 ──────────────────────────────────────────────

class TestSchedulerConfig:
    def test_poll_interval_floor(self, app_config):
        """触网站点的轮询间隔低于安全下限时应被提升（PRD §8.5 账号安全）。

        v0.6：poll_interval_minutes 驱动 completion_poll；rss_capture 有独立的
        5 分钟下限（MIN_SITE_INTERVAL_MINUTES）。
        """
        from audio_hub.scheduler import build_scheduler

        cfg = app_config.model_copy(update={"poll_interval_minutes": 1})
        cfg.search.rss_interval_minutes = 1
        runner = PipelineRunner(None, cfg, qb_client=None)
        sched = build_scheduler(runner, cfg)
        try:
            job = sched.get_job("completion_poll")
            assert job is not None
            # 1 分钟应被提升到 5 分钟下限
            assert job.trigger.interval.total_seconds() >= 300
            # 触网站点的 RSS 捕获同样受 5 分钟下限约束
            rss_job = sched.get_job("rss_capture")
            assert rss_job is not None
            assert rss_job.trigger.interval.total_seconds() >= 300
        finally:
            # build_scheduler 只配置未 start，shutdown 会抛 SchedulerNotRunningError
            sched.remove_all_jobs()

    def test_max_instances_one(self, app_config):
        """全部 job max_instances=1 + coalesce，与 runner 内部防重入双重保险。"""
        from audio_hub.scheduler import build_scheduler
        runner = PipelineRunner(None, app_config, qb_client=None)
        sched = build_scheduler(runner, app_config)
        try:
            # v0.6 job 清单：旧 pipeline_round 已拆成 capture / search / 完成轮询等
            assert sched.get_job("pipeline_round") is None
            for job_id in ("rss_capture", "subscription_search", "completion_poll",
                           "site_auth_check", "seeding_check"):
                job = sched.get_job(job_id)
                assert job is not None, f"缺少 job {job_id}"
                assert job.max_instances == 1
                assert job.coalesce is True
        finally:
            sched.remove_all_jobs()

    @pytest.mark.asyncio
    async def test_scheduler_starts_and_shuts_down(self, app_config):
        """调度器能在运行中的事件循环里启动与关闭（验证 lifespan 路径可用）。

        AsyncIOScheduler.start() 要求已存在运行中的 loop，故本用例必须是 async。
        DummyRunner 只有 run_once：completion_poll 回落到它，触网 job 不注册。
        """
        from audio_hub.scheduler import build_scheduler

        cfg = app_config.model_copy(update={"poll_interval_minutes": 5})

        class DummyRunner:
            def __init__(self):
                self.adapters = {}
                self.calls = 0

            async def run_once(self):
                self.calls += 1
                return {}

        runner = DummyRunner()
        sched = build_scheduler(runner, cfg)
        try:
            sched.start()
            assert sched.running is True
            assert sched.get_job("completion_poll") is not None
        finally:
            if sched.running:
                sched.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_pipeline_runner_constructs_adapters_from_config(self, app_config, db):
        """PipelineRunner 应能从配置构造适配器（未注册站点被跳过而非崩溃）。"""
        runner = PipelineRunner(db, app_config, qb_client=None)
        # app_config 里的 fakesite 未注册，应被 registry 跳过
        assert runner.adapters == {}


# ── 连载追踪接入（v0.4 模块 C × scheduler）──────────────────────────────────

class TestSeriesTrackingIntegration:
    """订阅项绑定连载后，一轮 RSS 捕获应打通：分卷级去重（album_key 含 volN）→ series_parts 登记。"""

    async def _bind_series(self, db, *, total_parts=None) -> tuple[WatchEntry, int]:
        from audio_hub.models import SeriesEntry

        entry = WatchEntry(id=str(uuid.uuid4()), query="三体",
                           kind=WatchKind.AUDIOBOOK, min_confidence="low")
        await repo.add_watch_entry(db, entry)
        series_id = await repo.add_series(db, SeriesEntry(
            watch_id=entry.id, name="三体有声书", total_parts=total_parts,
        ))
        await repo.set_watch_series(db, entry.id, series_id)
        entry.series_id = series_id
        return entry, series_id

    @pytest.mark.asyncio
    async def test_dry_run_round_registers_parts_and_rejects_duplicate(self, app_config, db):
        """一轮 dry-run 捕获：两个不同分集落入 series_parts，同轮重复分集被组内择优拒掉。"""
        _, series_id = await self._bind_series(db)

        torrents = [
            make_torrent("三体 第1集 有声书 M4B", category=MediaCategory.AUDIOBOOK),
            make_torrent("三体 第2集 有声书 M4B", category=MediaCategory.AUDIOBOOK),
            # 另一份第 1 集（不同 info_hash，模拟多站点同时放出）→ 同一 album_key（三体|vol1）
            # 组内择优后按 SKIPPED_DUP 入账（v0.6：重复版本不再记 rejected）
            make_torrent("三体 第1集 有声书 FLAC", category=MediaCategory.AUDIOBOOK),
        ]
        runner, _ = build_runner(app_config, db, torrents)
        summary = await runner.run_rss_capture_once()

        assert summary["candidates"] == 3
        assert summary["accepted"] == 2
        assert summary["skipped_dup"] == 1

        parts = await repo.list_series_parts(db, series_id)
        assert [p.part_no for p in parts] == [1, 2]
        # 登记的 task_id 必须指向真实落库的 task（create_task 返回的 id 已回填）
        for p in parts:
            assert p.task_id
            assert await repo.get_task(db, p.task_id) is not None

        series = await repo.get_series(db, series_id)
        assert series.last_part == 2
        assert series.status == "active"      # total_parts 未知，不完结

    @pytest.mark.asyncio
    async def test_second_round_rejects_already_registered_part(self, app_config, db):
        """第二轮出现已登记分集的新副本（新 info_hash），应被「已拥有该分卷」拒掉。"""
        _, series_id = await self._bind_series(db)

        runner, adapter = build_runner(app_config, db, [
            make_torrent("三体 第1集 有声书 M4B", category=MediaCategory.AUDIOBOOK),
        ])
        s1 = await runner.run_rss_capture_once()
        assert s1["accepted"] == 1

        adapter._torrents = [
            make_torrent("三体 第1集 有声书 FLAC", category=MediaCategory.AUDIOBOOK),
        ]
        s2 = await runner.run_rss_capture_once()
        assert s2["accepted"] == 0
        # v0.6：album_key 含分卷号（三体|vol1），已拥有 → SKIPPED_DUP(already_owned)
        assert s2["skipped_dup"] == 1

        parts = await repo.list_series_parts(db, series_id)
        assert [p.part_no for p in parts] == [1]

    @pytest.mark.asyncio
    async def test_completing_series_emits_event(self, app_config, db):
        """total_parts 已知且集齐 → series 置 completed 并发 series_completed 事件。"""
        from audio_hub.notify.base import BaseNotifier

        class CollectNotifier(BaseNotifier):
            def __init__(self):
                self.events = []

            async def emit(self, event):
                self.events.append(event)

        _, series_id = await self._bind_series(db, total_parts=2)

        adapter = InjectedAdapter("fakesite", [
            make_torrent("三体 第1集 有声书 M4B", category=MediaCategory.AUDIOBOOK),
            make_torrent("三体 第2集 有声书 M4B", category=MediaCategory.AUDIOBOOK),
        ])
        notifier = CollectNotifier()
        runner = PipelineRunner(db, app_config, qb_client=None, notifier=notifier)
        runner.adapters = {"fakesite": adapter}
        summary = await runner.run_rss_capture_once()
        assert summary["accepted"] == 2

        series = await repo.get_series(db, series_id)
        assert series.status == "completed"
        kinds = [e.kind for e in notifier.events]
        assert kinds.count("series_completed") == 1


# ── 站点熔断：site_stats 写入 + 熔断跳过 + 事件落库 ────────────────────────

class BrokenAdapter(BaseAdapter):
    """search 与 fetch_rss 都抛未预期异常：模拟站点改版 / 宕机。计数调用次数。"""

    def __init__(self, name: str):
        self.config = None
        self.site_name = name
        self.calls = 0

    async def fetch_rss(self, query=None):
        self.calls += 1
        raise RuntimeError("rss down")

    async def search(self, keyword):
        self.calls += 1
        raise RuntimeError("search down")

    def parse_title(self, raw):
        return {"category": MediaCategory.MUSIC, "format_tags": [], "discount": None}


class TestSiteCircuitBreaker:
    @pytest.mark.asyncio
    async def test_three_failed_rounds_open_circuit_and_skip_fourth(self, app_config, db):
        """一站连招 3 轮异常 → 第 4 轮被跳过（适配器不被调用），events 表有一条 site_circuit_open。

        健康站全程不受影响：熔断是按站隔离的。
        """
        from audio_hub.notify.db_notify import DbNotifier

        await repo.add_watch_entry(db, WatchEntry(
            id=str(uuid.uuid4()), query="周杰伦", kind=WatchKind.MUSIC, min_confidence="medium",
        ))
        broken = BrokenAdapter("brokensite")
        healthy = InjectedAdapter("fakesite", [make_torrent("周杰伦 范特西 FLAC")])
        runner = PipelineRunner(db, app_config, qb_client=None, notifier=DbNotifier(db))
        runner.adapters = {"brokensite": broken, "fakesite": healthy}

        for i in range(3):
            summary = await runner.run_rss_capture_once()
            assert "brokensite" in summary["site_errors"], f"第 {i + 1} 轮应记录站点错误"
            assert summary["skipped_circuit"] == [], f"第 {i + 1} 轮不应已熔断"
        # 未预期异常不重试：每轮只碰适配器一次（v0.6 无 search→RSS 轮内降级）
        assert broken.calls == 3

        st = await repo.get_site_stats(db, "brokensite")
        assert st["consecutive_fail"] == 3
        assert st["total_requests"] == 3
        assert st["total_errors"] == 3
        assert "RuntimeError" in st["last_error"]
        assert st["circuit_open_until"] is not None
        assert await repo.is_circuit_open(db, "brokensite")

        # 健康站也在计数，且没有被连带
        ok = await repo.get_site_stats(db, "fakesite")
        assert ok["total_requests"] == 3
        assert ok["consecutive_fail"] == 0
        assert ok["circuit_open_until"] is None

        # 第 4 轮：还在熔断期，适配器不被碰，也不再计入请求数
        s4 = await runner.run_rss_capture_once()
        assert s4["skipped_circuit"] == ["brokensite"]
        assert "brokensite" not in s4["site_errors"]
        assert broken.calls == 3
        assert (await repo.get_site_stats(db, "brokensite"))["total_requests"] == 3
        assert (await repo.get_site_stats(db, "fakesite"))["total_requests"] == 4

        # 事件：恰好一条 site_circuit_open，warning 级，detail 带 until
        events = await repo.list_events(db, kind="site_circuit_open")
        assert len(events) == 1
        ev = events[0]
        assert ev.site == "brokensite"
        assert ev.level == "warning"
        import json
        detail = json.loads(ev.detail)
        assert detail["until"] == st["circuit_open_until"]
        assert detail["consecutive_fail"] == 3

    @pytest.mark.asyncio
    async def test_threshold_read_from_config(self, app_config, db):
        """config.search.circuit_fail_threshold=1 → 第一轮失败就熔断。"""
        app_config.search.circuit_fail_threshold = 1
        app_config.search.circuit_open_minutes = 5
        await repo.add_watch_entry(db, WatchEntry(
            id=str(uuid.uuid4()), query="x", kind=WatchKind.MUSIC,
        ))
        broken = BrokenAdapter("brokensite")
        runner = PipelineRunner(db, app_config, qb_client=None, notifier=LogNotifier())
        runner.adapters = {"brokensite": broken}

        await runner.run_rss_capture_once()
        assert await repo.is_circuit_open(db, "brokensite")
        s2 = await runner.run_rss_capture_once()
        assert s2["skipped_circuit"] == ["brokensite"]
        assert broken.calls == 1
