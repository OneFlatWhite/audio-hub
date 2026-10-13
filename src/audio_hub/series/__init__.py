"""连载追踪（v0.4 模块 C）：从标题解析分集号、登记分卷、判定完结。"""

from .tracker import (
    DEFAULT_PART_PATTERN,
    SeriesContext,
    build_series_context,
    extract_part_no,
    refresh_status,
    register_part,
    register_task,
)

__all__ = [
    "DEFAULT_PART_PATTERN",
    "SeriesContext",
    "build_series_context",
    "extract_part_no",
    "refresh_status",
    "register_part",
    "register_task",
]
