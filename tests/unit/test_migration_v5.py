"""Migration v5：站点 / 下载器中心与订阅引擎的表、列、索引与旧行回填。

重点锁住三件事：
1. init_db 对既有库重复执行不报 "duplicate column"（幂等）；
2. v4 库升到 v5 时旧 watchlist 行按 kind 回填 sub_type / artist；
3. 二次执行不会再次回填（避免把用户改过的 sub_type 打回）。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from audio_hub import db as dbmod
from audio_hub.db import SCHEMA_VERSION, init_db

pytestmark = pytest.mark.asyncio


async def _tables(conn) -> set[str]:
    async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
        return {r[0] for r in await cur.fetchall()}


async def _indexes(conn) -> set[str]:
    async with conn.execute("SELECT name FROM sqlite_master WHERE type='index'") as cur:
        return {r[0] for r in await cur.fetchall()}


async def _columns(conn, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {r[1] for r in await cur.fetchall()}


async def _make_v4_db(path: Path) -> aiosqlite.Connection:
    """只跑 v1–v4 脚本 + v4 的列迁移，得到一个「旧版」库。"""
    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    for script in (dbmod._MIGRATION_V1, dbmod._MIGRATION_V2,
                   dbmod._MIGRATION_V3, dbmod._MIGRATION_V4):
        await conn.executescript(script)
    # v4 的列（不含 v5 部分）
    v4_task_cols = [c for c in dbmod._TASK_COLUMNS
                    if c[0] not in {"downloader_id", "watch_id", "album_key", "quality_tag"}]
    await dbmod._migrate_columns(conn, "tasks", v4_task_cols)
    await dbmod._migrate_columns(conn, "watchlist", [("series_id", "INTEGER")])
    await conn.execute("PRAGMA user_version = 4")
    await conn.commit()
    return conn


class TestFreshInit:
    async def test_new_tables_exist(self, test_db):
        tables = await _tables(test_db)
        for t in ("sites", "site_categories", "downloaders", "subscription_hits"):
            assert t in tables, f"缺表 {t}"

    async def test_hit_indexes_exist(self, test_db):
        idx = await _indexes(test_db)
        assert "idx_hits_watch_album" in idx
        assert "idx_hits_info_hash" in idx

    async def test_task_columns_added(self, test_db):
        cols = await _columns(test_db, "tasks")
        for c in ("downloader_id", "watch_id", "album_key", "quality_tag"):
            assert c in cols, f"tasks 缺列 {c}"

    async def test_watchlist_columns_added(self, test_db):
        cols = await _columns(test_db, "watchlist")
        expected = {
            "sub_type", "artist", "album", "author", "status",
            "search_interval_hours", "last_search_at", "next_search_at", "completed_at",
            "upgrade_enabled", "min_seeders", "hit_count", "downloader_id", "save_path",
            "qb_category", "qb_tags", "preferred_quality", "min_size_mb", "max_size_mb",
            "free_only", "exclude_keywords", "include_keywords",
        }
        missing = expected - cols
        assert not missing, f"watchlist 缺列 {sorted(missing)}"

    async def test_sites_columns(self, test_db):
        cols = await _columns(test_db, "sites")
        for c in ("id", "adapter_type", "enabled", "cookie_enc", "rss_url_enc", "api_key_enc",
                  "music_cat_ids", "audiobook_cat_ids", "auth_status", "user_info_json",
                  "last_rss_test_json", "hr_min_seed_hours", "hr_min_ratio"):
            assert c in cols, f"sites 缺列 {c}"

    async def test_downloaders_columns(self, test_db):
        cols = await _columns(test_db, "downloaders")
        for c in ("id", "name", "type", "host", "port", "username", "password_enc",
                  "api_key_enc", "save_path", "category", "tag", "enabled", "is_default",
                  "priority", "last_test_json", "last_test_at"):
            assert c in cols, f"downloaders 缺列 {c}"

    async def test_schema_version_is_5(self, test_db):
        assert SCHEMA_VERSION == 5
        async with test_db.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == 5


class TestIdempotent:
    async def test_init_twice_no_error(self, tmp_path: Path):
        path = tmp_path / "twice.db"
        conn = await init_db(path)
        await conn.close()
        conn = await init_db(path)   # 第二次：IF NOT EXISTS + PRAGMA 检测，不得抛 duplicate column
        try:
            assert "sites" in await _tables(conn)
            assert "sub_type" in await _columns(conn, "watchlist")
        finally:
            await conn.close()

    async def test_second_run_does_not_rebackfill(self, tmp_path: Path):
        """用户把 sub_type 改成 keyword 后，重启不得被回填逻辑打回 artist。"""
        path = tmp_path / "keep.db"
        conn = await init_db(path)
        await conn.execute(
            "INSERT INTO watchlist (id, kind, query, sub_type, created_at) "
            "VALUES ('w1', 'music', 'X', 'keyword', '2026-01-01T00:00:00')"
        )
        await conn.commit()
        await conn.close()

        conn = await init_db(path)
        try:
            async with conn.execute("SELECT sub_type FROM watchlist WHERE id='w1'") as cur:
                assert (await cur.fetchone())[0] == "keyword"
        finally:
            await conn.close()


class TestUpgradeFromV4:
    async def test_old_watchlist_rows_backfilled(self, tmp_path: Path):
        path = tmp_path / "v4.db"
        conn = await _make_v4_db(path)
        assert "sub_type" not in await _columns(conn, "watchlist")
        await conn.execute(
            "INSERT INTO watchlist (id, kind, query, created_at) VALUES "
            "('m1', 'music', '周杰伦', '2026-01-01T00:00:00')"
        )
        await conn.execute(
            "INSERT INTO watchlist (id, kind, query, created_at) VALUES "
            "('a1', 'audiobook', '三体', '2026-01-01T00:00:00')"
        )
        await conn.execute(
            "INSERT INTO tasks (id, info_hash, site, title, state, created_at, updated_at) "
            "VALUES ('t1', 'abc', 'hhanclub', 'x', 'discovered', '2026-01-01', '2026-01-01')"
        )
        await conn.commit()
        await conn.close()

        conn = await init_db(path)
        try:
            async with conn.execute(
                "SELECT id, sub_type, artist, album, author, query, status FROM watchlist"
            ) as cur:
                rows = {r["id"]: r for r in await cur.fetchall()}
            m, a = rows["m1"], rows["a1"]
            assert m["sub_type"] == "artist"
            assert m["artist"] == "周杰伦", "music 行的 artist 应回填为 query"
            assert m["status"] == "active"
            assert a["sub_type"] == "audiobook"
            assert a["author"] in (None, ""), "audiobook 行 author 留空"
            assert a["album"] in (None, "")
            assert a["query"] == "三体", "query 保留为书名"

            # 旧任务：新列可读且为 NULL（downloader_id 回填由 seed_from_yaml 负责）
            async with conn.execute("SELECT downloader_id, quality_tag FROM tasks") as cur:
                t = await cur.fetchone()
            assert t["downloader_id"] is None
            async with conn.execute("PRAGMA user_version") as cur:
                assert (await cur.fetchone())[0] == 5
        finally:
            await conn.close()

    async def test_v4_db_gains_tables_and_indexes(self, tmp_path: Path):
        path = tmp_path / "v4b.db"
        conn = await _make_v4_db(path)
        assert "sites" not in await _tables(conn)
        await conn.close()
        conn = await init_db(path)
        try:
            tables = await _tables(conn)
            assert {"sites", "site_categories", "downloaders", "subscription_hits"} <= tables
            assert {"idx_hits_watch_album", "idx_hits_info_hash"} <= await _indexes(conn)
        finally:
            await conn.close()
