"""归档决策：库内无同名 → new；库内更差 → upgrade；库内不差 → skip。

决策键是 (watch_id, title_norm)：同一订阅项下的同一张专辑/同一本书才互相比较，
不同订阅项即使标题归一化后相同（例如同名专辑的不同艺术家）也互不干扰。

质量分复用 matcher 的音质权重表（0～1.0）× 100 取整，保证"升级"判断与
搜索阶段的打分口径一致，不另起一套标准。
"""

from __future__ import annotations

import re

from loguru import logger

from .. import repo
from ..adapters.base import normalize_title
from ..models import ArchiveDecision, LibraryItem, Task, WatchKind
from ..pipeline.matcher import detect_audiobook_format, detect_quality

# 标签 → 权重的反查表：task.quality 存的是 matcher 产出的标签（flac_16 / m4b ...），
# 优先按标签取权重，拿不到再从标题重新识别。
_LABEL_WEIGHTS: dict[str, float] = {
    "dsd": 1.00, "flac_24": 0.95, "flac_16": 0.85, "ape": 0.75, "wav": 0.75,
    "alac": 0.70, "mp3": 0.40,
    # 有声书格式（见 matcher._AUDIOBOOK_FORMATS）
    "m4b": 0.95, "audiobook": 0.85,
}
_AUDIOBOOK_LABELS = {"m4b", "audiobook"}

# 目录名里不允许出现的字符（Windows + POSIX 取并集，SMB 共享两边都要能读）
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_MAX_DIRNAME = 120


def _task_watch_id(task: Task) -> str | None:
    """Task 上的订阅项外键叫 watch_entry_id；library_items 列叫 watch_id。"""
    return getattr(task, "watch_entry_id", None) or getattr(task, "watch_id", None)


def title_norm_of(task: Task) -> str:
    """归档决策与库目录名共用的归一化标题。"""
    return normalize_title(task.title or "")


def safe_dirname(name: str, fallback: str) -> str:
    """把归一化标题变成能落盘的目录名；为空则用 fallback（通常是 task.id）。"""
    cleaned = _UNSAFE_CHARS.sub(" ", name or "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:_MAX_DIRNAME].rstrip(" .") or fallback


def quality_score(task: Task, kind: WatchKind | None = None) -> int:
    """0～100 的整数质量分。

    先按 task.quality 标签反查权重；标签缺失/未知时按 kind 从标题 + format_tags
    重新识别（有声书走格式表，MP3 对有声书是合格格式）。
    """
    label = (task.quality or "").strip().lower()
    if label in _LABEL_WEIGHTS:
        return round(_LABEL_WEIGHTS[label] * 100)

    text = f"{task.title} {' '.join(task.format_tags or [])}"
    if kind == WatchKind.AUDIOBOOK:
        _, weight = detect_audiobook_format(text)
    else:
        _, weight = detect_quality(text)
    return round(weight * 100)


async def resolve_task_kind(db, task: Task) -> WatchKind:
    """任务类型：优先取所属订阅项的 kind；无订阅项时按音质标签 / 标题线索判断。"""
    watch_id = _task_watch_id(task)
    if watch_id:
        kind = await repo.get_watch_kind(db, watch_id)
        if kind is not None:
            return kind

    if (task.quality or "").lower() in _AUDIOBOOK_LABELS:
        return WatchKind.AUDIOBOOK
    label, _ = detect_audiobook_format(task.title or "")
    if label in _AUDIOBOOK_LABELS:
        return WatchKind.AUDIOBOOK
    return WatchKind.MUSIC


async def decide(db, task: Task, cfg=None) -> tuple[ArchiveDecision, LibraryItem | None]:
    """归档决策。

    Returns:
        (decision, existing)：existing 为库内已有的同名条目（NEW 时为 None）。
        UPGRADE / SKIP 时调用方据 existing 做替换或跳过。
    """
    title_norm = title_norm_of(task)
    existing = await repo.find_library_item(db, _task_watch_id(task), title_norm)
    if existing is None:
        logger.debug(f"归档决策 new: {title_norm!r}")
        return ArchiveDecision.NEW, None

    kind = existing.kind
    new_score = quality_score(task, kind)
    if new_score > existing.quality_score:
        logger.info(
            f"归档决策 upgrade: {title_norm!r} {existing.quality_score} → {new_score}"
        )
        return ArchiveDecision.UPGRADE, existing

    # DEBUG：skip 是逐条例行结论（库内不差），升级才值得 INFO；刷屏会淹没异常
    logger.debug(
        f"归档决策 skip: {title_norm!r} 库内 {existing.quality_score} ≥ 新 {new_score}"
    )
    return ArchiveDecision.SKIP, existing
