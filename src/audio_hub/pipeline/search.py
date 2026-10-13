"""搜索编排层：并发调用适配器，聚合候选，隔离单站失败。

设计原则：
- 单站失败不影响其他站（NexusPHP search 不可靠是常态）
- search 失败自动 fallback 到全量 RSS（NexusPHP 特有降级路径）
- 每站独立超时，避免慢站拖垮整轮
- 请求预算受控：watchlist × 站点数 = 请求数，需在调度层限流
- 站点熔断（v0.5）：连续 N 次失败后该站熔断 M 分钟，熔断期内整轮跳过不发请求。
  触发风控的后果落在账号上，宁可漏抓一轮也不要在站点已经报错时继续打。

熔断与计数依赖 db / notifier，二者均为可选 keyword-only 参数：不传时
search_round 保持纯函数行为（内存态测试与脚本沿用旧调用方式不受影响）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from loguru import logger

from .. import repo
from ..adapters.base import AdapterError, BaseAdapter, NormalizedTorrent
from ..models import WatchEntry
from ..notify.base import Event
from ..timeutil import utcnow

# 单站单请求超时（秒）
_PER_REQUEST_TIMEOUT = 45.0

#: 熔断默认值。与 config.SearchConfig 同值；scheduler 从 config 读后传入，
#: 这里的常量只服务于不带 config 的直接调用。
DEFAULT_CIRCUIT_FAIL_THRESHOLD = 3
DEFAULT_CIRCUIT_OPEN_MINUTES = 30

#: 熔断打开时发出的事件种类（/events 页与推送可按此过滤）
EVENT_SITE_CIRCUIT_OPEN = "site_circuit_open"

#: v0.6 认证门禁：站点因 auth_status != valid 被整轮跳过时的事件（每站每日最多一条）
EVENT_SITE_AUTH_INVALID_SKIPPED = "site_auth_invalid_skipped"


def _auth_status_of(adapter: BaseAdapter) -> str:
    """取适配器所属站点的 auth_status；没有 config / 字段（测试替身、旧脚本）视为 valid。"""
    cfg = getattr(adapter, "config", None)
    status = getattr(cfg, "auth_status", None) if cfg is not None else None
    return str(getattr(status, "value", status) or "valid")


async def _skip_auth_invalid(
    adapters: dict[str, BaseAdapter], result: SearchRoundResult, db, notifier,
) -> dict[str, BaseAdapter]:
    """轮次开头剔除 auth_status != valid 的站，并记 site_auth_invalid_skipped 事件。

    build_adapters 默认已不构造这类站，这里是第二道闸：适配器是启动时构造的，
    中途认证失效（auth_check 写回 expired）后若未重建，还得靠这一层挡住。
    事件每站每日最多一条：每 30 分钟一条「已跳过」只会把事件流冲成噪音。
    """
    kept: dict[str, BaseAdapter] = {}
    for name, adapter in adapters.items():
        status = _auth_status_of(adapter)
        if status == "valid":
            kept[name] = adapter
            continue
        site = adapter.site_name
        result.skipped_auth.append(site)
        logger.warning(f"{site} 认证状态为 {status}，本轮跳过（请到 /sites 重新测试认证）")
        if db is None or notifier is None:
            continue
        try:
            if await _auth_skip_emitted_today(db, site):
                continue
            await notifier.emit(Event(
                kind=EVENT_SITE_AUTH_INVALID_SKIPPED, site=site,
                title=f"{site} 认证状态为 {status}，已从自动搜索中跳过",
                detail={"auth_status": status},
                level="warning",
            ))
        except Exception as e:  # noqa: BLE001 - 事件是旁路，不影响搜索
            logger.warning(f"{site} 记录认证跳过事件失败: {type(e).__name__}: {e}")
    return kept


async def _auth_skip_emitted_today(db, site: str) -> bool:
    """今天（UTC）是否已经给该站发过 site_auth_invalid_skipped。"""
    today = utcnow().date()
    recent = await repo.list_events(db, kind=EVENT_SITE_AUTH_INVALID_SKIPPED, site=site, limit=1)
    return bool(recent) and recent[0].created_at is not None \
        and recent[0].created_at.date() == today


@dataclass
class SearchRoundResult:
    """一轮搜索的聚合结果。"""
    candidates: list[NormalizedTorrent] = field(default_factory=list)
    site_errors: dict[str, str] = field(default_factory=dict)   # 站点 → 错误说明
    site_counts: dict[str, int] = field(default_factory=dict)   # 站点 → 原始返回条数
    fallback_used: list[str] = field(default_factory=list)      # 触发了 RSS fallback 的站点
    skipped_circuit: list[str] = field(default_factory=list)    # 因熔断被整轮跳过的站点
    skipped_auth: list[str] = field(default_factory=list)       # 因认证状态非 valid 被整轮跳过的站点

    def record_raw(self, site: str, n: int) -> None:
        """记录站点原始返回条数（去重前），用于转化率分析。"""
        self.site_counts[site] = self.site_counts.get(site, 0) + n

    @property
    def total_raw(self) -> int:
        """原始返回总数（去重前）。与 len(candidates) 的差即去重量。"""
        return sum(self.site_counts.values())


async def _fetch_one_site(
    adapter: BaseAdapter,
    entry: WatchEntry,
    result: SearchRoundResult,
) -> tuple[list[NormalizedTorrent], str | None]:
    """单个站点 × 单个订阅项的取数，含 search → RSS fallback。

    返回 (种子列表, 失败说明)。失败说明为 None 表示本次请求最终拿到了数据
    （search 直接命中，或 fallback 成功）——站点健康计数以此为准，
    而不是看 site_errors 有没有写：search 超时但 RSS 成功对站点而言仍是「可用」。
    """
    site = adapter.site_name

    # 若订阅项限定了站点，跳过不匹配的适配器
    if entry.sites and site not in entry.sites:
        return [], None

    # 路径 1：主动搜索（馒头可靠，NexusPHP 不可靠）
    try:
        torrents = await asyncio.wait_for(adapter.search(entry.query), _PER_REQUEST_TIMEOUT)
        if torrents:
            return torrents, None
        # search 返回空：这也是「search 参数被站点忽略」的典型症状，
        # 不能当作「确实没有匹配资源」——必须 fallback，否则会静默漏抓。
        logger.debug(f"{site} search({entry.query!r}) 返回空，降级到全量 RSS")
        return await _fallback_rss(adapter, entry, result)
    except TimeoutError:
        # asyncio.wait_for 抛的就是它：Python 3.11 起 asyncio.TimeoutError 即内建 TimeoutError
        result.site_errors[site] = f"search 超时（>{_PER_REQUEST_TIMEOUT}s）"
        logger.warning(f"{site} search({entry.query!r}) 超时")
        return await _fallback_rss(adapter, entry, result)
    except AdapterError as e:
        # AdapterError 含「search 参数被忽略」的典型症状 → fallback
        logger.debug(f"{site} search 失败，尝试 RSS fallback: {e}")
        return await _fallback_rss(adapter, entry, result)
    except Exception as e:  # noqa: BLE001 - 单站异常不得中断整轮
        err = f"search 未预期异常: {type(e).__name__}: {e}"
        result.site_errors[site] = err
        logger.exception(f"{site} search({entry.query!r}) 未预期异常")
        return [], err


async def _fallback_rss(
    adapter: BaseAdapter,
    entry: WatchEntry,
    result: SearchRoundResult,
) -> tuple[list[NormalizedTorrent], str | None]:
    """降级到全量 RSS，由 matcher 层做本地过滤。返回 (种子列表, 失败说明)。"""
    site = adapter.site_name
    try:
        torrents = await asyncio.wait_for(adapter.fetch_rss(), _PER_REQUEST_TIMEOUT)
        if site not in result.fallback_used:
            result.fallback_used.append(site)
        logger.info(f"{site} 已降级到全量 RSS，返回 {len(torrents)} 条待本地过滤")
        return torrents, None
    except AdapterError as e:
        err = f"RSS fallback 也失败: {e}"
        result.site_errors[site] = err
        logger.warning(f"{site} RSS fallback 失败: {e}")
        return [], err
    except TimeoutError:                # 含 asyncio.wait_for 超时（3.11+ 同一个类）
        err = "RSS fallback 超时"
        result.site_errors[site] = err
        return [], err
    except Exception as e:  # noqa: BLE001
        err = f"RSS fallback 未预期异常: {type(e).__name__}"
        result.site_errors[site] = err
        logger.exception(f"{site} RSS fallback 未预期异常: {e}")
        return [], err


async def _record_outcome(
    db, notifier, site: str, error: str | None, rows: int, *,
    threshold: int, open_minutes: int,
) -> None:
    """把单次请求结果写进 site_stats；刚打开熔断时发 site_circuit_open 事件。

    统计是旁路：写库失败只记日志，绝不让一轮搜索因为统计表出问题而中断。
    """
    try:
        if error is None:
            await repo.record_site_success(db, site, rows=rows)
            return
        opened = await repo.record_site_failure(
            db, site, error, threshold=threshold, open_minutes=open_minutes,
        )
    except Exception as e:  # noqa: BLE001 - 统计旁路不得外溢
        logger.warning(f"{site} 写 site_stats 失败: {type(e).__name__}: {e}")
        return

    if not opened or notifier is None:
        return
    stats = await repo.get_site_stats(db, site) or {}
    await notifier.emit(Event(
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


async def search_round(
    adapters: dict[str, BaseAdapter],
    entries: list[WatchEntry],
    max_concurrency: int = 2,
    *,
    db=None,
    notifier=None,
    circuit_fail_threshold: int = DEFAULT_CIRCUIT_FAIL_THRESHOLD,
    circuit_open_minutes: int = DEFAULT_CIRCUIT_OPEN_MINUTES,
) -> SearchRoundResult:
    """执行一轮搜索：所有启用的订阅项 × 所有可用适配器。

    Args:
        adapters: {站点名: 适配器}
        entries: 已启用的订阅项列表
        max_concurrency: 并发上限。**必须限制**——PT 站对高频请求敏感，
            且这是账号安全问题而非性能问题。默认 2。
        db: 可选。传入则每次请求前查熔断、请求后写 site_stats；不传则不做统计。
        notifier: 可选。熔断刚打开时发 site_circuit_open 事件；需同时传 db。
        circuit_fail_threshold / circuit_open_minutes: 熔断阈值与时长，
            scheduler 从 config.search 读取后传入，设置页改动下一轮即生效。

    Returns:
        SearchRoundResult，含聚合候选、各站错误、fallback 记录、熔断跳过的站点
    """
    result = SearchRoundResult()
    if not adapters:
        logger.error("没有可用适配器，本轮搜索跳过")
        return result
    if not entries:
        logger.info("watchlist 为空，本轮搜索跳过")
        return result

    # v0.6 认证门禁：未验过 / 已失效的站不发一个请求
    adapters = await _skip_auth_invalid(adapters, result, db, notifier)
    if not adapters:
        logger.error("所有站点认证状态均非 valid，本轮搜索跳过")
        return result

    sem = asyncio.Semaphore(max_concurrency)
    seen_hashes: set[str] = set()   # 同轮内跨订阅项去重

    async def guarded(adapter: BaseAdapter, entry: WatchEntry) -> list[NormalizedTorrent]:
        site = adapter.site_name
        # 订阅项限定了站点时不发请求，也不计入站点请求数
        if entry.sites and site not in entry.sites:
            return []
        async with sem:
            # 每次请求前都查而非只在轮次开头查：同站多订阅项时，
            # 前一个请求刚把熔断打开，后面的请求就不该再发出去。
            if db is not None and await repo.is_circuit_open(db, site):
                if site not in result.skipped_circuit:
                    result.skipped_circuit.append(site)
                    logger.warning(f"{site} 处于熔断期，本轮跳过")
                return []
            torrents, error = await _fetch_one_site(adapter, entry, result)
            if db is not None:
                await _record_outcome(
                    db, notifier, site, error, len(torrents),
                    threshold=circuit_fail_threshold, open_minutes=circuit_open_minutes,
                )
            return torrents

    tasks = [
        guarded(adapter, entry)
        for entry in entries
        for adapter in adapters.values()
    ]
    logger.info(
        f"搜索轮开始：{len(entries)} 个订阅项 × {len(adapters)} 个站点 "
        f"= {len(tasks)} 个请求（并发上限 {max_concurrency}）"
    )

    batches = await asyncio.gather(*tasks, return_exceptions=True)

    for batch in batches:
        if isinstance(batch, BaseException):
            # gather 已捕获，此处仅记录，不中断
            logger.error(f"搜索任务异常: {batch!r}")
            continue
        # 原始条数在去重前统计，供转化率分析（PRD §13.7）
        for t in batch:
            result.record_raw(t.site, 1)
        for t in batch:
            key = t.info_hash or f"{t.site}:{t.title_normalized}"
            if key in seen_hashes:
                continue
            seen_hashes.add(key)
            result.candidates.append(t)

    logger.info(
        f"搜索轮完成：原始 {result.total_raw} 条 → 去重后 {len(result.candidates)} 条候选；"
        f"失败站点 {list(result.site_errors) or '无'}；"
        f"fallback 站点 {result.fallback_used or '无'}；"
        f"熔断跳过 {result.skipped_circuit or '无'}；"
        f"认证跳过 {result.skipped_auth or '无'}"
    )
    return result
