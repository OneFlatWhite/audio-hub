"""订阅引擎（MoviePilot 式）：RSS 捕获 → 匹配 → 决策，以及到点的主动搜索。

与旧 pipeline/search.py + scheduler 的区别：
- 以「订阅」为中心：每条订阅独立决策、独立记命中流水（subscription_hits）；
- 专辑级去重：同一专辑的多个版本只取一个（站点优先级 → 音质 → 优惠 → 做种），
  已拥有的专辑跳过，开了洗版且更高档才升级；
- 一次性订阅（专辑型 / 非连载有声书）首次派发后自动完成；
- 引擎只建任务记录，**不调下载器**：派发由 dispatcher / scheduler（波次 3）负责。

请求纪律：每站请求间隔 ≥ MIN_REQUEST_INTERVAL_SECONDS，单轮每站最多 MAX_SEARCH_PER_SITE
次 search；预算不够的订阅整条顺延（不更新 next_search_at，下轮优先）。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

from .. import repo
from ..adapters.base import AdapterError, BaseAdapter, DiscountType, NormalizedTorrent
from ..models import (
    FoundVia,
    HitDecision,
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
from ..notify.base import Event
from ..pipeline.matcher import _CONFIDENCE_RANK, _DISCOUNT_WEIGHTS, SubMatch, match_subscription
from ..series.tracker import register_task
from ..timeutil import utcnow

# ── 事件种类（/events 页与推送按此过滤）──────────────────────
EVENT_SUBSCRIPTION_HIT = "subscription_hit"
EVENT_SUBSCRIPTION_COMPLETED = "subscription_completed"
EVENT_SUBSCRIPTION_UPGRADE = "subscription_upgrade"
#: 站点熔断刚打开时的事件（warning 级，每次打开只发一条）
EVENT_SITE_CIRCUIT_OPEN = "site_circuit_open"

# ── 请求纪律 ────────────────────────────────────────────────
#: 同站两次请求的最小间隔（秒）。模块级变量：测试可置 0。
MIN_REQUEST_INTERVAL_SECONDS = 2.0
#: 单轮每站最多发多少次 search
MAX_SEARCH_PER_SITE = 20
#: 单请求超时（秒），与 pipeline/search 一致
PER_REQUEST_TIMEOUT = 45.0

# ── 决策原因（写进 subscription_hits.reject_reason）────────────
REASON_OUTRANKED = "outranked_in_group"
REASON_ALREADY_OWNED = "already_owned"
REASON_NO_BETTER_QUALITY = "already_owned_no_better_quality"
REASON_AUTO_DOWNLOAD_DISABLED = "auto_download_disabled"

#: dry_run 下「本该派发」的任务与待审任务同为 scored，靠这个 reject_reason 区分；
#: 它不在 repo.PENDING_REVIEW_REASONS 里，不会混进待审队列。
DRY_RUN_REASON = "subscription_dry_run"


# ── Summary ─────────────────────────────────────────────────

def _count_by_decision(hits: Iterable[SubscriptionHit]) -> dict[str, int]:
    out: dict[str, int] = {}
    for h in hits:
        out[h.decision.value] = out.get(h.decision.value, 0) + 1
    return out


@dataclass
class CaptureSummary:
    """run_rss_capture_once 的结果。"""
    dry_run: bool = True
    sites_polled: list[str] = field(default_factory=list)
    skipped_sites: dict[str, str] = field(default_factory=dict)   # 站点 → 跳过原因
    site_errors: dict[str, str] = field(default_factory=dict)
    raw_count: int = 0                 # 各站 RSS 原始条数之和
    candidates: int = 0                # 去重后条数
    matched: int = 0                   # 命中任一订阅的 (候选, 订阅) 对数
    hits: list[SubscriptionHit] = field(default_factory=list)
    created_tasks: list[Task] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)            # 本轮转为 completed 的订阅 id

    def by_decision(self) -> dict[str, int]:
        return _count_by_decision(self.hits)


@dataclass
class SearchSummary:
    """run_subscription_search_once 的结果。"""
    dry_run: bool = True
    due: int = 0
    searched: list[str] = field(default_factory=list)           # 本轮完成搜索的订阅 id
    deferred: list[str] = field(default_factory=list)           # 预算不足顺延的订阅 id
    skipped_no_sites: list[str] = field(default_factory=list)   # 限定站点均不可用的订阅 id
    no_sites: bool = False                                      # 整轮没有任何可用站点
    requests: dict[str, int] = field(default_factory=dict)      # 站点 → 本轮 search 次数
    site_errors: dict[str, str] = field(default_factory=dict)
    skipped_sites: dict[str, str] = field(default_factory=dict)
    hits: list[SubscriptionHit] = field(default_factory=list)
    created_tasks: list[Task] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)

    def by_decision(self) -> dict[str, int]:
        return _count_by_decision(self.hits)


# ── 站点可用性 / 优先级 ──────────────────────────────────────

async def build_site_priority(db) -> dict[str, int]:
    """{站点 id 与 name: priority}，来自 sites 表（已启用）。供 decide 的组内择优。"""
    out: dict[str, int] = {}
    for s in await repo.list_sites(db, enabled_only=True):
        out[s.id] = s.priority
        out[s.name] = s.priority
    return out


def _adapter_site(key: str, adapter: BaseAdapter) -> str:
    return str(getattr(adapter, "site_name", None) or key)


def _auth_status_of(adapter: BaseAdapter) -> str:
    cfg = getattr(adapter, "config", None)
    status = getattr(cfg, "auth_status", None) if cfg is not None else None
    return str(getattr(status, "value", status) or "valid")


async def _usable_adapters(
    db, adapters: dict[str, BaseAdapter], skipped: dict[str, str],
) -> dict[str, BaseAdapter]:
    """只保留 sites 表里启用且 auth_status=valid 的站；表里没有记录的（测试替身）看适配器自带状态。"""
    records = {}
    for s in await repo.list_sites(db):
        records[s.id] = s
        records[s.name] = s
    kept: dict[str, BaseAdapter] = {}
    for key, adapter in adapters.items():
        site = _adapter_site(key, adapter)
        rec = records.get(key) or records.get(site)
        if rec is not None:
            if not rec.enabled:
                skipped[site] = "disabled"
                continue
            status = str(getattr(rec.auth_status, "value", rec.auth_status))
        else:
            status = _auth_status_of(adapter)
        if status != "valid":
            skipped[site] = f"auth_{status}"
            continue
        kept[key] = adapter
    return kept


# ── 适配器调用（签名容错 + 超时 + 熔断计数）──────────────────

def _accepts_kw(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _call_search(adapter: BaseAdapter, query: str, kind: str) -> list[NormalizedTorrent]:
    fn = adapter.search
    coro = fn(query, kind=kind) if _accepts_kw(fn, "kind") else fn(query)
    return list(await asyncio.wait_for(coro, PER_REQUEST_TIMEOUT) or [])


async def _call_rss(adapter: BaseAdapter, kind: str | None) -> list[NormalizedTorrent]:
    fn = adapter.fetch_rss
    coro = fn(kind=kind) if (kind and _accepts_kw(fn, "kind")) else fn()
    return list(await asyncio.wait_for(coro, PER_REQUEST_TIMEOUT) or [])


def _circuit_params(config) -> tuple[int, int]:
    search_cfg = getattr(config, "search", None)
    return (
        int(getattr(search_cfg, "circuit_fail_threshold", 3)),
        int(getattr(search_cfg, "circuit_open_minutes", 30)),
    )


async def _record_outcome(db, site: str, error: str | None, rows: int, config,
                          notifier=None) -> None:
    """站点健康计数是旁路，失败只记日志；刚打开熔断时发 site_circuit_open 事件。"""
    threshold, open_minutes = _circuit_params(config)
    try:
        if error is None:
            await repo.record_site_success(db, site, rows=rows)
            return
        opened = await repo.record_site_failure(
            db, site, error, threshold=threshold, open_minutes=open_minutes,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{site} 写 site_stats 失败: {type(e).__name__}: {e}")
        return
    if not opened:
        return
    stats = await repo.get_site_stats(db, site) or {}
    await _emit(notifier, Event(
        kind=EVENT_SITE_CIRCUIT_OPEN,
        site=site,
        title=f"{site} 连续失败 {stats.get('consecutive_fail', threshold)} 次，已熔断",
        detail={
            "until": stats.get("circuit_open_until"),
            "consecutive_fail": stats.get("consecutive_fail", threshold),
            "open_minutes": open_minutes,
            "last_error": error,
        },
        level="warning",
    ))


class _SiteThrottle:
    """每站请求节流：最小间隔 + 单轮预算。"""

    def __init__(self, budget: int | None = None) -> None:
        # 运行时读模块变量，测试 monkeypatch 才能生效
        self.budget = MAX_SEARCH_PER_SITE if budget is None else budget
        self.used: dict[str, int] = {}
        self._last: dict[str, float] = {}

    def remaining(self, site: str) -> int:
        return self.budget - self.used.get(site, 0)

    async def acquire(self, site: str) -> None:
        last = self._last.get(site)
        if last is not None and MIN_REQUEST_INTERVAL_SECONDS > 0:
            wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last[site] = time.monotonic()
        self.used[site] = self.used.get(site, 0) + 1


# ── 搜索词 ──────────────────────────────────────────────────

def build_queries(watch: WatchEntry) -> list[str]:
    """按订阅类型生成搜索词（去空去重，保持顺序）。

    artist：歌手名 + 别名逐个搜；album：「歌手 专辑」一次；
    audiobook：书名、作者各一次；keyword：关键词 + 别名。
    """
    if watch.sub_type == SubType.ARTIST:
        raw = [watch.artist or watch.query, *watch.aliases]
    elif watch.sub_type == SubType.ALBUM:
        raw = [f"{watch.artist or ''} {watch.album or watch.query}".strip()]
    elif watch.sub_type == SubType.AUDIOBOOK:
        raw = [watch.query, watch.author]
    else:
        raw = [watch.query, *watch.aliases]
    out: list[str] = []
    for q in raw:
        q = (q or "").strip()
        if q and q.lower() not in {x.lower() for x in out}:
            out.append(q)
    return out


def _dedupe(torrents: Iterable[NormalizedTorrent]) -> list[NormalizedTorrent]:
    seen: set[str] = set()
    out: list[NormalizedTorrent] = []
    for t in torrents:
        key = t.info_hash or f"{t.site}:{t.title_normalized or t.title}"
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


# ── 决策 ────────────────────────────────────────────────────

def _rank_key(m: SubMatch, site_priority: dict[str, int]) -> tuple:
    """组内择优：站点优先级 → 音质档 → 优惠 → 做种数 → 分数。"""
    t = m.torrent
    discount = _DISCOUNT_WEIGHTS.get(getattr(t, "discount", DiscountType.UNKNOWN), 0.0)
    return (
        site_priority.get(t.site, 0),
        quality_rank(m.quality_tag),
        discount,
        m.seeders if m.seeders is not None else 0,
        m.score,
    )


def is_one_shot(watch: WatchEntry) -> bool:
    """首次派发即完成的订阅：专辑型，或未绑连载的有声书型。

    公开给调度层：scheduler._dispatch_created 在实跑派发确认成功后据此
    决定要不要把订阅置 completed（decide 只在 dry_run 时当场完结，见下）。
    """
    if watch.sub_type == SubType.ALBUM:
        return True
    return watch.sub_type == SubType.AUDIOBOOK and watch.series_id is None


async def _resolve_downloader(db, watch: WatchEntry, config):
    """任务归属下载器：订阅指定 > 默认实例。返回 (downloader_id, category, save_path)。

    qb_tags 不进 Task（没有对应列）：派发时由 dispatcher 按 downloader_id / watch_id 回读。
    """
    rec = None
    if watch.downloader_id:
        rec = await repo.get_downloader(db, watch.downloader_id)
    if rec is None:
        rec = await repo.get_default_downloader(db)
    qb = getattr(config, "qbittorrent", None)
    category = watch.qb_category or (rec.category if rec else None) \
        or getattr(qb, "category", None)
    save_path = watch.save_path or (rec.save_path if rec else None) \
        or getattr(qb, "save_path", None)
    return (rec.id if rec else None), category or None, save_path or None


def _task_state(decision: HitDecision, dry_run: bool) -> tuple[TaskState, str | None]:
    if decision == HitDecision.REVIEW:
        return TaskState.SCORED, None
    if dry_run:
        return TaskState.SCORED, DRY_RUN_REASON
    # 真实派发交给 dispatcher / scheduler（波次 3）：引擎只把任务放到起点
    return TaskState.DISCOVERED, None


async def _create_task(
    db, watch: WatchEntry, m: SubMatch, decision: HitDecision, reason: str | None,
    *, config, dry_run: bool,
) -> Task:
    t = m.torrent
    state, state_reason = _task_state(decision, dry_run)
    downloader_id, category, save_path = await _resolve_downloader(db, watch, config)
    task = Task(
        watch_entry_id=watch.id, watch_id=watch.id,
        info_hash=t.info_hash, site=t.site, title=t.title,
        state=state, score=m.score, confidence=m.confidence,
        reject_reason=reason if decision == HitDecision.REVIEW else state_reason,
        qb_category=category, qb_save_path=save_path,
        download_url=t.download_url, size_bytes=t.size_bytes,
        quality=m.quality_tag, format_tags=list(t.format_tags),
        site_category=t.site_category,
        series_id=watch.series_id if watch.kind == WatchKind.AUDIOBOOK else None,
        downloader_id=downloader_id, album_key=m.album_key or None, quality_tag=m.quality_tag,
    )
    task.id = await repo.create_task(db, task)
    await repo.upsert_torrent_seen(db, TorrentRecord(
        info_hash=t.info_hash, site=t.site,
        title_normalized=t.title_normalized or t.title, task_id=task.id,
    ))
    return task


def _hit_of(m: SubMatch, decision: HitDecision, found_via: FoundVia,
            reason: str | None = None, task_id: str | None = None) -> SubscriptionHit:
    t = m.torrent
    discount = getattr(t, "discount", None)
    return SubscriptionHit(
        watch_id=m.watch.id, site=t.site, torrent_title=t.title, info_hash=t.info_hash or "",
        album_key=m.album_key or "", quality_tag=m.quality_tag,
        discount=getattr(discount, "value", discount) if discount else None,
        size_bytes=int(t.size_bytes or 0), seeders=int(m.seeders or 0), score=m.score,
        found_via=found_via, decision=decision, reject_reason=reason, task_id=task_id,
    )


async def _emit(notifier, event: Event) -> None:
    if notifier is None:
        return
    try:
        await notifier.emit(event)
    except Exception as e:  # noqa: BLE001 - 通知是旁路
        logger.warning(f"订阅事件发送失败 {event.kind}: {type(e).__name__}: {e}")


async def decide(
    db, watch: WatchEntry, candidates: list,
    *, config, notifier=None, found_via: FoundVia | str, dry_run: bool = True,
    site_priority: dict[str, int] | None = None, now: datetime | None = None,
) -> list[SubscriptionHit]:
    """对一条订阅的候选做决策，全部写入 subscription_hits，返回新写入的命中。

    candidates 可以是 NormalizedTorrent（引擎内先做 match_subscription）或现成的 SubMatch。
    流程：剔除已决策过的种子 → 按 album_key 分组 → 组内择优 → 去重 / 洗版 / 派发 / 待审。
    """
    via = FoundVia(found_via) if not isinstance(found_via, FoundVia) else found_via
    site_priority = site_priority or {}
    now = now or utcnow()

    matches: list[SubMatch] = []
    for c in candidates:
        m = c if isinstance(c, SubMatch) else match_subscription(watch, c)
        if not m.matched:
            continue
        if await repo.has_hit(db, watch.id, m.torrent.info_hash):
            continue
        matches.append(m)
    if not matches:
        return []

    groups: dict[str, list[SubMatch]] = {}
    for m in matches:
        key = m.album_key or f"#{m.torrent.info_hash or id(m)}"
        groups.setdefault(key, []).append(m)

    hits: list[SubscriptionHit] = []

    async def record(m: SubMatch, decision: HitDecision, reason: str | None = None,
                     task: Task | None = None) -> SubscriptionHit:
        hit = _hit_of(m, decision, via, reason, task.id if task else None)
        await repo.add_hit(db, hit)
        hits.append(hit)
        return hit

    for group in groups.values():
        accepted = [m for m in group if m.accepted]
        for m in group:
            if not m.accepted:
                await record(m, HitDecision.REJECTED, m.reject_reason or "filtered")
        if not accepted:
            continue
        accepted.sort(key=lambda m: _rank_key(m, site_priority), reverse=True)
        best = accepted[0]
        for m in accepted[1:]:
            await record(m, HitDecision.SKIPPED_DUP, REASON_OUTRANKED)

        decision, reason = await _decide_best(db, watch, best)
        if decision == HitDecision.SKIPPED_DUP:
            await record(best, decision, reason)
            continue

        task = await _create_task(db, watch, best, decision, reason, config=config, dry_run=dry_run)
        hit = await record(best, decision, reason, task)
        if decision == HitDecision.REVIEW:
            continue

        await repo.incr_hit(db, watch.id)
        watch.hit_count += 1
        await _emit(notifier, Event(
            kind=EVENT_SUBSCRIPTION_UPGRADE if decision == HitDecision.UPGRADE
            else EVENT_SUBSCRIPTION_HIT,
            site=best.torrent.site, title=best.torrent.title,
            detail={
                "watch_id": watch.id, "watch_query": watch.query,
                "sub_type": watch.sub_type.value, "album_key": best.album_key,
                "quality_tag": best.quality_tag, "decision": decision.value,
                "found_via": via.value, "task_id": task.id, "dry_run": dry_run,
            },
        ))
        if watch.kind == WatchKind.AUDIOBOOK and watch.series_id is not None:
            await register_task(db, task, notifier)

        # 一次性订阅的完结：dry_run 没有真实派发，命中即完成（维持现状）；
        # 实跑时任务才刚建为 DISCOVERED、还没碰 qb —— completed 延后到
        # scheduler._dispatch_created 确认派发成功，否则 qb 不在线 / 直链换取
        # 失败会把订阅永远卡在「已完成但没下到」，且不再重试。
        if dry_run and decision == HitDecision.DISPATCHED and is_one_shot(watch) \
                and watch.status != SubStatus.COMPLETED:
            await repo.set_sub_status(db, watch.id, SubStatus.COMPLETED, completed_at=now)
            watch.status = SubStatus.COMPLETED
            watch.completed_at = now
            await _emit(notifier, Event(
                kind=EVENT_SUBSCRIPTION_COMPLETED, site=best.torrent.site,
                title=f"订阅已完成：{watch.query}",
                detail={"watch_id": watch.id, "sub_type": watch.sub_type.value,
                        "album_key": best.album_key, "task_id": hit.task_id},
            ))
    return hits


async def _decide_best(db, watch: WatchEntry, best: SubMatch) -> tuple[HitDecision, str | None]:
    """组内最优候选的去向：已拥有 → 跳过 / 洗版；未拥有 → 派发 / 待审。"""
    if await repo.has_album(db, watch.id, best.album_key):
        if not watch.upgrade_enabled:
            return HitDecision.SKIPPED_DUP, REASON_ALREADY_OWNED
        current = await repo.best_quality_for_album(db, watch.id, best.album_key)
        if quality_rank(best.quality_tag) > quality_rank(current):
            return HitDecision.UPGRADE, None
        return HitDecision.SKIPPED_DUP, REASON_NO_BETTER_QUALITY

    threshold = _CONFIDENCE_RANK.get(watch.min_confidence, 2)
    actual = _CONFIDENCE_RANK.get(best.confidence, 0)
    if actual < threshold:
        return HitDecision.REVIEW, f"confidence_below_threshold({best.confidence}<{watch.min_confidence})"
    if not watch.auto_download:
        return HitDecision.REVIEW, REASON_AUTO_DOWNLOAD_DISABLED
    return HitDecision.DISPATCHED, None


# ── 入口 1：RSS 捕获 ─────────────────────────────────────────

async def _active_watches(db) -> list[WatchEntry]:
    return [w for w in await repo.list_watch_entries(db, enabled_only=True)
            if w.status == SubStatus.ACTIVE]


async def _collect_tasks(db, hits: list[SubscriptionHit], into: list[Task]) -> None:
    seen = {t.id for t in into}
    for h in hits:
        if h.task_id and h.task_id not in seen:
            task = await repo.get_task(db, h.task_id)
            if task is not None:
                into.append(task)
                seen.add(task.id)


async def run_rss_capture_once(
    db, adapters: dict[str, BaseAdapter], *, config, notifier=None,
    now: datetime | None = None, dry_run: bool = True,
) -> CaptureSummary:
    """拉一轮各站 RSS，对所有活跃订阅做匹配与决策。

    每站 RSS 只拉一次（适配器 fetch_rss 若接受 kind，则按活跃订阅涉及的类型各拉一次）。
    不受 search 预算限制，但同样受熔断与认证门禁约束。
    """
    now = now or utcnow()
    summary = CaptureSummary(dry_run=dry_run)
    watches = await _active_watches(db)
    if not watches:
        logger.info("RSS 捕获：没有活跃订阅，跳过")
        return summary
    usable = await _usable_adapters(db, adapters, summary.skipped_sites)
    if not usable:
        logger.warning("RSS 捕获：没有可用站点")
        return summary
    site_priority = await build_site_priority(db)
    kinds = sorted({w.kind.value for w in watches})

    raw: list[NormalizedTorrent] = []
    for key, adapter in usable.items():
        site = _adapter_site(key, adapter)
        if await repo.is_circuit_open(db, site):
            summary.skipped_sites[site] = "circuit_open"
            continue
        calls = kinds if _accepts_kw(adapter.fetch_rss, "kind") else [None]
        summary.sites_polled.append(site)
        for kind in calls:
            try:
                items = await _call_rss(adapter, kind)
            except TimeoutError:
                err = f"RSS 超时（>{PER_REQUEST_TIMEOUT}s）"
                summary.site_errors[site] = err
                await _record_outcome(db, site, err, 0, config, notifier)
                break
            except AdapterError as e:
                summary.site_errors[site] = f"RSS 失败: {e}"
                await _record_outcome(db, site, str(e), 0, config, notifier)
                break
            except Exception as e:  # noqa: BLE001 - 单站异常不得中断整轮
                err = f"RSS 未预期异常: {type(e).__name__}: {e}"
                summary.site_errors[site] = err
                logger.exception(f"{site} RSS 未预期异常")
                await _record_outcome(db, site, err, 0, config, notifier)
                break
            summary.raw_count += len(items)
            raw.extend(items)
            await _record_outcome(db, site, None, len(items), config)

    candidates = _dedupe(raw)
    summary.candidates = len(candidates)

    for watch in watches:
        pool = [t for t in candidates if not watch.sites or t.site in watch.sites]
        matches = [m for m in (match_subscription(watch, t) for t in pool) if m.matched]
        if not matches:
            continue
        summary.matched += len(matches)
        hits = await decide(
            db, watch, matches, config=config, notifier=notifier, found_via=FoundVia.RSS,
            dry_run=dry_run, site_priority=site_priority, now=now,
        )
        summary.hits.extend(hits)
        await _collect_tasks(db, hits, summary.created_tasks)
        if watch.status == SubStatus.COMPLETED:
            summary.completed.append(watch.id)

    logger.info(
        f"RSS 捕获完成：{len(summary.sites_polled)} 站 / 原始 {summary.raw_count} 条 → "
        f"候选 {summary.candidates} → 命中 {summary.matched}；决策 {summary.by_decision()}"
    )
    return summary


# ── 入口 2：主动搜索 ─────────────────────────────────────────

async def run_subscription_search_once(
    db, adapters: dict[str, BaseAdapter], *, config, notifier=None,
    now: datetime | None = None, dry_run: bool = True, only_watch_id: str | None = None,
) -> SearchSummary:
    """对到点的订阅逐条主动搜索（only_watch_id 指定时无视到点，立即搜那一条）。

    订阅按 priority 排序依次消耗各站预算；某条订阅在任一目标站预算不足就整条顺延，
    不更新 next_search_at，下轮它仍是「到点」的。
    """
    now = now or utcnow()
    summary = SearchSummary(dry_run=dry_run)

    if only_watch_id:
        w = await repo.get_watch_entry(db, only_watch_id)
        watches = [w] if w is not None else []
    else:
        watches = await repo.list_due_subscriptions(db, now)
    summary.due = len(watches)
    if not watches:
        return summary

    usable = await _usable_adapters(db, adapters, summary.skipped_sites)
    if not usable:
        summary.no_sites = True
        logger.warning("订阅搜索：没有可用站点（未启用 / 认证未通过）")
        return summary
    site_priority = await build_site_priority(db)
    throttle = _SiteThrottle()

    for watch in watches:
        targets = [
            (key, a) for key, a in usable.items()
            if not watch.sites or key in watch.sites or _adapter_site(key, a) in watch.sites
        ]
        if not targets:
            summary.skipped_no_sites.append(watch.id)
            continue
        queries = build_queries(watch)
        if not queries:
            summary.skipped_no_sites.append(watch.id)
            continue
        need = len(queries)
        if any(throttle.remaining(_adapter_site(k, a)) < need for k, a in targets):
            summary.deferred.append(watch.id)
            logger.info(f"订阅 {watch.query!r} 本轮站点预算不足，顺延到下轮")
            continue

        raw: list[NormalizedTorrent] = []
        for key, adapter in targets:
            site = _adapter_site(key, adapter)
            if await repo.is_circuit_open(db, site):
                summary.skipped_sites[site] = "circuit_open"
                continue
            for q in queries:
                await throttle.acquire(site)
                summary.requests[site] = summary.requests.get(site, 0) + 1
                try:
                    items = await _call_search(adapter, q, watch.kind.value)
                except TimeoutError:
                    err = f"search 超时（>{PER_REQUEST_TIMEOUT}s）"
                    summary.site_errors[site] = err
                    await _record_outcome(db, site, err, 0, config, notifier)
                    break
                except AdapterError as e:
                    summary.site_errors[site] = f"search 失败: {e}"
                    await _record_outcome(db, site, str(e), 0, config, notifier)
                    break
                except Exception as e:  # noqa: BLE001
                    err = f"search 未预期异常: {type(e).__name__}: {e}"
                    summary.site_errors[site] = err
                    logger.exception(f"{site} search({q!r}) 未预期异常")
                    await _record_outcome(db, site, err, 0, config, notifier)
                    break
                raw.extend(items)
                await _record_outcome(db, site, None, len(items), config)

        candidates = _dedupe(raw)
        matches = [m for m in (match_subscription(watch, t) for t in candidates) if m.matched]
        hits = await decide(
            db, watch, matches, config=config, notifier=notifier, found_via=FoundVia.SEARCH,
            dry_run=dry_run, site_priority=site_priority, now=now,
        )
        summary.hits.extend(hits)
        await _collect_tasks(db, hits, summary.created_tasks)
        if watch.status == SubStatus.COMPLETED:
            summary.completed.append(watch.id)
        await repo.mark_searched(db, watch.id, now)
        summary.searched.append(watch.id)

    logger.info(
        f"订阅搜索完成：到点 {summary.due} / 已搜 {len(summary.searched)} / "
        f"顺延 {len(summary.deferred)}；请求 {summary.requests}；决策 {summary.by_decision()}"
    )
    return summary
