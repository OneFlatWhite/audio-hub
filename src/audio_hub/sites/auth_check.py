"""认证巡检：对所有有凭据的站点跑一遍 check_auth，并把结果写回 sites.auth_status。

只提供 run_auth_check_once 一个入口，不在这里注册 scheduler job ——
什么时候跑、跑多频繁是调度层的事（波次 3 接入），本模块只保证「跑一次」是
幂等、不抛出、逐站隔离的。

写回规则：
- 认证通过 → valid（带 user_info）。
- AdapterAuthError（Cookie 过期 / API Key 失效 / 被 302 到登录页）→ expired，
  并在状态**由 valid 跌落**时发 site_auth_expired 事件：失效通常意味着要去站点
  重新登录拿 Cookie，这件事值得推送一次；已经是 expired 的站不再重复吵。
- 其他异常（超时、网络、解析失败）→ 状态不变，只记录 auth_error：一次网络抖动
  不该把站点踢出搜索。
- 没有可用于认证的凭据（NexusPHP 无 Cookie / mteam 无 API Key）→ 跳过不写。
"""

from __future__ import annotations

from loguru import logger

from .. import repo
from ..adapters.base import AdapterAuthError, AdapterError
from ..adapters.registry import make_adapter_for_test, resolve_site_name
from ..models import AuthStatus
from ..notify.base import Event
from ..timeutil import utcnow

#: 认证由 valid 跌落为 expired 时发出的事件种类
EVENT_SITE_AUTH_EXPIRED = "site_auth_expired"


def auth_capable(cfg) -> bool:
    """该站点配置是否具备 check_auth 所需的凭据。

    NexusPHP 站内认证靠 Cookie；mteam 靠 API Key。仅有 RSS passkey 的站点
    只能「测试 RSS」，无法判断登录态，巡检时跳过。
    """
    name = resolve_site_name(cfg.name)
    if name == "mteam":
        return bool((cfg.api_key or "").strip())
    return bool((cfg.cookie or "").strip())


async def check_site_auth(db, cfg, notifier=None) -> str:
    """对单个站点做一次认证检查并写回。返回写回后的 auth_status 值。

    任何异常都在这里吞掉并体现在返回值 / auth_error 上，调用方不需要 try。
    """
    sid = resolve_site_name(cfg.name)
    prev = str(getattr(cfg, "auth_status", "") or AuthStatus.UNCONFIGURED.value)
    now = utcnow()
    try:
        adapter = make_adapter_for_test(cfg)
        info = await adapter.check_auth()
    except AdapterAuthError as e:
        await repo.set_site_auth(db, sid, AuthStatus.EXPIRED, error=str(e), checked_at=now)
        logger.warning(f"站点 {sid} 认证失效: {e}")
        if prev == AuthStatus.VALID.value and notifier is not None:
            try:
                await notifier.emit(Event(
                    kind=EVENT_SITE_AUTH_EXPIRED, site=sid,
                    title=f"{sid} 认证失效，已从自动搜索中移除",
                    detail={"error": str(e), "previous": prev},
                    level="warning",
                ))
            except Exception as ne:  # noqa: BLE001 - 事件是旁路
                logger.warning(f"{sid} 发送认证失效事件失败: {type(ne).__name__}: {ne}")
        return AuthStatus.EXPIRED.value
    except Exception as e:  # noqa: BLE001 - 单站异常不得中断巡检
        err = str(e) if isinstance(e, AdapterError) else f"{type(e).__name__}: {e}"
        await repo.set_site_auth(db, sid, prev, error=err, checked_at=now)
        logger.warning(f"站点 {sid} 认证检查未完成（状态保持 {prev}）: {err}")
        return prev

    await repo.set_site_auth(db, sid, AuthStatus.VALID, error=None, user_info=info, checked_at=now)
    logger.info(f"站点 {sid} 认证有效: {info.username or '?'} / {info.level or '?'}")
    return AuthStatus.VALID.value


async def run_auth_check_once(db, config, notifier=None, app=None) -> dict:
    """巡检一轮：遍历 config.sites 中具备认证凭据的站点，逐站 check_auth 并写回。

    Args:
        db: aiosqlite 连接。
        config: AppConfig（读 .sites；运行时与 DB 同步，见 settings_store.rebuild_adapters）。
        notifier: 可选，valid → expired 时发 site_auth_expired 事件。
        app: 可选，传入则在任一站点状态发生变化后 await settings_store.rebuild_adapters(app)，
            让门禁立刻生效（失效站从 runner.adapters 移除 / 恢复站加回）。

    Returns:
        {"checked": n, "changed": [站点...], "skipped": [站点...], "results": {站点: 状态}}
    """
    summary: dict = {"checked": 0, "changed": [], "skipped": [], "results": {}}
    for cfg in list(getattr(config, "sites", []) or []):
        sid = resolve_site_name(cfg.name)
        if not auth_capable(cfg):
            summary["skipped"].append(sid)
            continue
        prev = str(getattr(cfg, "auth_status", "") or AuthStatus.UNCONFIGURED.value)
        status = await check_site_auth(db, cfg, notifier)
        summary["checked"] += 1
        summary["results"][sid] = status
        if status != prev:
            summary["changed"].append(sid)

    if summary["changed"] and app is not None:
        from .. import settings_store
        try:
            await settings_store.rebuild_adapters(app)
        except Exception as e:  # noqa: BLE001 - 重建失败只记日志，下次保存 / 重启会补上
            logger.error(f"认证巡检后重建适配器失败: {type(e).__name__}: {e}")

    logger.info(
        f"认证巡检完成：检查 {summary['checked']} 站，状态变化 {summary['changed'] or '无'}，"
        f"跳过（无认证凭据）{summary['skipped'] or '无'}"
    )
    return summary
