"""site_stats 仓储层：成功/失败计数、熔断打开与到期自动关闭。

全部用 now= 注入时钟，不 sleep、不依赖墙钟。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from audio_hub import repo

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


class TestSuccess:
    @pytest.mark.asyncio
    async def test_first_success_creates_row(self, test_db):
        """首次写入自动建行（upsert），不要求先初始化。"""
        assert await repo.get_site_stats(test_db, "hhanclub") is None
        await repo.record_site_success(test_db, "hhanclub", rows=12, now=T0)
        st = await repo.get_site_stats(test_db, "hhanclub")
        assert st["total_requests"] == 1
        assert st["total_errors"] == 0
        assert st["consecutive_fail"] == 0
        assert st["last_count"] == 12
        assert st["last_success_at"] == T0.isoformat(timespec="seconds")
        assert st["circuit_open_until"] is None

    @pytest.mark.asyncio
    async def test_success_resets_fail_count_and_closes_circuit(self, test_db):
        """连败后一次成功：consecutive_fail 归零、熔断清掉，累计计数保留。"""
        for i in range(3):
            await repo.record_site_failure(
                test_db, "hdsky", f"boom {i}", now=T0 + timedelta(seconds=i), threshold=3,
            )
        assert await repo.is_circuit_open(test_db, "hdsky", now=T0 + timedelta(minutes=1))

        await repo.record_site_success(test_db, "hdsky", rows=5, now=T0 + timedelta(minutes=2))
        st = await repo.get_site_stats(test_db, "hdsky")
        assert st["consecutive_fail"] == 0
        assert st["circuit_open_until"] is None
        assert st["total_requests"] == 4
        assert st["total_errors"] == 3
        assert not await repo.is_circuit_open(test_db, "hdsky", now=T0 + timedelta(minutes=2))

    @pytest.mark.asyncio
    async def test_success_without_rows_keeps_last_count(self, test_db):
        await repo.record_site_success(test_db, "x", rows=7, now=T0)
        await repo.record_site_success(test_db, "x", now=T0)
        assert (await repo.get_site_stats(test_db, "x"))["last_count"] == 7


class TestFailureAndCircuit:
    @pytest.mark.asyncio
    async def test_three_failures_open_circuit_once(self, test_db):
        """前两次不开；第三次打开并返回 True；第四次仍熔断但返回 False（不重复报警）。"""
        r1 = await repo.record_site_failure(test_db, "s", "e1", now=T0, threshold=3)
        r2 = await repo.record_site_failure(test_db, "s", "e2", now=T0, threshold=3)
        assert (r1, r2) == (False, False)
        assert not await repo.is_circuit_open(test_db, "s", now=T0)

        r3 = await repo.record_site_failure(
            test_db, "s", "e3", now=T0, threshold=3, open_minutes=30,
        )
        assert r3 is True
        st = await repo.get_site_stats(test_db, "s")
        assert st["consecutive_fail"] == 3
        assert st["total_errors"] == 3
        assert st["total_requests"] == 3
        assert st["last_error"] == "e3"
        assert st["circuit_open_until"] == (T0 + timedelta(minutes=30)).isoformat(
            timespec="seconds",
        )
        assert await repo.is_circuit_open(test_db, "s", now=T0 + timedelta(minutes=29))

        r4 = await repo.record_site_failure(test_db, "s", "e4", now=T0 + timedelta(minutes=1))
        assert r4 is False
        assert (await repo.get_site_stats(test_db, "s"))["consecutive_fail"] == 4

    @pytest.mark.asyncio
    async def test_circuit_expires_automatically(self, test_db):
        """过期后 is_circuit_open 为 False，无需任何写操作。"""
        for _ in range(3):
            await repo.record_site_failure(test_db, "s", "e", now=T0, threshold=3, open_minutes=30)
        assert await repo.is_circuit_open(test_db, "s", now=T0 + timedelta(minutes=29, seconds=59))
        assert not await repo.is_circuit_open(test_db, "s", now=T0 + timedelta(minutes=30))
        assert not await repo.is_circuit_open(test_db, "s", now=T0 + timedelta(hours=5))

    @pytest.mark.asyncio
    async def test_failure_after_expiry_reopens_and_reports(self, test_db):
        """熔断过期后再失败：连败计数未清（没成功过），立即重新打开并再次返回 True。"""
        for _ in range(3):
            await repo.record_site_failure(test_db, "s", "e", now=T0, threshold=3, open_minutes=30)
        later = T0 + timedelta(minutes=31)
        reopened = await repo.record_site_failure(
            test_db, "s", "e again", now=later, threshold=3, open_minutes=30,
        )
        assert reopened is True
        assert await repo.is_circuit_open(test_db, "s", now=later + timedelta(minutes=1))

    @pytest.mark.asyncio
    async def test_threshold_and_minutes_are_configurable(self, test_db):
        r = await repo.record_site_failure(test_db, "s", "e", now=T0, threshold=1, open_minutes=5)
        assert r is True
        st = await repo.get_site_stats(test_db, "s")
        assert st["circuit_open_until"] == (T0 + timedelta(minutes=5)).isoformat(timespec="seconds")

    @pytest.mark.asyncio
    async def test_unknown_site_is_not_open(self, test_db):
        assert not await repo.is_circuit_open(test_db, "never-seen")

    @pytest.mark.asyncio
    async def test_long_error_is_truncated(self, test_db):
        await repo.record_site_failure(test_db, "s", "x" * 2000, now=T0)
        assert len((await repo.get_site_stats(test_db, "s"))["last_error"]) == 500


class TestList:
    @pytest.mark.asyncio
    async def test_list_returns_all_sites_keyed(self, test_db):
        await repo.record_site_success(test_db, "b", rows=1, now=T0)
        await repo.record_site_failure(test_db, "a", "e", now=T0)
        out = await repo.list_site_stats(test_db)
        assert set(out) == {"a", "b"}
        assert out["a"]["consecutive_fail"] == 1
        assert out["b"]["total_requests"] == 1
