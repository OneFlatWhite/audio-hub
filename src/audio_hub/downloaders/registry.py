"""下载器注册表：downloader_id → DownloaderConfig + 懒建的 qb 客户端。

设计要点：
- 配置来自 downloaders 表（registry_store.load_downloader_configs），YAML 只是种子。
- 客户端**懒建并缓存**：启动时不逐个连 qb（某台不在线不该拖慢启动），
  首次 get(id).client 时才构建；构建失败缓存 None，下次 replace 后重试。
- dry_run 时 client 永远为 None：空跑模式下不允许任何真实派发，
  与 Dispatcher / CompletionMonitor 的 dry_run 语义一致。
- replace(configs) 整体替换并清空客户端缓存，供设置页热生效。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from ..config import DownloaderConfig

#: 客户端工厂签名：接收 DownloaderConfig，返回 qb client 或 None（连接失败）
ClientFactory = Callable[[DownloaderConfig], Any]

_UNSET = object()


@dataclass
class DownloaderHandle:
    """注册表里的一个下载器：配置 + 懒建的客户端。"""

    config: DownloaderConfig
    _factory: ClientFactory | None = field(default=None, repr=False)
    _dry_run: bool = False
    _client: Any = field(default=_UNSET, repr=False)

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def client(self):
        """qb 客户端；dry_run / 工厂缺失 / 构建失败时为 None。首次访问才构建。"""
        if self._dry_run or self._factory is None:
            return None
        if self._client is _UNSET:
            try:
                self._client = self._factory(self.config)
            except Exception as e:  # noqa: BLE001 - 单台 qb 不可用不得影响其它实例
                logger.error(f"下载器 {self.config.id} 客户端构建失败: {e}")
                self._client = None
        return self._client

    def reset_client(self) -> None:
        self._client = _UNSET


class DownloaderRegistry:
    """downloader_id → DownloaderHandle。"""

    def __init__(
        self, configs: list[DownloaderConfig], *,
        client_factory: ClientFactory | None = None, dry_run: bool = False,
    ):
        self._factory = client_factory
        self._dry_run = dry_run
        self._handles: dict[str, DownloaderHandle] = {}
        self.replace(configs)

    # ── 查询 ──

    @property
    def configs(self) -> list[DownloaderConfig]:
        return [h.config for h in self._handles.values()]

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def get(self, downloader_id: str | None) -> DownloaderHandle | None:
        """按 id 取；id 为空 / 未知返回 None（调用方据此回落到 default()）。"""
        if not downloader_id:
            return None
        return self._handles.get(downloader_id)

    def default(self) -> DownloaderHandle | None:
        """默认下载器：is_default 的那台；没有则优先级最高的已启用实例；表空为 None。"""
        for h in self._handles.values():
            if h.config.is_default:
                return h
        enabled = self.all_enabled()
        return enabled[0] if enabled else None

    def all_enabled(self) -> list[DownloaderHandle]:
        """已启用实例，优先级高的在前（同优先级保持配置顺序）。"""
        return sorted(
            (h for h in self._handles.values() if h.config.enabled),
            key=lambda h: -int(h.config.priority or 0),
        )

    def resolve(self, downloader_id: str | None) -> DownloaderHandle | None:
        """get 失败时回落到 default：任务 / 订阅上指定的下载器被删后仍能派发。"""
        return self.get(downloader_id) or self.default()

    def __contains__(self, downloader_id: str) -> bool:
        return downloader_id in self._handles

    def __len__(self) -> int:
        return len(self._handles)

    def __iter__(self):
        return iter(self._handles.values())

    # ── 变更 ──

    def replace(self, configs: list[DownloaderConfig]) -> None:
        """整体替换配置并清空客户端缓存（设置页保存后热生效）。"""
        self._handles = {
            c.id: DownloaderHandle(config=c, _factory=self._factory, _dry_run=self._dry_run)
            for c in configs
        }
        if len(self._handles) != len(configs):
            logger.warning("下载器配置存在重复 id，后者覆盖前者")
