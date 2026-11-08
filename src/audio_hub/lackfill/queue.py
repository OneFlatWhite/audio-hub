"""Re0 缺集回填队列的持久化读写。"""

from __future__ import annotations

import json

import aiosqlite

from .models import ShowLack


def _decode(row: aiosqlite.Row) -> dict:
    item = dict(row)
    seasons = json.loads(item.pop("missing_json"))
    item["seasons"] = {str(season): list(eps) for season, eps in seasons.items()}
    item["missing"] = sum(len(eps) for eps in item["seasons"].values())
    return item


async def upsert(conn: aiosqlite.Connection, lack: ShowLack, now: str) -> None:
    await conn.execute(
        """
        INSERT INTO lackfill_queue
            (tmdb_id, title, year, emby_id, missing_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tmdb_id) DO UPDATE SET
            title = excluded.title,
            year = excluded.year,
            emby_id = excluded.emby_id,
            missing_json = excluded.missing_json,
            updated_at = excluded.updated_at
        """,
        (
            lack.tmdb_id,
            lack.title,
            lack.year,
            lack.emby_id,
            json.dumps(lack.missing, ensure_ascii=False),
            now,
            now,
        ),
    )
    await conn.commit()


async def get(conn: aiosqlite.Connection, tmdb_id: int) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM lackfill_queue WHERE tmdb_id = ?", (tmdb_id,)
    )
    row = await cur.fetchone()
    return _decode(row) if row is not None else None


async def remove(conn: aiosqlite.Connection, tmdb_id: int) -> None:
    await conn.execute("DELETE FROM lackfill_queue WHERE tmdb_id = ?", (tmdb_id,))
    await conn.commit()


async def update_missing(
    conn: aiosqlite.Connection, tmdb_id: int, missing: dict[int, list[int]], now: str
) -> None:
    await conn.execute(
        "UPDATE lackfill_queue SET missing_json = ?, updated_at = ? WHERE tmdb_id = ?",
        (json.dumps(missing, ensure_ascii=False), now, tmdb_id),
    )
    await conn.commit()


async def list_pending(conn: aiosqlite.Connection) -> list[dict]:
    cur = await conn.execute(
        """
        SELECT q.* FROM lackfill_queue q
        WHERE NOT EXISTS (
            SELECT 1 FROM lackfill_ledger l
            WHERE l.show_key = ('tmdb:' || q.tmdb_id)
              AND l.status IN ('pending', 'partial')
        )
          AND NOT EXISTS (
            SELECT 1 FROM lackfill_reservations r
            WHERE r.show_key = ('tmdb:' || q.tmdb_id)
        )
        ORDER BY q.created_at, q.tmdb_id
        """
    )
    return [_decode(row) for row in await cur.fetchall()]
