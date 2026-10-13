"""DB 仓储层：watchlist / tasks / torrents 三表 + v0.4 七表的读写。

单独成层的原因：pipeline 与 monitor 都需要访问 DB，
把 SQL 收敛到一处，避免各模块散落裸 SQL。

幂等约定（PRD §13.6）：
- upsert_torrent_seen：info_hash 主键，重复插入静默忽略
- create_task：先查 info_hash 是否已有任务，有则复用
- add_series_part：(series_id, part_no) 唯一，重复插入静默忽略
- upsert_seed_record：task_id 主键，重复则覆写

v0.4 表的函数命名统一为 add_* / get_* / list_* / count_* / update_*，
列表函数一律支持 limit/offset 分页与按关键字段筛选。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from loguru import logger

from .config import DownloaderConfig, SiteConfig
from .models import (
    PREFERRED_QUALITIES,
    AuthStatus,
    DownloaderRecord,
    EventRecord,
    FoundVia,
    HitDecision,
    LibraryItem,
    PartEntry,
    QuarantineItem,
    RecycleEntry,
    RssTestResult,
    SeedRecord,
    SeriesEntry,
    SiteCategory,
    SiteRecord,
    SiteUserInfo,
    SubscriptionHit,
    SubStatus,
    SubType,
    Task,
    TaskState,
    TorrentRecord,
    WatchEntry,
    WatchKind,
    quality_rank,
)
from .timeutil import iso_utc, parse_iso, utcnow


def _now() -> str:
    return iso_utc()


def _iso(dt: datetime | None) -> str | None:
    """datetime → ISO 文本；None 透传（可空时间列用）。"""
    return dt.isoformat() if dt else None


def _dt(raw: str | None) -> datetime:
    """ISO 文本 → datetime；解析失败或为空时回落到当前时间（非空列用）。"""
    return parse_iso(raw) or utcnow()


def _where(conds: list[str]) -> str:
    return (" WHERE " + " AND ".join(conds)) if conds else ""


def _col(r, name: str, default=None):
    """读一列；列不存在（旧库尚未加列）或为 NULL 时回落 default。

    sqlite3.Row 对不存在的列抛 IndexError，aiosqlite 原样透传。
    """
    try:
        v = r[name]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def _csv(items: list[str] | None) -> str | None:
    """list → 逗号串（qb_tags 列）；空列表存 NULL。"""
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    return ",".join(items) if items else None


def _split_csv(raw: str | None) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _json_list(raw: str | None) -> list[str]:
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


# ── watchlist ───────────────────────────────────────────────

def _row_watch_entry(r) -> WatchEntry:
    """watchlist 行 → WatchEntry。v0.6 新列全部经 _col 读取，兼容未迁移的旧行。

    sub_type 列缺失 / 为空时不显式传入，交给 WatchEntry 的 validator 按 kind 推导。
    """
    pq = _col(r, "preferred_quality", "any")
    status_raw = _col(r, "status", SubStatus.ACTIVE.value)
    data: dict = {
        "id": r["id"],
        "kind": WatchKind(r["kind"]),
        "query": r["query"],
        "aliases": _json_list(r["aliases"]),
        "regex": r["regex"],
        "sites": _json_list(r["sites"]),
        "min_confidence": r["min_confidence"],
        "auto_download": bool(r["auto_download"]),
        "priority": r["priority"],
        "enabled": bool(r["enabled"]),
        "series_id": r["series_id"],
        "created_at": parse_iso(r["created_at"]) or utcnow(),
        "artist": _col(r, "artist", ""),
        "album": _col(r, "album", ""),
        "author": _col(r, "author", ""),
        "status": SubStatus(status_raw) if status_raw in SubStatus._value2member_map_
        else SubStatus.ACTIVE,
        "search_interval_hours": int(_col(r, "search_interval_hours", 6)),
        "last_search_at": parse_iso(_col(r, "last_search_at")),
        "next_search_at": parse_iso(_col(r, "next_search_at")),
        "completed_at": parse_iso(_col(r, "completed_at")),
        "upgrade_enabled": bool(_col(r, "upgrade_enabled", 0)),
        "min_seeders": int(_col(r, "min_seeders", 0)),
        "hit_count": int(_col(r, "hit_count", 0)),
        "downloader_id": _col(r, "downloader_id"),
        "save_path": _col(r, "save_path"),
        "qb_category": _col(r, "qb_category"),
        "qb_tags": _split_csv(_col(r, "qb_tags")),
        "preferred_quality": pq if pq in PREFERRED_QUALITIES else "any",
        "min_size_mb": _col(r, "min_size_mb"),
        "max_size_mb": _col(r, "max_size_mb"),
        "free_only": bool(_col(r, "free_only", 0)),
        "exclude_keywords": _json_list(_col(r, "exclude_keywords")),
        "include_keywords": _json_list(_col(r, "include_keywords")),
    }
    sub_type_raw = _col(r, "sub_type")
    if sub_type_raw in SubType._value2member_map_:
        data["sub_type"] = SubType(sub_type_raw)
    return WatchEntry(**data)


async def list_watch_entries(db, *, enabled_only: bool = True) -> list[WatchEntry]:
    """读取订阅项。"""
    sql = "SELECT * FROM watchlist"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY priority DESC, created_at ASC"

    async with db.execute(sql) as cur:
        rows = await cur.fetchall()
    return [_row_watch_entry(r) for r in rows]


async def add_watch_entry(db, entry: WatchEntry) -> str:
    """新增（或按 id 整行覆写）订阅项。返回 id。写入 v0.6 全部新列。"""
    if not entry.id:
        entry.id = str(uuid.uuid4())
    await db.execute(
        """INSERT OR REPLACE INTO watchlist
           (id, kind, query, aliases, regex, sites, min_confidence,
            auto_download, priority, enabled, series_id, created_at,
            sub_type, artist, album, author, status, search_interval_hours,
            last_search_at, next_search_at, completed_at, upgrade_enabled,
            min_seeders, hit_count, downloader_id, save_path, qb_category, qb_tags,
            preferred_quality, min_size_mb, max_size_mb, free_only,
            exclude_keywords, include_keywords)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,
                   ?,?,?,?,?,?,
                   ?,?,?,?,
                   ?,?,?,?,?,?,
                   ?,?,?,?,
                   ?,?)""",
        (
            entry.id, entry.kind.value, entry.query,
            json.dumps(entry.aliases, ensure_ascii=False),
            entry.regex,
            json.dumps(entry.sites, ensure_ascii=False),
            entry.min_confidence, int(entry.auto_download),
            entry.priority, int(entry.enabled), entry.series_id,
            entry.created_at.isoformat() if entry.created_at else _now(),
            entry.sub_type.value, entry.artist, entry.album, entry.author,
            entry.status.value, int(entry.search_interval_hours),
            _iso(entry.last_search_at), _iso(entry.next_search_at), _iso(entry.completed_at),
            int(entry.upgrade_enabled),
            int(entry.min_seeders), int(entry.hit_count), entry.downloader_id,
            entry.save_path, entry.qb_category, _csv(entry.qb_tags),
            entry.preferred_quality, entry.min_size_mb, entry.max_size_mb,
            int(entry.free_only),
            json.dumps(entry.exclude_keywords, ensure_ascii=False),
            json.dumps(entry.include_keywords, ensure_ascii=False),
        ),
    )
    await db.commit()
    logger.info(f"已添加订阅项: {entry.kind.value} / {entry.query!r} (id={entry.id[:8]})")
    return entry.id


async def delete_watch_entry(db, entry_id: str) -> bool:
    cur = await db.execute("DELETE FROM watchlist WHERE id = ?", (entry_id,))
    await db.commit()
    return cur.rowcount > 0


# ── torrents（去重指纹）─────────────────────────────────────

async def get_known_hashes(db, limit: int = 100_000) -> set[str]:
    """读取所有已见过的 info_hash，供 matcher 去重。

    这是去重的第一道闸（PRD §13.2 四层指纹的第 1 层）。
    """
    async with db.execute("SELECT info_hash FROM torrents LIMIT ?", (limit,)) as cur:
        rows = await cur.fetchall()
    return {r["info_hash"] for r in rows if r["info_hash"]}


async def upsert_torrent_seen(
    db, record: TorrentRecord
) -> bool:
    """记录已见种子。返回 True 表示新插入，False 表示已存在（幂等跳过）。"""
    if not record.info_hash:
        return False
    async with db.execute(
        "SELECT 1 FROM torrents WHERE info_hash = ?", (record.info_hash,)
    ) as cur:
        if await cur.fetchone():
            return False
    await db.execute(
        """INSERT INTO torrents (info_hash, site, title_normalized, first_seen_at, task_id)
           VALUES (?,?,?,?,?)""",
        (
            record.info_hash, record.site, record.title_normalized,
            record.first_seen_at.isoformat() if record.first_seen_at else _now(),
            record.task_id,
        ),
    )
    await db.commit()
    return True


# ── tasks（全链路状态机）────────────────────────────────────

async def create_task(db, task: Task) -> str:
    """创建任务。若同 info_hash 已有任务则复用其 id（幂等）。

    空 info_hash 不参与幂等查询：空串匹配会把所有缺哈希的种子坍缩成
    同一个任务（v0.7.6 前真实发生过：mteam search 的 infoHash 为 null、
    NexusPHP HTML 搜索页无哈希字段）。适配器层现保证合成伪哈希，此处兜底。
    """
    if not task.id:
        task.id = str(uuid.uuid4())

    if task.info_hash:
        async with db.execute(
            "SELECT id FROM tasks WHERE info_hash = ?", (task.info_hash,)
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            return existing["id"]

    await db.execute(
        """INSERT INTO tasks
           (id, watch_entry_id, info_hash, site, title, state, score, confidence,
            reject_reason, qb_category, qb_save_path, archived_path, error_message,
            download_url, size_bytes, quality, format_tags, site_category,
            series_id, part_no, validation_result, archive_decision,
            downloader_id, watch_id, album_key, quality_tag,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            task.id, task.watch_entry_id, task.info_hash, task.site, task.title,
            task.state.value, task.score, task.confidence, task.reject_reason,
            task.qb_category, task.qb_save_path, task.archived_path, task.error_message,
            task.download_url, task.size_bytes, task.quality,
            json.dumps(task.format_tags, ensure_ascii=False), task.site_category,
            task.series_id, task.part_no, task.validation_result, task.archive_decision,
            task.downloader_id, task.watch_id, task.album_key, task.quality_tag,
            task.created_at.isoformat() if task.created_at else _now(),
            _now(),
        ),
    )
    await db.commit()
    return task.id


