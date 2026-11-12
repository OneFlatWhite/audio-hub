"""CD2 落地验证：真实目录/单文件/多文件、非空与稳定性、移动和 Emby 刷新。"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import aiosqlite

from .emby import EmbyClient
from .refill import validate_package_name


async def insert(
    conn: aiosqlite.Connection,
    ledger_id: int,
    package_name: str,
    mode: str,
    source_dir: str,
    feed_dir: str,
    now: str,
    *,
    commit: bool = True,
) -> int:
    if mode not in {"move", "verify", "move_file", "move_files"}:
        raise ValueError("未知 CD2 验证模式")
    package_name = validate_package_name(package_name)
    await conn.execute(
        """INSERT INTO lackfill_cloud
           (ledger_id, package_name, mode, source_dir, feed_dir, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'waiting', ?, ?)
           ON CONFLICT(ledger_id) DO NOTHING""",
        (ledger_id, package_name, mode, source_dir, feed_dir, now, now),
    )
    if commit:
        await conn.commit()
    row = await get_by_ledger(conn, ledger_id)
    return int(row["id"])


async def get_by_ledger(conn: aiosqlite.Connection, ledger_id: int) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM lackfill_cloud WHERE ledger_id = ?", (ledger_id,)
    )
    row = await cur.fetchone()
    return dict(row) if row is not None else None


async def list_by_status(conn: aiosqlite.Connection, *statuses: str) -> list[dict]:
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    cur = await conn.execute(
        f"SELECT * FROM lackfill_cloud WHERE status IN ({placeholders}) ORDER BY id",
        tuple(statuses),
    )
    return [dict(row) for row in await cur.fetchall()]


async def insert_files(
    conn: aiosqlite.Connection,
    ledger_id: int,
    file_names: list[str],
    source_dir: str,
    feed_dir: str,
    now: str,
    *,
    commit: bool = True,
) -> list[int]:
    ids = []
    for file_name in file_names:
        file_name = validate_package_name(file_name)
        await conn.execute(
            """INSERT INTO lackfill_cloud_files
               (ledger_id, file_name, source_dir, feed_dir, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'waiting', ?, ?)
               ON CONFLICT(ledger_id, file_name) DO NOTHING""",
            (ledger_id, file_name, source_dir, feed_dir, now, now),
        )
        cur = await conn.execute(
            "SELECT id FROM lackfill_cloud_files WHERE ledger_id = ? AND file_name = ?",
            (ledger_id, file_name),
        )
        ids.append((await cur.fetchone())[0])
    if commit:
        await conn.commit()
    return ids


async def list_files(conn: aiosqlite.Connection, ledger_id: int) -> list[dict]:
    cur = await conn.execute(
        "SELECT * FROM lackfill_cloud_files WHERE ledger_id = ? ORDER BY id", (ledger_id,)
    )
    return [dict(row) for row in await cur.fetchall()]


async def update_file_snapshot(conn, row: dict, snapshot: dict, stable_since: str | None, now: str) -> None:
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    await conn.execute(
        """UPDATE lackfill_cloud_files
           SET snapshot_json = ?, stable_since = ?, updated_at = ? WHERE id = ?""",
        (snapshot_json, stable_since, now, row["id"]),
    )
    row["snapshot_json"] = snapshot_json
    row["stable_since"] = stable_since
    row["updated_at"] = now
    await conn.commit()


async def update_file_status(
    conn, row: dict, status: str, now: str, error: str | None = None
) -> None:
    await conn.execute(
        "UPDATE lackfill_cloud_files SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error, now, row["id"]),
    )
    row["status"] = status
    row["error"] = error
    row["updated_at"] = now
    await conn.commit()


async def update_snapshot(
    conn: aiosqlite.Connection,
    entry_id: int,
    snapshot: dict,
    stable_since: str | None,
    now: str,
) -> None:
    await conn.execute(
        "UPDATE lackfill_cloud SET snapshot_json = ?, stable_since = ?, updated_at = ? WHERE id = ?",
        (json.dumps(snapshot, ensure_ascii=False), stable_since, now, entry_id),
    )
    await conn.commit()


async def update_status(
    conn: aiosqlite.Connection,
    entry_id: int,
    status: str,
    now: str,
    error: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE lackfill_cloud SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error, now, entry_id),
    )
    await conn.commit()


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _snapshot(package: Path, *, single_file: bool = False) -> dict:
    if single_file:
        if not package.is_file():
            return {}
        stat = package.stat()
        return {package.name: [stat.st_size, int(stat.st_mtime)]} if stat.st_size > 0 else {}
    if not package.is_dir():
        return {}
    result = {}
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        if stat.st_size <= 0:
            continue
        result[str(path.relative_to(package))] = [stat.st_size, int(stat.st_mtime)]
    return result


def _stable(
    row: dict,
    snapshot: dict,
    now: str,
    stable_seconds: int,
) -> tuple[bool, str | None]:
    if not snapshot:
        return False, now
    previous = json.loads(row["snapshot_json"] or "{}")
    stable_since = row["stable_since"]
    if previous != snapshot or not stable_since:
        return False, now
    newest_mtime = max(item[1] for item in snapshot.values())
    now_ts = _timestamp(now)
    if now_ts - newest_mtime < stable_seconds:
        return False, stable_since
    if now_ts - _timestamp(stable_since) < stable_seconds:
        return False, stable_since
    return True, stable_since


async def _wait(
    conn: aiosqlite.Connection,
    row: dict,
    now: str,
    stats: dict,
    error: str | None = None,
) -> None:
    stats["waiting"] += 1
    await update_status(conn, row["id"], "waiting", now, error)


async def _fail(
    conn: aiosqlite.Connection,
    row: dict,
    now: str,
    stats: dict,
    error: str,
) -> None:
    stats["failed"] += 1
    await update_status(conn, row["id"], "failed", now, error)


async def _ready(
    conn: aiosqlite.Connection,
    row: dict,
    now: str,
    stats: dict,
    emby,
) -> None:
    try:
        await emby.refresh_library()
    except Exception as exc:  # noqa: BLE001 —— 文件已落地，刷新失败下轮重试
        await _wait(conn, row, now, stats, f"Emby 刷新失败：{type(exc).__name__}")
        return
    stats["ready"] += 1
    stats["refreshed"] += 1
    await update_status(conn, row["id"], "ready", now)


async def _poll_file_group(conn, row: dict, now: str, stats: dict, emby, stable_seconds: int) -> None:
    files = await list_files(conn, row["ledger_id"])
    if not files:
        await _fail(conn, row, now, stats, "多文件资源未登记文件")
        return
    for item in files:
        if item["status"] in {"ready", "failed"}:
            continue
        source_root = Path(item["source_dir"])
        feed_root = Path(item["feed_dir"])
        if not source_root.is_dir() or not feed_root.is_dir():
            await update_file_status(conn, item, "waiting", now, "CD2 挂载目录不存在")
            continue
        source_file = source_root / item["file_name"]
        feed_file = feed_root / item["file_name"]
        if feed_file.exists():
            snapshot = _snapshot(feed_file, single_file=True)
            stable, stable_since = _stable(item, snapshot, now, stable_seconds)
            if stable:
                await update_file_status(conn, item, "ready", now)
            else:
                await update_file_snapshot(conn, item, snapshot, stable_since, now)
                await update_file_status(conn, item, "waiting", now)
            continue
        if not source_file.exists():
            await update_file_status(conn, item, "waiting", now)
            continue
        snapshot = _snapshot(source_file, single_file=True)
        stable, stable_since = _stable(item, snapshot, now, stable_seconds)
        if not stable:
            await update_file_snapshot(conn, item, snapshot, stable_since, now)
            await update_file_status(conn, item, "waiting", now)
            continue
        if feed_file.exists():
            await update_file_status(conn, item, "failed", now, "目标文件已存在，未覆盖")
            continue
        try:
            shutil.move(str(source_file), str(feed_file))
        except OSError as exc:
            await update_file_status(conn, item, "failed", now, f"文件移动失败：{type(exc).__name__}")
            continue
        if not _snapshot(feed_file, single_file=True):
            await update_file_status(conn, item, "failed", now, "移动后文件为空")
        else:
            await update_file_status(conn, item, "ready", now)
    if any(item["status"] == "failed" for item in files):
        await _fail(conn, row, now, stats, "多文件资源存在失败文件")
    elif all(item["status"] == "ready" for item in files):
        await _ready(conn, row, now, stats, emby)
    else:
        await _wait(conn, row, now, stats)


async def poll_cloud_once(
    conn: aiosqlite.Connection,
    cfg,
    now: str,
    *,
    emby=None,
) -> dict:
    stats = {"checked": 0, "waiting": 0, "ready": 0, "failed": 0, "refreshed": 0}
    if not getattr(cfg, "cloud_verify_enabled", False):
        stats["skipped"] = True
        return stats
    rows = await list_by_status(conn, "waiting")
    if not rows:
        return stats
    own_emby = emby is None
    if own_emby:
        emby = EmbyClient(cfg.emby_url, cfg.emby_api_key)
    stable_seconds = max(1, int(getattr(cfg, "cloud_stable_minutes", 5))) * 60
    try:
        for row in rows:
            stats["checked"] += 1
            try:
                if row["mode"] == "move_files":
                    await _poll_file_group(conn, row, now, stats, emby, stable_seconds)
                    continue
                source_root = Path(row["source_dir"])
                feed_root = Path(row["feed_dir"])
                if not source_root.is_dir() or not feed_root.is_dir():
                    await _wait(conn, row, now, stats, "CD2 挂载目录不存在")
                    continue
                feed_package = feed_root / row["package_name"]
                source_package = source_root / row["package_name"]
                single_file = row["mode"] == "move_file"
                if feed_package.exists():
                    snapshot = _snapshot(feed_package, single_file=single_file)
                    stable, stable_since = _stable(row, snapshot, now, stable_seconds)
                    if stable:
                        await _ready(conn, row, now, stats, emby)
                    else:
                        await update_snapshot(conn, row["id"], snapshot, stable_since, now)
                        await _wait(conn, row, now, stats)
                    continue
                if row["mode"] == "verify":
                    await _wait(conn, row, now, stats)
                    continue
                if not source_package.exists():
                    await _wait(conn, row, now, stats)
                    continue
                snapshot = _snapshot(source_package, single_file=single_file)
                stable, stable_since = _stable(row, snapshot, now, stable_seconds)
                if not stable:
                    await update_snapshot(conn, row["id"], snapshot, stable_since, now)
                    await _wait(conn, row, now, stats)
                    continue
                if feed_package.exists():
                    await _fail(conn, row, now, stats, "目标目录已存在，未覆盖")
                    continue
                shutil.move(str(source_package), str(feed_package))
                if not _snapshot(feed_package, single_file=single_file):
                    await _fail(conn, row, now, stats, "移动后目标包为空")
                    continue
                await _ready(conn, row, now, stats, emby)
            except (OSError, ValueError) as exc:
                await _wait(conn, row, now, stats, f"CD2 检查失败：{type(exc).__name__}")
    finally:
        if own_emby:
            await emby.aclose()
    return stats
