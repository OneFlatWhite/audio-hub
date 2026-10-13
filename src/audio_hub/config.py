"""应用配置加载：Pydantic Settings + Fernet 解密凭据。

配置文件路径由环境变量 AUDIO_HUB_CONFIG 指定，默认 ./config/config.yaml。
敏感字段（passkey / api_key / abs_token / webhook）在 config.yaml 中以
*_encrypted 后缀 + Fernet token 存储，启动时自动解密到去掉后缀的同名字段，
运行时明文不落盘。

v0.4 新增子模型：paths / validation / scrape / seeding / notify，全部有默认值，
旧 YAML 不写这些段仍能加载。v0.5 补 search（站点熔断阈值）。

v0.6 起 sites / downloaders 表是权威源：YAML 的 sites 与 qbittorrent 段只在
首次启动时作种子导入（见 registry_store.seed_from_yaml）。SiteConfig /
DownloaderConfig 仍是适配器与派发器的内存输入，改由 DB 行构造。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .crypto import decrypt_value


def split_ids(raw: str | None) -> list[str]:
    """逗号分隔的分类 ID → 去空白、去空项的列表。"""
    return [p.strip() for p in str(raw or "").split(",") if p.strip()]


class SiteConfig(BaseModel):
    """单个 PT 站点的运行时配置（适配器的输入）。

    v0.6 增 cookie / user_agent / proxy / domain / auth_status / priority：
    NexusPHP 站内搜索需要 Cookie + UA，mteam 需要 API Key + UA。
    凭据字段（rss_url / api_key / cookie）在这里是**明文**—— 本对象只在
    进程内流转，不往页面和日志里丢；页面用 models.SiteRecord。
    """
    name: str                           # hhanclub / hdsky / audiences / mteam
    enabled: bool = True
    rss_url: str = ""                   # RSS 端点 URL（含 passkey token）
    search_url: str = ""                # 搜索端点（可选）
    api_key: str = ""                   # M-Team API Key（加密存储）
    music_cat_id: str = ""              # 音乐区分类 ID，允许逗号分隔多个
    audiobook_cat_id: str = ""          # 有声书区分类 ID（v0.2+），允许逗号分隔
    max_rows: int = 50
    title_bracket_style: str = "【】"   # 【】 or []
    #: 站点级 H&R 要求覆盖：None 表示沿用 seeding.default_*
    hr_min_seed_hours: int | None = None
    hr_min_ratio: float | None = None
    # ── v0.6 ──
    cookie: str = ""                    # NexusPHP 登录 Cookie（加密存储）
    user_agent: str = ""
    proxy: str = ""                     # http(s)://host:port，空 = 直连
    domain: str = ""                    # https://hhanclub.net，站内搜索 / 认证的基址
    auth_status: str = "unconfigured"   # models.AuthStatus 的值
    priority: int = 0                   # 同专辑多候选时的站点优先级，越大越优先

    @property
    def music_cat_ids(self) -> list[str]:
        return split_ids(self.music_cat_id)

    @property
    def audiobook_cat_ids(self) -> list[str]:
        return split_ids(self.audiobook_cat_id)


class DownloaderConfig(BaseModel):
    """一个下载器实例的连接配置（v0.6 前名为 QbittorrentConfig）。

    认证方式：username 非空时用用户名 / 密码登录，否则沿用 API Key（qb 5.2+）。
    v0.8 起支持多种下载器类型（type=qbittorrent/transmission）：
    Transmission 无 API Key 概念，凭据可选（内网白名单场景可不填）。
    """
    id: str = "default"
    name: str = "默认 qBittorrent"
    type: str = "qbittorrent"           # qbittorrent / transmission
    host: str = "localhost"
    port: int = 8085
    username: str = ""
    password: str = ""                  # 加密存储
    api_key: str = ""                   # 加密存储
    category: str = "audio-hub"
    tag: str = "hub"
    save_path: str = "/musiclab/seeds"
    enabled: bool = True
    is_default: bool = False
    priority: int = 0


#: 旧名别名：现有代码与测试仍以 QbittorrentConfig 构造单实例配置
QbittorrentConfig = DownloaderConfig


class PathsConfig(BaseModel):
    """归档链路涉及的全部目录（容器内视角）。

    全部属「需重启」项：归档/校验/回收模块在启动时按这些路径创建目录并
    校验可写性，运行中换路径会让半截文件落在旧目录。
    """
    #: 已完成种子 cp 到此处供 MTW 刮削（不动原种，保护做种哈希）
    scratch_inbox: str = "/musiclab/scratch/inbox"
    library_music: str = "/musiclab/library"
    library_audiobook: str = "/audiolab/library"
    #: 升级替换时旧版本移入此处，到期后清理
    recycle_dir: str = "/musiclab/recycle"
    #: 校验不通过的内容隔离到此处等待人工处理
    quarantine_dir: str = "/musiclab/quarantine"
    recycle_retention_days: int = 30


class ValidationConfig(BaseModel):
    """完成后文件校验（假无损 / 空包 / 扩展名）。"""
    enabled: bool = True
    allowed_exts: list[str] = Field(default_factory=lambda: [
        "flac", "ape", "wav", "dsf", "dff", "mp3", "m4a", "m4b",
        "aac", "ogg", "opus", "wma",
    ])
    min_audio_files: int = 1
    fake_lossless_check: bool = True
    #: auCDtect 可执行文件路径；空则跳过频谱检测，只做容器层检查
    aucdtect_path: str = ""
    on_fail: Literal["quarantine", "pass_through"] = "quarantine"


class ScrapeConfig(BaseModel):
    """刮削联动：MusicTag Web 与 Audiobookshelf。"""
    mtw_webhook_url: str = ""
    abs_url: str = ""
    abs_library_id: str = ""
    #: YAML 中以 abs_token_encrypted 存储，启动时解密到此字段
    abs_token: str = ""
    timeout_seconds: int = 15


class SearchConfig(BaseModel):
    """搜索轮次的站点熔断与各定时任务间隔。

    连续失败达到阈值即打开熔断，熔断期内该站整轮跳过（不发任何请求）。
    触发风控的后果落在账号上，宁可漏抓一轮也不要在站点已经报错时继续打。
    """
    #: 连续失败多少次打开熔断
    circuit_fail_threshold: int = 3
    #: 熔断持续时长（分钟），到期自动关闭
    circuit_open_minutes: int = 30
    #: RSS 捕获（rss_capture job）间隔，分钟
    rss_interval_minutes: int = 10
    #: 订阅主动搜索（subscription_search job）间隔，分钟
    subscription_scan_minutes: int = 15
    #: 站点认证巡检（site_auth_check job）间隔，小时
    auth_check_hours: int = 24


class SeedingConfig(BaseModel):
    """做种 / H&R 管理。"""
    enabled: bool = True
    check_interval_minutes: int = 30
    default_min_seed_hours: int = 72
    default_min_ratio: float = 1.0
    on_satisfied: Literal["keep", "pause"] = "keep"


class DiscoverConfig(BaseModel):
    """发现墙（v0.7，/discover 页）。

    封面缓存目录不在此处配置：固定取 db_path 同级的 covers/（即 config.yaml
    所在目录），跟随数据卷走，少一个需要解释的路径键。
    """
    #: 封面来源：itunes（免费无 key，服务端代理 + 磁盘缓存）/ none（全部占位卡）
    cover_provider: Literal["itunes", "none"] = "itunes"
    #: 发现墙进程内缓存 TTL（分钟），期内重复打开页面不再请求站点
    cache_ttl_minutes: int = 15
    #: 手动刷新的每站最短间隔（分钟）：同一站期内被刷新过则沿用旧数据并标注
    refresh_min_interval_minutes: int = 5


#: 通知事件种类的全集，也是设置页 notify.kinds 的合法值集合。
#: 与代码里实际产出的事件一一对应（grep `Event(kind=` / `EVENT_*` 常量核对）；
#: "test" 是推送测试专用、不落事件流，不在其列。
NOTIFY_KINDS: tuple[str, ...] = (
    # 派发与归档链路
    "dispatched", "completed", "failed", "routed", "quarantined",
    "archive_skipped", "scrape_failed",
    # 订阅引擎（v0.6）
    "subscription_hit", "subscription_upgrade", "subscription_completed",
    # 做种与连载
    "hr_satisfied", "series_completed",
    # 站点与下载器健康
    "adapter_error", "site_circuit_open", "site_auth_expired",
    "downloader_unreachable",
    # 轮次汇总
    "round_summary",
)


class NotifyConfig(BaseModel):
    """外部推送渠道。凭据在 YAML 中以 *_encrypted 存储。"""
    wecom_webhook: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    generic_webhook_url: str = ""
    #: 哪些事件种类外推；空列表表示全部不推
    kinds: list[str] = Field(default_factory=lambda: list(NOTIFY_KINDS))


class WebConfig(BaseModel):
    """Web 界面与认证配置（PRD 第十二章）。

    会话采用服务端 DB 存储 + 随机不透明 token cookie，故**不需要签名密钥**：
    服务端可随时吊销，且少一个需要保护的密钥文件。
    """
    #: 会话有效期（天），滑动续期
    session_days: int = 7
    #: 登录限流：单 IP 在窗口内允许的尝试次数（对齐 Navidrome AuthRequestLimit 默认 5）
    auth_request_limit: int = 5
    #: 限流窗口（分钟）
    auth_window_minutes: int = 15
    #: cookie Secure 标志。走 HTTPS/Tailscale 时应为 true；
    #: 纯内网 HTTP 下必须为 false，否则浏览器不会回传 cookie。
    cookie_secure: bool = False
    #: 监听端口
    port: int = 8090


class AppConfig(BaseModel):
    """应用顶层配置。"""
    dry_run: bool = True                # v0.1 默认空跑
    db_path: str = "/config/hub.db"
    log_level: str = "INFO"
    poll_interval_minutes: int = 30
    sites: list[SiteConfig] = Field(default_factory=list)
    #: 单对象保持 YAML 兼容：多下载器在 downloaders 表，这里只是首启种子
    qbittorrent: DownloaderConfig = Field(default_factory=DownloaderConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    scrape: ScrapeConfig = Field(default_factory=ScrapeConfig)
    seeding: SeedingConfig = Field(default_factory=SeedingConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    discover: DiscoverConfig = Field(default_factory=DiscoverConfig)

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_scratch_inbox(cls, data: Any) -> Any:
        """兼容旧 YAML：顶层 scratch_inbox 映射到 paths.scratch_inbox。

        早期文档与部分开发配置把刮削收件箱写在顶层；现在归到 paths 段。
        两处都写时以 paths 段为准（更新的写法胜出）。
        """
        if not isinstance(data, dict) or "scratch_inbox" not in data:
            return data
        data = dict(data)
        legacy = data.pop("scratch_inbox")
        paths = data.get("paths")
        paths = dict(paths) if isinstance(paths, dict) else {}
        paths.setdefault("scratch_inbox", legacy)
        data["paths"] = paths
        return data


def _decrypt_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    """递归解密配置中的敏感字段。

    约定：以 '_encrypted' 结尾的字段值为 Fernet token，
    解密后存入去掉后缀的同名字段。
    """
    result = dict(data)
    keys_to_remove = []
    for key, value in data.items():
        if key.endswith("_encrypted") and isinstance(value, str) and value:
            plain_key = key.removesuffix("_encrypted")
            try:
                result[plain_key] = decrypt_value(value)
            except Exception as e:
                raise RuntimeError(f"解密字段 '{key}' 失败: {e}") from e
            keys_to_remove.append(key)
        elif isinstance(value, dict):
            result[key] = _decrypt_sensitive(value)
        elif isinstance(value, list):
            result[key] = [
                _decrypt_sensitive(item) if isinstance(item, dict) else item
                for item in value
            ]
    for k in keys_to_remove:
        del result[k]
    return result


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载并解密应用配置。

    Args:
        path: config.yaml 路径；默认从环境变量 AUDIO_HUB_CONFIG 读取

    Returns:
        AppConfig 实例（敏感字段已解密到内存）
    """
    if path is None:
        path = os.environ.get("AUDIO_HUB_CONFIG", "config/config.yaml")
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {p}\n"
            "请复制 config/config.example.yaml 为 config/config.yaml 并填入实际值。"
        )

    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    decrypted = _decrypt_sensitive(raw)
    return AppConfig.model_validate(decrypted)
