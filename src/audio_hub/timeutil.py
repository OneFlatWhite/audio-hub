"""时间工具：统一使用 UTC 感知时间戳。

为什么不用 naive datetime：配置中 TZ=Asia/Shanghai，而 DB 里存的时间戳若为
naive，跨时区比较（如「做种时长是否达标」「上次搜索距今多久」）会产生错误结果，
且这类 bug 只在特定时区/夏令时边界暴露，极难排查。

统一约定：**存储与比较一律用 UTC-aware**，展示层再按需转本地时区。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

try:
    from zoneinfo import ZoneInfo
    SITE_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 - tzdata 缺失时退化为 UTC，不阻断
    SITE_TZ = UTC

# 站点返回的无时区时间串常见格式（馒头 createdDate 等）
_SITE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")

# 末尾带时区偏移（±HH:MM / ±HHMM）
_HAS_OFFSET_RE = re.compile(r"[+-]\d{2}:?\d{2}$")


def utcnow() -> datetime:
    """当前 UTC 时间（timezone-aware）。"""
    return datetime.now(UTC)


def iso_utc() -> str:
    """当前 UTC 时间的 ISO 8601 字符串，用于 DB 存储。"""
    return utcnow().isoformat(timespec="seconds")


def from_timestamp(value: float) -> datetime | None:
    """Unix 时间戳 → UTC-aware datetime。失败返回 None（不阻断解析）。"""
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None


def parse_iso(value: str) -> datetime | None:
    """解析 ISO 8601 字符串 → UTC-aware datetime。

    Python 3.11+ 的 fromisoformat 原生支持 'Z' 后缀，故无需手工替换。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # naive 输入按 UTC 处理，保证返回值一定 aware
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_site_datetime(value: str) -> datetime | None:
    """解析站点返回的**无时区标记**时间串 → UTC-aware datetime。

    与 parse_iso 的区别：parse_iso 把 naive 输入当 UTC，本函数当 Asia/Shanghai。

    为什么需要单独一个函数：馒头等国内站点返回的 createdDate 形如
    "2026-09-30 08:00:00"，实际是北京时间。若按 UTC 解析会偏 8 小时，
    导致「发布时间距今」判断失真——影响新种优先与做种时长计算。

    带时区标记（含 Z 或 ±HH:MM）的字符串仍按标记解析。
    """
    if not value:
        return None
    s = value.strip()

    # 带显式时区标记 → 走 parse_iso
    if s.endswith(("Z", "z")) or _HAS_OFFSET_RE.search(s):
        return parse_iso(s)

    for fmt in _SITE_FORMATS:
        try:
            # 刻意解析为 naive：站点时间串无时区标记，下一行按 SITE_TZ 补全。
            # 若在此处带 %z 反而无法解析这类字符串。
            naive = datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=SITE_TZ).astimezone(UTC)

    # 格式不认识时最后尝试 fromisoformat
    return parse_iso(s)
