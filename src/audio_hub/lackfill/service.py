"""缺集扫描服务:编排 Emby 与 TMDB,产出 ScanReport。

本模块不 import 主包 config:``LackfillService`` 只要求 cfg 对象带以下属性,
便于测试时用简单 stub 替换——

- ``enabled``: 总开关(扫描入口是否配置完整由 :attr:`configured` 独立判定);
- ``emby_url`` / ``emby_api_key``: Emby 连接;
- ``tmdb_api_key`` / ``tmdb_language``: TMDB 连接;
- ``max_round_shows``: 单轮扫描剧集数预算;
- ``ay_api_*`` / ``ay_tg_id`` / ``ay_max_links``: AY API 限额补充与选包;
- ``ay_round_clicks``: 单轮 AY 查询和提交预算;
- ``p115_cookie`` / ``transfer_cid``: 115 转存主链;
- ``sa_base_url`` / ``sa_username`` / ``sa_secret``: SA 备链;
- ``wecom_webhook``: 企微告警 webhook(可选,空则不告警);
- ``verify_timeout_days``: 单次补齐尝试的超时天数(默认 7 天);
- ``cloud_*``: CD2 文件落地验证、稳定时间与轮询间隔;
- ``max_verify_rounds``: 连续无进展的最大轮次(默认 3,达到即判 timeout)。

流程:Emby 拉全库剧集 → 预算内逐剧处理(缺 TMDB ID 记错误跳过)→
TMDB 取各季已播出集数(逐季请求,调用间隔 50ms)→ Emby 取已有集数 →
diff 算缺集并写入 Re0 持久化队列;单剧异常保留旧队列并记入 errors。
Re0 主源按队列运行,AY 在需要时补充;115 快照必须含真实缺集。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime

from loguru import logger

from . import cloud as cloud_watch
from . import ledger, queue, reservations
from .ay import AYClient, AYError, episode_cover, pick_ay_links, save_quota
from .diff import compute_missing
from .emby import EmbyClient
from .models import ScanReport, ShowLack
from .p115 import P115Client, P115Error, parse_share_url
from .refill import (
    classify_resource_link,
    ed2k_package_name,
    normalize_ed2k_group,
    resource_identity,
    validate_package_name,
    validate_share_package,
)
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

    async def scan(
        self,
        limit: int | None = None,
        conn=None,
        now: str | None = None,
    ) -> ScanReport:
        """执行一轮缺集扫描；传入 conn 时同步更新持久化回填队列。"""
        report = ScanReport(started_at=datetime.now())
        if not self.configured:
            report.errors.append(
                "缺集扫描未配置:需要 emby_url / emby_api_key / tmdb_api_key"
            )
            report.finished_at = datetime.now()
            return report

        budget = min(self.cfg.max_round_shows, limit) if limit is not None else self.cfg.max_round_shows
        queue_now = now or report.started_at.isoformat()
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
                await self._scan_one(
                    show, emby, tmdb, today, report, conn=conn, now=queue_now
                )
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
        *,
        conn=None,
        now: str = "",
    ) -> None:
        """处理单部剧；失败时保留该剧已有队列，避免误删待补记录。"""
        name = show.get("name") or f"emby:{show.get('emby_id')}"
        tmdb_id = show.get("tmdb_id")
        if not tmdb_id:
            report.errors.append(f"{name}: 缺少 TMDB ID,跳过")
            return
        try:
            tmdb_id = int(tmdb_id)
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
                lack = ShowLack(
                    tmdb_id=tmdb_id,
                    title=show.get("name") or "",
                    year=show.get("year"),
                    emby_id=show["emby_id"],
                    missing=missing,
                )
                report.lacks.append(lack)
                if conn is not None:
                    await queue.upsert(conn, lack, now)
            elif conn is not None:
                await queue.remove(conn, tmdb_id)
        except Exception as exc:  # noqa: BLE001 —— 单剧失败不拖垮整轮扫描
            logger.warning(f"缺集扫描单剧失败,跳过: {name} ({exc})")
            report.errors.append(f"{name}: {exc}")

    async def _alert(self, content: str) -> None:
        """企微告警;未配置 webhook 时静默跳过。"""
        webhook = getattr(self.cfg, "wecom_webhook", None)
        if webhook:
            await send_wecom_callback(webhook, content)

    def _new_sa_client(self) -> SAClient:
        return SAClient(
            self.cfg.sa_base_url,
            self.cfg.sa_username,
            self.cfg.sa_secret,
            callback_url=getattr(self.cfg, "sa_callback_url", ""),
            callback_token=getattr(self.cfg, "sa_callback_token", ""),
            callback_aeskey=getattr(self.cfg, "sa_callback_aeskey", ""),
            callback_corpid=getattr(self.cfg, "sa_callback_corpid", ""),
            callback_userid=getattr(self.cfg, "sa_callback_userid", ""),
            callback_agentid=getattr(self.cfg, "sa_callback_agentid", ""),
        )

    def _cloud_options(self, offline: bool) -> tuple[str, str, str] | None:
        if not getattr(self.cfg, "cloud_verify_enabled", False):
            return None
        mode = "move" if offline or getattr(self.cfg, "cloud_share_move", False) else "verify"
        return mode, self.cfg.cloud_staging_dir, self.cfg.cloud_feed_dir

    async def transfer_one(
        self,
        conn,
        show_key: str,
        season: str | None,
        missing: list[int] | dict[int, list[int]],
        share_url: str,
        package_name: str,
        *,
        now: str,
        emby_id: str = "",
        p115=None,
        sa=None,
        emby=None,
        before_receive=None,
        reservation: bool = False,
        verify_round: int = 0,
        required_episodes: set[tuple[int, int]] | None = None,
    ) -> dict:
        """115 验包后转存并登记台账；CD2 开启时等待文件确认再刷新 Emby。

        AY 通过 required_episodes 额外要求包内真有目标缺集；Re0 默认不传，
        保持原有剧名/目标季校验。外部注入的 client 不由本方法关闭。
        失败返回 failed；受理成功返回 ok 和 entry_id，不代表已入库。
        """
        own_p115, own_sa, own_emby = p115 is None, sa is None, emby is None
        if own_p115:
            p115 = P115Client(self.cfg.p115_cookie)
        if own_sa:
            sa = self._new_sa_client()
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
                snapshot = await p115.inspect_share(share_code, receive_code)
                actual_package_name = validate_share_package(
                    snapshot, package_name, missing, season=season
                )
                if required_episodes is not None and not (
                    episode_cover(snapshot.files) & required_episodes
                ):
                    raise P115Error("验包失败：包内没有目标缺集")
            except P115Error as exc:
                await self._alert(f"lackfill 验包失败[{show_key} {season or ''}]: {exc}")
                return {"outcome": "failed", "reason": str(exc)}
            files = snapshot.files
            file_ids = [f["file_id"] for f in files if not f["is_dir"]]
            cid = str(getattr(self.cfg, "transfer_cid", "") or "0")
            if before_receive is not None:
                await before_receive()
            if not await p115.receive(share_code, receive_code, file_ids, cid):
                try:
                    await sa.submit_message([share_url], actual_package_name)
                except SAError as exc:
                    await self._alert(f"lackfill 主备转存均失败[{show_key}]: {exc}")
                    return {"outcome": "failed", "reason": str(exc)}
            entry_id = await ledger.insert(
                conn,
                show_key=show_key,
                season=season,
                missing=missing,
                share_code=share_code,
                package_name=actual_package_name,
                now=now,
                emby_id=emby_id,
                verify_round=verify_round,
                commit=False,
            )
            cloud_options = self._cloud_options(offline=False)
            if cloud_options is not None:
                await cloud_watch.insert(
                    conn, entry_id, actual_package_name, *cloud_options, now, commit=False
                )
            if reservation:
                await reservations.release(conn, show_key, commit=False)
            await conn.commit()
            if cloud_options is None:
                await emby.refresh_library()
            return {"outcome": "ok", "entry_id": entry_id}
        finally:
            if own_p115:
                await p115.aclose()
            if own_sa:
                await sa.aclose()
            if own_emby:
                await emby.aclose()

    async def offline_one(
        self,
        conn,
        show_key: str,
        season: str | None,
        missing: list[int] | dict[int, list[int]],
        link: str | list[str],
        package_name: str,
        *,
        now: str,
        emby_id: str = "",
        sa=None,
        emby=None,
        before_receive=None,
        reservation: bool = False,
        verify_round: int = 0,
    ) -> dict:
        """单个或整包 ed2k 走 SA；API 按文件验证，消息回退按目录验证。"""
        own_sa, own_emby = sa is None, emby is None
        if own_sa:
            sa = self._new_sa_client()
        if own_emby:
            emby = EmbyClient(self.cfg.emby_url, self.cfg.emby_api_key)
        try:
            try:
                links, identity, file_names = normalize_ed2k_group(link)
                if len(links) > 1 and getattr(self.cfg, "cloud_verify_enabled", False):
                    validate_package_name(package_name)
            except ValueError as exc:
                await self._alert(f"lackfill 离线失败[{show_key}]: {exc}")
                return {"outcome": "failed", "reason": str(exc)}
            if before_receive is not None:
                await before_receive()
            try:
                result = await sa.offline(
                    links, parent_id=getattr(self.cfg, "sa_parent_id", "")
                )
                if not result.get("ok"):
                    raise SAError("SA 离线提交未被接受")
            except SAError:
                try:
                    result = await sa.submit_message(links, package_name)
                except SAError as exc:
                    await self._alert(f"lackfill 离线主备均失败[{show_key}]: {exc}")
                    return {"outcome": "failed", "reason": str(exc)}
            entry_id = await ledger.insert(
                conn,
                show_key=show_key,
                season=season,
                missing=missing,
                share_code=identity,
                package_name=package_name,
                now=now,
                emby_id=emby_id,
                verify_round=verify_round,
                commit=False,
            )
            cloud_options = self._cloud_options(offline=True)
            cloud_package_name = package_name
            if cloud_options is not None and result.get("via", "api") == "api" and len(links) > 1:
                await cloud_watch.insert(
                    conn, entry_id, package_name, "move_files", *cloud_options[1:], now,
                    commit=False,
                )
                await cloud_watch.insert_files(
                    conn, entry_id, file_names, *cloud_options[1:], now, commit=False
                )
            else:
                if cloud_options is not None and result.get("via", "api") == "api":
                    cloud_options = ("move_file", *cloud_options[1:])
                    cloud_package_name = file_names[0]
                if cloud_options is not None:
                    await cloud_watch.insert(
                        conn, entry_id, cloud_package_name, *cloud_options, now, commit=False
                    )
            if reservation:
                await reservations.release(conn, show_key, commit=False)
            await conn.commit()
            if cloud_options is None:
                await emby.refresh_library()
            return {"outcome": "ok", "entry_id": entry_id}
        finally:
            if own_sa:
                await sa.aclose()
            if own_emby:
                await emby.aclose()

    async def _sync_current_queue(self, conn, show_key: str, have: dict, now: str) -> None:
        """按当前 Emby 过滤当前队列，保留等待期间新播出的缺集。"""
        if not show_key.startswith("tmdb:") or not show_key[5:].isdigit():
            return
        tmdb_id = int(show_key[5:])
        current = await queue.get(conn, tmdb_id)
        if current is None:
            return
        remaining = {
            str(season): [ep for ep in eps if ep not in have.get(int(season), set())]
            for season, eps in current["seasons"].items()
        }
        remaining = {season: eps for season, eps in remaining.items() if eps}
        if remaining:
            await queue.update_missing(conn, tmdb_id, remaining, now)
        else:
            await queue.remove(conn, tmdb_id)

    async def fill_ay(
        self,
        conn,
        queued: dict,
        click_budget_left: int,
        *,
        ay=None,
        tmdb=None,
        now: str,
        execute: bool = True,
        p115=None,
        sa=None,
        emby=None,
    ) -> dict:
        """AY 查询、选包与现有转存链编排；execute=False 只预览，不提交资源。"""
        result = {
            "status": "none",
            "clicks": 0,
            "reason": "",
            "quota_left": None,
            "resource_count": 0,
            "resources": [],
            "selected_links": [],
            "picks": [],
        }
        if not self.cfg.enabled or not getattr(self.cfg, "ay_api_enabled", False):
            return {**result, "status": "disabled", "reason": "爱影 API 限额补充未启用"}
        if not all((
            getattr(self.cfg, "ay_api_url", ""),
            getattr(self.cfg, "ay_api_token", ""),
            getattr(self.cfg, "ay_tg_id", ""),
            getattr(self.cfg, "tmdb_api_key", ""),
        )):
            return {
                **result,
                "status": "unconfigured",
                "reason": "需要爱影 API 地址、Token、TG ID 和 TMDB API Key",
            }
        if click_budget_left < 2:
            return {**result, "status": "budget", "reason": "本轮预算不足，保留 Re0 队列"}

        own_ay, own_tmdb = ay is None, tmdb is None
        if own_ay:
            ay = AYClient(self.cfg.ay_api_url, self.cfg.ay_api_token)
        if own_tmdb:
            tmdb = TmdbClient(self.cfg.tmdb_api_key, self.cfg.tmdb_language)
        try:
            try:
                response = await ay.query(queued["tmdb_id"], self.cfg.ay_tg_id)
            except AYError as exc:
                return {**result, "status": "failed", "reason": str(exc)}
            result["clicks"] = 1
            result["quota_left"] = response.get("quota_left")
            await save_quota(conn, result["quota_left"], now)

            def public(resource: dict) -> dict:
                return {
                    key: str(resource.get(key) or "")[:20000]
                    for key in ("name", "notes", "size", "category", "link")
                }

            resources = response.get("resources") or []
            result["resource_count"] = len(resources)
            result["resources"] = [public(resource) for resource in resources]
            result["http_status"] = response.get("http_status")
            result["api_message"] = response.get("message") or ""
            if not resources:
                return {
                    **result,
                    "reason": f"{response.get('message') or '爱影未返回资源'}；保留 Re0 队列",
                }

            missing = {
                int(season): [int(ep) for ep in episodes]
                for season, episodes in queued["seasons"].items()
            }
            lack_eps = {(season, ep) for season, episodes in missing.items() for ep in episodes}
            seasons = set(missing)
            for resource in resources:
                text = f"{resource.get('name') or ''} {resource.get('notes') or ''}"
                seasons |= {int(value) for value in re.findall(r"S(\d{1,2})", text, re.IGNORECASE)}
            season_eps = {}
            for season in sorted(value for value in seasons if 0 < value <= 40):
                try:
                    season_eps[season] = await tmdb.season_aired_episode_numbers(
                        queued["tmdb_id"], season, date.today()
                    )
                    await asyncio.sleep(TMDB_THROTTLE_SECONDS)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"AY 选包读取 TMDB 季集失败[tmdb:{queued['tmdb_id']} S{season}]: "
                        f"{type(exc).__name__}"
                    )
                    season_eps[season] = []
            picks, pick_reason = pick_ay_links(
                resources, getattr(self.cfg, "ay_max_links", 3), lack_eps, season_eps
            )
            result.update({
                "picks": [public(resource) for resource in picks],
                "selected_links": [str(resource.get("link") or "") for resource in picks],
                "pick_reason": pick_reason,
                "reason": pick_reason,
            })
            if not picks:
                return {**result, "reason": f"{pick_reason}；无可提交资源，保留 Re0 队列"}
            if not execute:
                return {**result, "status": "preview"}
            if click_budget_left < 1 + len(picks):
                return {
                    **result,
                    "status": "budget",
                    "reason": "预算不足以提交全部候选包，未转存并保留 Re0 队列",
                }

            show_key = f"tmdb:{queued['tmdb_id']}"
            errors = []
            for resource in picks:
                reserved = False
                try:
                    raw_link = str(resource.get("link") or "").strip()
                    if not raw_link:
                        raise ValueError("资源没有链接")
                    if raw_link.lstrip().lower().startswith("ed2k://") and (
                        "\n" in raw_link or "\r" in raw_link
                    ):
                        kind, link = "ed2k", raw_link
                    else:
                        kind, link = classify_resource_link(raw_link)
                    identity = resource_identity(link)
                    reservation = await reservations.reserve(conn, show_key, identity, now)
                    if reservation != "reserved":
                        return {
                            **result,
                            "status": "inflight",
                            "reason": "已有活动台账或转存预留，不重复提交",
                        }
                    reserved = True
                    result["clicks"] += 1
                    verify_round = await ledger.latest_round(conn, show_key)

                    async def mark_attempted():
                        await reservations.mark_attempted(conn, show_key, now)

                    try:
                        if kind == "share":
                            outcome = await self.transfer_one(
                                conn, show_key, None, missing, link, queued["title"],
                                now=now,
                                emby_id=queued["emby_id"],
                                p115=p115,
                                sa=sa,
                                emby=emby,
                                before_receive=mark_attempted,
                                reservation=True,
                                verify_round=verify_round,
                                required_episodes=lack_eps,
                            )
                        else:
                            package_name = (
                                str(resource.get("name") or queued["title"]).strip()
                                if "\n" in link or "\r" in link
                                else ed2k_package_name(link)
                            )
                            outcome = await self.offline_one(
                                conn, show_key, None, missing, link, package_name,
                                now=now,
                                emby_id=queued["emby_id"],
                                sa=sa,
                                emby=emby,
                                before_receive=mark_attempted,
                                reservation=True,
                                verify_round=verify_round,
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"AY 转存异常[{show_key}]: {type(exc).__name__}")
                        outcome = {"outcome": "failed", "reason": "转存链异常"}
                    if outcome.get("outcome") == "ok":
                        return {**result, "status": "ok", "entry_id": outcome["entry_id"]}
                    errors.append(str(outcome.get("reason") or "转存失败")[:300])
                except ValueError as exc:
                    errors.append(str(exc)[:300])
                finally:
                    if reserved:
                        await reservations.release(conn, show_key)
            return {
                **result,
                "status": "failed",
                "reason": "；".join(errors) or "没有链接转存成功，保留 Re0 队列",
            }
        finally:
            if own_ay:
                await ay.aclose()
            if own_tmdb:
                await tmdb.aclose()

    async def verify_pending(self, conn, *, now: str, emby=None) -> dict:
        """核销 pending/partial 台账；超时关闭本次尝试并允许换包。"""
        stats = {"checked": 0, "complete": 0, "partial": 0, "bumped": 0, "timeout": 0}
        rows = await ledger.list_by_status(conn, "pending", "partial")
        if not rows:
            return stats
        own_emby = emby is None
        if own_emby:
            emby = EmbyClient(self.cfg.emby_url, self.cfg.emby_api_key)
        legacy_seconds = getattr(self.cfg, "verify_timeout_seconds", None)
        timeout_seconds = (
            legacy_seconds
            if legacy_seconds is not None
            else getattr(self.cfg, "verify_timeout_days", 7) * 86400
        )
        max_rounds = getattr(self.cfg, "max_verify_rounds", 3)
        try:
            for row in rows:
                stats["checked"] += 1
                have = await emby.episode_map(row["emby_id"])
                season = row["season"]
                missing = json.loads(row["missing_json"])
                multi_season = isinstance(missing, dict)
                if not season and not multi_season:
                    await ledger.update_status(conn, row["id"], "manual", now)
                    await self._alert(
                        f"lackfill 旧台账需人工确认[{row['show_key']}]: 缺少季号，不自动核销"
                    )
                    continue
                if multi_season:
                    still = {
                        str(season_no): [
                            ep for ep in eps if ep not in have.get(int(season_no), set())
                        ]
                        for season_no, eps in missing.items()
                    }
                    still = {season_no: eps for season_no, eps in still.items() if eps}
                    before_count = sum(len(eps) for eps in missing.values())
                    remaining_count = sum(len(eps) for eps in still.values())
                else:
                    have_eps = have.get(int(str(season).lstrip("S")), set())
                    still = [ep for ep in missing if ep not in have_eps]
                    before_count = len(missing)
                    remaining_count = len(still)
                if not still:
                    await ledger.update_status(conn, row["id"], "complete", now)
                    await self._sync_current_queue(conn, row["show_key"], have, now)
                    stats["complete"] += 1
                    continue
                if remaining_count < before_count:
                    await ledger.update_missing(conn, row["id"], still, now)
                    await ledger.update_status(conn, row["id"], "partial", now)
                    await ledger.reset_round(conn, row["id"], now)
                    await self._sync_current_queue(conn, row["show_key"], have, now)
                    stats["partial"] += 1
                    continue
                round_started = row["updated_at"] or row["created_at"]
                age = (
                    datetime.fromisoformat(now) - datetime.fromisoformat(round_started)
                ).total_seconds()
                if age <= timeout_seconds:
                    continue
                next_round = row["verify_round"] + 1
                await ledger.bump_round(conn, row["id"], now)
                await ledger.update_status(conn, row["id"], "timeout", now)
                if next_round >= max_rounds:
                    await self._alert(
                        f"lackfill 核销超时[{row['show_key']}]: 仍缺 {still}"
                    )
                    stats["timeout"] += 1
                else:
                    stats["bumped"] += 1
        finally:
            if own_emby:
                await emby.aclose()
        return stats
