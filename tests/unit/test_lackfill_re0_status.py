"""Re0 主源心跳、每剧状态和 AY 补充资格。"""

from __future__ import annotations

import importlib


def _status():
    return importlib.import_module("audio_hub.lackfill.re0_status")


async def test_heartbeat_persists_safe_fields_and_expires(test_db):
    module = _status()
    await module.report_status(test_db, {
        "status": "running",
        "tmdb_id": 100,
        "title": "测试剧",
        "reason": "资源页",
        "stage": "resource",
        "attempts": 2,
        "auto_running": True,
        "script_version": "1.1.0",
        "queue_count": 4,
        "cookie": "must-not-persist",
        "url": "https://re0.me/private?session=must-not-persist",
        "token": "must-not-persist",
    }, "2026-10-29T01:00:00")

    current = await module.worker_status(test_db, "2026-10-29T01:01:00")
    assert current["online"] is True
    assert current["status"] == "running"
    assert current["auto_running"] is True
    assert current["stage"] == "resource"
    assert current["attempts"] == 2
    assert current["script_version"] == "1.1.0"
    assert current["queue_count"] == 4
    assert "must-not-persist" not in str(current)

    stale = await module.worker_status(test_db, "2026-10-29T01:03:01")
    assert stale["online"] is False
    assert stale["status"] == "offline"
    assert stale["last_seen"] == "2026-10-29T01:00:00"


async def test_ay_eligibility_requires_explicit_empty_or_three_failures(test_db):
    module = _status()
    base = {"tmdb_id": 100, "title": "测试剧", "stage": "search", "auto_running": True}
    await module.report_status(test_db, {**base, "status": "failed", "attempts": 1}, "2026-10-29T01:00:00")
    assert await module.eligible_for_ay(test_db, 100) is False

    await module.report_status(test_db, {**base, "status": "failed", "attempts": 3}, "2026-10-29T01:01:00")
    assert await module.eligible_for_ay(test_db, 100) is True

    await module.report_status(test_db, {**base, "status": "running", "attempts": 0}, "2026-10-29T01:02:00")
    assert await module.eligible_for_ay(test_db, 100) is False

    await module.report_status(test_db, {**base, "status": "not_found", "attempts": 1}, "2026-10-29T01:03:00")
    assert await module.eligible_for_ay(test_db, 100) is True

    await module.report_status(test_db, {**base, "status": "success"}, "2026-10-29T01:04:00")
    assert await module.eligible_for_ay(test_db, 100) is False


async def test_legacy_failure_without_attempts_increments_server_side(test_db):
    module = _status()
    for minute in (0, 1, 2):
        await module.report_status(test_db, {
            "status": "failed", "tmdb_id": 100, "title": "测试剧", "reason": "超时",
        }, f"2026-10-29T01:0{minute}:00")
    assert await module.eligible_for_ay(test_db, 100) is True
