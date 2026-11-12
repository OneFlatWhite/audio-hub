"""新剧自动检索(M5):TMDB 热门新剧冷启动监控 → 自动入缺集队列。

职责边界(刻意收窄):
- 只负责「把一部**不在 Emby 里**的新剧带进库」——监控期间,已播出集
  全部视为缺集写入 lackfill_queue,由既有 Re0/AY → 115 → CD2 → Emby
  链路补齐;
- 一旦该剧出现在 Emby(首轮入库完成),监控项自动**毕业**退出;此后的
  新播出集由常规 Emby 扫描链路接管(那时它已经是库里的剧了)。

发现源是 TMDB /trending/tv/week,受「新剧每日配额」限制;配额 0 = 关闭
发现(已监控的剧仍会继续入队直到毕业)。
"""

from __future__ import annotations

from datetime import date

import aiosqlite

from .emby import EmbyClient
from .tmdb import TmdbClient


async def list_monitored(conn: aiosqlite.Connection) -> list[dict]:
    cur = await conn.execute(
        "SELECT * FROM lackfill_newshows ORDER BY added_at, tmdb_id"
    )
    return [dict(r) for r in await cur.fetchall()]


async def _add(conn, tmdb_id: int, title: str, year: int | None, now: str) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM lackfill_newshows WHERE tmdb_id = ?", (tmdb_id,)
    )
    if await cur.fetchone():
        return False
    await conn.execute(
        """INSERT INTO lackfill_newshows (tmdb_id, title, year, added_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (tmdb_id, title, year, now, now),
    )
    await conn.commit()
    return True


async def run_once(
    conn: aiosqlite.Connection,
    cfg,
    emby: EmbyClient,
    tmdb: TmdbClient,
    *,
    today: date | None = None,
    now: str = "",
) -> dict:
    """新剧检索一轮:毕业 → 发现 → 入队。返回统计;单剧异常不中断整轮。"""
    from . import queue
    from .models import ShowLack

    today = today or date.today()
    now = now or f"{today.isoformat()}T00:00:00"
    stats = {"monitored": 0, "discovered": 0, "enqueued": 0, "graduated": 0, "errors": []}

    # ① 毕业:已进入 Emby 的监控剧退出(后续新集归常规扫描管)
    emby_series = {s["tmdb_id"] for s in await emby.list_series() if s.get("tmdb_id")}
    for row in await list_monitored(conn):
        if row["tmdb_id"] in emby_series:
            await conn.execute(
                "DELETE FROM lackfill_newshows WHERE tmdb_id = ?", (row["tmdb_id"],)
            )
            await conn.commit()
            stats["graduated"] += 1

    # ② 发现:TMDB 本周热门 → 跳过 Emby 已有/已监控 → 受配额限制入监控表
    quota = int(getattr(cfg, "new_show_daily_quota", 0) or 0)
    if quota > 0:
        for item in await tmdb.trending_tv_week():
            if stats["discovered"] >= quota:
                break
            tmdb_id = int(item["id"])
            if tmdb_id in emby_series:
                continue
            year = None
            if item.get("first_air_date"):
                try:
                    year = int(str(item["first_air_date"])[:4])
                except ValueError:
                    year = None
            if await _add(conn, tmdb_id, item["name"], year, now):
                stats["discovered"] += 1

    # ③ 入队:监控中(且仍不在 Emby)的剧,全部已播出集写入缺集队列
    for row in await list_monitored(conn):
        if row["tmdb_id"] in emby_series:
            continue
        stats["monitored"] += 1
        try:
            detail = await tmdb.tv(row["tmdb_id"])
            missing: dict[int, list[int]] = {}
            for season in detail.get("seasons") or []:
                sn = int(season.get("season_number") or 0)
                if sn == 0:
                    continue
                eps = await tmdb.season_aired_episode_numbers(row["tmdb_id"], sn, today)
                if eps:
                    missing[sn] = eps
            if not missing:
                continue
            await queue.upsert(
                conn,
                ShowLack(
                    tmdb_id=row["tmdb_id"],
                    title=row["title"] or detail.get("name") or str(row["tmdb_id"]),
                    year=row.get("year"),
                    emby_id="",
                    missing=missing,
                ),
                now,
            )
            stats["enqueued"] += 1
            await conn.execute(
                "UPDATE lackfill_newshows SET updated_at = ? WHERE tmdb_id = ?",
                (now, row["tmdb_id"]),
            )
            await conn.commit()
        except Exception as exc:  # noqa: BLE001 —— 单剧失败不挡整轮
            stats["errors"].append(f"{row['title']}: {type(exc).__name__}")
    return stats
