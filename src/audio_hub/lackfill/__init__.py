"""补缺集子包：Emby/TMDB 扫描、Re0 回填、115 转存与核销。

- 扫描结果写入 queue，跨重启为油猴提供待找队列，不依赖 MoviePilot。
- refill 分流 115 分享与 ed2k；p115、SA 真实协议负责转存/离线。
- cloud 以 CD2 真实包目录和稳定性确认文件落地，再刷新 Emby。
- ledger 保存台账，service 按季核对 Emby 实际入库并同步队列。
"""

from __future__ import annotations

from . import cloud, ledger, queue, refill, reservations
from .diff import compute_missing, is_complete
from .emby import EmbyClient
from .models import ScanReport, ShowLack
from .p115 import P115Client, P115Error, parse_share_url
from .sa import SAClient, SAError, send_wecom_callback
from .service import LackfillService
from .tmdb import TmdbClient

__all__ = [
    "EmbyClient",
    "LackfillService",
    "P115Client",
    "P115Error",
    "SAClient",
    "SAError",
    "ScanReport",
    "ShowLack",
    "TmdbClient",
    "cloud",
    "compute_missing",
    "is_complete",
    "ledger",
    "parse_share_url",
    "queue",
    "refill",
    "reservations",
    "send_wecom_callback",
]
