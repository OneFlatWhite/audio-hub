"""CLI / 诊断脚本的运行时装配：与 main.py lifespan 对齐的「配置 → DB → 注册表」链路。

为什么需要它：v0.6 把站点 / 下载器的权威源迁到 DB（sites / downloaders 表），
YAML 只剩首启种子。直接拿 load_config() 的结果构建适配器会拿到
auth_status=unconfigured 的空壳（YAML 没有这一列），认证门禁一拦就是零适配器
——CLI 的 run-once / verify-adapters 与诊断脚本因此被静默废掉。

本模块把 lifespan 的装配顺序抽出来复用：
init_db → hydrate_config（界面覆盖层）→ seed_from_yaml（首启种子）→
从 DB 重载站点与下载器（含解密凭据）。

两种模式：
- create_db=True（生产 CLI：run-once）：库不存在就建，行为与服务启动一致；
- create_db=False（诊断入口：verify-adapters / check_qb）：库不存在时降级为
  纯 YAML，不在全新环境里凭空造库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from . import registry_store, settings_store
from .config import AppConfig, DownloaderConfig
from .db import init_db


@dataclass
class RuntimeContext:
    """bootstrap 的装配结果。

    db 为 None 表示纯 YAML 降级（库不存在且未要求建库）：config 与 downloaders
    全部来自 YAML，站点 auth_status 是默认值 unconfigured —— 诊断场景应配合
    build_adapters(..., require_valid_auth=False) 显式绕过门禁。
    """

    config: AppConfig
    db: Any | None
    downloaders: list[DownloaderConfig]
    source: str  # "db" | "yaml"
    notes: list[str] = field(default_factory=list)

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()


async def bootstrap_runtime(config: AppConfig, *, create_db: bool = False) -> RuntimeContext:
    """按 lifespan 顺序装配运行时：hydrate 覆盖层 → YAML 首启种子 → 从 DB 重载。

    create_db=False 且库不存在时不建库，返回纯 YAML 的降级上下文（source="yaml"）。
    """
    if not create_db and not Path(config.db_path).exists():
        logger.info(f"数据库不存在（{config.db_path}），按纯 YAML 配置运行")
        return RuntimeContext(
            config=config,
            db=None,
            downloaders=[config.qbittorrent],
            source="yaml",
            notes=[("数据库不存在，站点与下载器配置全部来自 config.yaml；"
                    "界面（/sites、/downloaders）里的改动未生效")],
        )

    db = await init_db(config.db_path)
    await settings_store.hydrate_config(db, config, settings_store.all_specs(config))
    await registry_store.seed_from_yaml(db, config)
    config.sites = await registry_store.load_site_configs(db)
    downloaders = await registry_store.load_downloader_configs(db)
    return RuntimeContext(config=config, db=db, downloaders=downloaders, source="db")


def default_downloader(downloaders: list[DownloaderConfig]) -> DownloaderConfig | None:
    """默认下载器配置：is_default 优先，其次优先级最高的已启用实例。

    选择规则与 DownloaderRegistry.default() 一致，供不走注册表的诊断脚本使用。
    """
    for c in downloaders:
        if c.is_default:
            return c
    enabled = [c for c in downloaders if c.enabled]
    if not enabled:
        return None
    return max(enabled, key=lambda c: int(c.priority or 0))
