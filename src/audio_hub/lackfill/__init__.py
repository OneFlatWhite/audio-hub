"""缺集补齐子包(v0.8 / M1):Emby/TMDB 直连 + 缺集扫描。

职责边界:
- 扫描侧:从 Emby 拉全库剧集与已有集数,对照 TMDB 播出进度算出「缺哪些集」,
  产出 ScanReport(含逐剧 ShowLack 与错误清单),不做下载与任务编排。
- 客户端层(emby / tmdb)是薄封装:只做认证、分页参数与响应整形,
  非本包状态;TMDB 侧调用由 service 统一节流(每调用间 50ms)。
- 纯逻辑(diff)与数据结构(models)不依赖网络,可独立单测;
  子包不 import 主包 config,扫描入口只认带约定属性的 cfg 对象,保持可测性。
"""

from __future__ import annotations

from .diff import compute_missing, is_complete
from .emby import EmbyClient
from .models import ScanReport, ShowLack
from .service import LackfillService
from .tmdb import TmdbClient

__all__ = [
    "EmbyClient",
    "LackfillService",
    "ScanReport",
    "ShowLack",
    "TmdbClient",
    "compute_missing",
    "is_complete",
]
