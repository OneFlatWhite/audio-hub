"""适配器注册表：站点名 → 适配器实例。

新增站点时只需在 _ADAPTER_CLASS_MAP 注册一行，无需改动 pipeline 层。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from .base import BaseAdapter
from .mteam import MTeamAdapter
from .nexusphp import NexusPhpAdapter

if TYPE_CHECKING:
    from ..config import AppConfig, SiteConfig

# 站点名 → 适配器类
_ADAPTER_CLASS_MAP: dict[str, type[BaseAdapter]] = {
    "hhanclub": NexusPhpAdapter,
    "hdsky": NexusPhpAdapter,
    "audiences": NexusPhpAdapter,
    "mteam": MTeamAdapter,
}

# 别名（用户可能在 config 里写不同拼法）
_SITE_ALIASES = {
    "hhanclub.net": "hhanclub",
    "hhclub": "hhanclub",
    "憨憨": "hhanclub",
    "hdsky.me": "hdsky",
    "天空": "hdsky",
    "audiences.me": "audiences",
    "观众": "audiences",
    "m-team": "mteam",
    "m-team.cc": "mteam",
    "馒头": "mteam",
}


def resolve_site_name(raw: str) -> str:
    """归一化站点名。"""
    key = raw.strip().lower()
    return _SITE_ALIASES.get(key, key)


def build_adapter(site_config: SiteConfig) -> BaseAdapter:
    """根据 SiteConfig 构造对应适配器实例。

    未注册的站点按 NexusPHP 适配器处理 —— 与 registry_store.adapter_type_for
    的回退一致（v0.12 起支持自定义 NexusPHP 站点：站名任意、domain + cookie
    由用户填写）。mteam 等特殊架构仍须在此注册。

    Raises:
        AdapterError: 适配器构造失败（配置问题，而非「未注册」）
    """
    name = resolve_site_name(site_config.name)
    cls = _ADAPTER_CLASS_MAP.get(name)
    if cls is None:
        logger.info(f"站点 {site_config.name!r} 未注册，按 NexusPHP 适配器处理")
        cls = NexusPhpAdapter
    # 适配器内部用归一化后的名字，保证 site 字段一致
    site_config = site_config.model_copy(update={"name": name})
    return cls(site_config)


def build_adapters(
    config_or_sites: AppConfig | list[SiteConfig], *, require_valid_auth: bool = True,
) -> dict[str, BaseAdapter]:
    """构造所有已启用站点的适配器。

    接受 AppConfig（取 .sites，兼容旧调用方）或直接的 SiteConfig 列表
    （v0.6 起站点配置来自 DB，见 registry_store.load_site_configs）。
    单个站点配置错误不应阻断其他站点，故逐个 try/except。

    require_valid_auth 是 v0.6 的认证门禁：默认 True，auth_status != valid 的站
    即便 enabled 也不构造 —— 没验过凭据的站参与自动搜索只会用失败触发熔断、
    甚至拿过期 Cookie 反复敲站点。站点中心的「测试」端点不走这里，
    用 make_adapter_for_test。传 False 仅供脚本 / 诊断场景。

    Returns:
        {站点名: 适配器实例}
    """
    sites = config_or_sites if isinstance(config_or_sites, list) else config_or_sites.sites
    adapters: dict[str, BaseAdapter] = {}
    for site in sites:
        if not site.enabled:
            logger.info(f"站点 {site.name} 已禁用，跳过")
            continue
        if require_valid_auth and getattr(site, "auth_status", "valid") != "valid":
            logger.info(f"站点 {site.name} 认证状态为 {site.auth_status}，跳过")
            continue
        try:
            adapters[resolve_site_name(site.name)] = build_adapter(site)
        except Exception as e:  # noqa: BLE001 - 单站构造失败不得拖垮其他站
            logger.error(f"站点 {site.name} 适配器构造失败，已跳过: {e}")
    if not adapters:
        logger.warning("没有任何可用适配器，检查站点配置（/sites 或 config.yaml）")
    return adapters


def make_adapter_for_test(site_config: SiteConfig) -> BaseAdapter:
    """为站点中心的测试端点构造适配器：不看 enabled，也不看 auth_status。

    测试动作的目的恰恰是把 unconfigured / expired 的站验成 valid，
    门禁对它没有意义；语义与 build_adapter 相同，单独命名是为了让
    调用处一眼能看出「这里有意绕过门禁」。
    """
    return build_adapter(site_config)


def supported_sites() -> list[str]:
    """返回已支持的站点名列表。"""
    return sorted(_ADAPTER_CLASS_MAP)


# 各站凭据需求：{字段名: 是否必需}
#
# 为什么按适配器类型区分：
# - NexusPHP 系（憨憨/天空/观众）走 torrentrss.php，passkey **内联在 URL 里**，
#   故凭据就是整条 rss_url。适配器 build_rss_url 在缺 URL 时抛 AdapterError
#   （nexusphp.py），启用但没填会在每轮搜索失败后触发熔断。
# - mteam 走 REST API，认证用 Authorization header（mteam.py _headers），
#   故 api_key 必需；其 rss_url 是可选的补充路径 —— 适配器在缺 rss_url 时
#   会降级到 search（mteam.py fetch_rss），所以不算必需。
#
# 声明放这里而非界面层：新增站点时注册适配器类的同时声明凭据需求，
# 界面会自动出现对应输入框，不会漏。
_CREDENTIAL_KINDS: dict[str, dict[str, bool]] = {
    "nexusphp": {"rss_url": True},
    "mteam": {"api_key": True, "rss_url": False},
}

#: 适配器类 -> 凭据需求类型名
_CLASS_TO_KIND = {
    "NexusPhpAdapter": "nexusphp",
    "MTeamAdapter": "mteam",
}


def site_credential_kinds(site_name: str) -> dict[str, bool]:
    """返回该站点需要的凭据：{字段名: 是否必需}。

    未注册的站点按 NexusPHP 处理（rss_url 必需），与 build_adapter 的
    回退一致 —— 自定义站点据此出现凭据输入框。
    """
    name = resolve_site_name(site_name)
    cls = _ADAPTER_CLASS_MAP.get(name)
    kind = _CLASS_TO_KIND.get(cls.__name__) if cls is not None else "nexusphp"
    return dict(_CREDENTIAL_KINDS.get(kind, {}))
