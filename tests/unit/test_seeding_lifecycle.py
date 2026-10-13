"""做种生命周期单测：用假 qb 客户端覆盖建档、达标、pause、removed、站点阈值、qb 异常。

不触网、不碰真实 qb；通知器用内存收集器断言 hr_satisfied 事件。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from audio_hub import repo
from audio_hub.config import AppConfig, SeedingConfig, SiteConfig
from audio_hub.models import SeedRecord, Task, TaskState
from audio_hub.seeding.lifecycle import (
    QbUnavailable,
    SeedingMonitor,
    SeedingSummary,
    is_hr_satisfied,
    progress_pct,
)
from audio_hub.timeutil import utcnow

HASH_A = "a" * 40
HASH_B = "b" * 40


class FakeQb:
    """只实现本模块会用到的三个方法；记录调用以便断言。"""

    def __init__(self, torrents=None, *, fail=False):
        self.torrents = list(torrents or [])
        self.fail = fail
        self.paused: list = []
        self.resumed: list = []

    def torrents_info(self, tag=None, **_):
        if self.fail:
            raise ConnectionError("qb down")
        return list(self.torrents)

    def torrents_pause(self, torrent_hashes=None, **_):
        if self.fail:
            raise ConnectionError("qb down")
        self.paused.append(torrent_hashes)

    def torrents_resume(self, torrent_hashes=None, **_):
        if self.fail:
            raise ConnectionError("qb down")
        self.resumed.append(torrent_hashes)

    def __getattr__(self, name):
        # 任何删除类接口都不该被调用；若被调用，直接让测试炸掉
        if "delete" in name:
            raise AssertionError(f"禁止调用 {name}")
        raise AttributeError(name)


class CollectNotifier:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


def _torrent(h, *, seeding_time=0, ratio=0.0, state="uploading", name="x"):
    return SimpleNamespace(hash=h, seeding_time=seeding_time, ratio=ratio, state=state, name=name)


def _config(**seeding) -> AppConfig:
    return AppConfig(
        dry_run=False,
        sites=[
            SiteConfig(name="hhanclub", enabled=False),
            SiteConfig(name="mteam", enabled=False, hr_min_seed_hours=10, hr_min_ratio=0.5),
        ],
        seeding=SeedingConfig(
            default_min_seed_hours=72, default_min_ratio=1.0, **seeding,
        ),
    )


async def _task(db, info_hash, site="hhanclub", title="Album X") -> str:
    return await repo.create_task(db, Task(
        info_hash=info_hash, site=site, title=title, state=TaskState.COMPLETED,
    ))


def _monitor(db, qb, config=None, notifier=None) -> SeedingMonitor:
    return SeedingMonitor(
        db, config or _config(), notifier=notifier, qb_client_factory=lambda: qb,
    )


# ── 纯函数 ──────────────────────────────────────────────────

class TestPureHelpers:
    def test_is_hr_satisfied_either_dimension(self):
        assert is_hr_satisfied(72 * 3600, 0.1, 72 * 3600, 1.0)
        assert is_hr_satisfied(10, 1.0, 72 * 3600, 1.0)
        assert not is_hr_satisfied(10, 0.5, 72 * 3600, 1.0)
        # 某维度未设要求则该维度不参与判定
        assert not is_hr_satisfied(10 ** 9, 0.0, None, 1.0)
        assert not is_hr_satisfied(0, 0.0, None, None)

    def test_progress_pct_takes_max_and_clamps(self):
        rec = SeedRecord(task_id="t", info_hash="h", site="s",
                         seed_seconds=36 * 3600, ratio=0.25,
                         required_seconds=72 * 3600, required_ratio=1.0)
        assert progress_pct(rec) == 50
        rec.ratio = 3.0
        assert progress_pct(rec) == 100
        rec.required_seconds = None
        rec.required_ratio = None
        assert progress_pct(rec) == 0

    def test_summary_as_dict(self):
        d = SeedingSummary(checked=2, newly_satisfied=1).as_dict()
        assert d["checked"] == 2 and d["newly_satisfied"] == 1 and d["skipped"] is None


# ── 轮询 ────────────────────────────────────────────────────

class TestPollOnce:
    async def test_new_torrent_creates_record_with_defaults(self, test_db):
        tid = await _task(test_db, HASH_A)
        qb = FakeQb([_torrent(HASH_A, seeding_time=3600, ratio=0.2)])
        summary = await _monitor(test_db, qb).poll_once()

        assert summary.checked == 1 and summary.created == 1 and summary.newly_satisfied == 0
        rec = await repo.get_seed_record(test_db, tid)
        assert rec is not None
        assert rec.status == "seeding"
        assert rec.seed_seconds == 3600 and rec.ratio == pytest.approx(0.2)
        assert rec.required_seconds == 72 * 3600 and rec.required_ratio == 1.0
        assert rec.hr_satisfied_at is None

    async def test_site_override_thresholds(self, test_db):
        tid = await _task(test_db, HASH_B, site="mteam")
        qb = FakeQb([_torrent(HASH_B)])
        await _monitor(test_db, qb).poll_once()
        rec = await repo.get_seed_record(test_db, tid)
        assert rec.required_seconds == 10 * 3600 and rec.required_ratio == 0.5

    async def test_torrent_without_task_is_skipped(self, test_db):
        qb = FakeQb([_torrent("c" * 40, seeding_time=10 ** 6)])
        summary = await _monitor(test_db, qb).poll_once()
        assert summary.checked == 0 and summary.created == 0
        assert await repo.count_seed_records(test_db) == 0

    async def test_satisfied_by_seed_time_emits_and_keeps(self, test_db):
        tid = await _task(test_db, HASH_A, title="Long Seeded")
        qb = FakeQb([_torrent(HASH_A, seeding_time=72 * 3600, ratio=0.1)])
        notifier = CollectNotifier()
        summary = await _monitor(test_db, qb, notifier=notifier).poll_once()

        assert summary.newly_satisfied == 1 and summary.paused == 0
        rec = await repo.get_seed_record(test_db, tid)
        assert rec.status == "satisfied" and rec.hr_satisfied_at is not None
        assert qb.paused == []                       # on_satisfied 默认 keep
        kinds = [e.kind for e in notifier.events]
        assert kinds == ["hr_satisfied"]
        ev = notifier.events[0]
        assert ev.site == "hhanclub" and ev.title == "Long Seeded"
        assert ev.detail["task_id"] == tid and ev.detail["action"] == "keep"

    async def test_satisfied_by_ratio(self, test_db):
        tid = await _task(test_db, HASH_A)
        qb = FakeQb([_torrent(HASH_A, seeding_time=60, ratio=1.05)])
        summary = await _monitor(test_db, qb).poll_once()
        assert summary.newly_satisfied == 1
        assert (await repo.get_seed_record(test_db, tid)).status == "satisfied"

    async def test_on_satisfied_pause_calls_qb_pause(self, test_db):
        await _task(test_db, HASH_A)
        qb = FakeQb([_torrent(HASH_A, ratio=2.0)])
        cfg = _config(on_satisfied="pause")
        summary = await _monitor(test_db, qb, config=cfg).poll_once()
        assert summary.paused == 1
        assert qb.paused == [HASH_A]

    async def test_dry_run_polls_but_skips_pause(self, test_db):
        tid = await _task(test_db, HASH_A)
        qb = FakeQb([_torrent(HASH_A, ratio=2.0)])
        cfg = _config(on_satisfied="pause").model_copy(update={"dry_run": True})
        summary = await _monitor(test_db, qb, config=cfg).poll_once()
        assert summary.checked == 1 and summary.newly_satisfied == 1
        assert summary.paused == 0 and qb.paused == []
        assert (await repo.get_seed_record(test_db, tid)).status == "satisfied"

    async def test_pause_failure_after_satisfied_is_swallowed(self, test_db):
        """达标后 qb pause 抛错：记录仍标 satisfied，paused 不计数，轮询不炸。"""
        tid = await _task(test_db, HASH_A)

        class PauseBroken(FakeQb):
            def torrents_pause(self, torrent_hashes=None, **_):
                raise ConnectionError("pause down")

        qb = PauseBroken([_torrent(HASH_A, ratio=2.0)])
        cfg = _config(on_satisfied="pause")
        summary = await _monitor(test_db, qb, config=cfg).poll_once()
        assert summary.newly_satisfied == 1 and summary.paused == 0
        assert summary.skipped is None
        rec = await repo.get_seed_record(test_db, tid)
        assert rec.status == "satisfied" and rec.hr_satisfied_at is not None

    async def test_satisfied_emitted_only_once(self, test_db):
        await _task(test_db, HASH_A)
        qb = FakeQb([_torrent(HASH_A, ratio=2.0)])
        notifier = CollectNotifier()
        mon = _monitor(test_db, qb, notifier=notifier)
        await mon.poll_once()
        qb.torrents[0].ratio = 2.5
        s2 = await mon.poll_once()
        assert s2.newly_satisfied == 0
        assert len(notifier.events) == 1

    async def test_missing_in_qb_marks_removed(self, test_db):
        tid = await _task(test_db, HASH_A)
        qb = FakeQb([_torrent(HASH_A, seeding_time=100)])
        mon = _monitor(test_db, qb)
        await mon.poll_once()

        qb.torrents.clear()
        summary = await mon.poll_once()
        assert summary.gone == 1 and summary.checked == 0
        assert (await repo.get_seed_record(test_db, tid)).status == "removed"

        # 重新出现 → 恢复跟踪
        qb.torrents.append(_torrent(HASH_A, seeding_time=200))
        await mon.poll_once()
        rec = await repo.get_seed_record(test_db, tid)
        assert rec.status == "seeding" and rec.seed_seconds == 200

    async def test_manual_paused_keeps_status_until_satisfied(self, test_db):
        tid = await _task(test_db, HASH_A)
        qb = FakeQb([_torrent(HASH_A, seeding_time=100, ratio=0.1)])
        mon = _monitor(test_db, qb)
        await mon.poll_once()
        await repo.update_seed_record(test_db, tid, status="paused")

        qb.torrents[0].seeding_time = 500
        await mon.poll_once()
        rec = await repo.get_seed_record(test_db, tid)
        assert rec.status == "paused" and rec.seed_seconds == 500   # 数值更新，状态不变

        qb.torrents[0].ratio = 1.2
        await mon.poll_once()
        assert (await repo.get_seed_record(test_db, tid)).status == "satisfied"

    async def test_qb_failure_returns_empty_summary_without_raising(self, test_db):
        tid = await _task(test_db, HASH_A)
        await repo.upsert_seed_record(test_db, SeedRecord(
            task_id=tid, info_hash=HASH_A, site="hhanclub", seed_seconds=5,
        ))
        qb = FakeQb(fail=True)
        summary = await _monitor(test_db, qb).poll_once()
        assert summary.checked == 0 and summary.gone == 0
        assert summary.skipped
        # 不可达时绝不能把现有记录误判为 removed
        assert (await repo.get_seed_record(test_db, tid)).status == "seeding"

    async def test_no_client_configured_is_skipped(self, test_db):
        cfg = _config()                      # qbittorrent.api_key 为空 → 自建客户端返回 None
        mon = SeedingMonitor(test_db, cfg, qb_client_factory=lambda: None)
        summary = await mon.poll_once()
        assert summary.skipped and summary.checked == 0

    async def test_uppercase_task_hash_still_matches(self, test_db):
        tid = await _task(test_db, HASH_A.upper())
        qb = FakeQb([_torrent(HASH_A, seeding_time=10)])
        await _monitor(test_db, qb).poll_once()
        assert await repo.get_seed_record(test_db, tid) is not None


# ── 手动动作 ─────────────────────────────────────────────────

class TestManualActions:
    async def _seeded(self, db, qb, **kw):
        tid = await _task(db, HASH_A, title="Manual")
        await repo.upsert_seed_record(db, SeedRecord(
            task_id=tid, info_hash=HASH_A, site="hhanclub",
            required_seconds=72 * 3600, required_ratio=1.0, **kw,
        ))
        return tid

    async def test_pause_and_resume(self, test_db):
        qb = FakeQb()
        mon = _monitor(test_db, qb)
        tid = await self._seeded(test_db, qb)

        rec = await mon.pause(tid)
        assert rec.status == "paused" and qb.paused == [HASH_A]
        assert (await repo.get_seed_record(test_db, tid)).status == "paused"

        rec = await mon.resume(tid)
        assert rec.status == "seeding" and qb.resumed == [HASH_A]

    async def test_resume_restores_satisfied_if_previously_met(self, test_db):
        qb = FakeQb()
        mon = _monitor(test_db, qb)
        tid = await self._seeded(test_db, qb, status="paused", hr_satisfied_at=utcnow())
        rec = await mon.resume(tid)
        assert rec.status == "satisfied"

    async def test_pause_unknown_returns_none(self, test_db):
        assert await _monitor(test_db, FakeQb()).pause("nope") is None
        assert await _monitor(test_db, FakeQb()).resume("nope") is None
        assert await _monitor(test_db, FakeQb()).mark_satisfied("nope") is None

    async def test_pause_when_qb_down_raises_and_keeps_status(self, test_db):
        qb = FakeQb(fail=True)
        mon = _monitor(test_db, qb)
        tid = await self._seeded(test_db, qb)
        with pytest.raises(QbUnavailable):
            await mon.pause(tid)
        assert (await repo.get_seed_record(test_db, tid)).status == "seeding"

    async def test_pause_in_dry_run_marks_only(self, test_db):
        qb = FakeQb()
        cfg = _config().model_copy(update={"dry_run": True})
        mon = _monitor(test_db, qb, config=cfg)
        tid = await self._seeded(test_db, qb)
        rec = await mon.pause(tid)
        assert rec.status == "paused" and qb.paused == []

    async def test_mark_satisfied_emits_once(self, test_db):
        qb = FakeQb()
        notifier = CollectNotifier()
        mon = _monitor(test_db, qb, notifier=notifier)
        tid = await self._seeded(test_db, qb)

        rec = await mon.mark_satisfied(tid)
        assert rec.status == "satisfied" and rec.hr_satisfied_at is not None
        assert len(notifier.events) == 1
        ev = notifier.events[0]
        assert ev.kind == "hr_satisfied" and ev.detail["manual"] is True
        assert ev.title == "Manual"

        await mon.mark_satisfied(tid)             # 幂等：不重复发事件
        assert len(notifier.events) == 1
        assert qb.paused == []                    # 手动标记不碰 qb


# ── 站点快照热更新（项 4）────────────────────────────────────

class TestRefreshSites:
    """refresh_sites 让做种监视器立刻按新 H&R 阈值轮询。

    陷阱：SeedingMonitor 构造时把 config.sites 拷成 _site_cfg 快照，
    站点中心改 H&R 后 config.sites 已整体替换，监视器却仍按旧阈值跑，
    界面「已生效」与实际行为不一致，要到重启才收敛。
    """

    async def test_refresh_applies_new_hr_thresholds(self, test_db):
        tid = await _task(test_db, HASH_A, site="mteam")
        qb = FakeQb([_torrent(HASH_A)])
        mon = _monitor(test_db, qb)
        await mon.poll_once()
        rec = await repo.get_seed_record(test_db, tid)
        assert rec.required_seconds == 10 * 3600 and rec.required_ratio == 0.5

        mon.refresh_sites([
            SiteConfig(name="hhanclub", enabled=False),
            SiteConfig(name="mteam", enabled=False,
                       hr_min_seed_hours=48, hr_min_ratio=2.0),
        ])

        tid2 = await _task(test_db, HASH_B, site="mteam")
        qb.torrents.append(_torrent(HASH_B))
        await mon.poll_once()
        rec2 = await repo.get_seed_record(test_db, tid2)
        assert rec2.required_seconds == 48 * 3600 and rec2.required_ratio == 2.0
