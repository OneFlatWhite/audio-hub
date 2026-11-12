"""AY 每剧退避与月度额度保护。"""

from __future__ import annotations

import importlib


def _state():
    return importlib.import_module("audio_hub.lackfill.ay_state")


async def test_ay_backoff_is_persistent_and_success_clears_it(test_db):
    module = _state()
    assert await module.is_due(test_db, 100, "2026-10-29T00:00:00") is True

    await module.record_result(test_db, 100, {
        "status": "none", "reason": "无资源", "quota_left": 20,
    }, "2026-10-29T00:00:00")
    assert await module.is_due(test_db, 100, "2026-10-29T05:59:59") is False
    assert await module.is_due(test_db, 100, "2026-10-29T06:00:00") is True

    await module.record_result(test_db, 100, {
        "status": "failed", "reason": "网络失败", "quota_left": 19,
    }, "2026-10-29T06:00:00")
    assert await module.is_due(test_db, 100, "2026-10-29T17:59:59") is False
    assert await module.is_due(test_db, 100, "2026-10-29T18:00:00") is True

    await module.record_result(test_db, 100, {"status": "ok", "entry_id": 1}, "2026-10-29T18:00:00")
    assert await module.is_due(test_db, 100, "2026-10-29T18:00:00") is True


async def test_zero_quota_blocks_current_month_but_allows_next_month_refresh(test_db):
    module = _state()
    exhausted = {"quota_left": 0, "updated_at": "2026-10-29T23:00:00"}
    assert module.quota_exhausted(exhausted, "2026-10-29T23:30:00") is True
    assert module.quota_exhausted(exhausted, "2026-11-01T00:10:00") is False

    await module.record_result(test_db, 100, {
        "status": "none", "reason": "本月额度耗尽", "quota_left": 0,
    }, "2026-10-29T23:00:00")
    assert await module.is_due(test_db, 100, "2026-10-30T12:00:00") is False
    assert await module.is_due(test_db, 100, "2026-11-01T00:09:00") is False
    assert await module.is_due(test_db, 100, "2026-11-01T00:10:00") is True


async def test_budget_and_preview_do_not_create_retry_state(test_db):
    module = _state()
    await module.record_result(test_db, 100, {"status": "budget"}, "2026-10-29T00:00:00")
    await module.record_result(test_db, 100, {"status": "preview"}, "2026-10-29T00:00:00")
    assert await module.is_due(test_db, 100, "2026-10-29T00:00:00") is True


async def test_schema_v10_preserves_existing_queue(tmp_path):
    import aiosqlite

    from audio_hub import db as dbmod

    path = tmp_path / "v9.db"
    conn = await aiosqlite.connect(path)
    await conn.executescript(dbmod._MIGRATION_V7)
    await conn.execute(
        "INSERT INTO lackfill_queue VALUES (100, '测试剧', 2024, 'emby-100', '{\"1\":[3]}', '2026-01-01', '2026-01-01')"
    )
    await conn.execute("PRAGMA user_version = 9")
    await conn.commit()
    await conn.close()

    conn = await dbmod.init_db(path)
    try:
        tables = await dbmod._tables(conn) if hasattr(dbmod, "_tables") else None
        if tables is None:
            cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in await cur.fetchall()}
        assert {"lackfill_re0_tasks", "lackfill_ay_tasks", "lackfill_cloud_files"} <= tables
        cur = await conn.execute("SELECT title FROM lackfill_queue WHERE tmdb_id = 100")
        assert (await cur.fetchone())[0] == "测试剧"
        cur = await conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == 10
    finally:
        await conn.close()
