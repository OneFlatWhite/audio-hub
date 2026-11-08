"""lackfill 补齐台账 CRUD。

状态机流转归 service 层，本模块只做台账记录的读写。
所有函数接收外部传入的 aiosqlite.Connection，不自建连接；
写操作在函数内部自行 commit（与 db.py 既有风格一致）；
时间戳一律由调用方以 now 参数传入（ISO 字符串）。
"""

from __future__ import annotations

import json

import aiosqlite


async def insert(
    conn: aiosqlite.Connection,
    *,
    show_key: str,
    season: str | None,
    missing: list[int] | dict[int, list[int]],
    share_code: str | None,
    package_name: str | None,
    now: str,
    emby_id: str = "",
    verify_round: int = 0,
    commit: bool = True,
) -> int:
    cur = await conn.execute(
        """
        INSERT INTO lackfill_ledger
            (show_key, season, missing_json, share_code, package_name,
             emby_id, status, verify_round, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (show_key, season, json.dumps(missing), share_code, package_name,
         emby_id, verify_round, now, now),
    )
    if commit:
        await conn.commit()
    return int(cur.lastrowid)


async def get(conn: aiosqlite.Connection, entry_id: int) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM lackfill_ledger WHERE id = ?",
        (entry_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return dict(row)


async def update_status(conn: aiosqlite.Connection, entry_id: int, status: str, now: str) -> None:
    await conn.execute(
        "UPDATE lackfill_ledger SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, entry_id),
    )
    await conn.commit()


async def update_missing(
    conn: aiosqlite.Connection,
    entry_id: int,
    missing: list[int] | dict[int, list[int]],
    now: str,
) -> None:
    await conn.execute(
        "UPDATE lackfill_ledger SET missing_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(missing), now, entry_id),
    )
    await conn.commit()


async def bump_round(conn: aiosqlite.Connection, entry_id: int, now: str) -> None:
    await conn.execute(
        "UPDATE lackfill_ledger SET verify_round = verify_round + 1, updated_at = ? WHERE id = ?",
        (now, entry_id),
    )
    await conn.commit()


async def reset_round(conn: aiosqlite.Connection, entry_id: int, now: str) -> None:
    await conn.execute(
        "UPDATE lackfill_ledger SET verify_round = 0, updated_at = ? WHERE id = ?",
        (now, entry_id),
    )
    await conn.commit()


async def latest_round(conn: aiosqlite.Connection, show_key: str) -> int:
    cur = await conn.execute(
        "SELECT COALESCE(MAX(verify_round), 0) FROM lackfill_ledger WHERE show_key = ?",
        (show_key,),
    )
    row = await cur.fetchone()
    return int(row[0])


async def list_by_status(conn: aiosqlite.Connection, *statuses: str) -> list[dict]:
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    cur = await conn.execute(
        f"SELECT * FROM lackfill_ledger WHERE status IN ({placeholders}) ORDER BY id",
        tuple(statuses),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def find_open_by_share(
    conn: aiosqlite.Connection, show_key: str, share_code: str
) -> dict | None:
    cur = await conn.execute(
        """
        SELECT * FROM lackfill_ledger
        WHERE show_key = ? AND share_code = ? AND status IN ('pending', 'partial')
        ORDER BY id DESC LIMIT 1
        """,
        (show_key, share_code),
    )
    row = await cur.fetchone()
    return dict(row) if row is not None else None
