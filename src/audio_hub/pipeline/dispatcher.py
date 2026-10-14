"""派发层：把采纳的种子交给 qBittorrent 下载。

Dry-run 是本模块的核心设计（PRD §12 / v0.1 空跑要求）：
- dry_run=True：只记录候选 + 写日志 + 落 DB 状态，**绝不调用 qb API**
- dry_run=False：真实派发，Conflict409Error 视为成功（幂等）

v0.6 多下载器：
- 构造接收 DownloaderRegistry；旧 `qb_config/client` 参数仍可用，内部包成单实例注册表
- 下载器解析顺序：task.downloader_id → watch.downloader_id → registry.default()
- save_path / category 覆盖顺序：task 级（qb_save_path/qb_category）→ watch 级
  （save_path/qb_category）→ 下载器默认；tags = 下载器 tag + watch.qb_tags + 站点名
- 下载器不存在 / 已禁用 / 客户端不可用 → 任务 FAILED，err=downloader_unavailable

安全约束：
- 派发前必须二次确认类型（防御性：matcher 已判，但这里是下载前最后关口）
- 实跑前经适配器 resolve_download_url 换取直链（馒头 genDlToken），dry-run 不发请求
- 实跑再下载 .torrent 算出**真实 info_hash** 并以字节投递（v0.7.7）：
  适配器合成哈希只为内部去重；完成监控 / 启动对账按 qb 里的真实哈希关联任务，
  不写回真实哈希，任务会被误判「qb 中不存在」
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..adapters.base import AdapterError, BaseAdapter, MediaCategory, NormalizedTorrent
from ..downloaders.registry import DownloaderHandle, DownloaderRegistry
from ..models import Task, TaskState, WatchEntry
from .matcher import MatchResult
from .torrent_file import fetch_and_hash

if TYPE_CHECKING:
    from ..config import DownloaderConfig

#: 站点名 → 适配器实例；未知站点返回 None
#: 用回调而非 dict：runner.adapters 会被设置页热重建整体替换，回调保证每次拿到最新实例
AdapterLookup = Callable[[str], BaseAdapter | None]

#: 错误码：目标下载器不存在 / 已禁用 / 客户端不可用
ERR_DOWNLOADER_UNAVAILABLE = "downloader_unavailable"


@dataclass
class DispatchTarget:
    """一次派发的落点：下载器 + 已按覆盖顺序解析好的 qb 参数。"""
    handle: DownloaderHandle
    save_path: str
    category: str
    tags: list[str]

    @property
    def downloader_id(self) -> str:
        return self.handle.id


@dataclass
class DispatchResult:
    """一次派发的结果。

    dispatched / failed 的元素类型随入口而异：dispatch() 为 MatchResult，
    dispatch_tasks() 为 Task。downloader_ids 记录每个 info_hash 实际落到的下载器。
    """
    dispatched: list[Any] = field(default_factory=list)   # 成功派发/记录
    failed: list[tuple[Any, str]] = field(default_factory=list)  # (项, 原因)
    dry_run: bool = True
    downloader_ids: dict[str, str] = field(default_factory=dict)  # info_hash → downloader_id

    @property
    def ok_count(self) -> int:
        return len(self.dispatched)


def _first_nonblank(*values: str | None, default: str) -> str:
    """按顺序取第一个非空白值（task → watch → 下载器默认）。"""
    for v in values:
        if v and v.strip():
            return v.strip()
    return default


def _merge_tags(*groups) -> list[str]:
    """合并多组 tag，去空去重并保持先后顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        if not group:
            continue
        items = [group] if isinstance(group, str) else group
        for raw in items:
            t = (raw or "").strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


