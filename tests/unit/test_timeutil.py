"""timeutil 单元测试：UTC 一致性 + 站点本地时间解析（8 小时偏移防护）。"""

from __future__ import annotations

from datetime import UTC

from audio_hub.timeutil import (
    from_timestamp,
    iso_utc,
    parse_iso,
    parse_site_datetime,
    utcnow,
)


class TestUtcConsistency:
    def test_utcnow_is_aware(self):
        assert utcnow().tzinfo is not None

    def test_iso_utc_parseable_and_aware(self):
        s = iso_utc()
        dt = parse_iso(s)
        assert dt is not None
        assert dt.tzinfo is not None

    def test_from_timestamp_utc(self):
        dt = from_timestamp(0)
        assert dt is not None
        assert dt.tzinfo == UTC
        assert dt.year == 1970

    def test_parse_iso_z_suffix(self):
        dt = parse_iso("2026-09-30T08:00:00Z")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_parse_iso_naive_treated_as_utc(self):
        dt = parse_iso("2026-09-30T08:00:00")
        assert dt.tzinfo == UTC

    def test_parse_iso_empty_returns_none(self):
        assert parse_iso("") is None
        assert parse_iso(None) is None


class TestSiteDatetime:
    """馒头 createdDate 是北京时间无时区标记，必须按 Asia/Shanghai 解析。"""

    def test_naive_site_time_is_shanghai(self):
        dt = parse_site_datetime("2026-09-30 08:00:00")
        assert dt is not None
        assert dt.tzinfo is not None
        # 北京 08:00 = UTC 00:00
        utc = dt.astimezone(UTC)
        assert utc.hour == 0
        assert utc.day == 30

    def test_site_time_not_treated_as_utc(self):
        """关键回归：若误当 UTC，北京 08:00 会解析成 UTC 08:00（差 8 小时）。"""
        dt = parse_site_datetime("2026-09-30 08:00:00")
        assert dt.astimezone(UTC).hour != 8

    def test_iso_with_z_still_respected(self):
        """带 Z 标记的串按标记解析，不套用上海时区。"""
        dt = parse_site_datetime("2026-09-30T08:00:00Z")
        assert dt.astimezone(UTC).hour == 8

    def test_iso_with_offset_respected(self):
        dt = parse_site_datetime("2026-09-30T08:00:00+08:00")
        assert dt.astimezone(UTC).hour == 0

    def test_t_format(self):
        dt = parse_site_datetime("2026-09-30T12:30:00")
        assert dt is not None
        assert dt.astimezone(UTC).hour == 4   # 北京 12:30 → UTC 04:30

    def test_empty_returns_none(self):
        assert parse_site_datetime("") is None

    def test_millisecond_timestamp_via_from_timestamp(self):
        """馒头时间戳可能是毫秒。"""
        dt = from_timestamp(1_700_000_000_000 / 1000)
        assert dt is not None
        assert dt.year == 2023
