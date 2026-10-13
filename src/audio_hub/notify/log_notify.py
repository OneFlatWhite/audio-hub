"""通知 sink：结构化 JSON 日志。

事件结构与便捷方法在 base.py；本模块只负责「写日志」这一件事。
`Event` 从这里再导出一次，保持 `from audio_hub.notify.log_notify import Event` 的旧写法可用。
"""

from __future__ import annotations

from loguru import logger

from .base import BaseNotifier, Event

__all__ = ["Event", "LogNotifier"]

# Event.level → loguru 级别；未知值回落到构造时指定的默认级别
_LEVEL_MAP = {"warning": "WARNING", "error": "ERROR"}


class LogNotifier(BaseNotifier):
    """把事件写入结构化日志。"""

    def __init__(self, level: str = "INFO"):
        self.level = level

    async def emit(self, event: Event) -> None:
        logger.log(_LEVEL_MAP.get(event.level, self.level), event.as_log())
