"""AY 优先调度：配置门禁、单轮预算与 Re0 保留语义。"""

from types import SimpleNamespace

import pytest

from audio_hub.config import AppConfig
from audio_hub.lackfill.service import LackfillService
from audio_hub.scheduler import PipelineRunner, build_scheduler

pytestmark = pytest.mark.asyncio


def _cfg(**over):
    values = {
        "enabled": True,
        "ay_api_enabled": True,
        "ay_api_url": "https://ay.example.com/api/user",
        "ay_api_token": "secret",
        "ay_tg_id": "12345",
        "ay_round_clicks": 4,
        "ay_scan_minutes": 7,
    }
    values.update(over)
    return SimpleNamespace(**values)


def _runner(cfg, db=None):
    async def run_once():
        return {}

    return SimpleNamespace(config=SimpleNamespace(lackfill=cfg), db=db, run_once=run_once)


async def test_ay_job_uses_configured_scan_interval():
    config = AppConfig()
    config.lackfill.ay_scan_minutes = 7
    scheduler = build_scheduler(PipelineRunner(None, config, qb_client=None), config)
    try:
        job = scheduler.get_job("lackfill_ay")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 420
        assert job.max_instances == 1
        assert job.coalesce is True
    finally:
        scheduler.remove_all_jobs()


async def test_ay_job_skips_when_disabled(monkeypatch):
    async def no_queue(*args):
        raise AssertionError("未启用时不得读取或处理回填队列")

    monkeypatch.setattr("audio_hub.lackfill.queue.list_pending", no_queue)
    result = await PipelineRunner.run_lackfill_ay_once(_runner(_cfg(ay_api_enabled=False)))
    assert result == {"skipped": True}


async def test_ay_job_stops_at_round_click_budget(test_db, monkeypatch):
    queued = {
        "tmdb_id": 100,
        "title": "测试剧",
        "seasons": {"1": [3]},
        "emby_id": "emby-100",
    }
    calls = []

    async def pending(_db):
        return [queued]

    async def fill(self, conn, row, budget, **kwargs):
        calls.append((row["tmdb_id"], budget))
        return {"status": "none", "clicks": 2, "reason": "无资源，保留 Re0"}

    monkeypatch.setattr("audio_hub.lackfill.queue.list_pending", pending)
    monkeypatch.setattr(LackfillService, "fill_ay", fill)
    result = await PipelineRunner.run_lackfill_ay_once(_runner(_cfg(), db=test_db))

    assert calls == [(100, 4)]
    assert result["checked"] == 1
    assert result["none"] == 1
    assert result["clicks"] == 2
    assert result["budget_left"] == 2


async def test_ay_only_supplements_failed_tasks_when_re0_is_running(test_db, monkeypatch):
    from datetime import datetime

    from audio_hub.lackfill import queue, re0_status
    from audio_hub.lackfill.models import ShowLack

    now = datetime.now().isoformat()
    for tmdb_id in (100, 200, 300):
        await queue.upsert(test_db, ShowLack(
            tmdb_id=tmdb_id, title=f"剧{tmdb_id}", year=2024,
            emby_id=f"emby-{tmdb_id}", missing={1: [3]},
        ), now)
    await re0_status.report_status(test_db, {
        "status": "failed", "tmdb_id": 100, "attempts": 3, "title": "剧100",
    }, now)
    await re0_status.report_status(test_db, {
        "status": "running", "tmdb_id": 200, "attempts": 0, "title": "剧200",
    }, now)
    await re0_status.report_status(test_db, {
        "status": "not_found", "tmdb_id": 300, "attempts": 1, "title": "剧300",
    }, now)
    await re0_status.report_status(test_db, {
        "status": "running", "auto_running": True, "reason": "运行中",
    }, now)
    calls = []

    async def fill(self, conn, row, budget, **kwargs):
        calls.append(row["tmdb_id"])
        return {"status": "none", "clicks": 1, "reason": "无资源", "quota_left": 90}

    monkeypatch.setattr(LackfillService, "fill_ay", fill)
    result = await PipelineRunner.run_lackfill_ay_once(
        _runner(_cfg(re0_browser_enabled=True), db=test_db)
    )
    assert sorted(calls) == [100, 300]
    assert result["source_mode"] == "available"


