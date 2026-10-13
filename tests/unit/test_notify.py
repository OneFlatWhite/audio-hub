"""通知抽象：MultiNotifier 扇出容错、DbNotifier 落库、便捷方法的 level 约定。"""

from __future__ import annotations

from audio_hub import repo
from audio_hub.notify import DbNotifier, Event, LogNotifier, MultiNotifier, Notifier


class _Collector:
    def __init__(self):
        self.seen: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.seen.append(event)


class _Broken:
    async def emit(self, event: Event) -> None:
        raise RuntimeError("sink down")


class TestMultiNotifier:
    async def test_fan_out_and_isolate_failure(self):
        good = _Collector()
        multi = MultiNotifier([_Broken(), good])
        await multi.dispatched("hhanclub", "X", score=1.0)
        # 坏 sink 不影响好 sink，也不向调用方抛异常
        assert [e.kind for e in good.seen] == ["dispatched"]
        assert good.seen[0].level == "info"

    async def test_convenience_methods_set_level(self):
        c = _Collector()
        multi = MultiNotifier([c])
        await multi.failed("s", "t", "boom")
        await multi.adapter_error("s", "503")
        await multi.round_summary(candidates=1, accepted=0, review=0, rejected=1)
        assert [e.level for e in c.seen] == ["error", "error", "info"]
        assert c.seen[2].detail["candidates"] == 1

    def test_protocol_conformance(self):
        assert isinstance(LogNotifier(), Notifier)
        assert isinstance(MultiNotifier(), Notifier)
        assert isinstance(_Collector(), Notifier)

    async def test_log_notifier_still_works(self):
        await LogNotifier().completed("s", "t")   # 不抛即可


class TestDbNotifier:
    async def test_emit_persists_event(self, test_db):
        n = DbNotifier(test_db)
        await n.failed("mteam", "Album", "dispatch_failed")
        await n.emit(Event(kind="quarantined", site="", title="", detail={}))

        assert await repo.count_events(test_db) == 2
        [failed] = await repo.list_events(test_db, kind="failed")
        assert failed.level == "error"
        assert failed.site == "mteam"
        assert '"reason": "dispatch_failed"' in (failed.detail or "")
        # 空 site/title/detail 存 NULL 而非空串，便于 SQL 判空
        [q] = await repo.list_events(test_db, kind="quarantined")
        assert q.site is None and q.title is None and q.detail is None
