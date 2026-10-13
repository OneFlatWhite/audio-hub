"""通知 sink：事件落 SQLite `events` 表，供 /events 页回看。

只经 repo.add_event 写库（SQL 收敛约定）。detail 以 JSON 文本存储，
页面展示时原样输出；需要结构化查询时再按 kind 拆列。
"""

from __future__ import annotations

import json

from .. import repo
from ..models import EventRecord
from ..timeutil import parse_iso, utcnow
from .base import BaseNotifier, Event


class DbNotifier(BaseNotifier):
    """把事件写入 events 表。

    持有 lifespan 打开的那条 aiosqlite 连接——与 runner 共用，
    避免另开连接与 WAL 争锁。
    """

    def __init__(self, db):
        self.db = db

    async def emit(self, event: Event) -> None:
        await repo.add_event(self.db, EventRecord(
            kind=event.kind,
            level=event.level,
            site=event.site or None,
            title=event.title or None,
            detail=json.dumps(event.detail, ensure_ascii=False, default=str)
            if event.detail else None,
            created_at=parse_iso(event.at) or utcnow(),
        ))
