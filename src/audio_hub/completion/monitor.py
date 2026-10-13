"""完成监听：轮询 qb1 状态，推进任务状态机，并执行归档链路。

崩溃恢复（PRD §13.6）是本模块的核心职责，不只是轮询：
- 中枢重启后必须与 qb 对账，找回 in-flight 任务
- 归档中途崩溃会留下 .upgrading 临时文件，需清理
- 每个环节幂等，重复执行不产生副作用

v0.4 归档链路（⑧校验 ⑨落位）：
    cp 原种 → scratch_inbox/<title>__<task8>/
      → validate_dir
          ✗ 且 on_fail=quarantine → move 到 quarantine_dir/<task.id>/，task→quarantined
          ✓ 或 pass_through       → decide
                new / upgrade     → copytree scratch → library/<title>/，task→routed，触发刮削
                                    （upgrade 先把旧目录 move 进 recycle_dir/<日期>/<item_id>/）
                skip              → 删 scratch 副本，task→archived_skipped
原种目录（qb save_path）只读：全链路对它只有一次 cp，没有任何 move / unlink。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from loguru import logger

from .. import repo
from ..archive.decision import (
    decide,
    quality_score,
    resolve_task_kind,
    safe_dirname,
    title_norm_of,
)
from ..archive.fs import copy_tree, move_tree, path_within, remove_tree, unique_path
from ..models import ArchiveDecision, LibraryItem, QuarantineItem, RecycleEntry, Task, TaskState
from ..notify.base import Event
from ..scrape.trigger import trigger_for_kind
from ..timeutil import utcnow
from ..validation.checker import ValidationResult, validate_dir

# qb 状态 → 中枢 TaskState 映射
_COMPLETE_STATES = {
    "uploading", "stalledUP", "checkingUP", "pausedUP", "stoppedUP",
    "queuedUP", "forcedUP",
}
_ERROR_STATES = {"error", "missingFiles"}

# 对账时认为"仍在进行"的状态（查询走 repo.list_inflight_tasks，此处保留供外部引用）
_INFLIGHT_STATES = {"dispatched", "downloading", "completed"}


@dataclass
class ArchiveOutcome:
    """一次归档链路的结果。status 取值：routed / quarantined / skipped / failed。"""
    status: str
    decision: ArchiveDecision | None = None
    #: 最终落位：routed → 库内目录；quarantined → 隔离目录；skipped / failed → None
    path: Path | None = None
    scratch_path: Path | None = None
    validation: ValidationResult | None = None
    message: str = ""


async def _emit(notifier, event: Event) -> None:
    """通知是旁路：没配 notifier 就跳过，sink 抛异常也不许影响归档。"""
    if notifier is None:
        return
    try:
        await notifier.emit(event)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"事件 {event.kind} 发送失败: {type(e).__name__}: {e}")


class CompletionMonitor:
    """监听 qb 下载完成情况，推进任务状态机。

    v0.6 多下载器：给了 registry 时逐个已启用下载器轮询，每台只对账
    downloader_id 归属它的任务（NULL 视为 "default"）；单台连不上只跳过该台。
    未给 registry 时保持旧单实例语义：qb_client 轮询全部 in-flight 任务。
    """

    def __init__(
        self, db, qb_client, config, *, dry_run: bool = True, notifier=None, registry=None,
    ):
        """
        Args:
            db: aiosqlite 连接
            qb_client: （旧式单实例）qbittorrentapi.Client；dry_run 或给了 registry 时可为 None
            config: AppConfig
            dry_run: True 时不调用 qb，仅做 DB 侧对账
            notifier: 事件接收端（MultiNotifier）；None 时归档事件只写日志。
                      PipelineRunner 构造时传入自身持有的 notifier，归档事件由此进 events 表。
            registry: DownloaderRegistry（v0.6）；给出时按下载器分流轮询
        """
        self.db = db
        self.qb = qb_client
        self.config = config
        self.dry_run = dry_run
        self.notifier = notifier
        self.registry = registry
        self.tag = config.qbittorrent.tag

    # ── 下载器遍历 ──

    def _handles(self) -> list[tuple[str | None, str, object]]:
        """[(downloader_id, tag, client)]。downloader_id=None 表示「全部任务」（旧单实例）。"""
        if self.registry is None:
            return [(None, self.tag, self.qb)]
        return [
            (h.id, h.config.tag or self.tag, h.client) for h in self.registry.all_enabled()
        ]

    async def _fetch_torrents(self, downloader_id: str | None, tag: str, client) -> dict | None:
        """拉取一台 qb 上带标签的种子 {hash: torrent}；客户端不可用 / 连接失败返回 None。"""
        label = downloader_id or "qb"
        if client is None:
            logger.warning(f"下载器 {label} 客户端不可用，本轮跳过其任务")
            return None
        try:
            return {t.hash: t for t in client.torrents_info(tag=tag)}
        except Exception as e:  # noqa: BLE001 - 单台不可达不影响其它下载器
            logger.error(f"无法连接下载器 {label}（{e}），本轮跳过其任务")
            await _emit(self.notifier, Event(
                kind="downloader_unreachable", site="", title=label, level="warning",
                detail={"downloader_id": downloader_id, "error": str(e)[:200]},
            ))
            return None

    # ── 启动对账（崩溃恢复）──────────────────────────────────

    async def reconcile_on_startup(self) -> dict[str, int]:
        """启动时与 qb 对账，找回中断的任务。

        Returns:
            统计 {"recovered": N, "orphaned": N, "cleaned_tmp": N}
        """
        stats = {"recovered": 0, "orphaned": 0, "cleaned_tmp": 0}

        # 1. 清理上次归档中断留下的临时文件（幂等：不存在则跳过）
        stats["cleaned_tmp"] = self._cleanup_upgrading_tmp()

        handles = self._handles()
        if self.dry_run or not any(c is not None for _, _, c in handles):
            # dry-run 无真实种子，仅把 in-flight 任务标记为可对账状态
            logger.info(
                f"[DRY-RUN] 启动对账：跳过 qb 查询，清理临时文件 {stats['cleaned_tmp']} 个"
            )
            return stats

        for downloader_id, tag, client in handles:
            # 2. 从该台 qb 拉取所有带标签的种子真实状态；连不上则保留其任务原状态待下轮
            qb_torrents = await self._fetch_torrents(downloader_id, tag, client)
            if qb_torrents is None:
                continue

            # 3. 与 DB 中归属该台的 in-flight 任务对账
            rows = await repo.list_inflight_tasks(self.db, downloader_id=downloader_id)
            for row in rows:
                task_id, info_hash = row["id"], row["info_hash"]
                t = qb_torrents.get(info_hash)
                if t is None:
                    # qb 里没有 → 可能已被人工删除
                    await self._set_state(
                        task_id, "failed", error="qb 中不存在（可能已被人工删除）",
                    )
                    stats["orphaned"] += 1
                    logger.warning(f"任务 {task_id} 对账为孤儿：qb 中无此种子")
                elif str(t.state) in _COMPLETE_STATES:
                    await self._set_state(task_id, "completed")
                    stats["recovered"] += 1
                    logger.info(f"任务 {task_id} 对账恢复为 completed")
                elif str(t.state) in _ERROR_STATES:
                    await self._set_state(task_id, "failed", error=f"qb 状态: {t.state}")
                    stats["orphaned"] += 1
                else:
                    await self._set_state(task_id, "downloading")
                    stats["recovered"] += 1

        await self.db.commit()
        logger.info(
            f"启动对账完成：恢复 {stats['recovered']} / 孤儿 {stats['orphaned']} / "
            f"清理临时文件 {stats['cleaned_tmp']}"
        )
        return stats

    def _cleanup_upgrading_tmp(self) -> int:
        """清理 .upgrading 临时文件（归档中途崩溃的残留）。根目录不存在则安全跳过。"""
        count = 0
        for root in self._library_roots():
            if not root.exists():
                continue
            for tmp in root.rglob("*.upgrading"):
                try:
                    tmp.unlink()
                    count += 1
                    logger.info(f"清理归档残留临时文件: {tmp}")
                except OSError as e:
                    logger.warning(f"清理临时文件失败 {tmp}: {e}")
        return count

    def _library_roots(self) -> list[Path]:
        """.upgrading 残留清理的扫描范围：**成品库**（音乐 / 有声书），而非原种目录。

        升级替换的半截文件只会落在 library 下；qb save_path 是只读原种，
        全链路对它只有 cp，不可能留下归档临时文件，也不该被扫描。
        两个库可能配置成同一目录，故去重。
        """
        roots: list[Path] = []
        for raw in (self.config.paths.library_music, self.config.paths.library_audiobook):
            if not raw:
                continue
            p = Path(raw)
            if p not in roots:
                roots.append(p)
        return roots

    # ── 周期轮询 ────────────────────────────────────────────

    async def poll_once(self) -> dict[str, int]:
        """执行一次轮询：检查所有 in-flight 任务的 qb 状态，完成的走归档链路。

        Returns:
            {"completed": N, "downloading": N, "failed": N, "routed": N,
             "quarantined": N, "recycle_purged": N}
        """
        stats = await self._poll_inner()
        # 回收区到期清理挂在轮询尾部：与 qb 是否可达无关，dry-run 也要跑
        try:
            stats["recycle_purged"] = await purge_expired_recycle(self.db, self.config)
        except Exception as e:  # noqa: BLE001 - 清理失败不影响轮询主流程
            logger.warning(f"回收区清理异常: {type(e).__name__}: {e}")
            stats["recycle_purged"] = 0
        return stats

    async def _poll_inner(self) -> dict[str, int]:
        stats = {"completed": 0, "downloading": 0, "failed": 0, "routed": 0, "quarantined": 0}
        if self.dry_run:
            logger.debug("[DRY-RUN] 轮询跳过（无真实种子）")
            return stats

        for downloader_id, tag, client in self._handles():
            qb_torrents = await self._fetch_torrents(downloader_id, tag, client)
            if qb_torrents is None:
                continue
            rows = await repo.list_inflight_tasks(self.db, downloader_id=downloader_id)
            for row in rows:
                task_id, info_hash = row["id"], row["info_hash"]
                t = qb_torrents.get(info_hash)
                if t is None:
                    continue                  # 尚未出现在 qb（派发延迟），下轮再看
                state = str(t.state)
                if state in _COMPLETE_STATES:
                    await self._set_state(task_id, "completed")
                    await self.db.commit()
                    stats["completed"] += 1
                    logger.info(
                        f"任务 {task_id} 下载完成 | downloader={downloader_id or 'default'} | "
                        f"seeding_time={getattr(t, 'seeding_time', 0)}s"
                    )
                    outcome = await self._archive(task_id, t)
                    if outcome is not None and outcome.status in ("routed", "quarantined"):
                        stats[outcome.status] += 1
                elif state in _ERROR_STATES:
                    await self._set_state(task_id, "failed", error=f"qb 状态: {state}")
                    stats["failed"] += 1
                    logger.error(f"任务 {task_id} 下载失败: qb state={state}")
                else:
                    stats["downloading"] += 1

        await self.db.commit()
        if any(stats.values()):
            logger.info(f"轮询完成: {stats}")
        return stats

    async def _archive(self, task_id: str, torrent) -> ArchiveOutcome | None:
        """对刚完成的任务跑归档链路。失败只记日志，任务停在 completed 等下轮重试。"""
        task = await repo.get_task(self.db, task_id)
        if task is None:
            return None
        content_path = getattr(torrent, "content_path", None)
        if not content_path:
            save_path = getattr(torrent, "save_path", None) or self.config.qbittorrent.save_path
            content_path = str(Path(save_path) / getattr(torrent, "name", task.title))
        try:
            return await process_completed_task(
                self.db, self.config, task, content_path, notifier=self.notifier,
            )
        except Exception as e:  # noqa: BLE001 - 单任务归档异常不得中断轮询
            logger.exception(f"任务 {task_id} 归档链路异常: {e}")
            await _emit(self.notifier, Event(
                kind="failed", site=task.site, title=task.title, level="error",
                detail={"reason": f"archive_exception: {type(e).__name__}: {e}"},
            ))
            return ArchiveOutcome(status="failed", message=str(e))

    # ── DB 辅助 ─────────────────────────────────────────────

    async def _set_state(self, task_id: str, state: str, error: str | None = None) -> None:
        """幂等地更新任务状态（相同状态重复写不产生副作用）。"""
        from ..timeutil import iso_utc
        now = iso_utc()
        if error is not None:
            await self.db.execute(
                "UPDATE tasks SET state=?, error_message=?, updated_at=? WHERE id=?",
                (state, error, now, task_id),
            )
        else:
            await self.db.execute(
                "UPDATE tasks SET state=?, updated_at=? WHERE id=?",
                (state, now, task_id),
            )


# ── 归档链路（模块级函数，隔离区放行与测试直接复用）──────────────

def scratch_dirname(task: Task) -> str:
    """scratch 子目录名：<安全标题>__<task 前 8 位>。

    带 task 片段是为了唯一：同名专辑的升级版与旧版若共用一个 scratch 目录，
    幂等检查会把旧副本当成新内容归档。
    """
    base = safe_dirname(title_norm_of(task), task.id or "task")
    return f"{base}__{(task.id or '')[:8]}" if task.id else base


async def copy_to_scratch(qb_content_path: str | Path, dst: Path) -> Path | None:
    """cp 原种内容到 dst（幂等：dst 已存在直接返回）。

    用 cp 而非 mv：**移动原种会破坏 PT 做种哈希**（PRD §8.2 红线）。
    """
    src = Path(qb_content_path)
    if not src.exists():
        logger.warning(f"归档跳过：源路径不存在 {src}")
        return None
    if dst.exists():
        logger.info(f"归档幂等跳过：目标已存在 {dst}")
        return dst
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            await asyncio.to_thread(shutil.copytree, src, dst, symlinks=False)
        else:
            await asyncio.to_thread(shutil.copy2, src, dst)
        logger.info(f"已拷入刮削工作区: {src} → {dst}")
        return dst
    except OSError as e:
        logger.error(f"拷贝失败 {src} → {dst}: {e}")
        return None


async def route_completed_task(
    task_row, qb_content_path: str, scratch_inbox: Path,
    *, db=None, cfg=None, notifier=None,
) -> Path | None:
    """把已完成的种子内容拷入刮削工作区，并（给了 db/cfg 时）走完整归档链路。

    兼容 v0.1 调用：task_row=None 或未传 db/cfg → 只做 cp 到 scratch_inbox/<源名>，
    返回副本路径。完整链路返回最终落位（库内目录 / 隔离目录），skip / 失败返回 None。
    """
    if task_row is None or db is None or cfg is None:
        legacy_dst = Path(scratch_inbox) / Path(qb_content_path).name
        return await copy_to_scratch(qb_content_path, legacy_dst)

    if isinstance(task_row, Task):
        task = task_row
    else:
        task_id = task_row if isinstance(task_row, str) else task_row["id"]
        task = await repo.get_task(db, task_id)
        if task is None:
            logger.warning(f"归档跳过：任务不存在 {task_id}")
            return None

    outcome = await process_completed_task(
        db, cfg, task, qb_content_path, scratch_inbox=Path(scratch_inbox), notifier=notifier,
    )
    return outcome.path


async def process_completed_task(
    db, cfg, task: Task, qb_content_path: str | Path,
    *, scratch_inbox: Path | None = None, notifier=None,
) -> ArchiveOutcome:
    """完整链路：cp → 校验 → 隔离 / 归档。见模块 docstring 的流程图。"""
    inbox = Path(scratch_inbox or cfg.paths.scratch_inbox)
    scratch = await copy_to_scratch(qb_content_path, inbox / scratch_dirname(task))
    if scratch is None:
        return ArchiveOutcome(status="failed", message="source_missing_or_copy_failed")

    result = await asyncio.to_thread(validate_dir, scratch, cfg.validation)
    validation_json = json.dumps(result.to_dict(), ensure_ascii=False)

    if not result.ok and cfg.validation.on_fail == "quarantine":
        qdir = unique_path(Path(cfg.paths.quarantine_dir) / (task.id or scratch.name))
        await move_tree(scratch, qdir)
        await repo.add_quarantine_item(db, QuarantineItem(
            task_id=task.id, scratch_path=str(qdir), reasons=list(result.reasons),
        ))
        await repo.set_task_state(
            db, task.id, TaskState.QUARANTINED, validation_result=validation_json,
        )
        await _emit(notifier, Event(
            kind="quarantined", site=task.site, title=task.title, level="warning",
            detail={"reasons": result.reasons, "path": str(qdir), "task_id": task.id},
        ))
        logger.warning(f"任务 {task.id[:8]} 已隔离: {result.reasons}")
        return ArchiveOutcome(
            status="quarantined", path=qdir, scratch_path=scratch, validation=result,
        )

    if not result.ok:
        logger.warning(f"校验未通过但 on_fail=pass_through，继续归档: {result.reasons}")
    # 校验结果先落库，再进归档；归档中途异常时也能在记录页看到校验结论
    await repo.set_task_state(db, task.id, task.state, validation_result=validation_json)
    outcome = await archive_from_scratch(db, cfg, task, scratch, notifier=notifier)
    outcome.validation = result
    return outcome


async def archive_from_scratch(
    db, cfg, task: Task, scratch_path: Path, *, notifier=None,
) -> ArchiveOutcome:
    """从 scratch 副本归档到库：决策 → （回收旧版）→ copytree → 记库 → 刮削。

    隔离区放行（quarantine release）把目录移回 scratch 后也调这里，故不含校验。
    scratch 副本在 routed 后**保留**（MTW 从这里刮削）；skip 时删除。
    """
    scratch_path = Path(scratch_path)
    kind = await resolve_task_kind(db, task)
    decision, existing = await decide(db, task, cfg)
    title_norm = title_norm_of(task)

    if decision == ArchiveDecision.SKIP:
        await repo.set_task_state(
            db, task.id, TaskState.ARCHIVED_SKIPPED, archive_decision=decision.value,
        )
        await remove_tree(scratch_path, root=Path(cfg.paths.scratch_inbox))
        await _emit(notifier, Event(
            kind="archive_skipped", site=task.site, title=task.title,
            detail={"existing_path": existing.path if existing else None, "task_id": task.id},
        ))
        return ArchiveOutcome(status="skipped", decision=decision, scratch_path=scratch_path)

    root = Path(
        cfg.paths.library_audiobook if kind.value == "audiobook" else cfg.paths.library_music
    )
    root.mkdir(parents=True, exist_ok=True)
    target = root / safe_dirname(title_norm, task.id)

    if decision == ArchiveDecision.UPGRADE and existing is not None:
        await _recycle_old_version(db, cfg, existing, reason=f"upgrade:{task.id}")

    target = unique_path(target, hint=(task.id or "")[:8])
    await copy_tree(scratch_path, target)

    score = quality_score(task, kind)
    if decision == ArchiveDecision.UPGRADE and existing is not None:
        await repo.update_library_item(
            db, existing.id, task_id=task.id, quality=task.quality,
            quality_score=score, path=str(target),
        )
    else:
        await repo.add_library_item(db, LibraryItem(
            watch_id=task.watch_entry_id, task_id=task.id, title_norm=title_norm,
            kind=kind, quality=task.quality, quality_score=score, path=str(target),
        ))

    await repo.set_task_state(
        db, task.id, TaskState.ROUTED,
        archived_path=str(target), archive_decision=decision.value,
    )
    await _emit(notifier, Event(
        kind="routed", site=task.site, title=task.title,
        detail={"decision": decision.value, "path": str(target), "kind": kind.value,
                "quality_score": score, "task_id": task.id},
    ))
    logger.info(f"任务 {task.id[:8]} 已归档 ({decision.value}): {target}")

    # 刮削：音乐让 MTW 处理 scratch 副本（成品库不被改写），有声书让 ABS 重扫库
    scrape_path = scratch_path if kind.value == "music" else target
    ok, msg = await trigger_for_kind(cfg.scrape, kind, scrape_path, task.title)
    if not ok:
        await _emit(notifier, Event(
            kind="scrape_failed", site=task.site, title=task.title, level="warning",
            detail={"message": msg, "kind": kind.value, "path": str(scrape_path)},
        ))

    return ArchiveOutcome(
        status="routed", decision=decision, path=target, scratch_path=scratch_path, message=msg,
    )


def recycle_slot(cfg, item_id: int | str | None, now=None) -> Path:
    """回收区落位：recycle_dir/<YYYYMMDD>/<library_item_id>/（冲突时加后缀）。"""
    day = (now or utcnow()).strftime("%Y%m%d")
    return unique_path(Path(cfg.paths.recycle_dir) / day / str(item_id or "unknown"))


async def _recycle_old_version(
    db, cfg, existing: LibraryItem, *, reason: str,
) -> RecycleEntry | None:
    """升级替换：把库内旧目录 move 进回收区并登记。旧目录已不存在则只记日志。"""
    old = Path(existing.path)
    if not old.exists():
        logger.warning(f"升级替换：旧版本路径已不存在，跳过回收 {old}")
        return None
    dst = recycle_slot(cfg, existing.id)
    await move_tree(old, dst)
    entry = RecycleEntry(
        library_item_id=existing.id, original_path=str(old), recycle_path=str(dst), reason=reason,
    )
    await repo.add_recycle_entry(db, entry)
    logger.info(f"旧版本已移入回收区: {old} → {dst}")
    return entry


async def purge_expired_recycle(db, cfg, *, now=None) -> int:
    """删除回收区里超过 retention_days 的条目（目录 + DB 行）。返回清理条数。

    只删位于 recycle_dir 之下的路径；围栏不过的条目跳过并保留 DB 行，
    让人在资料库页看得见、手工处理。
    """
    now = now or utcnow()
    before = now - timedelta(days=cfg.paths.recycle_retention_days)
    entries = await repo.list_expired_recycle_entries(db, before)
    if not entries:
        return 0
    recycle_root = Path(cfg.paths.recycle_dir)
    purged = 0
    for entry in entries:
        rp = Path(entry.recycle_path)
        if not path_within(rp, recycle_root):
            logger.error(f"回收区清理跳过：{rp} 不在 {recycle_root} 之下（条目 {entry.id}）")
            continue
        try:
            await remove_tree(rp, root=recycle_root)
        except OSError as e:
            logger.warning(f"回收区清理失败 {rp}: {e}")
            continue
        await repo.delete_recycle_entry(db, entry.id)
        purged += 1
        logger.info(f"回收区到期清理: {rp}")
    return purged
