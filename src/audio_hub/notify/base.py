"""通知抽象：Event 结构、Notifier 协议、便捷方法 mixin、扇出器。

设计决定（v0.4）：`emit` 定义为 **async**。
- DbNotifier 要经 repo 写库，repo 全是 async；若 emit 同步，就得用
  create_task 投递，事件落库与调用方失去顺序保证，测试里也难以等待。
- 调用方只有 scheduler.PipelineRunner 五处，全部在 async 上下文中，加 await 即可。
- LogNotifier 跟着改 async，三种 sink 签名统一，MultiNotifier 无需区分。

后续推送渠道（企业微信 / Telegram / 通用 webhook）只需实现 `async def emit`，
加进 MultiNotifier 的 sinks 即可，不改调用方。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from ..timeutil import iso_utc


@dataclass
class Event:
    """统一事件结构。

    level：info / warning / error，决定日志级别与事件页着色；
    便捷方法会按事件种类自动给 level（failed / adapter_error → error）。
    """
    kind: str                        # dispatched / completed / failed / quarantined / ...
    site: str = ""
    title: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    level: str = "info"
    at: str = field(default_factory=iso_utc)

    def as_log(self) -> str:
        return f"EVENT {self.kind} | {self.to_json()}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)


@runtime_checkable
class Notifier(Protocol):
    """任何事件接收端都只需实现这一个方法。"""

    async def emit(self, event: Event) -> None: ...


class BaseNotifier:
    """便捷方法 mixin：把业务动作翻译成 Event 并交给 `emit`。

    LogNotifier / MultiNotifier / 未来的推送 sink 都继承它，
    这样调用方无论拿到哪一种实现，`notifier.dispatched(...)` 的写法都一样。
    """

    async def emit(self, event: Event) -> None:  # pragma: no cover - 子类必须覆写
        raise NotImplementedError

    async def round_summary(
        self,
        *,
        candidates: int,
        accepted: int,
        review: int,
        rejected: int,
        site_errors: dict[str, str] | None = None,
        fallback_used: list[str] | None = None,
        skipped_circuit: list[str] | None = None,
        dry_run: bool = True,
    ) -> None:
        """一轮搜索的汇总事件（PRD §13.7 可观测指标）。"""
        await self.emit(Event(
            kind="round_summary",
            detail={
                "candidates": candidates,
                "accepted": accepted,
                "review": review,
                "rejected": rejected,
                "site_errors": site_errors or {},
                "fallback_used": fallback_used or [],
                "skipped_circuit": skipped_circuit or [],
                "dry_run": dry_run,
            },
        ))

    async def dispatched(self, site: str, title: str, **detail: Any) -> None:
        await self.emit(Event(kind="dispatched", site=site, title=title, detail=detail))

    async def completed(self, site: str, title: str, **detail: Any) -> None:
        await self.emit(Event(kind="completed", site=site, title=title, detail=detail))

    async def failed(self, site: str, title: str, reason: str) -> None:
        await self.emit(Event(
            kind="failed", site=site, title=title, detail={"reason": reason}, level="error",
        ))

    async def adapter_error(self, site: str, reason: str) -> None:
        """站点异常事件——这是发现站点改版的唯一信号（PRD §13.7）。"""
        await self.emit(Event(
            kind="adapter_error", site=site, detail={"reason": reason}, level="error",
        ))


class MultiNotifier(BaseNotifier):
    """扇出到多个 sink。单个 sink 抛异常只记 warning，不影响其他 sink 与调用方。

    通知是旁路：DB 写失败、webhook 超时都不应让流水线轮次中断。

    sink 分两类：**基础 sink**（日志 / DB，随进程生命周期固定）与
    **推送 sink**（企微 / Telegram / webhook，由设置页热重建）。区分方式
    是 sink 自带的 `is_push = True` 标记属性，而非两个列表 —— 这样 `add`
    的调用方不必关心类别，`replace_push_sinks` 也不会误删基础 sink。
    """

    def __init__(self, sinks: list[Notifier] | None = None):
        self.sinks: list[Notifier] = list(sinks or [])

    def add(self, sink: Notifier) -> None:
        self.sinks.append(sink)

    @property
    def push_sinks(self) -> list[Notifier]:
        """当前挂载的推送 sink（带 is_push 标记）。"""
        return [s for s in self.sinks if getattr(s, "is_push", False)]

    def replace_push_sinks(self, sinks: list[Notifier]) -> list[Notifier]:
        """整体替换推送 sink，基础 sink 原样保留。返回替换后的推送 sink 列表。

        设置页保存通知组后调用：推送器在构造时固化了 URL / token，
        只改 config 不重建的话仍会往旧地址发。
        """
        self.sinks = [s for s in self.sinks if not getattr(s, "is_push", False)]
        self.sinks.extend(sinks)
        return list(sinks)

    async def emit(self, event: Event) -> None:
        for sink in self.sinks:
            try:
                await sink.emit(event)
            except Exception as e:  # noqa: BLE001 - 旁路 sink 的任何异常都不得外溢
                logger.warning(
                    f"通知 sink {type(sink).__name__} 处理事件 {event.kind} 失败: "
                    f"{type(e).__name__}: {e}"
                )