async def set_task_state(
    db, task_id: str, state: TaskState | str, **fields
) -> None:
    """更新任务状态，可同时更新任意附加字段。"""
    state_val = state.value if isinstance(state, TaskState) else state
    allowed = {
        "score", "confidence", "reject_reason", "qb_category", "qb_save_path",
        "archived_path", "error_message", "download_url", "size_bytes",
        "quality", "site_category",
        "series_id", "part_no", "validation_result", "archive_decision",
        "downloader_id", "watch_id", "album_key", "quality_tag",
    }
    sets = ["state = ?", "updated_at = ?"]
    params: list = [state_val, _now()]
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            params.append(v)
        else:
            logger.warning(f"set_task_state 忽略未知字段: {k}")
    params.append(task_id)
    await db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
    await db.commit()


async def count_tasks_by_state(db) -> dict[str, int]:
    """按状态统计任务数，用于看板与可观测（PRD §13.7）。"""
    async with db.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state") as cur:
        rows = await cur.fetchall()
    return {r["state"]: r["n"] for r in rows}


# 待审项的 reject_reason 取值集合（matcher 产出），用于统计与查询。
# 抽成模块级常量，供 count_pending_review 与 web 层共用，避免判定逻辑漂移。
PENDING_REVIEW_REASONS = ("pending_review", "confidence_below", "auto_download_disabled")


async def count_pending_review(db) -> int:
    """待审队列条目数（导航栏徽标用）。

    待审 = state=scored 且 reject_reason 属于待审类（含 NULL）。
    与 web 层 _query_tasks(review_only=True) 用同一判据，避免两处不一致。
    """
    async with db.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE state='scored' "
        "AND (reject_reason IS NULL OR reject_reason LIKE 'pending_review%' "
        "     OR reject_reason LIKE 'confidence_below%' OR reject_reason='auto_download_disabled')"
    ) as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def record_rejections(db, rejections: list[tuple[Task, str]]) -> int:
    """批量记录被拒候选（含原因），供规则迭代分析（PRD §13.7）。"""
    n = 0
    for task, reason in rejections:
        task.state = TaskState.REJECTED
        task.reject_reason = reason
        await create_task(db, task)
        n += 1
    return n


# ── library_items（v0.4 资料库）────────────────────────────────

def _row_library_item(r) -> LibraryItem:
    return LibraryItem(
        id=r["id"], watch_id=r["watch_id"], task_id=r["task_id"],
        title_norm=r["title_norm"] or "",
        kind=WatchKind(r["kind"]) if r["kind"] else WatchKind.MUSIC,
        quality=r["quality"], quality_score=r["quality_score"] or 0,
        path=r["path"] or "", created_at=_dt(r["created_at"]),
    )


