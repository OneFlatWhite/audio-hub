"""订阅引擎：决策（去重 / 洗版 / 完成态 / 待审分流）、RSS 捕获、主动搜索（限流顺延）。

全部用 FakeAdapter 与注入时钟；MIN_REQUEST_INTERVAL_SECONDS 置 0 以免 sleep。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from audio_hub import repo
from audio_hub.adapters.base import AdapterError, DiscountType, MediaCategory, NormalizedTorrent
from audio_hub.config import AppConfig, DownloaderConfig, SiteConfig
from audio_hub.models import (
    FoundVia,
    HitDecision,
    SeriesEntry,
    SubStatus,
    SubType,
    TaskState,
    WatchEntry,
    WatchKind,
)
from audio_hub.subscription import engine
from audio_hub.subscription.engine import (
    EVENT_SUBSCRIPTION_COMPLETED,
    EVENT_SUBSCRIPTION_HIT,
    EVENT_SUBSCRIPTION_UPGRADE,
    build_queries,
    decide,
    run_rss_capture_once,
    run_subscription_search_once,
)

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
CONFIG = AppConfig(db_path="/tmp/x.db", dry_run=True)


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_key):
    monkeypatch.setattr(engine, "MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(tmp_key))


# ── 替身 ────────────────────────────────────────────────────

class _FakeConfig:
    def __init__(self, auth_status="valid", priority=0):
        self.auth_status = auth_status
        self.priority = priority


class FakeAdapter:
    """search(keyword, kind) 返回预置结果；记录每次调用；可注入异常。"""

    def __init__(self, site, results=None, rss=None, *, auth_status="valid", raise_search=None):
        self.site_name = site
        self.config = _FakeConfig(auth_status)
        self.results = results or []
        self.rss = rss or []
        self.raise_search = raise_search
        self.search_calls: list[tuple[str, str | None]] = []
        self.rss_calls = 0

    async def search(self, keyword, kind=None):
        self.search_calls.append((keyword, kind))
        if self.raise_search:
            raise self.raise_search
        return [t for t in self.results if keyword.lower() in t.title.lower()] or list(self.results)

    async def fetch_rss(self, query=None):
        self.rss_calls += 1
        return list(self.rss)


class LegacyAdapter(FakeAdapter):
    """旧签名：search 只接 keyword。"""

    async def search(self, keyword):
        self.search_calls.append((keyword, None))
        return list(self.results)


class Recorder:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)

    def kinds(self):
        return [e.kind for e in self.events]


def torrent(title, *, site="hhanclub", ih=None, size_gb=1.0, discount=DiscountType.NORMAL,
            seeders=10, category=MediaCategory.MUSIC):
    return NormalizedTorrent(
        info_hash=ih or f"{abs(hash((site, title))):040x}"[:40],
        title=title, title_normalized=title.lower(), site=site, category=category,
        download_url=f"https://{site}/dl/{abs(hash(title))}",
        size_bytes=int(size_gb * 1024 ** 3), discount=discount, seeders=seeders,
    )


async def add_watch(db, **kw):
    base = {"kind": WatchKind.MUSIC, "query": "周杰伦", "sub_type": SubType.ARTIST,
            "artist": "周杰伦", "min_confidence": "high", "auto_download": True}
    base.update(kw)
    w = WatchEntry(**base)
    await repo.add_watch_entry(db, w)
    return await repo.get_watch_entry(db, w.id)


async def _decide(db, watch, cands, *, notifier=None, dry_run=True, via=FoundVia.RSS, prio=None):
    return await decide(db, watch, cands, config=CONFIG, notifier=notifier, found_via=via,
                        dry_run=dry_run, site_priority=prio, now=NOW)


# ── build_queries ───────────────────────────────────────────

class TestBuildQueries:
    def test_artist_uses_name_and_aliases(self):
        w = WatchEntry(kind=WatchKind.MUSIC, query="周杰伦", sub_type=SubType.ARTIST,
                       artist="周杰伦", aliases=["Jay Chou", "周杰伦", ""])
        assert build_queries(w) == ["周杰伦", "Jay Chou"]

    def test_album_joins_artist_and_album(self):
        w = WatchEntry(kind=WatchKind.MUSIC, query="叶惠美", sub_type=SubType.ALBUM,
                       artist="周杰伦", album="叶惠美")
        assert build_queries(w) == ["周杰伦 叶惠美"]

    def test_audiobook_uses_title_and_author(self):
        w = WatchEntry(kind=WatchKind.AUDIOBOOK, query="三体", author="刘慈欣")
        assert build_queries(w) == ["三体", "刘慈欣"]

    def test_keyword_with_aliases(self):
        w = WatchEntry(kind=WatchKind.MUSIC, query="Hi-Res", sub_type=SubType.KEYWORD,
                       aliases=["24bit"])
        assert build_queries(w) == ["Hi-Res", "24bit"]


# ── decide ──────────────────────────────────────────────────

class TestDecideBasic:
    async def test_dispatch_creates_task_and_hit(self, test_db):
        w = await add_watch(test_db)
        rec = Recorder()
        hits = await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 (2003) [FLAC]")], notifier=rec)
        assert [h.decision for h in hits] == [HitDecision.DISPATCHED]
        h = hits[0]
        assert h.album_key == "周杰伦|叶惠美"
        assert h.quality_tag == "flac_16"
        assert h.found_via == FoundVia.RSS
        task = await repo.get_task(test_db, h.task_id)
        assert task is not None
        assert task.watch_id == w.id and task.album_key == "周杰伦|叶惠美"
        assert task.quality_tag == "flac_16"
        # dry_run：任务停在 scored，且不混进待审队列
        assert task.state == TaskState.SCORED
        assert task.reject_reason == engine.DRY_RUN_REASON
        assert await repo.count_pending_review(test_db) == 0
        assert rec.kinds() == [EVENT_SUBSCRIPTION_HIT]
        assert (await repo.get_watch_entry(test_db, w.id)).hit_count == 1
        # 歌手型不自动完成
        assert (await repo.get_watch_entry(test_db, w.id)).status == SubStatus.ACTIVE

    async def test_real_run_leaves_task_at_discovered(self, test_db):
        w = await add_watch(test_db)
        hits = await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC]")], dry_run=False)
        task = await repo.get_task(test_db, hits[0].task_id)
        assert task.state == TaskState.DISCOVERED
        assert task.reject_reason is None

    async def test_unmatched_candidates_are_silently_dropped(self, test_db):
        w = await add_watch(test_db)
        hits = await _decide(test_db, w, [torrent("林俊杰 - 曹操 [FLAC]")])
        assert hits == []
        assert await repo.list_hits(test_db, w.id) == []

    async def test_filtered_candidate_is_recorded_rejected(self, test_db):
        w = await add_watch(test_db, free_only=True)
        hits = await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC]")])
        assert [h.decision for h in hits] == [HitDecision.REJECTED]
        assert hits[0].reject_reason.startswith("not_free")
        assert hits[0].task_id is None
        assert await repo.count_pending_review(test_db) == 0

    async def test_same_torrent_not_decided_twice(self, test_db):
        w = await add_watch(test_db)
        t = torrent("周杰伦 - 叶惠美 [FLAC]")
        assert len(await _decide(test_db, w, [t])) == 1
        assert await _decide(test_db, w, [t]) == []
        assert len(await repo.list_hits(test_db, w.id)) == 1


class TestDecideReview:
    async def test_low_confidence_goes_to_review(self, test_db):
        w = await add_watch(test_db, min_confidence="high")
        hits = await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [MP3 320K]")])
        assert hits[0].decision == HitDecision.REVIEW
        assert hits[0].reject_reason.startswith("confidence_below_threshold")
        task = await repo.get_task(test_db, hits[0].task_id)
        assert task.state == TaskState.SCORED
        assert await repo.count_pending_review(test_db) == 1

    async def test_auto_download_disabled_goes_to_review(self, test_db):
        w = await add_watch(test_db, auto_download=False)
        rec = Recorder()
        hits = await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC]")], notifier=rec)
        assert hits[0].decision == HitDecision.REVIEW
        assert hits[0].reject_reason == "auto_download_disabled"
        assert rec.events == []
        assert (await repo.get_watch_entry(test_db, w.id)).hit_count == 0

    async def test_review_then_accept_writes_back_hit(self, test_db):
        w = await add_watch(test_db, auto_download=False)
        hits = await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC]")])
        n = await repo.set_hit_task(test_db, hits[0].info_hash, "task-x", HitDecision.DISPATCHED)
        assert n == 1
        stored = await repo.list_hits(test_db, w.id)
        assert stored[0].decision == HitDecision.DISPATCHED and stored[0].task_id == "task-x"
        assert await repo.has_album(test_db, w.id, "周杰伦|叶惠美")


class TestDecideDedupe:
    async def test_group_picks_best_and_marks_others_dup(self, test_db):
        w = await add_watch(test_db)
        cands = [
            torrent("周杰伦 - 叶惠美 (2003) [MP3 320K]", site="hdsky"),
            torrent("周杰伦 - 叶惠美 (2003) [FLAC 24-96]", site="hhanclub"),
            torrent("周杰伦 - 叶惠美 (2003) [FLAC]", site="audiences", discount=DiscountType.FREE),
        ]
        w.min_confidence = "low"
        hits = await _decide(test_db, w, cands)
        by = {h.site: h for h in hits}
        assert by["hhanclub"].decision == HitDecision.DISPATCHED
        assert by["audiences"].decision == HitDecision.SKIPPED_DUP
        assert by["hdsky"].decision == HitDecision.SKIPPED_DUP
        assert by["hdsky"].reject_reason == engine.REASON_OUTRANKED
        # 只建了一个任务
        assert sum(1 for h in hits if h.task_id) == 1

    async def test_site_priority_beats_quality(self, test_db):
        w = await add_watch(test_db)
        cands = [
            torrent("周杰伦 - 叶惠美 [FLAC 24-96]", site="hhanclub"),
            torrent("周杰伦 - 叶惠美 [FLAC]", site="mteam"),
        ]
        hits = await _decide(test_db, w, cands, prio={"mteam": 10, "hhanclub": 1})
        by = {h.site: h for h in hits}
        assert by["mteam"].decision == HitDecision.DISPATCHED
        assert by["hhanclub"].decision == HitDecision.SKIPPED_DUP

    async def test_already_owned_album_skipped(self, test_db):
        w = await add_watch(test_db)
        await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC]")])
        hits = await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 (2003) [FLAC 分轨]", site="hdsky")])
        assert hits[0].decision == HitDecision.SKIPPED_DUP
        assert hits[0].reject_reason == engine.REASON_ALREADY_OWNED
        assert hits[0].task_id is None

    async def test_different_albums_both_dispatch(self, test_db):
        w = await add_watch(test_db)
        hits = await _decide(test_db, w, [
            torrent("周杰伦 - 叶惠美 [FLAC]"), torrent("周杰伦 - 七里香 [FLAC]"),
        ])
        assert [h.decision for h in hits] == [HitDecision.DISPATCHED] * 2
        assert {h.album_key for h in hits} == {"周杰伦|叶惠美", "周杰伦|七里香"}


class TestDecideUpgrade:
    async def test_upgrade_when_better_quality(self, test_db):
        w = await add_watch(test_db, upgrade_enabled=True)
        rec = Recorder()
        await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC]")], notifier=rec)
        hits = await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC 24bit 96kHz]", site="hdsky")],
                             notifier=rec)
        assert hits[0].decision == HitDecision.UPGRADE
        assert hits[0].task_id
        assert rec.kinds() == [EVENT_SUBSCRIPTION_HIT, EVENT_SUBSCRIPTION_UPGRADE]
        assert await repo.best_quality_for_album(test_db, w.id, "周杰伦|叶惠美") == "flac_24"

    async def test_no_upgrade_for_equal_or_lower(self, test_db):
        w = await add_watch(test_db, upgrade_enabled=True, min_confidence="low")
        await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC]")])
        hits = await _decide(test_db, w, [
            torrent("周杰伦 - 叶惠美 [FLAC 分轨]", site="hdsky"),
            torrent("周杰伦 - 叶惠美 [MP3 320K]", site="audiences"),
        ])
        decisions = {h.site: (h.decision, h.reject_reason) for h in hits}
        assert decisions["hdsky"] == (HitDecision.SKIPPED_DUP, engine.REASON_NO_BETTER_QUALITY)
        assert decisions["audiences"][0] == HitDecision.SKIPPED_DUP


class TestDecideCompletion:
    async def test_album_subscription_completes_on_first_dispatch(self, test_db):
        w = await add_watch(test_db, sub_type=SubType.ALBUM, query="叶惠美", album="叶惠美")
        rec = Recorder()
        hits = await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC]")], notifier=rec)
        assert hits[0].decision == HitDecision.DISPATCHED
        stored = await repo.get_watch_entry(test_db, w.id)
        assert stored.status == SubStatus.COMPLETED
        assert stored.completed_at is not None
        assert rec.kinds() == [EVENT_SUBSCRIPTION_HIT, EVENT_SUBSCRIPTION_COMPLETED]
        assert rec.events[1].detail["watch_id"] == w.id

    async def test_album_review_does_not_complete(self, test_db):
        w = await add_watch(test_db, sub_type=SubType.ALBUM, query="叶惠美", album="叶惠美",
                            auto_download=False)
        await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC]")])
        assert (await repo.get_watch_entry(test_db, w.id)).status == SubStatus.ACTIVE

    async def test_real_run_defers_completion_to_dispatch(self, test_db):
        """实跑（dry_run=False）：一次性订阅不得在此刻 completed。

        陷阱：命中 ≠ 已进下载器。若 decide 时就标完成，派发失败（直链换取
        失败、qb 不可达）后订阅已完结，永远不会重试——用户看到「订阅完成」
        但库里什么都没有。completed 由 scheduler 在派发确认成功后补标。
        """
        w = await add_watch(test_db, sub_type=SubType.ALBUM, query="叶惠美", album="叶惠美")
        rec = Recorder()
        hits = await _decide(test_db, w, [torrent("周杰伦 - 叶惠美 [FLAC]")],
                             notifier=rec, dry_run=False)
        assert hits[0].decision == HitDecision.DISPATCHED
        stored = await repo.get_watch_entry(test_db, w.id)
        assert stored.status == SubStatus.ACTIVE, "实跑命中不应立即完结"
        assert stored.completed_at is None
        assert rec.kinds() == [EVENT_SUBSCRIPTION_HIT], \
            "subscription_completed 事件应延后到派发确认"

    async def test_standalone_audiobook_completes(self, test_db):
        w = await add_watch(test_db, kind=WatchKind.AUDIOBOOK, query="三体", author="刘慈欣",
                            artist="", sub_type=SubType.AUDIOBOOK, min_confidence="low")
        hits = await _decide(test_db, w, [torrent("三体 刘慈欣 有声书 [M4B]", size_gb=0.5,
                                                   category=MediaCategory.AUDIOBOOK)])
        assert hits[0].decision == HitDecision.DISPATCHED
        assert (await repo.get_watch_entry(test_db, w.id)).status == SubStatus.COMPLETED

    async def test_serial_audiobook_stays_active_and_registers_part(self, test_db):
        w = await add_watch(test_db, kind=WatchKind.AUDIOBOOK, query="盗墓笔记", artist="",
                            sub_type=SubType.AUDIOBOOK, min_confidence="low")
        sid = await repo.add_series(test_db, SeriesEntry(watch_id=w.id, name="盗墓笔记"))
        await repo.set_watch_series(test_db, w.id, sid)
        w = await repo.get_watch_entry(test_db, w.id)
        hits = await _decide(test_db, w, [torrent("盗墓笔记 第3卷 [MP3]", size_gb=0.5,
                                                   category=MediaCategory.AUDIOBOOK)])
        assert hits[0].decision == HitDecision.DISPATCHED
        assert hits[0].album_key == "盗墓笔记|vol3"
        assert (await repo.get_watch_entry(test_db, w.id)).status == SubStatus.ACTIVE
        parts = await repo.list_part_numbers_by_series(test_db)
        assert parts.get(sid) == {3}


# ── run_rss_capture_once ────────────────────────────────────

class TestRssCapture:
    async def test_capture_matches_active_subscriptions(self, test_db):
        w1 = await add_watch(test_db)
        w2 = await add_watch(test_db, query="林俊杰", artist="林俊杰", auto_download=False)
        await add_watch(test_db, query="陈奕迅", artist="陈奕迅", status=SubStatus.PAUSED)
        a = FakeAdapter("hhanclub", rss=[
            torrent("周杰伦 - 叶惠美 [FLAC]"), torrent("林俊杰 - 曹操 [FLAC]"),
            torrent("陈奕迅 - U87 [FLAC]"), torrent("BBC 纪录片 1080p"),
        ])
        s = await run_rss_capture_once(test_db, {"hhanclub": a}, config=CONFIG, now=NOW)
        assert a.rss_calls == 1
        assert s.sites_polled == ["hhanclub"]
        assert s.raw_count == 4 and s.candidates == 4
        assert s.by_decision() == {"dispatched": 1, "review": 1}
        assert {h.watch_id for h in s.hits} == {w1.id, w2.id}
        assert len(s.created_tasks) == 2

    async def test_capture_skips_invalid_auth_and_disabled_sites(self, test_db):
        await add_watch(test_db)
        bad = FakeAdapter("hdsky", rss=[torrent("周杰伦 - 叶惠美 [FLAC]", site="hdsky")],
                          auth_status="expired")
        await repo.upsert_site(test_db, SiteConfig(name="audiences", enabled=False))
        off = FakeAdapter("audiences", rss=[torrent("周杰伦 - 范特西 [FLAC]", site="audiences")])
        s = await run_rss_capture_once(test_db, {"hdsky": bad, "audiences": off}, config=CONFIG)
        assert s.sites_polled == []
        assert s.skipped_sites == {"hdsky": "auth_expired", "audiences": "disabled"}
        assert bad.rss_calls == 0 and off.rss_calls == 0

    async def test_site_error_does_not_break_round(self, test_db):
        await add_watch(test_db)
        good = FakeAdapter("hhanclub", rss=[torrent("周杰伦 - 叶惠美 [FLAC]")])
        bad = FakeAdapter("hdsky")

        async def boom(query=None):
            raise AdapterError("rss down")
        bad.fetch_rss = boom
        s = await run_rss_capture_once(test_db, {"hhanclub": good, "hdsky": bad}, config=CONFIG)
        assert "hdsky" in s.site_errors
        assert s.by_decision() == {"dispatched": 1}
        stats = await repo.get_site_stats(test_db, "hdsky")
        assert stats["consecutive_fail"] == 1

    async def test_watch_site_restriction_respected(self, test_db):
        await add_watch(test_db, sites=["mteam"])
        a = FakeAdapter("hhanclub", rss=[torrent("周杰伦 - 叶惠美 [FLAC]")])
        s = await run_rss_capture_once(test_db, {"hhanclub": a}, config=CONFIG)
        assert s.hits == [] and s.matched == 0


# ── run_subscription_search_once ────────────────────────────

class TestSubscriptionSearch:
    async def test_due_subscription_searched_and_rescheduled(self, test_db):
        w = await add_watch(test_db, aliases=["Jay Chou"], search_interval_hours=12)
        a = FakeAdapter("hhanclub", results=[torrent("周杰伦 - 叶惠美 [FLAC]")])
        s = await run_subscription_search_once(test_db, {"hhanclub": a}, config=CONFIG, now=NOW)
        assert s.due == 1 and s.searched == [w.id] and s.deferred == []
        assert [c[0] for c in a.search_calls] == ["周杰伦", "Jay Chou"]
        assert a.search_calls[0][1] == "music"
        assert s.requests == {"hhanclub": 2}
        assert s.by_decision() == {"dispatched": 1}
        assert s.hits[0].found_via == FoundVia.SEARCH
        stored = await repo.get_watch_entry(test_db, w.id)
        assert stored.last_search_at == NOW
        assert (stored.next_search_at - NOW).total_seconds() == 12 * 3600
        # 已排期，下一轮不再到点
        again = await run_subscription_search_once(test_db, {"hhanclub": a}, config=CONFIG, now=NOW)
        assert again.due == 0

    async def test_legacy_adapter_signature(self, test_db):
        await add_watch(test_db)
        a = LegacyAdapter("hhanclub", results=[torrent("周杰伦 - 叶惠美 [FLAC]")])
        s = await run_subscription_search_once(test_db, {"hhanclub": a}, config=CONFIG, now=NOW)
        assert s.by_decision() == {"dispatched": 1}

    async def test_only_watch_id_ignores_schedule(self, test_db):
        w = await add_watch(test_db, next_search_at=datetime(2030, 1, 1, tzinfo=UTC))
        other = await add_watch(test_db, query="林俊杰", artist="林俊杰")
        a = FakeAdapter("hhanclub", results=[torrent("周杰伦 - 叶惠美 [FLAC]")])
        s = await run_subscription_search_once(
            test_db, {"hhanclub": a}, config=CONFIG, now=NOW, only_watch_id=w.id,
        )
        assert s.searched == [w.id]
        assert other.id not in s.searched
        assert len(a.search_calls) == 1

    async def test_no_usable_sites_flagged(self, test_db):
        await add_watch(test_db)
        a = FakeAdapter("hhanclub", auth_status="unconfigured")
        s = await run_subscription_search_once(test_db, {"hhanclub": a}, config=CONFIG, now=NOW)
        assert s.no_sites is True
        assert s.searched == []
        # 没搜成不算搜过
        assert (await repo.list_due_subscriptions(test_db, NOW))

    async def test_budget_exhausted_defers_lower_priority(self, test_db, monkeypatch):
        monkeypatch.setattr(engine, "MAX_SEARCH_PER_SITE", 3)
        hi = await add_watch(test_db, priority=10, aliases=["Jay Chou"])        # 2 次
        mid = await add_watch(test_db, query="林俊杰", artist="林俊杰", priority=5)  # 1 次
        lo = await add_watch(test_db, query="陈奕迅", artist="陈奕迅", priority=1)   # 预算用尽
        a = FakeAdapter("hhanclub", results=[])
        s = await run_subscription_search_once(test_db, {"hhanclub": a}, config=CONFIG, now=NOW)
        assert s.searched == [hi.id, mid.id]
        assert s.deferred == [lo.id]
        assert s.requests == {"hhanclub": 3}
        assert (await repo.get_watch_entry(test_db, lo.id)).next_search_at is None
        # 顺延的下一轮仍到点
        due = await repo.list_due_subscriptions(test_db, NOW)
        assert [w.id for w in due] == [lo.id]

    async def test_search_error_records_failure_and_continues(self, test_db):
        w = await add_watch(test_db)
        bad = FakeAdapter("hdsky", raise_search=AdapterError("500"))
        good = FakeAdapter("hhanclub", results=[torrent("周杰伦 - 叶惠美 [FLAC]")])
        s = await run_subscription_search_once(
            test_db, {"hdsky": bad, "hhanclub": good}, config=CONFIG, now=NOW,
        )
        assert "hdsky" in s.site_errors
        assert s.searched == [w.id]
        assert s.by_decision() == {"dispatched": 1}

    async def test_circuit_open_site_skipped(self, test_db):
        await add_watch(test_db)
        for i in range(3):
            await repo.record_site_failure(test_db, "hhanclub", f"e{i}", threshold=3, open_minutes=30)
        a = FakeAdapter("hhanclub", results=[torrent("周杰伦 - 叶惠美 [FLAC]")])
        s = await run_subscription_search_once(test_db, {"hhanclub": a}, config=CONFIG)
        assert a.search_calls == []
        assert s.skipped_sites == {"hhanclub": "circuit_open"}

    async def test_watch_downloader_written_to_task(self, test_db):
        await repo.upsert_downloader(test_db, DownloaderConfig(
            id="nas", name="NAS", save_path="/nas/music", category="music-nas",
        ))
        w = await add_watch(test_db, downloader_id="nas", qb_category="override")
        a = FakeAdapter("hhanclub", results=[torrent("周杰伦 - 叶惠美 [FLAC]")])
        s = await run_subscription_search_once(test_db, {"hhanclub": a}, config=CONFIG, now=NOW)
        assert s.searched == [w.id]
        task = s.created_tasks[0]
        assert task.downloader_id == "nas"
        assert task.qb_save_path == "/nas/music"
        assert task.qb_category == "override"