async def test_ay_does_not_spend_quota_during_manual_challenge(test_db, monkeypatch):
    from datetime import datetime

    from audio_hub.lackfill import queue, re0_status
    from audio_hub.lackfill.models import ShowLack

    await queue.upsert(test_db, ShowLack(
        tmdb_id=100, title="测试剧", year=2024, emby_id="emby-100", missing={1: [3]},
    ), datetime.now().isoformat())
    await re0_status.report_status(test_db, {
        "status": "challenge", "auto_running": False, "reason": "需要真人",
    }, datetime.now().isoformat())

    async def forbidden(*args, **kwargs):
        raise AssertionError("人工接管期间不得调用 AY")

    monkeypatch.setattr(LackfillService, "fill_ay", forbidden)
    result = await PipelineRunner.run_lackfill_ay_once(
        _runner(_cfg(re0_browser_enabled=True), db=test_db)
    )
    assert result["skipped"] is True
    assert result["source_mode"] == "manual"


async def test_ay_zero_quota_never_queries(test_db, monkeypatch):
    from datetime import datetime

    from audio_hub.lackfill import ay

    await ay.save_quota(test_db, 0, datetime.now().isoformat())

    async def forbidden(*args, **kwargs):
        raise AssertionError("额度为 0 时不得查询")

    monkeypatch.setattr(LackfillService, "fill_ay", forbidden)
    result = await PipelineRunner.run_lackfill_ay_once(_runner(_cfg(), db=test_db))
    assert result["skipped"] is True
    assert "额度" in result["reason"]


async def test_ay_failed_task_is_not_queried_again_before_backoff(test_db, monkeypatch):
    from datetime import datetime

    from audio_hub.lackfill import queue
    from audio_hub.lackfill.models import ShowLack

    await queue.upsert(test_db, ShowLack(
        tmdb_id=100, title="测试剧", year=2024, emby_id="emby-100", missing={1: [3]},
    ), datetime.now().isoformat())
    calls = []

    async def fill(self, conn, row, budget, **kwargs):
        calls.append(row["tmdb_id"])
        return {"status": "none", "clicks": 1, "reason": "无资源", "quota_left": 90}

    monkeypatch.setattr(LackfillService, "fill_ay", fill)
    runner = _runner(_cfg(), db=test_db)
    await PipelineRunner.run_lackfill_ay_once(runner)
    await PipelineRunner.run_lackfill_ay_once(runner)
    assert calls == [100]


async def test_re0_browser_health_job_uses_seconds():
    config = AppConfig()
    config.lackfill.re0_browser_health_seconds = 45
    scheduler = build_scheduler(PipelineRunner(None, config, qb_client=None), config)
    try:
        job = scheduler.get_job("re0_browser_health")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 45
    finally:
        scheduler.remove_all_jobs()


async def test_browser_health_transition_notifies_once(test_db, monkeypatch):
    from audio_hub.lackfill import re0_status

    class Sink:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

    states = iter([
        {"cdp_ok": True, "ready": True, "state": "ready"},
        {"cdp_ok": False, "ready": False, "state": "offline"},
        {"cdp_ok": False, "ready": False, "state": "offline"},
        {"cdp_ok": True, "ready": True, "state": "ready"},
    ])

    async def inspect(_url):
        return next(states)

    monkeypatch.setattr("audio_hub.lackfill.re0_browser.inspect", inspect)
    sink = Sink()
    app = SimpleNamespace(state=SimpleNamespace())
    runner = SimpleNamespace(
        config=SimpleNamespace(lackfill=_cfg(re0_browser_enabled=True)),
        db=test_db, app=app, notifier=sink,
    )
    await re0_status.report_status(test_db, {
        "status": "running", "auto_running": True,
    }, __import__("datetime").datetime.now().isoformat())
    await PipelineRunner.run_re0_browser_health_once(runner)
    await re0_status.report_status(test_db, {
        "status": "waiting", "auto_running": False,
    }, __import__("datetime").datetime.now().isoformat())
    await PipelineRunner.run_re0_browser_health_once(runner)
    await PipelineRunner.run_re0_browser_health_once(runner)
    await re0_status.report_status(test_db, {
        "status": "running", "auto_running": True,
    }, __import__("datetime").datetime.now().isoformat())
    await PipelineRunner.run_re0_browser_health_once(runner)
    assert [event.level for event in sink.events] == ["warning", "info"]
