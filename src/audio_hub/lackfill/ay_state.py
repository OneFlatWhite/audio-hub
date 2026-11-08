"""AY 每剧退避与月度额度恢复，防止定时任务重复消耗限额。"""

from __future__ import annotations

from datetime import datetime, timedelta

_RETRY_MINUTES = 6 * 60
_MAX_RETRY_MINUTES = 7 * 24 * 60


def quota_exhausted(status: dict, now: str) -> bool:
    if status.get("quota_left") is None:
        return False
    try:
        if int(status.get("quota_left")) != 0:
            return False
        updated = datetime.fromisoformat(status["updated_at"])
        current = datetime.fromisoformat(now)
    except (KeyError, TypeError, ValueError):
        return True
    return (updated.year, updated.month) >= (current.year, current.month)


def _next_month(current: datetime) -> datetime:
    year, month = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
    return current.replace(year=year, month=month, day=1, hour=0, minute=10, second=0, microsecond=0)


async def is_due(conn, tmdb_id: int, now: str) -> bool:
    cur = await conn.execute(
        "SELECT next_retry_at FROM lackfill_ay_tasks WHERE tmdb_id = ?", (tmdb_id,)
    )
    row = await cur.fetchone()
    if row is None:
        return True
    if not row["next_retry_at"]:
        return False
    try:
        return datetime.fromisoformat(now) >= datetime.fromisoformat(row["next_retry_at"])
    except (TypeError, ValueError):
        return False


async def record_result(conn, tmdb_id: int, result: dict, now: str) -> None:
    status = result.get("status")
    if status == "ok":
        await conn.execute("DELETE FROM lackfill_ay_tasks WHERE tmdb_id = ?", (tmdb_id,))
        await conn.commit()
        return
    if status not in {"none", "failed"}:
        return
    cur = await conn.execute(
        "SELECT attempts FROM lackfill_ay_tasks WHERE tmdb_id = ?", (tmdb_id,)
    )
    previous = await cur.fetchone()
    attempts = (int(previous["attempts"]) if previous else 0) + 1
    current = datetime.fromisoformat(now)
    exhausted = result.get("quota_left") is not None and quota_exhausted(
        {"quota_left": result["quota_left"], "updated_at": now}, now
    )
    if exhausted:
        retry = _next_month(current)
        stored_status = "quota_exhausted"
    else:
        minutes = min(_RETRY_MINUTES * (2 ** (attempts - 1)), _MAX_RETRY_MINUTES)
        retry = current + timedelta(minutes=minutes)
        stored_status = status
    await conn.execute(
        """INSERT INTO lackfill_ay_tasks
           (tmdb_id, attempts, last_status, last_error, next_retry_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(tmdb_id) DO UPDATE SET
             attempts=excluded.attempts, last_status=excluded.last_status,
             last_error=excluded.last_error, next_retry_at=excluded.next_retry_at,
             updated_at=excluded.updated_at""",
        (
            tmdb_id, attempts, stored_status,
            str(result.get("reason") or "")[:500], retry.isoformat(), now,
        ),
    )
    await conn.commit()
