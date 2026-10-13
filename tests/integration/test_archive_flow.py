"""归档链路集成测试：cp → 校验 → 隔离 / 决策 → 落位 → 回收 → 刮削 → 到期清理。

tmp_path 下搭 seeds / scratch / library / recycle / quarantine 五个目录，走真实文件
操作与真实 SQLite；外部刮削器用 respx 拦截。每个用例最后都断言原种目录完好。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from audio_hub import repo
from audio_hub.archive.fs import move_tree, path_within, remove_tree
from audio_hub.completion.monitor import (
    CompletionMonitor,
    archive_from_scratch,
    process_completed_task,
    purge_expired_recycle,
    recycle_slot,
    route_completed_task,
)
from audio_hub.config import AppConfig, PathsConfig, ScrapeConfig, ValidationConfig
from audio_hub.models import (
    ArchiveDecision,
    LibraryItem,
    RecycleEntry,
    Task,
    TaskState,
    WatchEntry,
    WatchKind,
)
from audio_hub.timeutil import utcnow


def make_flac(path: Path, sample_rate: int = 44_100, bits: int = 16, channels: int = 2) -> Path:
    """最小 FLAC 头（与 tests/unit/test_validation.py 同构；tests 不是包，不跨文件 import）。"""
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36)
    streaminfo = bytearray(34)
    streaminfo[10:18] = packed.to_bytes(8, "big")
    head = b"fLaC" + bytes([0x80]) + (34).to_bytes(3, "big") + bytes(streaminfo)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 24bit 文件稍大，让 restore 用例能靠体积区分新旧版
    path.write_bytes(head + b"\x00" * (64 if bits <= 16 else 128))
    return path


# ── fixtures ────────────────────────────────────────────────

class FakeNotifier:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    d = {
        "seeds": tmp_path / "seeds",
        "scratch": tmp_path / "scratch" / "inbox",
        "music": tmp_path / "library" / "music",
        "audiobook": tmp_path / "library" / "audiobook",
        "recycle": tmp_path / "recycle",
        "quarantine": tmp_path / "quarantine",
    }
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def cfg(dirs) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(
            scratch_inbox=str(dirs["scratch"]), library_music=str(dirs["music"]),
            library_audiobook=str(dirs["audiobook"]), recycle_dir=str(dirs["recycle"]),
            quarantine_dir=str(dirs["quarantine"]), recycle_retention_days=30,
        ),
        validation=ValidationConfig(),
        scrape=ScrapeConfig(),
    )


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


def seed_torrent(dirs, name: str, *, flac_bits: int = 16, tracks: int = 2) -> Path:
    """在 seeds 下造一个"已下载完成"的专辑目录。"""
    src = dirs["seeds"] / name
    for i in range(1, tracks + 1):
        make_flac(src / f"{i:02d}.flac", bits=flac_bits)
    (src / "cover.jpg").write_bytes(b"\xff\xd8")
    return src


def snapshot(path: Path) -> dict[str, int]:
    return {str(p.relative_to(path)): p.stat().st_size for p in sorted(path.rglob("*"))
            if p.is_file()}


async def make_task(db, title: str, quality: str, *, kind=WatchKind.MUSIC,
                    watch_id: str | None = None, info_hash: str | None = None) -> Task:
    if watch_id is None:
        watch_id = await repo.add_watch_entry(db, WatchEntry(kind=kind, query=title.split()[0]))
    task = Task(
        watch_entry_id=watch_id, info_hash=info_hash or f"hash-{title}-{quality}",
        site="hhanclub", title=title, quality=quality, state=TaskState.COMPLETED,
    )
    task.id = await repo.create_task(db, task)
    return task


# ── NEW / UPGRADE / SKIP ───────────────────────────────────

class TestArchiveDecisions:
    async def test_new_routes_to_music_library(self, test_db, cfg, dirs, notifier):
        src = seed_torrent(dirs, "Jay Chou - Fantasy [FLAC]")
        before = snapshot(src)
        task = await make_task(test_db, "Jay Chou - Fantasy [FLAC]", "flac_16")

        outcome = await process_completed_task(
            test_db, cfg, task, src, notifier=notifier,
        )

        assert outcome.status == "routed" and outcome.decision == ArchiveDecision.NEW
        assert outcome.path is not None and path_within(outcome.path, dirs["music"])
        assert (outcome.path / "01.flac").exists()
        # scratch 副本保留给 MTW 刮削
        assert outcome.scratch_path is not None and outcome.scratch_path.exists()
        # 原种完好
        assert snapshot(src) == before

        saved = await repo.get_task(test_db, task.id)
        assert saved.state == TaskState.ROUTED
        assert saved.archived_path == str(outcome.path)
        assert saved.archive_decision == "new"
        assert json.loads(saved.validation_result)["ok"] is True

        lib = await repo.list_library_items(test_db)
        assert len(lib) == 1 and lib[0].quality_score == 85 and lib[0].task_id == task.id
        assert lib[0].kind == WatchKind.MUSIC and lib[0].watch_id == task.watch_entry_id
        assert notifier.kinds() == ["routed"]

    async def test_audiobook_routes_to_audiobook_library(self, test_db, cfg, dirs, notifier):
        src = seed_torrent(dirs, "三体 第一部 有声书")
        task = await make_task(test_db, "三体 第一部 有声书", "audiobook", kind=WatchKind.AUDIOBOOK)
        outcome = await process_completed_task(test_db, cfg, task, src, notifier=notifier)
        assert outcome.status == "routed"
        assert path_within(outcome.path, dirs["audiobook"])
        lib = await repo.list_library_items(test_db)
        assert lib[0].kind == WatchKind.AUDIOBOOK

    async def test_upgrade_moves_old_to_recycle(self, test_db, cfg, dirs, notifier):
        # 先入库一个 16bit 版本（方括号里的规格标记会被归一化去掉，两版 title_norm 相同）
        src16 = seed_torrent(dirs, "Jay Chou - Fantasy [FLAC]")
        t16 = await make_task(test_db, "Jay Chou - Fantasy [FLAC]", "flac_16")
        o16 = await process_completed_task(test_db, cfg, t16, src16, notifier=notifier)
        old_path = o16.path
        assert old_path.exists()

        # 同订阅项下来了 24bit
        src24 = seed_torrent(dirs, "Jay Chou - Fantasy [FLAC 24bit]", flac_bits=24)
        t24 = await make_task(
            test_db, "Jay Chou - Fantasy [FLAC 24bit]", "flac_24", watch_id=t16.watch_entry_id,
        )
        o24 = await process_completed_task(test_db, cfg, t24, src24, notifier=notifier)

        assert o24.status == "routed" and o24.decision == ArchiveDecision.UPGRADE
        # 新版落在原目录名（旧目录已让位）
        assert o24.path == old_path
        assert (o24.path / "01.flac").stat().st_size > 0
        # 旧版进了 recycle_dir/<YYYYMMDD>/<item_id>/
        entries = await repo.list_recycle_entries(test_db, restored=False)
        assert len(entries) == 1
        e = entries[0]
        assert e.original_path == str(old_path)
        assert path_within(e.recycle_path, dirs["recycle"])
        assert Path(e.recycle_path).parent.name == utcnow().strftime("%Y%m%d")
        assert Path(e.recycle_path).name == str(e.library_item_id)
        assert (Path(e.recycle_path) / "01.flac").exists()
        assert e.reason == f"upgrade:{t24.id}"
        # library_items 仍是一条，但指向新版
        lib = await repo.list_library_items(test_db)
        assert len(lib) == 1
        assert lib[0].quality_score == 95 and lib[0].task_id == t24.id
        assert lib[0].path == str(o24.path)
        assert (await repo.get_task(test_db, t24.id)).archive_decision == "upgrade"
        assert notifier.kinds() == ["routed", "routed"]
        # 两个原种都完好
        assert (src16 / "01.flac").exists() and (src24 / "01.flac").exists()

    async def test_skip_removes_scratch_copy_only(self, test_db, cfg, dirs, notifier):
        src24 = seed_torrent(dirs, "Jay Chou - Fantasy [FLAC 24bit]", flac_bits=24)
        t24 = await make_task(test_db, "Jay Chou - Fantasy [FLAC 24bit]", "flac_24")
        await process_completed_task(test_db, cfg, t24, src24, notifier=notifier)

        src16 = seed_torrent(dirs, "Jay Chou - Fantasy [FLAC 16bit]")
        t16 = await make_task(
            test_db, "Jay Chou - Fantasy [FLAC 16bit]", "flac_16", watch_id=t24.watch_entry_id,
        )
        outcome = await process_completed_task(test_db, cfg, t16, src16, notifier=notifier)

        assert outcome.status == "skipped" and outcome.decision == ArchiveDecision.SKIP
        assert outcome.path is None
        assert not outcome.scratch_path.exists(), "skip 应删除 scratch 副本"
        assert (src16 / "01.flac").exists(), "原种不得被删"
        saved = await repo.get_task(test_db, t16.id)
        assert saved.state == TaskState.ARCHIVED_SKIPPED
        assert saved.archive_decision == "skip"
        assert len(await repo.list_library_items(test_db)) == 1
        assert notifier.kinds()[-1] == "archive_skipped"

    async def test_name_collision_without_upgrade_gets_suffix(self, test_db, cfg, dirs):
        # 不同订阅项、同归一化标题 → 都是 NEW，第二个目录名加后缀而不是覆盖
        src_a = seed_torrent(dirs, "Fantasy FLAC a")
        src_b = seed_torrent(dirs, "Fantasy FLAC b")
        ta = await make_task(test_db, "Fantasy FLAC", "flac_16", info_hash="a")
        tb = await make_task(test_db, "Fantasy FLAC", "flac_16", info_hash="b")
        oa = await process_completed_task(test_db, cfg, ta, src_a)
        ob = await process_completed_task(test_db, cfg, tb, src_b)
        assert oa.status == ob.status == "routed"
        assert oa.path != ob.path
        assert ob.path.name.endswith(tb.id[:8])
        assert len(await repo.list_library_items(test_db)) == 2


# ── 隔离 ───────────────────────────────────────────────────

class TestQuarantine:
    async def test_fake_lossless_is_quarantined(self, test_db, cfg, dirs, notifier):
        src = seed_torrent(dirs, "Bad Album FLAC", flac_bits=8)
        before = snapshot(src)
        task = await make_task(test_db, "Bad Album FLAC", "flac_16")

        outcome = await process_completed_task(test_db, cfg, task, src, notifier=notifier)

        assert outcome.status == "quarantined"
        assert outcome.path == dirs["quarantine"] / task.id
        assert (outcome.path / "01.flac").exists()
        assert not outcome.scratch_path.exists(), "隔离是 move，scratch 不该留副本"
        assert snapshot(src) == before

        saved = await repo.get_task(test_db, task.id)
        assert saved.state == TaskState.QUARANTINED
        vr = json.loads(saved.validation_result)
        assert vr["ok"] is False and "suspect_lossless:01.flac" in vr["reasons"]

        qs = await repo.list_quarantine_items(test_db, status="held")
        assert len(qs) == 1
        assert qs[0].task_id == task.id and qs[0].scratch_path == str(outcome.path)
        assert qs[0].reasons == ["suspect_lossless:01.flac", "suspect_lossless:02.flac"]
        assert notifier.kinds() == ["quarantined"]
        assert notifier.events[0].level == "warning"
        assert len(await repo.list_library_items(test_db)) == 0

    async def test_pass_through_archives_despite_failure(self, test_db, cfg, dirs, notifier):
        cfg.validation.on_fail = "pass_through"
        src = seed_torrent(dirs, "Bad Album FLAC", flac_bits=8)
        task = await make_task(test_db, "Bad Album FLAC", "flac_16")
        outcome = await process_completed_task(test_db, cfg, task, src, notifier=notifier)
        assert outcome.status == "routed"
        saved = await repo.get_task(test_db, task.id)
        assert json.loads(saved.validation_result)["ok"] is False
        assert await repo.count_quarantine_items(test_db) == 0

    async def test_release_goes_through_archive(self, test_db, cfg, dirs, notifier):
        """放行 = 隔离目录移回 scratch → archive_from_scratch（页面 handler 的核心步骤）。"""
        src = seed_torrent(dirs, "Bad Album FLAC", flac_bits=8)
        task = await make_task(test_db, "Bad Album FLAC", "flac_16")
        q = await process_completed_task(test_db, cfg, task, src, notifier=notifier)
        item = (await repo.list_quarantine_items(test_db, status="held"))[0]

        back = dirs["scratch"] / q.path.name
        await move_tree(Path(item.scratch_path), back)
        outcome = await archive_from_scratch(test_db, cfg, task, back, notifier=notifier)
        await repo.set_quarantine_status(test_db, item.id, "released")

        assert outcome.status == "routed" and outcome.decision == ArchiveDecision.NEW
        assert (outcome.path / "01.flac").exists()
        assert not Path(item.scratch_path).exists()
        assert (await repo.get_task(test_db, task.id)).state == TaskState.ROUTED
        released = await repo.get_quarantine_item(test_db, item.id)
        assert released.status == "released" and released.resolved_at is not None
        assert notifier.kinds() == ["quarantined", "routed"]

    async def test_discard_only_touches_quarantine_dir(self, test_db, cfg, dirs):
        src = seed_torrent(dirs, "Bad Album FLAC", flac_bits=8)
        task = await make_task(test_db, "Bad Album FLAC", "flac_16")
        q = await process_completed_task(test_db, cfg, task, src)

        assert await remove_tree(q.path, root=dirs["quarantine"]) is True
        assert not q.path.exists()
        assert (src / "01.flac").exists()
        # 围栏：原种目录拒绝删除
        assert await remove_tree(src, root=dirs["quarantine"]) is False
        assert (src / "01.flac").exists()


# ── 回收区 ─────────────────────────────────────────────────

class TestRecycle:
    async def test_purge_expired_removes_dir_and_row(self, test_db, cfg, dirs):
        old_dir = dirs["recycle"] / "20200101" / "7"
        old_dir.mkdir(parents=True)
        (old_dir / "x.flac").write_bytes(b"x")
        fresh_dir = dirs["recycle"] / "20990101" / "8"
        fresh_dir.mkdir(parents=True)
        now = utcnow()
        await repo.add_recycle_entry(test_db, RecycleEntry(
            library_item_id=7, original_path="/lib/a", recycle_path=str(old_dir),
            reason="upgrade", created_at=now - timedelta(days=45),
        ))
        await repo.add_recycle_entry(test_db, RecycleEntry(
            library_item_id=8, original_path="/lib/b", recycle_path=str(fresh_dir),
            reason="upgrade", created_at=now - timedelta(days=1),
        ))

        purged = await purge_expired_recycle(test_db, cfg, now=now)

        assert purged == 1
        assert not old_dir.exists() and fresh_dir.exists()
        left = await repo.list_recycle_entries(test_db)
        assert [e.library_item_id for e in left] == [8]

    async def test_purge_refuses_paths_outside_recycle_dir(self, test_db, cfg, dirs):
        victim = dirs["seeds"] / "precious"
        victim.mkdir()
        (victim / "01.flac").write_bytes(b"x")
        await repo.add_recycle_entry(test_db, RecycleEntry(
            library_item_id=1, original_path="/lib/a", recycle_path=str(victim),
            reason="tampered", created_at=utcnow() - timedelta(days=400),
        ))
        purged = await purge_expired_recycle(test_db, cfg)
        assert purged == 0
        assert (victim / "01.flac").exists()
        assert len(await repo.list_recycle_entries(test_db)) == 1, "围栏不过的条目保留供人工处理"

    async def test_restore_logic_roundtrip(self, test_db, cfg, dirs):
        """与 routes_library.recycle_restore 相同的步骤：原路径占用 → 让位 → 移回。"""
        src16 = seed_torrent(dirs, "Album [FLAC]")
        t16 = await make_task(test_db, "Album [FLAC]", "flac_16")
        o16 = await process_completed_task(test_db, cfg, t16, src16)
        src24 = seed_torrent(dirs, "Album [FLAC 24bit]", flac_bits=24)
        t24 = await make_task(test_db, "Album [FLAC 24bit]", "flac_24", watch_id=t16.watch_entry_id)
        await process_completed_task(test_db, cfg, t24, src24)
        entry = (await repo.list_recycle_entries(test_db, restored=False))[0]

        dst = Path(entry.original_path)
        assert dst.exists()
        displaced = recycle_slot(cfg, entry.library_item_id)
        await move_tree(dst, displaced)
        await repo.add_recycle_entry(test_db, RecycleEntry(
            library_item_id=entry.library_item_id, original_path=str(dst),
            recycle_path=str(displaced), reason=f"displaced_by_restore:{entry.id}",
        ))
        await move_tree(Path(entry.recycle_path), dst)
        await repo.mark_recycle_restored(test_db, entry.id)
        await repo.update_library_item(test_db, entry.library_item_id, path=str(dst))

        # 原路径现在是 16bit 版本
        assert dst == o16.path
        assert (dst / "01.flac").stat().st_size == (src16 / "01.flac").stat().st_size
        # 新版进了回收区；recycle_slot 对同一 item 同一天不冲突
        assert displaced != Path(entry.recycle_path)
        assert (displaced / "01.flac").exists()
        pending = await repo.list_recycle_entries(test_db, restored=False)
        assert len(pending) == 1 and pending[0].reason.startswith("displaced_by_restore:")
        assert (await repo.get_recycle_entry(test_db, entry.id)).restored_at is not None


# ── 刮削触发 ───────────────────────────────────────────────

class TestScrapeTriggers:
    @respx.mock
    async def test_mtw_webhook_receives_scratch_path(self, test_db, cfg, dirs, notifier):
        cfg.scrape.mtw_webhook_url = "http://mtw.local/hook"
        route = respx.post("http://mtw.local/hook").mock(return_value=httpx.Response(200))
        src = seed_torrent(dirs, "Album FLAC")
        task = await make_task(test_db, "Album FLAC", "flac_16")
        outcome = await process_completed_task(test_db, cfg, task, src, notifier=notifier)

        assert route.called
        body = json.loads(route.calls.last.request.content)
        assert body == {"path": str(outcome.scratch_path), "kind": "music", "title": "Album FLAC"}
        assert "scrape_failed" not in notifier.kinds()

    @respx.mock
    async def test_abs_scan_with_bearer(self, test_db, cfg, dirs, notifier):
        cfg.scrape.abs_url = "http://abs.local/"
        cfg.scrape.abs_library_id = "lib-1"
        cfg.scrape.abs_token = "tok"
        route = respx.post("http://abs.local/api/libraries/lib-1/scan").mock(
            return_value=httpx.Response(200),
        )
        src = seed_torrent(dirs, "三体 有声书")
        task = await make_task(test_db, "三体 有声书", "audiobook", kind=WatchKind.AUDIOBOOK)
        outcome = await process_completed_task(test_db, cfg, task, src, notifier=notifier)

        assert outcome.status == "routed"
        assert route.called
        assert route.calls.last.request.headers["Authorization"] == "Bearer tok"
        assert notifier.kinds() == ["routed"]

    @respx.mock
    async def test_scrape_failure_does_not_block(self, test_db, cfg, dirs, notifier):
        cfg.scrape.mtw_webhook_url = "http://mtw.local/hook"
        respx.post("http://mtw.local/hook").mock(side_effect=httpx.ConnectError("down"))
        src = seed_torrent(dirs, "Album FLAC")
        task = await make_task(test_db, "Album FLAC", "flac_16")
        outcome = await process_completed_task(test_db, cfg, task, src, notifier=notifier)

        assert outcome.status == "routed"
        assert (await repo.get_task(test_db, task.id)).state == TaskState.ROUTED
        assert notifier.kinds() == ["routed", "scrape_failed"]
        assert notifier.events[1].level == "warning"


# ── 兼容入口 / 轮询接线 ────────────────────────────────────

class TestEntryPoints:
    async def test_route_completed_task_full_chain(self, test_db, cfg, dirs):
        src = seed_torrent(dirs, "Album FLAC")
        task = await make_task(test_db, "Album FLAC", "flac_16")
        # 传 task id 字符串也行
        result = await route_completed_task(
            task.id, str(src), dirs["scratch"], db=test_db, cfg=cfg,
        )
        assert result is not None and path_within(result, dirs["music"])
        assert (await repo.get_task(test_db, task.id)).state == TaskState.ROUTED

    async def test_route_completed_task_legacy_copy_only(self, dirs):
        src = seed_torrent(dirs, "Album FLAC")
        result = await route_completed_task(None, str(src), dirs["scratch"])
        assert result == dirs["scratch"] / src.name and result.exists()

    async def test_poll_once_archives_and_purges(self, test_db, cfg, dirs, notifier):
        src = seed_torrent(dirs, "Album FLAC")
        task = await make_task(test_db, "Album FLAC", "flac_16")
        await repo.set_task_state(test_db, task.id, TaskState.DOWNLOADING)
        # 过期回收条目，轮询末尾应被清理
        expired = dirs["recycle"] / "20200101" / "1"
        expired.mkdir(parents=True)
        await repo.add_recycle_entry(test_db, RecycleEntry(
            library_item_id=1, original_path="/x", recycle_path=str(expired),
            reason="upgrade", created_at=utcnow() - timedelta(days=90),
        ))

        torrent = SimpleNamespace(
            hash=task.info_hash, state="uploading", content_path=str(src),
            name=src.name, seeding_time=10,
        )
        qb = SimpleNamespace(torrents_info=lambda tag: [torrent])
        monitor = CompletionMonitor(test_db, qb, cfg, dry_run=False, notifier=notifier)

        stats = await monitor.poll_once()

        assert stats["completed"] == 1 and stats["routed"] == 1
        assert stats["recycle_purged"] == 1 and not expired.exists()
        assert (await repo.get_task(test_db, task.id)).state == TaskState.ROUTED
        assert notifier.kinds() == ["routed"]
        # 再轮询一次：任务已离开 in-flight，不会重复归档
        stats2 = await monitor.poll_once()
        assert stats2["routed"] == 0
        assert len(await repo.list_library_items(test_db)) == 1

    async def test_poll_once_quarantine_counts(self, test_db, cfg, dirs, notifier):
        src = seed_torrent(dirs, "Bad FLAC", flac_bits=8)
        task = await make_task(test_db, "Bad FLAC", "flac_16")
        await repo.set_task_state(test_db, task.id, TaskState.DOWNLOADING)
        torrent = SimpleNamespace(hash=task.info_hash, state="stalledUP", content_path=str(src))
        qb = SimpleNamespace(torrents_info=lambda tag: [torrent])
        monitor = CompletionMonitor(test_db, qb, cfg, dry_run=False, notifier=notifier)
        stats = await monitor.poll_once()
        assert stats["quarantined"] == 1
        assert (await repo.get_task(test_db, task.id)).state == TaskState.QUARANTINED


def test_library_item_model_roundtrip_smoke():
    """LibraryItem 默认值不随便变（归档链路依赖 quality_score 默认 0）。"""
    it = LibraryItem(title_norm="x", path="/p")
    assert it.quality_score == 0 and it.kind == WatchKind.MUSIC
