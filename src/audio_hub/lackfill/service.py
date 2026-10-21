"""缺集扫描服务:编排 Emby 与 TMDB,产出 ScanReport。

本模块不 import 主包 config:``LackfillService`` 只要求 cfg 对象带以下属性,
便于测试时用简单 stub 替换——

- ``enabled``: 总开关(扫描入口是否配置完整由 :attr:`configured` 独立判定);
- ``emby_url`` / ``emby_api_key``: Emby 连接;
- ``tmdb_api_key`` / ``tmdb_language``: TMDB 连接;
- ``max_round_shows``: 单轮扫描剧集数预算;
- ``p115_cookie`` / ``transfer_cid``: 115 转存主链;
- ``sa_base_url`` / ``sa_username`` / ``sa_secret``: SA 备链;
- ``wecom_webhook``: 企微告警 webhook(可选,空则不告警);
- ``verify_timeout_seconds``: 核销超时秒数(默认 604800,即 7 天);
- ``max_verify_rounds``: 核销最大轮次(默认 3,达到即判 timeout)。

流程:Emby 拉全库剧集 → 预算内逐剧处理(缺 TMDB ID 记错误跳过)→
TMDB 取各季已播出集数(逐季请求,调用间隔 50ms)→ Emby 取已有集数 →
diff 算缺集;单剧异常记入 errors 后继续,不中断整轮。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime

from loguru import logger

from . import ledger
from .diff import compute_missing
from .emby import EmbyClient
from .models import ScanReport, ShowLack
from .p115 import P115Client, P115Error, parse_share_url
from .sa import SAClient, SAError, send_wecom_callback
from .tmdb import TmdbClient

#: 相邻两次 TMDB 请求之间的间隔(秒),对免费档限速留余量
TMDB_THROTTLE_SECONDS = 0.05


class LackfillService:
    """缺集扫描编排器。

    Attributes:
        cfg: 带约定属性(见模块 docstring)的配置对象,类型不限定。
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    @property
    def configured(self) -> bool:
        """连接要素(emby_url / emby_api_key / tmdb_api_key)是否齐全。"""
        return bool(
            getattr(self.cfg, "emby_url", None)
            and getattr(self.cfg, "emby_api_key", None)
            and getattr(self.cfg, "tmdb_api_key", None)
        )

    async def scan(self, limit: int | None = None) -> ScanReport:
        """执行一轮缺集扫描。

        Args:
            limit: 本轮最多处理的剧集数上限;为 None 时只用 cfg.max_round_shows。
                实际预算取两者较小值。

        Returns:
            扫描报告;未配置连接要素时报告只带一条错误并立即返回。
        """
        report = ScanReport(started_at=datetime.now())
        if not self.configured:
            report.errors.append(
                "缺集扫描未配置:需要 emby_url / emby_api_key / tmdb_api_key"
            )
            report.finished_at = datetime.now()
            return report

        budget = min(self.cfg.max_round_shows, limit) if limit is not None else self.cfg.max_round_shows
        emby = EmbyClient(self.cfg.emby_url, self.cfg.emby_api_key)
        tmdb = TmdbClient(self.cfg.tmdb_api_key, self.cfg.tmdb_language)
        try:
            series_list = await emby.list_series()
            report.shows_total = len(series_list)
            today = date.today()
            for show in series_list:
                if report.shows_scanned >= budget:
                    break
                report.shows_scanned += 1
                await self._scan_one(show, emby, tmdb, today, report)
        finally:
            await emby.aclose()
            await tmdb.aclose()
            report.finished_at = datetime.now()
        return report

    async def _scan_one(
        self,
        show: dict,
        emby: EmbyClient,
        tmdb: TmdbClient,
        today: date,
        report: ScanReport,
    ) -> None:
        """处理单部剧:取播出进度与已有集数,算缺集;异常记 errors 不抛出。"""
        name = show.get("name") or f"emby:{show.get('emby_id')}"
        tmdb_id = show.get("tmdb_id")
        if not tmdb_id:
            report.errors.append(f"{name}: 缺少 TMDB ID,跳过")
            return
        try:
            detail = await tmdb.tv(tmdb_id)
            await asyncio.sleep(TMDB_THROTTLE_SECONDS)

            aired: dict[int, list[int]] = {}
            for season in detail.get("seasons") or []:
                season_number = season.get("season_number")
                if season_number is None or season_number == 0:
                    continue
                aired[season_number] = await tmdb.season_aired_episode_numbers(
                    tmdb_id, season_number, today
                )
                await asyncio.sleep(TMDB_THROTTLE_SECONDS)

            have = await emby.episode_map(show["emby_id"])
            missing = compute_missing(aired, have)
            if missing:
                report.lacks.append(
                    ShowLack(
                        tmdb_id=tmdb_id,
                        title=show.get("name") or "",
                        year=show.get("year"),
                        emby_id=show["emby_id"],
                        missing=missing,
                    )
                )
        except Exception as exc:  # noqa: BLE001 —— 单剧失败不拖垮整轮扫描
            logger.warning(f"缺集扫描单剧失败,跳过: {name} ({exc})")
            report.errors.append(f"{name}: {exc}")

    async def _alert(self, content: str) -> None:
        """企微告警;未配置 webhook 时静默跳过。"""
        webhook = getattr(self.cfg, "wecom_webhook", None)
        if webhook:
            await send_wecom_callback(webhook, content)

    async def transfer_one(
        self,
        conn,
        show_key: str,
        season: str | None,
        missing: list[int],
        share_url: str,
        package_name: str,
        *,
        now: str,
        emby_id: str = "",
        p115=None,
        sa=None,
        emby=None,
    ) -> dict:
        """单条补齐转存:115 主链失败时降级 SA 备链,成功则台账入库并刷新 Emby。

        外部注入的 client(p115/sa/emby)不由本方法关闭;未注入时自建并在
        finally 中关闭。任何失败都会发企微告警(若配置),返回
        ``{"outcome": "failed", "reason": ...}``;成功返回
        ``{"outcome": "ok", "entry_id": ...}``。
        """
        own_p115, own_sa, own_emby = p115 is None, sa is None, emby is None
        if own_p115:
            p115 = P115Client(self.cfg.p115_cookie)
        if own_sa:
            sa = SAClient(self.cfg.sa_base_url, self.cfg.sa_username, self.cfg.sa_secret)
        if own_emby:
            emby = EmbyClient(self.cfg.emby_url, self.cfg.emby_api_key)
        try:
            try:
                share_code, receive_code = parse_share_url(share_url)
            except ValueError as exc:
                await self._alert(f"lackfill 转存失败[{show_key} {season or ''}]: {exc}")
                return {"outcome": "failed", "reason": str(exc)}
            receive_code = receive_code or ""
            try:
                files = await p115.snap(share_code, receive_code)
            except P115Error as exc:
                await self._alert(f"lackfill 验包失败[{show_key} {season or ''}]: {exc}")
                return {"outcome": "failed", "reason": str(exc)}
            file_ids = [f["file_id"] for f in files if not f["is_dir"]]
            cid = str(getattr(self.cfg, "transfer_cid", "") or "0")
            if not await p115.receive(share_code, receive_code, file_ids, cid):
                try:
                    await sa.receive(share_code, receive_code, file_ids, cid)
                except SAError as exc:
                    await self._alert(f"lackfill 主备转存均失败[{show_key}]: {exc}")
                    return {"outcome": "failed", "reason": str(exc)}
            entry_id = await ledger.insert(
                conn,
                show_key=show_key,
                season=season,
                missing=missing,
                share_code=share_code,
                package_name=package_name,
                now=now,
                emby_id=emby_id,
            )
            await emby.refresh_library()
            return {"outcome": "ok", "entry_id": entry_id}
        finally:
            if own_p115:
                await p115.aclose()
            if own_sa:
                await sa.aclose()
            if own_emby:
                await emby.aclose()

    async def verify_pending(self, conn, *, now: str, emby=None) -> dict:
        """核销轮询:对 pending/partial 台账逐条向 Emby 核对缺集补齐情况。

        - 缺集清零 → ``complete``;
        - 缺集减少 → 更新缺集清单并转 ``partial``;
        - 无变化且未超时 → 跳过;
        - 超过 ``verify_timeout_seconds``:轮次未达 ``max_verify_rounds`` 则
          bump_round 计入下一轮,达到上限则告警并判 ``timeout``。
        """
        stats = {"checked": 0, "complete": 0, "partial": 0, "bumped": 0, "timeout": 0}
        rows = await ledger.list_by_status(conn, "pending", "partial")
        if not rows:
            return stats
        own_emby = emby is None
        if own_emby:
            emby = EmbyClient(self.cfg.emby_url, self.cfg.emby_api_key)
        timeout_seconds = getattr(self.cfg, "verify_timeout_seconds", 604800)
        max_rounds = getattr(self.cfg, "max_verify_rounds", 3)
        try:
            for row in rows:
                stats["checked"] += 1
                have = await emby.episode_map(row["emby_id"])
                season = row["season"]
                if season:
                    have_eps = have.get(int(str(season).lstrip("S")), set())
                else:
                    have_eps = set().union(*have.values()) if have else set()
                missing = json.loads(row["missing_json"])
                still = [ep for ep in missing if ep not in have_eps]
                if not still:
                    await ledger.update_status(conn, row["id"], "complete", now)
                    stats["complete"] += 1
                    continue
                if len(still) < len(missing):
                    await ledger.update_missing(conn, row["id"], still, now)
                    await ledger.update_status(conn, row["id"], "partial", now)
                    stats["partial"] += 1
                    continue
                age = (
                    datetime.fromisoformat(now) - datetime.fromisoformat(row["created_at"])
                ).total_seconds()
                if age <= timeout_seconds:
                    continue
                if row["verify_round"] >= max_rounds:
                    await self._alert(
                        f"lackfill 核销超时[{row['show_key']}]: 仍缺 {still}"
                    )
                    await ledger.update_status(conn, row["id"], "timeout", now)
                    stats["timeout"] += 1
                else:
                    await ledger.bump_round(conn, row["id"], now)
                    stats["bumped"] += 1
        finally:
            if own_emby:
                await emby.aclose()
        return stats
