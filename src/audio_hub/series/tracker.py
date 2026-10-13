"""连载追踪核心：分集号解析 → 分卷登记 → 完结判定。

职责边界：
- 本模块不做订阅词匹配（那是 matcher 的事），只回答两个问题：
  「这个标题是第几部」与「这个连载是否已集齐」。
- 所有 SQL 走 repo；通知走可选的 notifier（Notifier 协议，缺省不发）。

分集号解析原则：**宁可解析失败，不可解析错误**。解析失败的候选仍走原流程
（matcher 在 detail 里记 part_unparsed），只是不参与分卷去重与进度推进；
解析错误（比如把年份当分集号）则会误伤去重，故默认规则对括号数字限 1~3 位。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from loguru import logger

from .. import repo
from ..models import PartEntry, SeriesEntry, Task, WatchEntry
from ..notify.base import Event, Notifier

#: 默认分集规则，按常见写法排列：
#:   第3集 / 第 12 部 / 第七卷（不含中文数字）| Part 7 | EP05 / E12 | (03)
#: 每个分支各自一个捕获组，取第一个非空组。
DEFAULT_PART_PATTERN = (
    r"第\s*(\d+)\s*[集部卷章回期]|\bPart\s*(\d+)|\bEP?\s*(\d{1,3})\b|\((\d{1,3})\)"
)


@lru_cache(maxsize=128)
def _compile(pattern: str) -> re.Pattern | None:
    """编译并缓存规则；无效正则返回 None（调用方回退默认）。"""
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        logger.warning(f"连载分集规则无效，回退默认规则: {pattern!r} ({e})")
        return None


def extract_part_no(title: str, pattern: str | None = None) -> int | None:
    """从标题解析分集号。

    Args:
        title: 种子标题
        pattern: 连载自定义规则；为空或无效时用 DEFAULT_PART_PATTERN。
            规则至少要有一个捕获组，否则同样视为无效。

    Returns:
        第一个非空捕获组的整数值；未命中返回 None。
    """
    if not title:
        return None

    compiled: re.Pattern | None = None
    if pattern and pattern.strip():
        compiled = _compile(pattern.strip())
        if compiled is not None and compiled.groups == 0:
            logger.warning(f"连载分集规则缺少捕获组，回退默认规则: {pattern!r}")
            compiled = None
    if compiled is None:
        compiled = _compile(DEFAULT_PART_PATTERN)

    m = compiled.search(title)
    if not m:
        return None
    for g in m.groups():
        if g:
            try:
                return int(g)
            except ValueError:
                # 自定义规则的捕获组可能抓到非数字，继续看下一个组
                continue
    return None


# ── 旧 match_candidates 用的只读上下文 ─────────────────────────
# v0.6 订阅引擎改用 album_key（含 volN）+ has_album 做分卷去重，不再走这里；
# SeriesContext / build_series_context 暂保留给仍按旧方式比对分集的调用方与测试。

@dataclass
class SeriesContext:
    """一轮匹配所需的连载快照：规则 + 已登记分集号（旧 matcher 路径的只读上下文）。"""
    #: series.id → SeriesEntry（取 part_pattern / status）
    series_index: dict[int, SeriesEntry] = field(default_factory=dict)
    #: series.id → 已登记的 part_no 集合（分卷去重）
    known_parts: dict[int, set[int]] = field(default_factory=dict)


async def build_series_context(db, entries: list[WatchEntry]) -> SeriesContext:
    """按订阅项绑定的 series_id 读出连载与已登记分集。无连载订阅时返回空上下文。"""
    ids = {e.series_id for e in entries if e.series_id is not None}
    ctx = SeriesContext()
    if not ids:
        return ctx
    for sid in ids:
        series = await repo.get_series(db, sid)
        if series is None:
            logger.warning(f"订阅项引用的连载不存在: series_id={sid}")
            continue
        ctx.series_index[sid] = series
    parts = await repo.list_part_numbers_by_series(db)
    ctx.known_parts = {sid: parts.get(sid, set()) for sid in ctx.series_index}
    return ctx


# ── 登记与完结判定 ───────────────────────────────────────────

async def register_part(
    db, series: SeriesEntry, task: Task, notifier: Notifier | None = None,
) -> bool:
    """把一个任务登记为连载的一部。

    分集号优先用 task.part_no（matcher 已解析），否则按连载规则从标题解析。
    登记成功后刷新连载状态（可能触发 series_completed 事件）。

    Returns:
        True 表示新登记；False 表示解析失败或该分集已存在。
    """
    if series.id is None:
        logger.warning("register_part 收到未持久化的连载，跳过")
        return False

    part_no = task.part_no
    if part_no is None:
        part_no = extract_part_no(task.title, series.part_pattern)
    if part_no is None:
        logger.info(f"连载 {series.name!r} 无法从标题解析分集号，不登记: {task.title[:60]!r}")
        return False

    inserted = await repo.add_series_part(db, PartEntry(
        series_id=series.id, part_no=part_no,
        task_id=task.id or None, title=task.title,
    ))
    if not inserted:
        logger.debug(f"连载 {series.name!r} 第 {part_no} 部已登记过，跳过")
        return False

    logger.info(f"连载 {series.name!r} 登记第 {part_no} 部: {task.title[:60]!r}")
    await refresh_status(db, series.id, notifier=notifier)
    return True


async def register_task(db, task: Task, notifier: Notifier | None = None) -> bool:
    """按 task.series_id 找到连载并登记——给只持有 Task 的调用方（scheduler / review）用。"""
    if task.series_id is None:
        return False
    series = await repo.get_series(db, task.series_id)
    if series is None:
        logger.warning(f"任务 {task.id[:8] if task.id else '?'} 引用的连载不存在: {task.series_id}")
        return False
    return await register_part(db, series, task, notifier=notifier)


async def refresh_status(
    db, series_id: int, notifier: Notifier | None = None,
) -> SeriesEntry | None:
    """重算 last_part 与完结状态。

    - last_part = 已登记分集号的最大值（无分集为 0）
    - total_parts 已设且 last_part >= total_parts → completed，并发 series_completed
    - 已 completed 但条件不再满足（如用户把总部数改大）→ 回到 active
    - paused 不受完结判定影响以外的改动

    Returns:
        刷新后的 SeriesEntry；连载不存在返回 None。
    """
    series = await repo.get_series(db, series_id)
    if series is None:
        return None

    parts = await repo.list_series_parts(db, series_id)
    last_part = max((p.part_no for p in parts), default=0)
    done = series.total_parts is not None and last_part >= series.total_parts

    changes: dict = {}
    if last_part != series.last_part:
        changes["last_part"] = last_part
    if done and series.status != "completed":
        changes["status"] = "completed"
    elif not done and series.status == "completed":
        changes["status"] = "active"

    if changes:
        await repo.update_series(db, series_id, **changes)
        series = await repo.get_series(db, series_id)

    if changes.get("status") == "completed":
        logger.info(f"连载已完结: {series.name!r} ({last_part}/{series.total_parts})")
        if notifier is not None:
            await notifier.emit(Event(
                kind="series_completed",
                site="",
                title=series.name,
                detail={
                    "series_id": series_id,
                    "watch_id": series.watch_id,
                    "last_part": last_part,
                    "total_parts": series.total_parts,
                    "parts": len(parts),
                },
                level="info",
            ))
    return series
