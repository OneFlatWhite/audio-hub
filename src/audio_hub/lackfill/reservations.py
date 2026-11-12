"""跨请求、跨 worker 的转存意图预留与崩溃恢复。"""

from __future__ import annotations

import aiosqlite


async def reserve(
    conn: aiosqlite.Connection, show_key: str, share_code: str, now: str
) -> str:
    """单条 SQL 原子预留，避免共享连接上的嵌套事务。"""
    cur = await conn.execute(
        """INSERT OR IGNORE INTO lackfill_reservations
           (show_key, share_code, stage, created_at, updated_at)
           SELECT ?, ?, 'reserved', ?, ?
           WHERE NOT EXISTS (
               SELECT 1 FROM lackfill_ledger
               WHERE show_key = ? AND status IN ('pending', 'partial')
           )""",
        (show_key, share_code, now, now, show_key),
    )
    inserted = cur.rowcount == 1
    await conn.commit()
    if inserted:
        return "reserved"
    if await get(conn, show_key) is not None:
        return "reservation"
    return "ledger"


async def get(conn: aiosqlite.Connection, show_key: str) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM lackfill_reservations WHERE show_key = ?", (show_key,)
    )
    row = await cur.fetchone()
    return dict(row) if row is not None else None


async def list_active(conn: aiosqlite.Connection) -> list[dict]:
    cur = await conn.execute(
        "SELECT * FROM lackfill_reservations ORDER BY created_at, show_key"
    )
    return [dict(row) for row in await cur.fetchall()]


async def mark_attempted(conn: aiosqlite.Connection, show_key: str, now: str) -> None:
    await conn.execute(
        """UPDATE lackfill_reservations SET stage = 'attempted', updated_at = ?
           WHERE show_key = ?""",
        (now, show_key),
    )
    await conn.commit()


async def release(
    conn: aiosqlite.Connection, show_key: str, *, commit: bool = True
) -> None:
    await conn.execute("DELETE FROM lackfill_reservations WHERE show_key = ?", (show_key,))
    if commit:
        await conn.commit()


async def release_attempted(conn: aiosqlite.Connection, show_key: str) -> bool:
    cur = await conn.execute(
        "DELETE FROM lackfill_reservations WHERE show_key = ? AND stage = 'attempted'",
        (show_key,),
    )
    await conn.commit()
    return cur.rowcount > 0


async def recover_unstarted(conn: aiosqlite.Connection) -> int:
    """reserved 从未进入外部调用可安全清理；attempted 必须人工核对。"""
    cur = await conn.execute("DELETE FROM lackfill_reservations WHERE stage = 'reserved'")
    await conn.commit()
    return max(cur.rowcount, 0)
