"""下载器注册表：downloader_id → DownloaderConfig + 懒建的 qb 客户端。

设计要点：
- 配置来自 downloaders 表（registry_store.load_downloader_configs），YAML 只是种子。
- 客户端**懒建并缓存**：启动时不逐个连 qb（某台不在线不该拖慢启动），
  首次 get(id).client 时才构建；构建失败缓存 None，下次 replace 后重试。
  懒建含同步登录，异步调用方一律走 ensure_client（构建进线程），
  启动后由 warm_up 预热兜底（见 main.lifespan）。
- dry_run 时 client 永远为 None：空跑模式下不允许任何真实派发，
  与 Dispatcher / CompletionMonitor 的 dry_run 语义一致。
- replace(configs) 整体替换并清空客户端缓存，供设置页热生效。
- qbittorrentapi / transmission_rpc 都是同步 HTTP 库：所有客户端方法调用
  必须经 handle.call（per-client 锁 + asyncio.to_thread），直接在协程里
  调用会阻塞事件循环（Web 整站卡顿事故的根因）。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from ..config import DownloaderConfig

#: 客户端工厂签名：接收 DownloaderConfig，返回 qb client 或 None（连接失败）
ClientFactory = Callable[[DownloaderConfig], Any]

_UNSET = object()


class ClientGate:
    """同步客户端调用的线程门：同一客户端串行 + 不阻塞事件循环。

    - asyncio.to_thread：调用在默认线程池执行，事件循环照常跑；
    - per-client asyncio.Lock：requests.Session 非线程安全，同一客户端的
      调用必须串行（不同下载器之间互不等待）。

    注册表内的客户端用 DownloaderHandle.call（handle 自带门）；本类同时
    供旧式单实例 / 自建只读客户端两条旁路（completion.monitor、
    seeding.lifecycle）复用同一套语义。
    """

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def call(self, fn: Callable, /, *args, **kwargs):
        """在线程里执行 fn（应为已绑定到客户端的方法），返回其结果。"""
        async with self._lock:
            return await asyncio.to_thread(fn, *args, **kwargs)


@dataclass
class DownloaderHandle:
    """注册表里的一个下载器：配置 + 懒建的客户端。"""

    config: DownloaderConfig
    _factory: ClientFactory | None = field(default=None, repr=False)
    _dry_run: bool = False
    _client: Any = field(default=_UNSET, repr=False)
    _gate: ClientGate = field(default_factory=ClientGate, repr=False)
    #: 懒建互斥：warm_up 后台线程与请求路径同时首触时防重复构建
    _build_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def client(self):
        """qb 客户端；dry_run / 工厂缺失 / 构建失败时为 None。首次访问才构建。"""
        if self._dry_run or self._factory is None:
            return None
        if self._client is _UNSET:
            with self._build_lock:
                if self._client is _UNSET:
                    try:
                        self._client = self._factory(self.config)
                    except Exception as e:  # noqa: BLE001 - 单台 qb 不可用不得影响其它实例
                        logger.error(f"下载器 {self.config.id} 客户端构建失败: {e}")
                        self._client = None
        return self._client

    async def ensure_client(self):
        """异步取客户端：懒构建（含同步 auth_log_in / Transmission 握手）放进线程。

        生产路径由 lifespan 预热任务先行调用，此后 client 属性均为缓存命中；
        热重建（registry.replace 清缓存）后若轮询 / 请求抢先首触，本方法保证
        构建不落在事件循环上。返回值语义与 client 属性一致（失败为 None）。
        """
        if self._client is _UNSET and not self._dry_run and self._factory is not None:
            await asyncio.to_thread(lambda: self.client)   # 触发构建并缓存
        return self._client

    async def call(self, fn: Callable, /, *args, **kwargs):
        """本 handle 客户端的同步方法调用：per-client 锁串行化 + 线程执行。

        fn 须是已绑定到 self.client 的方法（调用方先确保客户端可用），如
        `await handle.call(client.torrents_info, tag=tag)`。
        """
        return await self._gate.call(fn, *args, **kwargs)

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

    async def warm_up(self) -> None:
        """对全部已启用下载器预建客户端（线程内构建 + 登录，不阻塞事件循环）。

        生产路径在 lifespan 启动后与下载器注册表热重建后各跑一次：否则首次
        轮询 / 首个请求会在事件循环上同步登录，单台无响应 qb 能卡住整站。
        单台失败只记日志 —— 预热是优化，懒建（ensure_client / client）仍是兜底。
        """
        handles = self.all_enabled()
        ok = 0
        for handle in handles:
            try:
                if await handle.ensure_client() is not None:
                    ok += 1
            except Exception as e:  # noqa: BLE001 - 单台失败不影响其它实例
                logger.warning(f"下载器 {handle.id} 客户端预热失败: {type(e).__name__}: {e}")
        logger.info(f"下载器客户端预热完成: {ok}/{len(handles)}")