class Dispatcher:
    """种子派发器。"""

    def __init__(
        self,
        qb_config: DownloaderConfig | None = None,
        client=None,
        dry_run: bool = True,
        adapter_lookup: AdapterLookup | None = None,
        *,
        registry: DownloaderRegistry | None = None,
        db=None,
    ):
        """
        Args:
            qb_config: （旧式单实例）qb 连接配置；给出且 registry 为 None 时包成单实例注册表
            client: （旧式单实例）qbittorrentapi.Client；dry_run=True 时可为 None
            dry_run: True 则不调用 qb API
            adapter_lookup: 站点名 → 适配器的查找函数，实跑前用于换取下载直链；
                            为 None 或查不到时 download_url 原样使用
            registry: 下载器注册表（v0.6）；与 qb_config 二选一
            db: dispatch_tasks 写回任务状态用；dispatch(matches) 不需要
        """
        if registry is None:
            if qb_config is None:
                raise ValueError("Dispatcher 需要 registry 或 qb_config 之一")
            # 旧式单实例：注册表 dry_run 固定 False，空跑由 Dispatcher 自身守卫，
            # 否则 dry_run 下 registry 会把测试注入的 client 吞成 None
            registry = DownloaderRegistry(
                [qb_config], client_factory=lambda _cfg: client, dry_run=False,
            )
        self.registry = registry
        self.dry_run = dry_run
        self.adapter_lookup = adapter_lookup
        self.db = db

    # ── 旧接口兼容：默认下载器的配置 / 客户端 ─────────────────

    @property
    def qb_config(self) -> DownloaderConfig | None:
        h = self.registry.default()
        return h.config if h else None

    @property
    def client(self):
        h = self.registry.default()
        return h.client if h else None

    # ── 下载器 / 目标解析 ─────────────────────────────────────

    def _pick_handle(self, *candidate_ids: str | None) -> DownloaderHandle | None:
        """按优先顺序取第一个存在且已启用的下载器；都不行则回落默认（也须已启用）。"""
        for did in candidate_ids:
            h = self.registry.get(did)
            if h is not None and h.config.enabled:
                return h
            if did and h is None:
                logger.warning(f"任务指定的下载器 {did!r} 不存在，回落默认下载器")
            elif did:
                logger.warning(f"任务指定的下载器 {did!r} 已禁用，回落默认下载器")
        h = self.registry.default()
        return h if (h is not None and h.config.enabled) else None

    def resolve_target(
        self,
        *,
        site: str,
        task_downloader_id: str | None = None,
        task_save_path: str | None = None,
        task_category: str | None = None,
        watch: WatchEntry | None = None,
    ) -> DispatchTarget | None:
        """解析派发落点；无可用下载器时返回 None。"""
        handle = self._pick_handle(
            task_downloader_id, watch.downloader_id if watch else None,
        )
        if handle is None:
            return None
        cfg = handle.config
        save_path = _first_nonblank(
            task_save_path, watch.save_path if watch else None, default=cfg.save_path,
        )
        category = _first_nonblank(
            task_category, watch.qb_category if watch else None, default=cfg.category,
        )
        tags = _merge_tags(cfg.tag, watch.qb_tags if watch else None, site)
        return DispatchTarget(handle=handle, save_path=save_path, category=category, tags=tags)

    # ── 直链解析 ───────────────────────────────────────────────

    async def resolve_download_url(
        self, torrent_or_url: NormalizedTorrent | str, site: str | None = None,
    ) -> str:
        """按站点适配器把 download_url 解析为可交给 qb 的直链。

        公开方法：除流水线实跑派发外，/review 采纳后的单任务派发（tasks 表里只有
        site + download_url 字符串）也应走此入口，否则馒头占位 URL 会被原样交给 qb。

        Args:
            torrent_or_url: NormalizedTorrent（site 取自其字段）或 URL 字符串
            site: 传 URL 字符串时必填，用于定位适配器

        Raises:
            AdapterError: 适配器换取直链失败
        """
        if isinstance(torrent_or_url, NormalizedTorrent):
            site = torrent_or_url.site
            url = torrent_or_url.download_url
        else:
            url = torrent_or_url or ""

        adapter = self.adapter_lookup(site) if (self.adapter_lookup and site) else None
        if adapter is None:
            # 无适配器（站点已禁用 / 未注入）：NexusPHP 直链本就可用，原样返回
            return url
        return await adapter.resolve_download_url(torrent_or_url)

    # ── 入口 1：匹配结果（旧 search_round 路径 / 单元测试） ────────

    async def dispatch(self, matches: list[MatchResult]) -> DispatchResult:
        """派发一批已采纳的种子。

        注意：本方法接收的是 matcher 输出中 action=='accept' 的项。
        调用方应先过滤，但此处再做一次防御性过滤。
        """
        result = DispatchResult(dry_run=self.dry_run)
        accepted = [m for m in matches if m.action == "accept"]

        if not accepted:
            logger.info("无采纳项，派发跳过")
            return result

        mode = "DRY-RUN（不实际下载）" if self.dry_run else "实跑"
        logger.info(f"开始派发 {len(accepted)} 个种子 | 模式: {mode}")

        for m in accepted:
            t = m.torrent
            # ── 防御性二次确认 ──
            if t.category == MediaCategory.VIDEO:
                result.failed.append((m, "defensive_block: VIDEO 类型不应进入派发"))
                logger.error(f"防御拦截：VIDEO 种子意外进入派发队列 {t.title[:60]!r}")
                continue
            if not t.download_url:
                result.failed.append((m, "no_download_url"))
                logger.warning(f"跳过无下载链接的候选: {t.title[:60]!r}")
                continue

            target = self.resolve_target(site=t.site, watch=m.watch_entry)
            if target is None:
                result.failed.append((m, ERR_DOWNLOADER_UNAVAILABLE))
                logger.error(f"无可用下载器，跳过 {t.title[:60]!r}")
                continue

            if self.dry_run:
                self._record_dry_run(t, target, extra=f"score={m.score} | conf={m.confidence}")
                ok, _err = True, ""
            else:
                ok, _err, _hash = await self._dispatch_real(t, target)
            if ok:
                result.dispatched.append(m)
                result.downloader_ids[t.info_hash] = target.downloader_id
            else:
                result.failed.append((m, _err))

        logger.info(
            f"派发完成：成功 {result.ok_count} / 失败 {len(result.failed)} | {mode}"
        )
        return result

    # ── 入口 2：引擎建好的 DISCOVERED 任务（v0.6 主路径） ──────────

    async def dispatch_tasks(self, tasks: list[Task]) -> DispatchResult:
        """派发订阅引擎建好的任务，并把结果写回 tasks 表。

        成功 → DISPATCHED + downloader_id/qb_save_path/qb_category；
        失败 → FAILED + error_message。dry_run 只记录不写库、不碰 qb。
        """
        result = DispatchResult(dry_run=self.dry_run)
        if not tasks:
            return result

        mode = "DRY-RUN（不实际下载）" if self.dry_run else "实跑"
        logger.info(f"开始派发 {len(tasks)} 个任务 | 模式: {mode}")

        for task in tasks:
            if not task.download_url:
                await self._fail_task(task, "no_download_url", result)
                continue

            watch = None
            watch_id = task.watch_id or task.watch_entry_id
            if watch_id and self.db is not None:
                from .. import repo
                watch = await repo.get_watch_entry(self.db, watch_id)

            target = self.resolve_target(
                site=task.site,
                task_downloader_id=task.downloader_id,
                task_save_path=task.qb_save_path,
                task_category=task.qb_category,
                watch=watch,
            )
            if target is None:
                await self._fail_task(task, ERR_DOWNLOADER_UNAVAILABLE, result)
                continue

            torrent = NormalizedTorrent(
                site=task.site, title=task.title, info_hash=task.info_hash,
                size_bytes=task.size_bytes or 0, download_url=task.download_url,
            )
            if self.dry_run:
                self._record_dry_run(torrent, target)
                ok, err = True, ""
            else:
                ok, err, real_hash = await self._dispatch_real(torrent, target)
                # _dispatch_real 已把真实哈希写回 torrent；同步到任务记录，
                # 完成监控 / 启动对账按它与 qb 种子关联
                if ok and real_hash and real_hash != task.info_hash:
                    task.info_hash = real_hash

            if not ok:
                await self._fail_task(task, err, result)
                continue
            result.dispatched.append(task)
            result.downloader_ids[task.info_hash] = target.downloader_id
            if not self.dry_run and self.db is not None:
                from .. import repo
                await repo.set_task_state(
                    self.db, task.id, TaskState.DISPATCHED,
                    downloader_id=target.downloader_id,
                    qb_save_path=target.save_path,
                    qb_category=target.category,
                    info_hash=task.info_hash,
                )
                task.downloader_id = target.downloader_id
                task.state = TaskState.DISPATCHED

        logger.info(
            f"派发完成：成功 {result.ok_count} / 失败 {len(result.failed)} | {mode}"
        )
        return result

    async def _fail_task(self, task: Task, err: str, result: DispatchResult) -> None:
        result.failed.append((task, err))
        logger.error(f"派发失败 | {task.site} | {task.title[:60]!r} | {err}")
        if self.dry_run or self.db is None:
            return
        from .. import repo
        await repo.set_task_state(
            self.db, task.id, TaskState.FAILED, error_message=err[:500],
        )
        task.state = TaskState.FAILED

    # ── Dry-run ─────────────────────────────────────────────

    def _record_dry_run(self, t: NormalizedTorrent, target: DispatchTarget, extra: str = "") -> None:
        """空跑模式：只记录，不碰 qb。"""
        logger.info(
            f"[DRY-RUN] 将派发 | site={t.site} | downloader={target.downloader_id} | "
            f"{extra + ' | ' if extra else ''}size={(t.size_bytes or 0)/1024**3:.2f}GB | "
            f"cat={target.category} | save={target.save_path} | tags={target.tags} | "
            f"title={t.title[:70]!r}"
        )

    # ── 实跑 ────────────────────────────────────────────────

    async def _dispatch_real(
        self, t: NormalizedTorrent, target: DispatchTarget,
    ) -> tuple[bool, str, str | None]:
        """真实派发到目标 qb：换直链 → 拉 .torrent 算真实哈希 → 投递。

        返回 (成功?, 错误原因, 真实 info_hash)。真实哈希取不到（magnet 无字节 /
        拉取失败）时为 None，调用方保留原哈希；取到时已写回 t.info_hash。
        """
        from qbittorrentapi import Conflict409Error, LoginFailed
        from qbittorrentapi.exceptions import APIConnectionError

        client = await target.handle.ensure_client()
        if client is None:
            logger.error(f"实跑模式但下载器 {target.downloader_id} 客户端不可用")
            return False, ERR_DOWNLOADER_UNAVAILABLE, None

        # ── 直链换取（馒头 genDlToken 等）：失败则标 failed，不碰 qb ──
        try:
            url = await self.resolve_download_url(t)
        except AdapterError as e:
            logger.error(f"换取下载直链失败 | {t.site} | {t.title[:60]!r} | {e}")
            return False, f"resolve_download_url_failed: {e}", None
        except Exception as e:  # noqa: BLE001 - 单条解析异常不影响其余派发
            logger.exception(f"换取下载直链未预期异常 | {t.site} | {e}")
            return False, f"resolve_download_url_failed: {type(e).__name__}: {e}", None
        if not url:
            logger.error(f"换取下载直链返回空值 | {t.site} | {t.title[:60]!r}")
            return False, "resolve_download_url_failed: 解析结果为空", None

        # ── 真实 info_hash：拉 .torrent 算 sha1；qb 入库后只认这个哈希 ──
        real_hash, blob = await fetch_and_hash(url)
        if real_hash and real_hash != t.info_hash:
            logger.info(f"真实 info_hash {real_hash[:12]}… 替换合成哈希 {t.info_hash[:12]}…")
            t.info_hash = real_hash

        try:
            add_kwargs = {
                "save_path": target.save_path,
                "category": target.category,
                "tags": target.tags,
                "is_paused": False,
            }
            # 客户端是同步 requests 库：必须经 handle.call 进线程（per-client 锁
            # 串行化），否则一次扫描多个命中会把事件循环卡成串行 HTTP
            if blob is not None:
                await target.handle.call(client.torrents_add, torrent_files=blob, **add_kwargs)
            else:
                await target.handle.call(client.torrents_add, urls=url, **add_kwargs)
            logger.info(f"已派发到 {target.downloader_id} | {t.site} | {t.title[:60]!r}")
            return True, "", real_hash
        except Conflict409Error:
            # 种子已存在（相同 info_hash）→ 视为成功，保证幂等
            logger.info(f"种子已存在（幂等跳过）| {t.site} | {t.title[:60]!r}")
            return True, "", real_hash
        except LoginFailed as e:
            logger.critical(f"下载器 {target.downloader_id} 认证失败，检查凭据: {e}")
            return False, f"qb 认证失败（凭据无效？）: {e}", None
        except APIConnectionError as e:
            logger.error(f"下载器 {target.downloader_id} 连接失败: {e}")
            return False, f"qb 连接失败: {e}", None
        except Exception as e:  # noqa: BLE001
            logger.exception(f"派发未预期异常: {e}")
            return False, f"{type(e).__name__}: {e}", None