async def add_library_item(db, item: LibraryItem) -> int:
    """新增库内成品，返回自增 id。"""
    cur = await db.execute(
        """INSERT INTO library_items
           (watch_id, task_id, title_norm, kind, quality, quality_score, path, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (item.watch_id, item.task_id, item.title_norm, item.kind.value,
         item.quality, item.quality_score, item.path,
         _iso(item.created_at) or _now()),
    )
    await db.commit()
    item.id = cur.lastrowid
    return item.id


async def get_library_item(db, item_id: int) -> LibraryItem | None:
    async with db.execute("SELECT * FROM library_items WHERE id = ?", (item_id,)) as cur:
        r = await cur.fetchone()
    return _row_library_item(r) if r else None


async def find_library_item(db, watch_id: str | None, title_norm: str) -> LibraryItem | None:
    """归档决策用：按 (watch_id, title_norm) 找已入库的旧版本（取质量最高的一条）。"""
    async with db.execute(
        "SELECT * FROM library_items WHERE watch_id IS ? AND title_norm = ? "
        "ORDER BY quality_score DESC, id DESC LIMIT 1",
        (watch_id, title_norm),
    ) as cur:
        r = await cur.fetchone()
    return _row_library_item(r) if r else None


async def list_library_items(
    db, *, watch_id: str | None = None, kind: WatchKind | str | None = None,
    limit: int = 200, offset: int = 0,
) -> list[LibraryItem]:
    conds: list[str] = []
    params: list = []
    if watch_id is not None:
        conds.append("watch_id = ?")
        params.append(watch_id)
    if kind is not None:
        conds.append("kind = ?")
        params.append(kind.value if isinstance(kind, WatchKind) else kind)
    params += [limit, offset]
    async with db.execute(
        f"SELECT * FROM library_items{_where(conds)} ORDER BY created_at DESC, id DESC "
        "LIMIT ? OFFSET ?", params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_library_item(r) for r in rows]


async def count_library_items(db, *, kind: WatchKind | str | None = None) -> int:
    conds: list[str] = []
    params: list = []
    if kind is not None:
        conds.append("kind = ?")
        params.append(kind.value if isinstance(kind, WatchKind) else kind)
    async with db.execute(
        f"SELECT COUNT(*) AS n FROM library_items{_where(conds)}", params
    ) as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


_LIBRARY_UPDATABLE = {"task_id", "quality", "quality_score", "path", "title_norm"}


async def update_library_item(db, item_id: int, **fields) -> None:
    """升级替换后更新质量 / 路径等字段；未知字段记 warning 并忽略。"""
    sets, params = [], []
    for k, v in fields.items():
        if k in _LIBRARY_UPDATABLE:
            sets.append(f"{k} = ?")
            params.append(v)
        else:
            logger.warning(f"update_library_item 忽略未知字段: {k}")
    if not sets:
        return
    params.append(item_id)
    await db.execute(f"UPDATE library_items SET {', '.join(sets)} WHERE id = ?", params)
    await db.commit()


async def delete_library_item(db, item_id: int) -> bool:
    cur = await db.execute("DELETE FROM library_items WHERE id = ?", (item_id,))
    await db.commit()
    return cur.rowcount > 0


# ── recycle_entries（v0.4 回收）──────────────────────────────────

def _row_recycle(r) -> RecycleEntry:
    return RecycleEntry(
        id=r["id"], library_item_id=r["library_item_id"],
        original_path=r["original_path"] or "", recycle_path=r["recycle_path"] or "",
        reason=r["reason"], created_at=_dt(r["created_at"]),
        restored_at=parse_iso(r["restored_at"]) if r["restored_at"] else None,
    )


async def add_recycle_entry(db, entry: RecycleEntry) -> int:
    cur = await db.execute(
        """INSERT INTO recycle_entries
           (library_item_id, original_path, recycle_path, reason, created_at, restored_at)
           VALUES (?,?,?,?,?,?)""",
        (entry.library_item_id, entry.original_path, entry.recycle_path, entry.reason,
         _iso(entry.created_at) or _now(), _iso(entry.restored_at)),
    )
    await db.commit()
    entry.id = cur.lastrowid
    return entry.id


async def get_recycle_entry(db, entry_id: int) -> RecycleEntry | None:
    async with db.execute("SELECT * FROM recycle_entries WHERE id = ?", (entry_id,)) as cur:
        r = await cur.fetchone()
    return _row_recycle(r) if r else None


async def list_recycle_entries(
    db, *, restored: bool | None = None, limit: int = 200, offset: int = 0,
) -> list[RecycleEntry]:
    """restored=None 全部；False 仅未恢复（可清理）；True 仅已恢复。"""
    conds: list[str] = []
    if restored is True:
        conds.append("restored_at IS NOT NULL")
    elif restored is False:
        conds.append("restored_at IS NULL")
    async with db.execute(
        f"SELECT * FROM recycle_entries{_where(conds)} ORDER BY created_at DESC, id DESC "
        "LIMIT ? OFFSET ?", (limit, offset),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_recycle(r) for r in rows]


async def list_expired_recycle_entries(db, before: datetime) -> list[RecycleEntry]:
    """到期清理用：未恢复且 created_at 早于 before 的条目。"""
    async with db.execute(
        "SELECT * FROM recycle_entries WHERE restored_at IS NULL AND created_at < ? "
        "ORDER BY created_at ASC", (before.isoformat(),),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_recycle(r) for r in rows]


async def count_recycle_entries(db, *, restored: bool | None = None) -> int:
    conds: list[str] = []
    if restored is True:
        conds.append("restored_at IS NOT NULL")
    elif restored is False:
        conds.append("restored_at IS NULL")
    async with db.execute(f"SELECT COUNT(*) AS n FROM recycle_entries{_where(conds)}") as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def mark_recycle_restored(db, entry_id: int) -> None:
    await db.execute(
        "UPDATE recycle_entries SET restored_at = ? WHERE id = ?", (_now(), entry_id)
    )
    await db.commit()


async def delete_recycle_entry(db, entry_id: int) -> bool:
    cur = await db.execute("DELETE FROM recycle_entries WHERE id = ?", (entry_id,))
    await db.commit()
    return cur.rowcount > 0


# ── quarantine_items（v0.4 隔离区）────────────────────────────────

def _row_quarantine(r) -> QuarantineItem:
    return QuarantineItem(
        id=r["id"], task_id=r["task_id"], scratch_path=r["scratch_path"] or "",
        reasons=json.loads(r["reasons"] or "[]"), status=r["status"] or "held",
        created_at=_dt(r["created_at"]),
        resolved_at=parse_iso(r["resolved_at"]) if r["resolved_at"] else None,
    )


async def add_quarantine_item(db, item: QuarantineItem) -> int:
    cur = await db.execute(
        """INSERT INTO quarantine_items
           (task_id, scratch_path, reasons, status, created_at, resolved_at)
           VALUES (?,?,?,?,?,?)""",
        (item.task_id, item.scratch_path, json.dumps(item.reasons, ensure_ascii=False),
         item.status, _iso(item.created_at) or _now(), _iso(item.resolved_at)),
    )
    await db.commit()
    item.id = cur.lastrowid
    return item.id


async def get_quarantine_item(db, item_id: int) -> QuarantineItem | None:
    async with db.execute("SELECT * FROM quarantine_items WHERE id = ?", (item_id,)) as cur:
        r = await cur.fetchone()
    return _row_quarantine(r) if r else None


async def list_quarantine_items(
    db, *, status: str | None = None, limit: int = 200, offset: int = 0,
) -> list[QuarantineItem]:
    conds: list[str] = []
    params: list = []
    if status:
        conds.append("status = ?")
        params.append(status)
    params += [limit, offset]
    async with db.execute(
        f"SELECT * FROM quarantine_items{_where(conds)} ORDER BY created_at DESC, id DESC "
        "LIMIT ? OFFSET ?", params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_quarantine(r) for r in rows]


async def count_quarantine_items(db, *, status: str | None = "held") -> int:
    """默认只数 held（导航栏徽标用）；status=None 数全部。"""
    conds: list[str] = []
    params: list = []
    if status:
        conds.append("status = ?")
        params.append(status)
    async with db.execute(
        f"SELECT COUNT(*) AS n FROM quarantine_items{_where(conds)}", params
    ) as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def set_quarantine_status(db, item_id: int, status: str) -> None:
    """held → released / discarded；非 held 状态自动打上 resolved_at。"""
    resolved = None if status == "held" else _now()
    await db.execute(
        "UPDATE quarantine_items SET status = ?, resolved_at = ? WHERE id = ?",
        (status, resolved, item_id),
    )
    await db.commit()


# ── series / series_parts（v0.4 连载）───────────────────────────────

def _row_series(r) -> SeriesEntry:
    return SeriesEntry(
        id=r["id"], watch_id=r["watch_id"], name=r["name"] or "",
        total_parts=r["total_parts"], part_pattern=r["part_pattern"],
        last_part=r["last_part"] or 0, status=r["status"] or "active",
        created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),
    )


async def add_series(db, series: SeriesEntry) -> int:
    cur = await db.execute(
        """INSERT INTO series
           (watch_id, name, total_parts, part_pattern, last_part, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (series.watch_id, series.name, series.total_parts, series.part_pattern,
         series.last_part, series.status, _iso(series.created_at) or _now(), _now()),
    )
    await db.commit()
    series.id = cur.lastrowid
    return series.id


async def get_series(db, series_id: int) -> SeriesEntry | None:
    async with db.execute("SELECT * FROM series WHERE id = ?", (series_id,)) as cur:
        r = await cur.fetchone()
    return _row_series(r) if r else None


async def get_series_by_watch(db, watch_id: str) -> SeriesEntry | None:
    """一个订阅项最多绑一个连载；多条时取最新。"""
    async with db.execute(
        "SELECT * FROM series WHERE watch_id = ? ORDER BY id DESC LIMIT 1", (watch_id,)
    ) as cur:
        r = await cur.fetchone()
    return _row_series(r) if r else None


async def list_series(
    db, *, status: str | None = None, limit: int = 200, offset: int = 0,
) -> list[SeriesEntry]:
    conds: list[str] = []
    params: list = []
    if status:
        conds.append("status = ?")
        params.append(status)
    params += [limit, offset]
    async with db.execute(
        f"SELECT * FROM series{_where(conds)} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_series(r) for r in rows]


async def count_series(db, *, status: str | None = None) -> int:
    conds: list[str] = []
    params: list = []
    if status:
        conds.append("status = ?")
        params.append(status)
    async with db.execute(f"SELECT COUNT(*) AS n FROM series{_where(conds)}", params) as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


_SERIES_UPDATABLE = {"name", "total_parts", "part_pattern", "last_part", "status"}


async def update_series(db, series_id: int, **fields) -> None:
    """更新连载元数据（last_part / status / total_parts 等），自动刷新 updated_at。"""
    sets, params = ["updated_at = ?"], [_now()]
    for k, v in fields.items():
        if k in _SERIES_UPDATABLE:
            sets.append(f"{k} = ?")
            params.append(v)
        else:
            logger.warning(f"update_series 忽略未知字段: {k}")
    params.append(series_id)
    await db.execute(f"UPDATE series SET {', '.join(sets)} WHERE id = ?", params)
    await db.commit()


def _row_part(r) -> PartEntry:
    return PartEntry(
        id=r["id"], series_id=r["series_id"], part_no=r["part_no"],
        task_id=r["task_id"], title=r["title"], created_at=_dt(r["created_at"]),
    )


async def add_series_part(db, part: PartEntry) -> bool:
    """登记连载的一部。返回 True 表示新插入，False 表示 (series_id, part_no) 已存在。

    新插入时同步抬高 series.last_part（取 max）。
    """
    cur = await db.execute(
        """INSERT OR IGNORE INTO series_parts (series_id, part_no, task_id, title, created_at)
           VALUES (?,?,?,?,?)""",
        (part.series_id, part.part_no, part.task_id, part.title,
         _iso(part.created_at) or _now()),
    )
    inserted = cur.rowcount > 0
    if inserted:
        part.id = cur.lastrowid
        await db.execute(
            "UPDATE series SET last_part = MAX(last_part, ?), updated_at = ? WHERE id = ?",
            (part.part_no, _now(), part.series_id),
        )
    await db.commit()
    return inserted


async def list_series_parts(db, series_id: int) -> list[PartEntry]:
    async with db.execute(
        "SELECT * FROM series_parts WHERE series_id = ? ORDER BY part_no ASC", (series_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [_row_part(r) for r in rows]


async def count_series_parts(db, series_id: int | None = None) -> int:
    conds: list[str] = []
    params: list = []
    if series_id is not None:
        conds.append("series_id = ?")
        params.append(series_id)
    async with db.execute(
        f"SELECT COUNT(*) AS n FROM series_parts{_where(conds)}", params
    ) as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def count_parts_by_series(db) -> dict[int, int]:
    """/series 页用：一次查出每个连载的已登记分卷数。"""
    async with db.execute(
        "SELECT series_id, COUNT(*) AS n FROM series_parts GROUP BY series_id"
    ) as cur:
        rows = await cur.fetchall()
    return {r["series_id"]: r["n"] for r in rows}


# ── seed_records（v0.4 做种 / H&R）────────────────────────────────

def _row_seed(r) -> SeedRecord:
    return SeedRecord(
        task_id=r["task_id"], info_hash=r["info_hash"] or "", site=r["site"] or "",
        started_at=_dt(r["started_at"]), seed_seconds=r["seed_seconds"] or 0,
        ratio=r["ratio"] or 0.0, required_seconds=r["required_seconds"],
        required_ratio=r["required_ratio"], status=r["status"] or "seeding",
        hr_satisfied_at=parse_iso(r["hr_satisfied_at"]) if r["hr_satisfied_at"] else None,
        updated_at=_dt(r["updated_at"]),
    )


async def upsert_seed_record(db, rec: SeedRecord) -> None:
    """按 task_id 插入或覆写（做种轮询每轮整行回写）。"""
    await db.execute(
        """INSERT OR REPLACE INTO seed_records
           (task_id, info_hash, site, started_at, seed_seconds, ratio,
            required_seconds, required_ratio, status, hr_satisfied_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (rec.task_id, rec.info_hash, rec.site, _iso(rec.started_at) or _now(),
         rec.seed_seconds, rec.ratio, rec.required_seconds, rec.required_ratio,
         rec.status, _iso(rec.hr_satisfied_at), _now()),
    )
    await db.commit()


async def get_seed_record(db, task_id: str) -> SeedRecord | None:
    async with db.execute("SELECT * FROM seed_records WHERE task_id = ?", (task_id,)) as cur:
        r = await cur.fetchone()
    return _row_seed(r) if r else None


async def list_seed_records(
    db, *, status: str | None = None, site: str | None = None,
    limit: int = 200, offset: int = 0,
) -> list[SeedRecord]:
    conds: list[str] = []
    params: list = []
    if status:
        conds.append("status = ?")
        params.append(status)
    if site:
        conds.append("site = ?")
        params.append(site)
    params += [limit, offset]
    async with db.execute(
        f"SELECT * FROM seed_records{_where(conds)} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_seed(r) for r in rows]


async def count_seed_records(db, *, status: str | None = None) -> int:
    conds: list[str] = []
    params: list = []
    if status:
        conds.append("status = ?")
        params.append(status)
    async with db.execute(
        f"SELECT COUNT(*) AS n FROM seed_records{_where(conds)}", params
    ) as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


_SEED_UPDATABLE = {
    "seed_seconds", "ratio", "required_seconds", "required_ratio", "status", "hr_satisfied_at",
}


async def update_seed_record(db, task_id: str, **fields) -> None:
    """局部更新做种进度 / 状态，自动刷新 updated_at。hr_satisfied_at 可传 datetime。"""
    sets, params = ["updated_at = ?"], [_now()]
    for k, v in fields.items():
        if k in _SEED_UPDATABLE:
            sets.append(f"{k} = ?")
            params.append(v.isoformat() if isinstance(v, datetime) else v)
        else:
            logger.warning(f"update_seed_record 忽略未知字段: {k}")
    params.append(task_id)
    await db.execute(f"UPDATE seed_records SET {', '.join(sets)} WHERE task_id = ?", params)
    await db.commit()


# ── events（v0.4 事件流）────────────────────────────────────────

def _row_event(r) -> EventRecord:
    return EventRecord(
        id=r["id"], kind=r["kind"] or "", level=r["level"] or "info", site=r["site"],
        title=r["title"], detail=r["detail"], created_at=_dt(r["created_at"]),
    )


async def add_event(db, ev: EventRecord) -> int:
    cur = await db.execute(
        "INSERT INTO events (kind, level, site, title, detail, created_at) VALUES (?,?,?,?,?,?)",
        (ev.kind, ev.level, ev.site, ev.title, ev.detail, _iso(ev.created_at) or _now()),
    )
    await db.commit()
    ev.id = cur.lastrowid
    return ev.id


async def get_event(db, event_id: int) -> EventRecord | None:
    async with db.execute("SELECT * FROM events WHERE id = ?", (event_id,)) as cur:
        r = await cur.fetchone()
    return _row_event(r) if r else None


async def list_events(
    db, *, kind: str | None = None, level: str | None = None, site: str | None = None,
    limit: int = 200, offset: int = 0,
) -> list[EventRecord]:
    conds: list[str] = []
    params: list = []
    if kind:
        conds.append("kind = ?")
        params.append(kind)
    if level:
        conds.append("level = ?")
        params.append(level)
    if site:
        conds.append("site = ?")
        params.append(site)
    params += [limit, offset]
    async with db.execute(
        f"SELECT * FROM events{_where(conds)} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_event(r) for r in rows]


async def count_events(db, *, kind: str | None = None, level: str | None = None) -> int:
    conds: list[str] = []
    params: list = []
    if kind:
        conds.append("kind = ?")
        params.append(kind)
    if level:
        conds.append("level = ?")
        params.append(level)
    async with db.execute(f"SELECT COUNT(*) AS n FROM events{_where(conds)}", params) as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def count_events_by_kind(db) -> dict[str, int]:
    """/events 页筛选标签用。"""
    async with db.execute("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind") as cur:
        rows = await cur.fetchall()
    return {r["kind"]: r["n"] for r in rows}


async def purge_events_before(db, before: datetime) -> int:
    """清理旧事件，返回删除行数。"""
    cur = await db.execute("DELETE FROM events WHERE created_at < ?", (before.isoformat(),))
    await db.commit()
    return cur.rowcount


# ---- seeding ----

# SQLite 单条语句的绑定参数上限（旧版 999），按此分块 IN 查询
_IN_CHUNK = 500


async def get_task_briefs(
    db, *, task_ids: list[str] | None = None, info_hashes: list[str] | None = None,
) -> list[dict]:
    """做种轮询 / 做种页用：按 task_id 或 info_hash 批量取任务摘要。

    返回 dict 列表（键 id / info_hash / site / title / state / downloader_id），不构造完整 Task，
    因为调用方只需要把 qb 里的 hash 关联到任务并拿标题展示。
    downloader_id 为 NULL 的旧任务归属 "default"（多下载器时做种轮询据此分流）。
    两个条件都为空时返回空列表（避免误扫全表）。
    """
    if task_ids:
        col, keys = "id", list(dict.fromkeys(task_ids))
    elif info_hashes:
        col, keys = "info_hash", list(dict.fromkeys(info_hashes))
    else:
        return []

    out: list[dict] = []
    for i in range(0, len(keys), _IN_CHUNK):
        chunk = keys[i:i + _IN_CHUNK]
        async with db.execute(
            f"SELECT id, info_hash, site, title, state, downloader_id FROM tasks "
            f"WHERE {col} IN ({','.join('?' * len(chunk))})",
            chunk,
        ) as cur:
            rows = await cur.fetchall()
        out.extend({
            "id": r["id"], "info_hash": r["info_hash"] or "",
            "site": r["site"] or "", "title": r["title"] or "", "state": r["state"] or "",
            "downloader_id": r["downloader_id"] or "default",
        } for r in rows)
    return out


# ---- series ----

async def get_watch_entry(db, entry_id: str) -> WatchEntry | None:
    """按 id 取单个订阅项（连载绑定时校验用）。"""
    async with db.execute("SELECT * FROM watchlist WHERE id = ?", (entry_id,)) as cur:
        r = await cur.fetchone()
    return _row_watch_entry(r) if r else None


async def set_watch_series(db, watch_id: str, series_id: int | None) -> None:
    """绑定 / 解绑订阅项的连载（series_id=None 为解绑）。"""
    await db.execute("UPDATE watchlist SET series_id = ? WHERE id = ?", (series_id, watch_id))
    await db.commit()


async def list_part_numbers_by_series(db) -> dict[int, set[int]]:
    """matcher 分卷去重用：series_id → 已登记的 part_no 集合。"""
    async with db.execute("SELECT series_id, part_no FROM series_parts") as cur:
        rows = await cur.fetchall()
    out: dict[int, set[int]] = {}
    for r in rows:
        if r["series_id"] is None or r["part_no"] is None:
            continue
        out.setdefault(r["series_id"], set()).add(r["part_no"])
    return out


async def delete_series(db, series_id: int) -> bool:
    """删除连载及其分卷，并解绑引用它的订阅项。返回是否确有删除。"""
    await db.execute("DELETE FROM series_parts WHERE series_id = ?", (series_id,))
    await db.execute("UPDATE watchlist SET series_id = NULL WHERE series_id = ?", (series_id,))
    cur = await db.execute("DELETE FROM series WHERE id = ?", (series_id,))
    await db.commit()
    return cur.rowcount > 0


# ---- archive/quarantine ----

def _row_task(r) -> Task:
    return Task(
        id=r["id"], watch_entry_id=r["watch_entry_id"], info_hash=r["info_hash"] or "",
        site=r["site"] or "", title=r["title"] or "",
        state=TaskState(r["state"]) if r["state"] else TaskState.DISCOVERED,
        score=r["score"] or 0.0, confidence=r["confidence"] or "high",
        reject_reason=r["reject_reason"], qb_category=r["qb_category"],
        qb_save_path=r["qb_save_path"], archived_path=r["archived_path"],
        error_message=r["error_message"], download_url=r["download_url"],
        size_bytes=r["size_bytes"] or 0, quality=r["quality"],
        format_tags=json.loads(r["format_tags"] or "[]"), site_category=r["site_category"],
        series_id=r["series_id"], part_no=r["part_no"],
        validation_result=r["validation_result"], archive_decision=r["archive_decision"],
        downloader_id=_col(r, "downloader_id"), watch_id=_col(r, "watch_id"),
        album_key=_col(r, "album_key"), quality_tag=_col(r, "quality_tag"),
        created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),
    )


async def get_task(db, task_id: str) -> Task | None:
    """归档链路 / 隔离区动作用：按 id 取完整 Task。"""
    async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
        r = await cur.fetchone()
    return _row_task(r) if r else None


async def get_watch_kind(db, watch_id: str) -> WatchKind | None:
    """归档落位用：订阅项的类型（music / audiobook）；订阅项已删返回 None。"""
    async with db.execute("SELECT kind FROM watchlist WHERE id = ?", (watch_id,)) as cur:
        r = await cur.fetchone()
    if r is None or not r["kind"]:
        return None
    try:
        return WatchKind(r["kind"])
    except ValueError:
        return None


async def update_quarantine_path(db, item_id: int, scratch_path: str) -> None:
    """隔离放行归档失败后，目录已在 scratch：更新条目路径以便重试。"""
    await db.execute(
        "UPDATE quarantine_items SET scratch_path = ? WHERE id = ?", (scratch_path, item_id),
    )
    await db.commit()


# ---- site_stats ----
#
# 站点健康计数与熔断。表在 v2 migration 建好，写入点在 pipeline/search.py。
# 全部用 upsert：站点首次出现时自动建行，不要求调用方先初始化。
# 时间全部 UTC-aware ISO 文本，与全库约定一致；调用方可传 now 以便测试推进时钟。

#: 熔断默认值（与 config.SearchConfig 同值）；调用方通常从 config 读后传入
_CIRCUIT_FAIL_THRESHOLD = 3
_CIRCUIT_OPEN_MINUTES = 30


def _row_site_stats(r) -> dict:
    return {
        "site": r["site"],
        "last_success_at": r["last_success_at"],
        "last_error": r["last_error"],
        "last_count": r["last_count"] or 0,
        "consecutive_fail": r["consecutive_fail"] or 0,
        "circuit_open_until": r["circuit_open_until"],
        "total_requests": r["total_requests"] or 0,
        "total_errors": r["total_errors"] or 0,
    }


async def get_site_stats(db, site: str) -> dict | None:
    """单站计数；从未写过返回 None。"""
    async with db.execute("SELECT * FROM site_stats WHERE site = ?", (site,)) as cur:
        r = await cur.fetchone()
    return _row_site_stats(r) if r else None


async def list_site_stats(db) -> dict[str, dict]:
    """全部站点计数：{site: row}，供 /sites 与总览合并配置展示。"""
    async with db.execute("SELECT * FROM site_stats ORDER BY site") as cur:
        rows = await cur.fetchall()
    return {r["site"]: _row_site_stats(r) for r in rows}


async def record_site_success(
    db, site: str, *, latency_ms: int | None = None, rows: int | None = None,
    now: datetime | None = None,
) -> None:
    """一次成功请求：total_requests+1，连续失败清零，关熔断，记上次成功时间与条数。

    latency_ms 现阶段只入日志不入表（表无对应列），保留参数位以免日后加列时改调用方。
    rows 为 None 时不动 last_count（成功但不知条数，如只是连通性探测）。
    """
    ts = now.isoformat(timespec="seconds") if now else _now()
    await db.execute(
        """INSERT INTO site_stats
             (site, last_success_at, last_count, consecutive_fail, circuit_open_until,
              total_requests, total_errors)
           VALUES (?, ?, ?, 0, NULL, 1, 0)
           ON CONFLICT(site) DO UPDATE SET
             last_success_at    = excluded.last_success_at,
             last_count         = COALESCE(?, site_stats.last_count),
             consecutive_fail   = 0,
             circuit_open_until = NULL,
             total_requests     = site_stats.total_requests + 1""",
        (site, ts, rows or 0, rows),
    )
    await db.commit()
    if latency_ms is not None:
        logger.debug(f"{site} 请求成功（{latency_ms} ms，{rows} 条）")


async def record_site_failure(
    db, site: str, error: str, *, now: datetime | None = None,
    threshold: int = _CIRCUIT_FAIL_THRESHOLD, open_minutes: int = _CIRCUIT_OPEN_MINUTES,
) -> bool:
    """一次失败请求：total_requests+1，total_errors+1，consecutive_fail+1，记 last_error。

    连续失败达到 threshold 时置 circuit_open_until = now + open_minutes。
    返回本次是否**刚好**打开熔断（未开/已过期 → 开），调用方据此只发一次
    site_circuit_open 事件；熔断期内的后续失败仍计数、仍延长熔断，但不重复报警。

    计数与阈值判定在同一条 UPSERT 的 SQL 里完成（site_stats.consecutive_fail + 1），
    同站多订阅项并发失败时不会因读-改-写交错丢计数或漏熔断；
    前后两次读只用于判断「是否刚打开」，最坏情况是重复发一次事件。
    """
    at = now or utcnow()
    threshold = max(int(threshold), 1)
    open_minutes = max(int(open_minutes), 1)
    open_until = (at + timedelta(minutes=open_minutes)).isoformat(timespec="seconds")

    prev = await get_site_stats(db, site)
    was_open = bool(prev and prev["circuit_open_until"]) and (
        (parse_iso(prev["circuit_open_until"]) or at) > at
    )

    await db.execute(
        """INSERT INTO site_stats
             (site, last_error, consecutive_fail, circuit_open_until,
              total_requests, total_errors)
           VALUES (?, ?, 1, CASE WHEN 1 >= ? THEN ? ELSE NULL END, 1, 1)
           ON CONFLICT(site) DO UPDATE SET
             last_error         = excluded.last_error,
             consecutive_fail   = site_stats.consecutive_fail + 1,
             circuit_open_until = CASE WHEN site_stats.consecutive_fail + 1 >= ?
                                       THEN ? ELSE site_stats.circuit_open_until END,
             total_requests     = site_stats.total_requests + 1,
             total_errors       = site_stats.total_errors + 1""",
        (site, (error or "")[:500], threshold, open_until, threshold, open_until),
    )
    await db.commit()

    cur = await get_site_stats(db, site)
    now_open = bool(cur and cur["circuit_open_until"]) and (
        (parse_iso(cur["circuit_open_until"]) or at) > at
    )
    just_opened = now_open and not was_open
    if just_opened:
        logger.warning(
            f"{site} 连续失败 {cur['consecutive_fail']} 次，"
            f"熔断至 {cur['circuit_open_until']}（{open_minutes} 分钟）"
        )
    return just_opened


async def is_circuit_open(db, site: str, now: datetime | None = None) -> bool:
    """该站是否处于熔断期。到期视为已关（不在这里改表，下次成功会清掉）。"""
    async with db.execute(
        "SELECT circuit_open_until FROM site_stats WHERE site = ?", (site,),
    ) as cur:
        r = await cur.fetchone()
    if not r or not r["circuit_open_until"]:
        return False
    until = parse_iso(r["circuit_open_until"])
    return until is not None and until > (now or utcnow())


# ---- 凭据加解密（sites / downloaders 共用）----
#
# *_enc 列存 Fernet token。解密失败（换了密钥文件）只记日忘并当作未配置，
# 与 settings_store.hydrate_config 的容错策略一致：让服务能起来重填，而不是死锁。

def _enc(plain: str | None) -> str | None:
    """明文 → token；空值存 NULL。"""
    if not plain:
        return None
    from .crypto import encrypt_value
    return encrypt_value(str(plain))


def _dec(token: str | None, *, label: str = "") -> str:
    """token → 明文；NULL / 解密失败返回空串。"""
    if not token:
        return ""
    from .crypto import decrypt_value
    try:
        return decrypt_value(token)
    except Exception as e:  # noqa: BLE001 - 单项解密失败不影响其它站点
        logger.error(f"凭据 {label or '?'} 解密失败，视为未配置: {type(e).__name__}: {e}")
        return ""


def _json_or_none(obj) -> str | None:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, ensure_ascii=False)


def _load_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# ---- sites ----
#
# v0.6 站点中心：sites 表是站点配置的唯一权威源（YAML 仅首启种子，见 registry_store）。
# 凭据列 cookie_enc / rss_url_enc / api_key_enc 永不随 SiteRecord 返回明文；
# 需要明文的唯一场景是构建适配器，走 site_config_from_row / get_site_secret。

#: get_site_secret 合法字段 → 列名
SITE_SECRET_COLUMNS = {
    "cookie": "cookie_enc",
    "rss_url": "rss_url_enc",
    "api_key": "api_key_enc",
}


def site_record_from_row(r) -> SiteRecord:
    """sites 行 → SiteRecord（不含明文，凭据只给 has_* 布尔）。"""
    auth_raw = r["auth_status"] or AuthStatus.UNCONFIGURED.value
    user_info = _load_json(r["user_info_json"])
    rss_test = _load_json(r["last_rss_test_json"])
    return SiteRecord(
        id=r["id"], name=r["name"],
        adapter_type=r["adapter_type"] or "nexusphp",
        enabled=bool(r["enabled"]),
        domain=r["domain"] or "", user_agent=r["user_agent"] or "",
        search_url=r["search_url"] or "", proxy=r["proxy"] or "",
        priority=int(r["priority"] or 0),
        music_cat_ids=_split_csv(r["music_cat_ids"]),
        audiobook_cat_ids=_split_csv(r["audiobook_cat_ids"]),
        max_rows=int(r["max_rows"] or 50),
        title_bracket_style=r["title_bracket_style"] or "【】",
        hr_min_seed_hours=r["hr_min_seed_hours"], hr_min_ratio=r["hr_min_ratio"],
        auth_status=AuthStatus(auth_raw) if auth_raw in AuthStatus._value2member_map_
        else AuthStatus.ERROR,
        auth_checked_at=parse_iso(r["auth_checked_at"]),
        auth_error=r["auth_error"],
        user_info=SiteUserInfo(**user_info) if isinstance(user_info, dict) else None,
        last_rss_test=RssTestResult(**rss_test) if isinstance(rss_test, dict) else None,
        has_cookie=bool(r["cookie_enc"]),
        has_api_key=bool(r["api_key_enc"]),
        has_rss=bool(r["rss_url_enc"]),
        created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),
    )


def site_config_from_row(r) -> SiteConfig:
    """sites 行 → SiteConfig（**含解密后的凭据明文**），仅供适配器构建与做种轮询使用。"""
    return SiteConfig(
        name=r["name"], enabled=bool(r["enabled"]),
        rss_url=_dec(r["rss_url_enc"], label=f"{r['id']}.rss_url"),
        search_url=r["search_url"] or "",
        api_key=_dec(r["api_key_enc"], label=f"{r['id']}.api_key"),
        music_cat_id=r["music_cat_ids"] or "",
        audiobook_cat_id=r["audiobook_cat_ids"] or "",
        max_rows=int(r["max_rows"] or 50),
        title_bracket_style=r["title_bracket_style"] or "【】",
        hr_min_seed_hours=r["hr_min_seed_hours"], hr_min_ratio=r["hr_min_ratio"],
        cookie=_dec(r["cookie_enc"], label=f"{r['id']}.cookie"),
        user_agent=r["user_agent"] or "", proxy=r["proxy"] or "",
        domain=r["domain"] or "",
        auth_status=r["auth_status"] or AuthStatus.UNCONFIGURED.value,
        priority=int(r["priority"] or 0),
    )


async def list_site_rows(db) -> list:
    """全部 sites 原始行（含 *_enc 列），供 registry_store 解密成 SiteConfig。"""
    async with db.execute("SELECT * FROM sites ORDER BY priority DESC, name") as cur:
        return list(await cur.fetchall())


async def list_sites(db, *, enabled_only: bool = False) -> list[SiteRecord]:
    """站点列表（不含凭据明文），优先级高的在前。"""
    sql = "SELECT * FROM sites"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY priority DESC, name"
    async with db.execute(sql) as cur:
        rows = await cur.fetchall()
    return [site_record_from_row(r) for r in rows]


async def get_site(db, site_id: str) -> SiteRecord | None:
    async with db.execute("SELECT * FROM sites WHERE id = ?", (site_id,)) as cur:
        r = await cur.fetchone()
    return site_record_from_row(r) if r else None


async def site_exists(db, site_id: str) -> bool:
    async with db.execute("SELECT 1 FROM sites WHERE id = ?", (site_id,)) as cur:
        return (await cur.fetchone()) is not None


async def upsert_site(
    db, cfg: SiteConfig, *, site_id: str | None = None,
    adapter_type: str | None = None, overwrite_empty_secrets: bool = False,
) -> str:
    """按 SiteConfig 写入 sites（存在则更新）。返回 site id。

    - site_id 省略时用 cfg.name（调用方应先用 adapters.registry.resolve_site_name 归一）。
    - 凭据（cookie / rss_url / api_key）非空才加密写入；空值默认**保留库中已有值**
      （界面「留空＝不修改」的约定）；overwrite_empty_secrets=True 时空值清除凭据。
    - 更新时不动 auth_* / user_info / last_rss_test（由 set_site_auth / set_site_rss_test 维护），
      也不动 adapter_type（除非显式传入）。
    """
    sid = site_id or cfg.name
    async with db.execute("SELECT * FROM sites WHERE id = ?", (sid,)) as cur:
        existing = await cur.fetchone()

    def _secret(plain: str, col: str) -> str | None:
        if plain:
            return _enc(plain)
        if existing is not None and not overwrite_empty_secrets:
            return existing[col]
        return None

    cookie_enc = _secret(cfg.cookie, "cookie_enc")
    rss_enc = _secret(cfg.rss_url, "rss_url_enc")
    api_enc = _secret(cfg.api_key, "api_key_enc")
    now = _now()
    a_type = adapter_type or (existing["adapter_type"] if existing is not None else None) \
        or "nexusphp"
    auth_status = cfg.auth_status or AuthStatus.UNCONFIGURED.value

    await db.execute(
        """INSERT INTO sites
             (id, name, adapter_type, enabled, domain, cookie_enc, user_agent, rss_url_enc,
              search_url, api_key_enc, proxy, priority, music_cat_ids, audiobook_cat_ids,
              max_rows, title_bracket_style, hr_min_seed_hours, hr_min_ratio,
              auth_status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name = excluded.name,
             adapter_type = excluded.adapter_type,
             enabled = excluded.enabled,
             domain = excluded.domain,
             cookie_enc = excluded.cookie_enc,
             user_agent = excluded.user_agent,
             rss_url_enc = excluded.rss_url_enc,
             search_url = excluded.search_url,
             api_key_enc = excluded.api_key_enc,
             proxy = excluded.proxy,
             priority = excluded.priority,
             music_cat_ids = excluded.music_cat_ids,
             audiobook_cat_ids = excluded.audiobook_cat_ids,
             max_rows = excluded.max_rows,
             title_bracket_style = excluded.title_bracket_style,
             hr_min_seed_hours = excluded.hr_min_seed_hours,
             hr_min_ratio = excluded.hr_min_ratio,
             updated_at = excluded.updated_at""",
        (
            sid, cfg.name, a_type, int(cfg.enabled), cfg.domain or "", cookie_enc,
            cfg.user_agent or "", rss_enc, cfg.search_url or "", api_enc, cfg.proxy or "",
            int(cfg.priority or 0), cfg.music_cat_id or "", cfg.audiobook_cat_id or "",
            int(cfg.max_rows or 50), cfg.title_bracket_style or "【】",
            cfg.hr_min_seed_hours, cfg.hr_min_ratio,
            auth_status, now, now,
        ),
    )
    await db.commit()
    return sid


async def delete_site(db, site_id: str) -> bool:
    """删站点及其分类映射。site_stats 保留（历史健康计数，不影响运行）。"""
    await db.execute("DELETE FROM site_categories WHERE site_id = ?", (site_id,))
    cur = await db.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    await db.commit()
    return cur.rowcount > 0


async def set_site_enabled(db, site_id: str, enabled: bool) -> bool:
    cur = await db.execute(
        "UPDATE sites SET enabled = ?, updated_at = ? WHERE id = ?",
        (int(enabled), _now(), site_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def set_site_auth(
    db, site_id: str, status: AuthStatus | str, *,
    error: str | None = None, user_info: SiteUserInfo | None = None,
    checked_at: datetime | None = None,
) -> bool:
    """写认证结果。user_info=None 时不清空已有用户信息（认证失效仍保留上次数据展示）。"""
    status_val = status.value if isinstance(status, AuthStatus) else str(status)
    sets = ["auth_status = ?", "auth_checked_at = ?", "auth_error = ?", "updated_at = ?"]
    params: list = [status_val, (checked_at or utcnow()).isoformat(), error, _now()]
    if user_info is not None:
        sets.append("user_info_json = ?")
        params.append(_json_or_none(user_info))
    params.append(site_id)
    cur = await db.execute(f"UPDATE sites SET {', '.join(sets)} WHERE id = ?", params)
    await db.commit()
    return cur.rowcount > 0


async def set_site_rss_test(db, site_id: str, result: RssTestResult) -> bool:
    cur = await db.execute(
        "UPDATE sites SET last_rss_test_json = ?, updated_at = ? WHERE id = ?",
        (_json_or_none(result), _now(), site_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def get_site_secret(db, site_id: str, field: str) -> str:
    """取单个凭据明文（cookie / rss_url / api_key），**仅供适配器构建与认证使用**。

    未配置 / 站点不存在返回空串。非法字段名抛 ValueError，不让调用方拼列名。
    """
    col = SITE_SECRET_COLUMNS.get(field)
    if col is None:
        raise ValueError(f"未知凭据字段: {field!r}，可选 {sorted(SITE_SECRET_COLUMNS)}")
    async with db.execute(f"SELECT {col} AS v FROM sites WHERE id = ?", (site_id,)) as cur:
        r = await cur.fetchone()
    if r is None:
        return ""
    return _dec(r["v"], label=f"{site_id}.{field}")


# ---- site_categories ----

def _row_site_category(r) -> SiteCategory:
    kind = r["local_kind"] or "other"
    return SiteCategory(
        id=r["id"], site_id=r["site_id"], remote_id=str(r["remote_id"]),
        remote_name=r["remote_name"] or "",
        local_kind=kind if kind in ("music", "audiobook", "other") else "other",
        discovered_at=_dt(r["discovered_at"]),
    )


async def list_site_categories(db, site_id: str) -> list[SiteCategory]:
    async with db.execute(
        "SELECT * FROM site_categories WHERE site_id = ? ORDER BY remote_id", (site_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_site_category(r) for r in rows]


async def replace_site_categories(db, site_id: str, cats: list[SiteCategory]) -> int:
    """用新发现的远端分类整体替换该站映射。

    使用者已手动标过的 local_kind 会保留（以 remote_id 对齐）：发现分类是重复动作，
    不该每次都把映射打回 other。传入非 other 的值时以传入为准。返回写入条数。
    """
    old = {c.remote_id: c.local_kind for c in await list_site_categories(db, site_id)}
    await db.execute("DELETE FROM site_categories WHERE site_id = ?", (site_id,))
    now = _now()
    n = 0
    for c in cats:
        kind = c.local_kind if c.local_kind != "other" else old.get(c.remote_id, "other")
        await db.execute(
            """INSERT OR IGNORE INTO site_categories
                 (site_id, remote_id, remote_name, local_kind, discovered_at)
               VALUES (?,?,?,?,?)""",
            (site_id, str(c.remote_id), c.remote_name or "", kind,
             c.discovered_at.isoformat() if c.discovered_at else now),
        )
        n += 1
    await db.commit()
    return n


async def set_category_kind(db, site_id: str, remote_id: str, local_kind: str) -> bool:
    if local_kind not in ("music", "audiobook", "other"):
        raise ValueError(f"非法分类映射: {local_kind!r}")
    cur = await db.execute(
        "UPDATE site_categories SET local_kind = ? WHERE site_id = ? AND remote_id = ?",
        (local_kind, site_id, str(remote_id)),
    )
    await db.commit()
    return cur.rowcount > 0


# ---- downloaders ----
#
# 多 qBittorrent 实例。is_default 全表最多一行为 1；不存在时 get_default_downloader
# 回落到优先级最高的已启用实例，保证有实例就有默认。

DOWNLOADER_SECRET_COLUMNS = {
    "password": "password_enc",
    "api_key": "api_key_enc",
}

#: 仍在下载器里占着位的任务状态（count_active_tasks_by_downloader 用）
_ACTIVE_TASK_STATES = (
    TaskState.DISPATCHED.value, TaskState.DOWNLOADING.value, TaskState.COMPLETED.value,
)


def downloader_record_from_row(r) -> DownloaderRecord:
    """downloaders 行 → DownloaderRecord（不含密码 / API Key 明文）。"""
    last_test = _load_json(r["last_test_json"])
    return DownloaderRecord(
        id=r["id"], name=r["name"], type=r["type"] or "qbittorrent",
        host=r["host"] or "localhost", port=int(r["port"] or 8085),
        username=r["username"] or "",
        save_path=r["save_path"] or "", category=r["category"] or "", tag=r["tag"] or "",
        enabled=bool(r["enabled"]), is_default=bool(r["is_default"]),
        priority=int(r["priority"] or 0),
        has_password=bool(r["password_enc"]), has_api_key=bool(r["api_key_enc"]),
        last_test=last_test if isinstance(last_test, dict) else None,
        last_test_at=parse_iso(r["last_test_at"]),
        created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),
    )


def downloader_config_from_row(r) -> DownloaderConfig:
    """downloaders 行 → DownloaderConfig（**含解密凭据**），仅供构建 qb 客户端。"""
    return DownloaderConfig(
        id=r["id"], name=r["name"],
        host=r["host"] or "localhost", port=int(r["port"] or 8085),
        username=r["username"] or "",
        password=_dec(r["password_enc"], label=f"downloader {r['id']}.password"),
        api_key=_dec(r["api_key_enc"], label=f"downloader {r['id']}.api_key"),
        category=r["category"] or "", tag=r["tag"] or "", save_path=r["save_path"] or "",
        enabled=bool(r["enabled"]), is_default=bool(r["is_default"]),
        priority=int(r["priority"] or 0),
    )


async def list_downloader_rows(db) -> list:
    """全部 downloaders 原始行（含 *_enc），供 registry_store 解密。"""
    async with db.execute(
        "SELECT * FROM downloaders ORDER BY is_default DESC, priority DESC, name",
    ) as cur:
        return list(await cur.fetchall())


async def list_downloaders(db, *, enabled_only: bool = False) -> list[DownloaderRecord]:
    sql = "SELECT * FROM downloaders"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY is_default DESC, priority DESC, name"
    async with db.execute(sql) as cur:
        rows = await cur.fetchall()
    return [downloader_record_from_row(r) for r in rows]


async def get_downloader(db, downloader_id: str) -> DownloaderRecord | None:
    async with db.execute("SELECT * FROM downloaders WHERE id = ?", (downloader_id,)) as cur:
        r = await cur.fetchone()
    return downloader_record_from_row(r) if r else None


async def get_default_downloader(db) -> DownloaderRecord | None:
    """is_default=1 的实例；没有则取优先级最高的已启用实例；表空返回 None。"""
    async with db.execute(
        "SELECT * FROM downloaders ORDER BY is_default DESC, enabled DESC, priority DESC, name "
        "LIMIT 1",
    ) as cur:
        r = await cur.fetchone()
    return downloader_record_from_row(r) if r else None


async def upsert_downloader(
    db, cfg: DownloaderConfig, *, overwrite_empty_secrets: bool = False,
) -> str:
    """按 DownloaderConfig 写入（存在则更新）。返回 id。

    password / api_key 非空才加密写入，空值默认保留库中已有值（留空＝不修改）。
    cfg.is_default=True 时同时清掉其它行的默认标记；False 时不动已有默认状态
    （编辑表单不带 is_default 不该把默认实例摔成非默认，换默认走 set_default_downloader）。
    """
    async with db.execute("SELECT * FROM downloaders WHERE id = ?", (cfg.id,)) as cur:
        existing = await cur.fetchone()

    def _secret(plain: str, col: str) -> str | None:
        if plain:
            return _enc(plain)
        if existing is not None and not overwrite_empty_secrets:
            return existing[col]
        return None

    pwd_enc = _secret(cfg.password, "password_enc")
    api_enc = _secret(cfg.api_key, "api_key_enc")
    is_default = bool(cfg.is_default) or bool(existing is not None and existing["is_default"])
    now = _now()
    if cfg.is_default:
        await db.execute("UPDATE downloaders SET is_default = 0 WHERE id != ?", (cfg.id,))
    await db.execute(
        """INSERT INTO downloaders
             (id, name, type, host, port, username, password_enc, api_key_enc,
              save_path, category, tag, enabled, is_default, priority, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             name = excluded.name,
             host = excluded.host,
             port = excluded.port,
             username = excluded.username,
             password_enc = excluded.password_enc,
             api_key_enc = excluded.api_key_enc,
             save_path = excluded.save_path,
             category = excluded.category,
             tag = excluded.tag,
             enabled = excluded.enabled,
             is_default = excluded.is_default,
             priority = excluded.priority,
             updated_at = excluded.updated_at""",
        (
            cfg.id, cfg.name, "qbittorrent", cfg.host, int(cfg.port), cfg.username or "",
            pwd_enc, api_enc, cfg.save_path or "", cfg.category or "", cfg.tag or "",
            int(cfg.enabled), int(is_default), int(cfg.priority or 0), now, now,
        ),
    )
    await db.commit()
    return cfg.id


async def delete_downloader(db, downloader_id: str) -> bool:
    cur = await db.execute("DELETE FROM downloaders WHERE id = ?", (downloader_id,))
    await db.commit()
    return cur.rowcount > 0


async def set_default_downloader(db, downloader_id: str) -> bool:
    """指定默认实例；实例不存在返回 False 且不改动任何行。"""
    async with db.execute("SELECT 1 FROM downloaders WHERE id = ?", (downloader_id,)) as cur:
        if await cur.fetchone() is None:
            return False
    now = _now()
    await db.execute("UPDATE downloaders SET is_default = 0 WHERE is_default = 1")
    await db.execute(
        "UPDATE downloaders SET is_default = 1, updated_at = ? WHERE id = ?", (now, downloader_id),
    )
    await db.commit()
    return True


async def set_downloader_test(
    db, downloader_id: str, result: dict, *, at: datetime | None = None,
) -> bool:
    """记「测试连接」结果（任意 JSON 可序列化 dict，如 {ok, version, error, elapsed_ms}）。"""
    cur = await db.execute(
        "UPDATE downloaders SET last_test_json = ?, last_test_at = ?, updated_at = ? WHERE id = ?",
        (_json_or_none(result), (at or utcnow()).isoformat(), _now(), downloader_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def get_downloader_secret(db, downloader_id: str, field: str) -> str:
    """取下载器凭据明文（password / api_key），仅供构建客户端。"""
    col = DOWNLOADER_SECRET_COLUMNS.get(field)
    if col is None:
        raise ValueError(f"未知凭据字段: {field!r}，可选 {sorted(DOWNLOADER_SECRET_COLUMNS)}")
    async with db.execute(
        f"SELECT {col} AS v FROM downloaders WHERE id = ?", (downloader_id,),
    ) as cur:
        r = await cur.fetchone()
    return _dec(r["v"], label=f"downloader {downloader_id}.{field}") if r else ""


async def count_active_tasks_by_downloader(db) -> dict[str, int]:
    """各下载器上仍在活动（已派发 / 下载中 / 已完成未归档）的任务数。

    downloader_id 为 NULL 的旧任务计入 "default"（seed_from_yaml 会回填，这里双保险）。
    """
    ph = ",".join("?" * len(_ACTIVE_TASK_STATES))
    async with db.execute(
        f"SELECT COALESCE(downloader_id, 'default') AS d, COUNT(*) AS n FROM tasks "
        f"WHERE state IN ({ph}) GROUP BY d",
        _ACTIVE_TASK_STATES,
    ) as cur:
        rows = await cur.fetchall()
    return {r["d"]: int(r["n"]) for r in rows}


# ---- subscription ----
#
# 订阅引擎（2B）的持久化：watchlist 的状态推进 + subscription_hits 命中流水。
# 「已拥有专辑」的判据统一为 decision ∈ {dispatched, upgrade}：待审 / 被拒 / 重复跳过
# 都不代表拿到了资源，不能拿来去重。

_OWNED_DECISIONS = (HitDecision.DISPATCHED.value, HitDecision.UPGRADE.value)


async def list_due_subscriptions(db, now: datetime) -> list[WatchEntry]:
    """到点该主动搜索的订阅：enabled 且 status=active 且 next_search_at 为空或已到。"""
    async with db.execute(
        """SELECT * FROM watchlist
           WHERE enabled = 1 AND status = ?
             AND (next_search_at IS NULL OR next_search_at <= ?)
           ORDER BY priority DESC, next_search_at ASC, created_at ASC""",
        (SubStatus.ACTIVE.value, now.isoformat()),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_watch_entry(r) for r in rows]


async def mark_searched(
    db, watch_id: str, now: datetime, *, interval_hours: int | None = None,
) -> None:
    """记一次主动搜索：last_search_at=now，next_search_at=now+间隔（省略则用该订阅自身的间隔）。"""
    if interval_hours is None:
        async with db.execute(
            "SELECT search_interval_hours FROM watchlist WHERE id = ?", (watch_id,),
        ) as cur:
            r = await cur.fetchone()
        interval_hours = int(r["search_interval_hours"] or 6) if r else 6
    interval_hours = max(int(interval_hours), 1)
    await db.execute(
        "UPDATE watchlist SET last_search_at = ?, next_search_at = ? WHERE id = ?",
        (now.isoformat(), (now + timedelta(hours=interval_hours)).isoformat(), watch_id),
    )
    await db.commit()


async def set_sub_status(
    db, watch_id: str, status: SubStatus | str, *, completed_at: datetime | None = None,
) -> bool:
    """推进订阅状态。置 completed 时自动记 completed_at（未传则现在）；回到 active 清空它。"""
    status_val = status.value if isinstance(status, SubStatus) else str(status)
    if status_val not in SubStatus._value2member_map_:
        raise ValueError(f"非法订阅状态: {status!r}")
    if status_val == SubStatus.COMPLETED.value:
        done = (completed_at or utcnow()).isoformat()
    elif status_val == SubStatus.ACTIVE.value:
        done = None
    else:
        done = _iso(completed_at)
    if status_val == SubStatus.PAUSED.value and completed_at is None:
        cur = await db.execute(
            "UPDATE watchlist SET status = ? WHERE id = ?", (status_val, watch_id),
        )
    else:
        cur = await db.execute(
            "UPDATE watchlist SET status = ?, completed_at = ? WHERE id = ?",
            (status_val, done, watch_id),
        )
    await db.commit()
    return cur.rowcount > 0


async def incr_hit(db, watch_id: str, n: int = 1) -> None:
    await db.execute(
        "UPDATE watchlist SET hit_count = COALESCE(hit_count, 0) + ? WHERE id = ?", (n, watch_id),
    )
    await db.commit()


def _row_hit(r) -> SubscriptionHit:
    via = r["found_via"] or FoundVia.RSS.value
    dec = r["decision"] or HitDecision.REVIEW.value
    return SubscriptionHit(
        id=r["id"], watch_id=r["watch_id"], site=r["site"] or "",
        torrent_title=r["torrent_title"] or "", info_hash=r["info_hash"] or "",
        album_key=r["album_key"] or "", quality_tag=r["quality_tag"], discount=r["discount"],
        size_bytes=int(r["size_bytes"] or 0), seeders=int(r["seeders"] or 0),
        score=float(r["score"] or 0.0),
        found_via=FoundVia(via) if via in FoundVia._value2member_map_ else FoundVia.RSS,
        decision=HitDecision(dec) if dec in HitDecision._value2member_map_
        else HitDecision.REVIEW,
        reject_reason=r["reject_reason"], task_id=r["task_id"],
        created_at=_dt(r["created_at"]),
    )


async def add_hit(db, hit: SubscriptionHit) -> int:
    """写一条命中记录，返回自增 id。不做去重：同一种子在不同轮次的决策可能不同，都要留痕。"""
    cur = await db.execute(
        """INSERT INTO subscription_hits
             (watch_id, site, torrent_title, info_hash, album_key, quality_tag, discount,
              size_bytes, seeders, score, found_via, decision, reject_reason, task_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            hit.watch_id, hit.site, hit.torrent_title, hit.info_hash or "", hit.album_key or "",
            hit.quality_tag, hit.discount, int(hit.size_bytes or 0), int(hit.seeders or 0),
            float(hit.score or 0.0), hit.found_via.value, hit.decision.value,
            hit.reject_reason, hit.task_id,
            hit.created_at.isoformat() if hit.created_at else _now(),
        ),
    )
    await db.commit()
    hit.id = cur.lastrowid
    return cur.lastrowid


async def list_hits(
    db, watch_id: str, *, decision: HitDecision | str | None = None,
    limit: int = 200, offset: int = 0,
) -> list[SubscriptionHit]:
    """某订阅的命中流水，最新在前；可按决策筛选。"""
    conds = ["watch_id = ?"]
    params: list = [watch_id]
    if decision is not None:
        conds.append("decision = ?")
        params.append(decision.value if isinstance(decision, HitDecision) else str(decision))
    params += [limit, offset]
    async with db.execute(
        f"SELECT * FROM subscription_hits{_where(conds)} "
        f"ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_hit(r) for r in rows]


async def has_album(db, watch_id: str, album_key: str) -> bool:
    """该订阅是否已拥有（已派发 / 已洗版）该专辑。album_key 为空一律 False（无法识别就不去重）。"""
    if not album_key:
        return False
    ph = ",".join("?" * len(_OWNED_DECISIONS))
    async with db.execute(
        f"SELECT 1 FROM subscription_hits WHERE watch_id = ? AND album_key = ? "
        f"AND decision IN ({ph}) LIMIT 1",
        (watch_id, album_key, *_OWNED_DECISIONS),
    ) as cur:
        return (await cur.fetchone()) is not None


async def best_quality_for_album(db, watch_id: str, album_key: str) -> str | None:
    """已拥有该专辑的最高音质档 quality_tag（按 models.QUALITY_RANK）；未拥有返回 None。

    洗版判定用：新候选 quality_rank > 此值才算更高档。排序在 Python 做，
    档位表只在 models 维护一份，不往 SQL 里复制 CASE WHEN。
    """
    if not album_key:
        return None
    ph = ",".join("?" * len(_OWNED_DECISIONS))
    async with db.execute(
        f"SELECT quality_tag FROM subscription_hits WHERE watch_id = ? AND album_key = ? "
        f"AND decision IN ({ph})",
        (watch_id, album_key, *_OWNED_DECISIONS),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return None
    tags = [r["quality_tag"] for r in rows]
    return max(tags, key=quality_rank)


async def has_hit(db, watch_id: str, info_hash: str) -> bool:
    """该订阅是否已对此种子做过决策（任何决策）。引擎用它跳过同一种子的重复入账。"""
    if not info_hash:
        return False
    async with db.execute(
        "SELECT 1 FROM subscription_hits WHERE watch_id = ? AND info_hash = ? LIMIT 1",
        (watch_id, info_hash),
    ) as cur:
        return (await cur.fetchone()) is not None


async def set_hit_task(
    db, info_hash: str, task_id: str | None, decision: HitDecision | str,
    *, watch_id: str | None = None,
) -> int:
    """待审采纳后回写命中流水：按 info_hash 找到还在 review 的命中，改成最终决策并挂上 task_id。

    只改 decision=review 的行：已经 dispatched / rejected 的历史决策不重写。
    返回影响行数（0 表示该种子不是订阅引擎送进待审的，比如旧 search_round 的待审项）。
    """
    if not info_hash:
        return 0
    dec = decision.value if isinstance(decision, HitDecision) else str(decision)
    conds = ["info_hash = ?", "decision = ?"]
    params: list = [task_id, dec, info_hash, HitDecision.REVIEW.value]
    if watch_id:
        conds.append("watch_id = ?")
        params.append(watch_id)
    cur = await db.execute(
        f"UPDATE subscription_hits SET task_id = ?, decision = ?{_where(conds)}", params,
    )
    await db.commit()
    return cur.rowcount


async def delete_hits_for_task(db, task_id: str) -> int:
    """删除某任务对应的命中流水（派发失败回滚用），返回删除行数。

    命中留着有两个副作用：has_hit 挡住同一种子的重新决策，decision=dispatched /
    upgrade 又让 has_album 把该专辑视为「已拥有」—— 派发一旦失败，订阅就永远
    等不到重试。删除后下一轮引擎可重新决策重新派发；失败的事实由 tasks 表里的
    任务行承担审计（本函数不动任务行）。
    """
    cur = await db.execute(
        "DELETE FROM subscription_hits WHERE task_id = ?", (task_id,),
    )
    await db.commit()
    return cur.rowcount if cur.rowcount else 0


async def list_owned_albums(db, watch_id: str) -> list[dict]:
    """该订阅「已获得」的专辑清单：按 album_key 聚合，每组取最高音质档与最近一次的标题 / 站点 / 时间。

    排序与择优在 Python 做（档位表在 models）；一个订阅的已拥有专辑最多也就几百条。
    """
    ph = ",".join("?" * len(_OWNED_DECISIONS))
    async with db.execute(
        f"SELECT album_key, torrent_title, site, quality_tag, created_at FROM subscription_hits "
        f"WHERE watch_id = ? AND album_key != '' AND decision IN ({ph}) "
        f"ORDER BY created_at DESC, id DESC",
        (watch_id, *_OWNED_DECISIONS),
    ) as cur:
        rows = await cur.fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.get(r["album_key"])
        if g is None:
            g = groups[r["album_key"]] = {
                "album_key": r["album_key"], "title": r["torrent_title"] or "",
                "site": r["site"] or "", "quality_tag": r["quality_tag"],
                "obtained_at": _dt(r["created_at"]), "versions": 0,
            }
        g["versions"] += 1
        if quality_rank(r["quality_tag"]) > quality_rank(g["quality_tag"]):
            g["quality_tag"] = r["quality_tag"]
    return list(groups.values())


async def set_watch_enabled(db, watch_id: str, enabled: bool) -> bool:
    cur = await db.execute(
        "UPDATE watchlist SET enabled = ? WHERE id = ?", (int(enabled), watch_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def update_watch_entry(db, entry: WatchEntry) -> bool:
    """按 id 更新订阅项的全部可编辑列。

    不走 add_watch_entry 的 INSERT OR REPLACE：tasks.watch_entry_id 有外键指向 watchlist，
    REPLACE 会先删后插，在 foreign_keys=ON 下被有历史任务的订阅直接拒绝。
    运行态列（hit_count / last_search_at / next_search_at / completed_at / status）不在此列，
    由引擎的专用函数推进；编辑表单不该改它们。
    """
    cur = await db.execute(
        """UPDATE watchlist SET
             kind = ?, query = ?, aliases = ?, regex = ?, sites = ?, min_confidence = ?,
             auto_download = ?, priority = ?, enabled = ?,
             sub_type = ?, artist = ?, album = ?, author = ?, search_interval_hours = ?,
             upgrade_enabled = ?, min_seeders = ?, downloader_id = ?, save_path = ?,
             qb_category = ?, qb_tags = ?, preferred_quality = ?, min_size_mb = ?,
             max_size_mb = ?, free_only = ?, exclude_keywords = ?, include_keywords = ?
           WHERE id = ?""",
        (
            entry.kind.value, entry.query,
            json.dumps(entry.aliases, ensure_ascii=False), entry.regex,
            json.dumps(entry.sites, ensure_ascii=False), entry.min_confidence,
            int(entry.auto_download), entry.priority, int(entry.enabled),
            entry.sub_type.value, entry.artist, entry.album, entry.author,
            int(entry.search_interval_hours),
            int(entry.upgrade_enabled), int(entry.min_seeders), entry.downloader_id,
            entry.save_path, entry.qb_category, _csv(entry.qb_tags), entry.preferred_quality,
            entry.min_size_mb, entry.max_size_mb, int(entry.free_only),
            json.dumps(entry.exclude_keywords, ensure_ascii=False),
            json.dumps(entry.include_keywords, ensure_ascii=False),
            entry.id,
        ),
    )
    await db.commit()
    return cur.rowcount > 0


async def delete_subscription(db, watch_id: str) -> bool:
    """删订阅：解开 tasks.watch_entry_id 外键（保留 watch_id 历史列）、清命中流水，再删 watchlist 行。

    不走 delete_watch_entry：foreign_keys=ON 下有历史任务的订阅直接 DELETE 会被外键拒绝。
    已建的任务 / 连载本身不删，它们是独立的历史记录。
    """
    await db.execute(
        "UPDATE tasks SET watch_entry_id = NULL WHERE watch_entry_id = ?", (watch_id,),
    )
    await db.execute("DELETE FROM subscription_hits WHERE watch_id = ?", (watch_id,))
    cur = await db.execute("DELETE FROM watchlist WHERE id = ?", (watch_id,))
    await db.commit()
    return cur.rowcount > 0


# ---- downloader routing ----
#
# 波次 3：多下载器下的任务归属。tasks.downloader_id 为 NULL 的旧任务一律视为 "default"，
# 与 count_active_tasks_by_downloader 的口径一致；完成监听 / 做种轮询按此分流，
# 每台 qb 只对账归属自己的任务。

#: 完成监听认为「仍在进行」的状态（与 completion.monitor._INFLIGHT_STATES 同源）
_INFLIGHT_TASK_STATES = (
    TaskState.DISPATCHED.value, TaskState.DOWNLOADING.value, TaskState.COMPLETED.value,
)


async def list_inflight_tasks(db, *, downloader_id: str | None = None) -> list[dict]:
    """in-flight 任务摘要（id / info_hash / state / downloader_id）。

    downloader_id 指定时只取归属该下载器的任务；None 取全部（单实例旧路径）。
    """
    ph = ",".join("?" * len(_INFLIGHT_TASK_STATES))
    sql = (
        f"SELECT id, info_hash, state, COALESCE(downloader_id, 'default') AS downloader_id "
        f"FROM tasks WHERE state IN ({ph})"
    )
    params: list = list(_INFLIGHT_TASK_STATES)
    if downloader_id is not None:
        sql += " AND COALESCE(downloader_id, 'default') = ?"
        params.append(downloader_id)
    async with db.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def count_tasks_referencing_downloader(db, downloader_id: str) -> int:
    """仍引用该下载器的**非终态**任务数（删除保护：有在途任务的下载器不可删）。

    终态 = routed / notified / rejected / failed / archived_skipped；其余都算在途
    （discovered / scored 尚未派发也算：删了下载器它们就没地方去）。
    """
    terminal = (
        TaskState.ROUTED.value, TaskState.NOTIFIED.value, TaskState.REJECTED.value,
        TaskState.FAILED.value, TaskState.ARCHIVED_SKIPPED.value,
    )
    ph = ",".join("?" * len(terminal))
    async with db.execute(
        f"SELECT COUNT(*) AS n FROM tasks "
        f"WHERE COALESCE(downloader_id, 'default') = ? AND state NOT IN ({ph})",
        (downloader_id, *terminal),
    ) as cur:
        r = await cur.fetchone()
    return int(r["n"]) if r else 0


# ---- subscription overview ----
#
# 总览页「订阅概览」卡：订阅中 / 已完成 / 今日命中 / 下次搜索时间。
# 只做计数与 MIN()，不构造模型。

async def count_subscriptions_by_status(db) -> dict[str, int]:
    """watchlist 按 status 计数（仅 enabled 行；禁用的订阅不算「订阅中」）。"""
    async with db.execute(
        "SELECT status, COUNT(*) AS n FROM watchlist WHERE enabled = 1 GROUP BY status",
    ) as cur:
        rows = await cur.fetchall()
    return {(r["status"] or SubStatus.ACTIVE.value): int(r["n"]) for r in rows}


async def count_hits_since(db, since: datetime) -> int:
    """since 之后产生的命中条数（全部决策，含待审 / 被拒：都是「发现了候选」）。"""
    async with db.execute(
        "SELECT COUNT(*) AS n FROM subscription_hits WHERE created_at >= ?",
        (since.isoformat(),),
    ) as cur:
        r = await cur.fetchone()
    return int(r["n"]) if r else 0


async def next_subscription_search_at(db) -> datetime | None:
    """最早的已排期下次主动搜索时间；没有活跃订阅排期时为 None。

    从未搜过的订阅（next_search_at 为空）不在这里体现，调用方应先看
    has_pending_subscription_search()：为真则下一轮扫描就会搜，不必等到这个时间。
    """
    async with db.execute(
        """SELECT MIN(next_search_at) AS t FROM watchlist
           WHERE enabled = 1 AND status = ? AND next_search_at IS NOT NULL""",
        (SubStatus.ACTIVE.value,),
    ) as cur:
        r = await cur.fetchone()
    return parse_iso(r["t"]) if r and r["t"] else None


async def has_pending_subscription_search(db) -> bool:
    """是否存在从未搜过（next_search_at 为空）的活跃订阅：下一轮扫描即会搜它。"""
    async with db.execute(
        """SELECT 1 FROM watchlist
           WHERE enabled = 1 AND status = ? AND next_search_at IS NULL LIMIT 1""",
        (SubStatus.ACTIVE.value,),
    ) as cur:
        return (await cur.fetchone()) is not None
