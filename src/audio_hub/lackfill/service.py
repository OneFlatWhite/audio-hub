"""缺集扫描服务:编排 Emby 与 TMDB,产出 ScanReport。

本模块不 import 主包 config:``LackfillService`` 只要求 cfg 对象带以下属性,
便于测试时用简单 stub 替换——

- ``enabled``: 总开关(扫描入口是否配置完整由 :attr:`configured` 独立判定);
- ``emby_url`` / ``emby_api_key``: Emby 连接;
- ``tmdb_api_key`` / ``tmdb_language``: TMDB 连接;
- ``max_round_shows``: 单轮扫描剧集数预算。

流程:Emby 拉全库剧集 → 预算内逐剧处理(缺 TMDB ID 记错误跳过)→
TMDB 取各季已播出集数(逐季请求,调用间隔 50ms)→ Emby 取已有集数 →
diff 算缺集;单剧异常记入 errors 后继续,不中断整轮。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from loguru import logger

from .diff import compute_missing
from .emby import EmbyClient
from .models import ScanReport, ShowLack
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
