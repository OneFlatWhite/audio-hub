"""Re0 脚本心跳与每剧状态；只保存白名单字段，不保存 URL 或凭据。"""

from __future__ import annotations

import json
from datetime import datetime

_WORKER_KEY = "__lackfill_re0_worker"
_STATUSES = {
    "running", "waiting", "success", "challenge", "failed", "quota", "manual", "not_found",
}


def _text(payload: dict, key: str, limit: int) -> str:
    return str(payload.get(key) or "")[:limit]


async def report_status(conn, payload: dict, now: str) -> dict:
    status = _text(payload, "status", 30)
    if status not in _STATUSES:
        raise ValueError("未知 Re0 状态")
    raw_tmdb = payload.get("tmdb_id")
    tmdb_id = int(raw_tmdb) if raw_tmdb else None
    attempts = max(int(payload.get("attempts") or 0), 0)
    if tmdb_id is not None and status in {"failed", "not_found"} and payload.get("attempts") is None:
        cur = await conn.execute(
            "SELECT attempts FROM lackfill_re0_tasks WHERE tmdb_id = ?", (tmdb_id,)
        )
        previous = await cur.fetchone()
        attempts = (int(previous["attempts"]) if previous else 0) + 1
    worker = {
        "status": status,
        "tmdb_id": tmdb_id,
        "title": _text(payload, "title", 300),
        "reason": _text(payload, "reason", 500),
        "stage": _text(payload, "stage", 40),
        "attempts": attempts,
        "auto_running": bool(payload.get("auto_running")),
        "script_version": _text(payload, "script_version", 20),
        "queue_count": max(int(payload.get("queue_count") or 0), 0),
        "last_seen": now,
    }
    await conn.execute(
        """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (_WORKER_KEY, json.dumps(worker, ensure_ascii=False), now),
    )
    if tmdb_id is not None:
        await conn.execute(
            """INSERT INTO lackfill_re0_tasks
               (tmdb_id, status, stage, attempts, title, reason, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tmdb_id) DO UPDATE SET
                 status=excluded.status, stage=excluded.stage, attempts=excluded.attempts,
                 title=excluded.title, reason=excluded.reason, updated_at=excluded.updated_at""",
            (
                tmdb_id, status, worker["stage"], attempts,
                worker["title"], worker["reason"], now,
            ),
        )
    await conn.commit()
    return worker


async def worker_status(conn, now: str, stale_after: int = 180) -> dict:
    cur = await conn.execute("SELECT value FROM app_settings WHERE key = ?", (_WORKER_KEY,))
    row = await cur.fetchone()
    if row is None:
        return {
            "status": "offline", "online": False, "auto_running": False,
            "last_seen": None, "tmdb_id": None, "title": "", "reason": "",
            "stage": "", "attempts": 0, "script_version": "", "queue_count": 0,
        }
    try:
        value = json.loads(row["value"])
        if not isinstance(value, dict):
            raise TypeError("invalid worker state")
    except (TypeError, ValueError, json.JSONDecodeError):
        value = {}
    try:
        age = (datetime.fromisoformat(now) - datetime.fromisoformat(value["last_seen"])).total_seconds()
        online = 0 <= age <= stale_after
    except (KeyError, TypeError, ValueError):
        online = False
    result = {
        "status": value.get("status") if online else "offline",
        "online": online,
        "auto_running": bool(value.get("auto_running")),
        "last_seen": value.get("last_seen"),
        "tmdb_id": value.get("tmdb_id"),
        "title": str(value.get("title") or "")[:300],
        "reason": str(value.get("reason") or "")[:500],
        "stage": str(value.get("stage") or "")[:40],
        "attempts": max(int(value.get("attempts") or 0), 0),
        "script_version": str(value.get("script_version") or "")[:20],
        "queue_count": max(int(value.get("queue_count") or 0), 0),
    }
    return result


async def eligible_for_ay(conn, tmdb_id: int) -> bool:
    cur = await conn.execute(
        "SELECT status, attempts FROM lackfill_re0_tasks WHERE tmdb_id = ?", (tmdb_id,)
    )
    row = await cur.fetchone()
    return bool(row and (row["status"] == "not_found" or (row["status"] == "failed" and row["attempts"] >= 3)))
