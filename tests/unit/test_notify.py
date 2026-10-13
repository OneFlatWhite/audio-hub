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


# ── 推送过滤：v0.6 新事件种类能过 kinds 白名单（项 3）───────────────────

class TestPushKindsFilter:
    """NOTIFY_KINDS 补全后，新事件默认进白名单、可被 choices 校验接受。

    陷阱：若 NOTIFY_KINDS 漏了 v0.6 新事件，设置页 choices 会拒绝它们，
    而默认白名单不含它们时订阅命中 / 站点熔断等关键告警永远推不出去。
    """

    def test_v06_kinds_accepted_by_default(self):
        from audio_hub.config import NotifyConfig
        from audio_hub.notify.webhook_notify import build_push_sinks

        sinks = build_push_sinks(NotifyConfig(generic_webhook_url="https://x.example.com"))
        assert len(sinks) == 1
        sink = sinks[0]
        for kind in (
            "archive_skipped", "scrape_failed",
            "subscription_hit", "subscription_upgrade", "subscription_completed",
            "hr_satisfied", "series_completed",
            "site_circuit_open", "site_auth_expired", "downloader_unreachable",
        ):
            assert sink.accepts(Event(kind=kind, site="", title="", detail={})), \
                f"默认白名单漏了 {kind}"

    def test_kinds_whitelist_still_filters(self):
        from audio_hub.config import NotifyConfig
        from audio_hub.notify.webhook_notify import build_push_sinks

        sinks = build_push_sinks(NotifyConfig(
            generic_webhook_url="https://x.example.com", kinds=["subscription_hit"],
        ))
        sink = sinks[0]
        assert sink.accepts(Event(kind="subscription_hit", site="", title="", detail={}))
        assert not sink.accepts(Event(kind="dispatched", site="", title="", detail={}))

    def test_validate_form_accepts_new_kinds(self):
        from audio_hub.settings_store import NOTIFY_SPECS, validate_form

        resolved, encoded = validate_form(NOTIFY_SPECS, {
            "notify_kinds": "site_auth_expired, subscription_completed , hr_satisfied",
        })
        assert resolved["notify_kinds"] == [
            "site_auth_expired", "subscription_completed", "hr_satisfied",
        ]

    def test_validate_form_rejects_unknown_kind(self):
        import pytest
        from audio_hub.settings_store import NOTIFY_SPECS, SettingsError, validate_form

        with pytest.raises(SettingsError):
            validate_form(NOTIFY_SPECS, {"notify_kinds": "dispatched,not_a_kind"})
