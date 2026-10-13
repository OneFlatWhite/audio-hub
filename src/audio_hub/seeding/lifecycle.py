"""做种生命周期：从 qb 同步做种时长 / 分享率，判定 H&R 达标，执行 keep / pause。

红线（PRD §8.2）：本模块**绝不**调用任何删除种子或文件的接口。
达标后的动作只有两种：keep（不动）与 pause（暂停做种，种子与文件原样保留）。

与 completion.monitor 的分工：那边推进任务状态机（downloading → completed），
这边只关心 completed 之后的做种义务；两者都以 qb 的 hub 标签为取数范围，
互不写对方的表。

判定口径：`seed_seconds >= required_seconds **或** ratio >= required_ratio`，
任一满足即达标 —— 多数 PT 站的 H&R 规则就是二选一，取「或」是对使用者最宽松
也最不会误暂停的口径。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from loguru import logger

from .. import repo
from ..models import SeedRecord
from ..notify.base import Event
from ..timeutil import utcnow

# 对账时一次性拉取的记录上限：seed_records 与任务同量级，远低于此值
_ALL_RECORDS = 100_000

# qb 里仍在跟踪（可能被移除）的状态；removed 自身不再参与对账
_TRACKED_STATUSES = ("seeding", "satisfied", "paused")


class QbUnavailable(RuntimeError):
    """qb 未配置或不可达。路由层据此 flash 错误，而不是 500。"""


@dataclass
class SeedingSummary:
    """一次轮询的统计。skipped 非空表示本轮根本没查到 qb（原因文本）。"""
    checked: int = 0            # 本轮在 qb 中找到且有对应任务的种子数
    newly_satisfied: int = 0    # 本轮首次达标的记录数
    gone: int = 0               # qb 中已不存在、被标记 removed 的记录数
    created: int = 0            # 本轮新建档的记录数
    paused: int = 0             # 本轮因达标而实际下发暂停的种子数
    skipped: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _build_client(qb_config):
    """按 cfg.type 构造客户端（qb / Transmission）；qb 未配置凭据返回 None。

    统一走 downloaders.client.build_client：Transmission 有独立词表翻译层。
    不在此处做显式登录验证：客户端惰性连接（Transmission 构造即握手），
    不可达的错误统一在调用点捕获。
    """
    from ..downloaders.client import build_client
    return build_client(qb_config, login=False)


def is_hr_satisfied(
    seed_seconds: int, ratio: float,
    required_seconds: int | None, required_ratio: float | None,
) -> bool:
    """H&R 达标判定：时长或分享率任一满足。要求为 None 的维度不参与判定。"""
    if required_seconds is not None and seed_seconds >= required_seconds:
        return True
    return required_ratio is not None and ratio >= required_ratio


def progress_pct(rec: SeedRecord) -> int:
    """展示用进度：取时长进度与分享率进度的较大者，0–100。"""
    pcts = [0.0]
    if rec.required_seconds:
        pcts.append(rec.seed_seconds / rec.required_seconds)
    if rec.required_ratio:
        pcts.append(rec.ratio / rec.required_ratio)
    return int(min(max(pcts), 1.0) * 100)


class SeedingMonitor:
    """H&R 跟踪器：建档、同步进度、判定达标、keep/pause、对账 removed。

    v0.6 多下载器：给了 registry 时逐个已启用下载器轮询，每台只处理 downloader_id
    归属它的任务；单台不可达只跳过该台，且该台的记录**不**参与 removed 判定。
    """

    def __init__(self, db, config, notifier=None, qb_client_factory=None, registry=None):
        """
        Args:
            db: aiosqlite 连接
            config: AppConfig
            notifier: 实现 `async emit(Event)` 的通知器；None 则只写日志
            qb_client_factory: （旧式单实例）无参可调用，返回 qbittorrentapi.Client 或 None。
                返回 None（dry_run 下不构造客户端）时回落到按配置自建 ——
                dry_run 仍要**只读**轮询，否则空跑期间看不到任何做种数据。
            registry: DownloaderRegistry（v0.6）；给出时按下载器分流，客户端取
                handle.client，为 None（dry_run）时按 handle.config 自建只读客户端并缓存。
        """
        self.db = db
        self.config = config
        self.notifier = notifier
        self.registry = registry
        self._qb_client_factory = qb_client_factory
        self._own_client = None
        self._own_clients: dict[str, object] = {}
        self._running = False
        self._site_cfg = {s.name: s for s in config.sites}

    def refresh_sites(self, sites) -> None:
        """刷新站点快照（站点级 H&R 覆盖等）。

        构造时的 config.sites 是一份快照；设置页改站点（rebuild_adapters 整体替换
        config.sites）后若不刷新，requirements_for 会一直用旧阈值到重启 ——
        界面显示「已生效」而轮询实际按旧规则跑，是最伤信任的隐错。
        """
        self._site_cfg = {s.name: s for s in sites}

    # ── qb 客户端 ────────────────────────────────────────────

    def _get_client(self):
        """旧式单实例：工厂优先，否则按 config.qbittorrent 自建。"""
        client = self._qb_client_factory() if self._qb_client_factory else None
        if client is not None:
            return client
        if self._own_client is None:
            self._own_client = _build_client(self.config.qbittorrent)
        return self._own_client

    def reset_clients(self) -> None:
        """下载器配置热重建后丢掉自建的客户端缓存，下轮按新配置重建。"""
        self._own_client = None
        self._own_clients.clear()

    def _client_for(self, downloader_id: str | None):
        """取某下载器的客户端；None 表示未配置凭据 / 下载器不存在。"""
        if self.registry is None:
            return self._get_client()
        handle = self.registry.get(downloader_id) or self.registry.default()
        if handle is None:
            return None
        if handle.client is not None:
            return handle.client
        if handle.id not in self._own_clients:
            self._own_clients[handle.id] = _build_client(handle.config)
        return self._own_clients[handle.id]

    def _require_client(self, downloader_id: str | None = None):
        try:
            client = self._client_for(downloader_id)
        except Exception as e:  # 构造失败与不可达同等处理
            raise QbUnavailable(f"{type(e).__name__}: {e}") from e
        if client is None:
            raise QbUnavailable("qb 未配置凭据或下载器不存在")
        return client

    def _downloaders(self) -> list[tuple[str | None, str]]:
        """本轮要轮询的 [(downloader_id, tag)]；旧单实例为 [(None, 全局 tag)]。"""
        if self.registry is None:
            return [(None, self.config.qbittorrent.tag)]
        return [
            (h.id, h.config.tag or self.config.qbittorrent.tag)
            for h in self.registry.all_enabled()
        ]

    def _qb_action(self, action: str, info_hash: str, downloader_id: str | None = None) -> bool:
        """对单个种子执行 pause / resume。dry_run 下只记日志，返回 False。

        刻意写成显式分支而非 getattr(client, f"torrents_{action}")：
        让「本模块只会调这两个接口」在代码上一眼可见（PRD §8.2 红线）。
        """
        if self.config.dry_run:
            logger.info(f"[DRY-RUN] 跳过 qb {action}: {info_hash[:12]}")
            return False
        client = self._require_client(downloader_id)
        try:
            if action == "pause":
                client.torrents_pause(torrent_hashes=info_hash)
            elif action == "resume":
                client.torrents_resume(torrent_hashes=info_hash)
            else:  # pragma: no cover - 调用方只会传上面两个值
                raise ValueError(f"未知动作 {action}")
        except Exception as e:
            raise QbUnavailable(f"qb {action} 失败: {type(e).__name__}: {e}") from e
        return True

    # ── 阈值 ────────────────────────────────────────────────

    def requirements_for(self, site: str) -> tuple[int, float]:
        """站点级 H&R 覆盖优先，None 回落到 seeding.default_*。返回 (秒, 分享率)。"""
        sc = self._site_cfg.get(site)
        seeding = self.config.seeding
        hours = seeding.default_min_seed_hours
        ratio = seeding.default_min_ratio
        if sc is not None:
            if sc.hr_min_seed_hours is not None:
                hours = sc.hr_min_seed_hours
            if sc.hr_min_ratio is not None:
                ratio = sc.hr_min_ratio
        return int(hours * 3600), float(ratio)

    # ── 周期轮询 ─────────────────────────────────────────────

    async def poll_once(self) -> SeedingSummary:
        """同步一轮做种进度。qb 不可达时记 warning 返回空摘要，**不抛**。"""
        if self._running:
            logger.warning("上一轮做种轮询尚未结束，本轮跳过")
            return SeedingSummary(skipped="busy")
        self._running = True
        try:
            return await self._poll_inner()
        finally:
            self._running = False

    async def _poll_inner(self) -> SeedingSummary:
        summary = SeedingSummary()
        now = utcnow()
        polled: dict[str | None, dict] = {}     # 成功轮询的下载器 → {hash: torrent}
        errors: list[str] = []

        for downloader_id, tag in self._downloaders():
            try:
                client = self._require_client(downloader_id)
                torrents = list(client.torrents_info(tag=tag))
            except Exception as e:  # noqa: BLE001 - 旁路任务：qb 问题不能让调度器停摆
                label = downloader_id or "qb"
                logger.warning(f"做种轮询跳过下载器 {label}：不可达（{e}）")
                errors.append(f"{label}: {e}")
                continue

            qb_by_hash = {
                str(t.hash).lower(): t for t in torrents if getattr(t, "hash", None)
            }
            polled[downloader_id] = qb_by_hash

            # tasks.info_hash 大小写取决于站点，两种写法都查一遍再按小写归并
            hashes = list(qb_by_hash)
            briefs = await repo.get_task_briefs(
                self.db, info_hashes=hashes + [h.upper() for h in hashes],
            ) if hashes else []
            task_by_hash = {
                b["info_hash"].lower(): b for b in briefs
                if b["info_hash"] and self._owns(downloader_id, b)
            }
            for h, t in qb_by_hash.items():
                brief = task_by_hash.get(h)
                if brief is None:
                    continue        # 同标签但非中枢派发（或归属别台）的种子，不建档
                await self._sync_one(brief, t, now, summary)

        if not polled:
            summary.skipped = "; ".join(errors) or "no_downloaders"
            return summary

        summary.gone = await self._mark_removed(polled)
        await self.db.commit()

        if summary.checked or summary.gone:
            logger.info(f"做种轮询完成: {summary.as_dict()}")
        return summary

    def _owns(self, downloader_id: str | None, brief: dict) -> bool:
        """任务是否归属该下载器；旧单实例（None）不区分。"""
        return downloader_id is None or (brief.get("downloader_id") or "default") == downloader_id

    async def _sync_one(self, brief: dict, t, now, summary: SeedingSummary) -> None:
        task_id = brief["id"]
        seed_seconds = int(getattr(t, "seeding_time", 0) or 0)
        ratio = float(getattr(t, "ratio", 0.0) or 0.0)

        rec = await repo.get_seed_record(self.db, task_id)
        if rec is None:
            req_s, req_r = self.requirements_for(brief["site"])
            rec = SeedRecord(
                task_id=task_id, info_hash=str(t.hash).lower(), site=brief["site"],
                started_at=now, required_seconds=req_s, required_ratio=req_r,
            )
            summary.created += 1
            logger.info(
                f"做种建档: {brief['site']} {brief['title'][:50]!r} "
                f"要求 {req_s // 3600}h / ratio {req_r:.2f}"
            )
        elif rec.status == "removed":
            # 曾被判定移除又重新出现（人工重新添加）→ 恢复跟踪，达标史保留
            rec.status = "satisfied" if rec.hr_satisfied_at else "seeding"

        rec.seed_seconds = seed_seconds
        rec.ratio = ratio
        rec.updated_at = now
        summary.checked += 1

        # 手工 paused 的记录只刷数值不改状态；唯一例外是首次达标
        if rec.hr_satisfied_at is None and is_hr_satisfied(
            seed_seconds, ratio, rec.required_seconds, rec.required_ratio,
        ):
            rec.status = "satisfied"
            rec.hr_satisfied_at = now
            summary.newly_satisfied += 1
            await self._on_satisfied(rec, brief["title"], summary, brief.get("downloader_id"))

        await repo.upsert_seed_record(self.db, rec)

    async def _on_satisfied(
        self, rec: SeedRecord, title: str, summary, downloader_id: str | None = None,
    ) -> None:
        action = self.config.seeding.on_satisfied
        logger.info(
            f"H&R 达标: {rec.site} {title[:50]!r} | "
            f"{rec.seed_seconds / 3600:.1f}h / ratio {rec.ratio:.2f} → {action}"
        )
        await self._emit_satisfied(rec, title, action=action, manual=False)
        if action != "pause":
            return
        # 复用 _qb_action：dry_run 下只记日志返回 False，qb 失败抛 QbUnavailable
        try:
            if self._qb_action("pause", rec.info_hash, downloader_id):
                summary.paused += 1
        except QbUnavailable as e:  # 暂停失败不影响达标记录，下轮不再重试（已达标）
            logger.warning(f"达标后暂停失败 {rec.info_hash[:12]}: {e}")

    async def _mark_removed(self, polled: dict[str | None, dict]) -> int:
        """seed_record 存在但其下载器中已无该 hash → removed（人工删种 / 换客户端）。

        只对**本轮成功轮询**的下载器做判定：某台不可达时其记录原样保留，
        否则一次网络抖动就会误标整台种子。
        """
        gone = 0
        records: list[SeedRecord] = []
        for st in _TRACKED_STATUSES:
            records.extend(await repo.list_seed_records(self.db, status=st, limit=_ALL_RECORDS))
        if not records:
            return 0

        if None in polled:          # 旧单实例：所有记录都对照同一台
            owner_of = dict.fromkeys((r.task_id for r in records), None)
        else:
            briefs = await repo.get_task_briefs(self.db, task_ids=[r.task_id for r in records])
            owner_of = {b["id"]: (b.get("downloader_id") or "default") for b in briefs}

        for rec in records:
            owner = owner_of.get(rec.task_id, "default" if None not in polled else None)
            qb_by_hash = polled.get(owner)
            if qb_by_hash is None:
                continue            # 该台本轮未成功轮询 / 下载器已禁用，不判定
            if rec.info_hash.lower() in qb_by_hash:
                continue
            await repo.update_seed_record(self.db, rec.task_id, status="removed")
            gone += 1
            logger.warning(f"做种记录 {rec.task_id[:8]} 在 qb 中已不存在，标记 removed")
        return gone

    async def _emit_satisfied(self, rec: SeedRecord, title: str, *, action: str, manual: bool):
        if self.notifier is None:
            return
        event = Event(
            kind="hr_satisfied", site=rec.site, title=title, level="info",
            detail={
                "task_id": rec.task_id,
                "info_hash": rec.info_hash,
                "seed_hours": round(rec.seed_seconds / 3600, 1),
                "ratio": round(rec.ratio, 2),
                "required_hours": (rec.required_seconds or 0) // 3600,
                "required_ratio": rec.required_ratio,
                "action": action,
                "manual": manual,
            },
        )
        try:
            await self.notifier.emit(event)
        except Exception as e:  # noqa: BLE001 - 通知是旁路
            logger.warning(f"hr_satisfied 事件发送失败: {e}")

    # ── 手动动作（/seeding 页按钮）───────────────────────────

    async def _title_of(self, task_id: str) -> str:
        briefs = await repo.get_task_briefs(self.db, task_ids=[task_id])
        return briefs[0]["title"] if briefs else ""

    async def _downloader_of(self, task_id: str) -> str | None:
        briefs = await repo.get_task_briefs(self.db, task_ids=[task_id])
        return briefs[0].get("downloader_id") if briefs else None

    async def pause(self, task_id: str) -> SeedRecord | None:
        """手动暂停：qb pause + status=paused。无记录返回 None；qb 失败抛 QbUnavailable。"""
        rec = await repo.get_seed_record(self.db, task_id)
        if rec is None:
            return None
        self._qb_action("pause", rec.info_hash, await self._downloader_of(task_id))
        await repo.update_seed_record(self.db, task_id, status="paused")
        rec.status = "paused"
        return rec

    async def resume(self, task_id: str) -> SeedRecord | None:
        """手动恢复：qb resume + 状态回到 satisfied（曾达标）或 seeding。"""
        rec = await repo.get_seed_record(self.db, task_id)
        if rec is None:
            return None
        self._qb_action("resume", rec.info_hash, await self._downloader_of(task_id))
        rec.status = "satisfied" if rec.hr_satisfied_at else "seeding"
        await repo.update_seed_record(self.db, task_id, status=rec.status)
        return rec

    async def mark_satisfied(self, task_id: str) -> SeedRecord | None:
        """手动标记达标（站点已确认免除 H&R 等场景）。幂等：已达标则只刷状态。"""
        rec = await repo.get_seed_record(self.db, task_id)
        if rec is None:
            return None
        first_time = rec.hr_satisfied_at is None
        rec.status = "satisfied"
        rec.hr_satisfied_at = rec.hr_satisfied_at or utcnow()
        await repo.update_seed_record(
            self.db, task_id, status="satisfied", hr_satisfied_at=rec.hr_satisfied_at,
        )
        if first_time:
            await self._emit_satisfied(
                rec, await self._title_of(task_id), action="manual", manual=True,
            )
        return rec
