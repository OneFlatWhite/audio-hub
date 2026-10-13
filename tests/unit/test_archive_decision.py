"""归档决策三分支：new / upgrade / skip，以及质量分与目录名辅助函数。"""

from __future__ import annotations

import pytest

from audio_hub import repo
from audio_hub.archive.decision import (
    decide,
    quality_score,
    resolve_task_kind,
    safe_dirname,
    title_norm_of,
)
from audio_hub.models import ArchiveDecision, LibraryItem, Task, WatchEntry, WatchKind


def _task(title: str, quality: str | None, watch_id: str | None = "w1", **kw) -> Task:
    return Task(
        id=kw.pop("id", "task-0001"), watch_entry_id=watch_id, info_hash="h",
        site="hhanclub", title=title, quality=quality, **kw,
    )


class TestQualityScore:
    def test_label_lookup_matches_matcher_weights(self):
        assert quality_score(_task("x", "flac_16")) == 85
        assert quality_score(_task("x", "flac_24")) == 95
        assert quality_score(_task("x", "dsd")) == 100
        assert quality_score(_task("x", "mp3")) == 40

    def test_unknown_label_falls_back_to_title_detection(self):
        t = _task("Jay Chou - Fantasy [FLAC 24bit 96kHz]", None)
        assert quality_score(t) == 95
        t2 = _task("Some Album MP3 320K", None)
        assert quality_score(t2) == 40

    def test_audiobook_kind_uses_format_table(self):
        t = _task("三体 有声书 M4B", None)
        assert quality_score(t, WatchKind.AUDIOBOOK) == 95
        # 有声书的 MP3 不是"低音质"，走格式表
        t2 = _task("三体 第一部 MP3", None)
        assert quality_score(t2, WatchKind.AUDIOBOOK) == 80
        assert quality_score(t2, WatchKind.MUSIC) == 40


class TestSafeDirname:
    def test_strips_unsafe_chars(self):
        assert safe_dirname('a/b:c*d?"e<f>g|h', "fb") == "a b c d e f g h"

    def test_empty_uses_fallback(self):
        assert safe_dirname("", "task-1") == "task-1"
        assert safe_dirname("...", "task-1") == "task-1"

    def test_truncates_long_names(self):
        assert len(safe_dirname("x" * 500, "fb")) == 120


class TestDecide:
    @pytest.mark.asyncio
    async def test_new_when_library_empty(self, test_db):
        t = _task("Jay Chou - Fantasy FLAC", "flac_16")
        decision, existing = await decide(test_db, t)
        assert decision == ArchiveDecision.NEW
        assert existing is None

    @pytest.mark.asyncio
    async def test_upgrade_when_new_score_higher(self, test_db):
        t_old = _task("Jay Chou - Fantasy [FLAC]", "flac_16")
        await repo.add_library_item(test_db, LibraryItem(
            watch_id="w1", task_id="old", title_norm=title_norm_of(t_old),
            kind=WatchKind.MUSIC, quality="flac_16", quality_score=85, path="/lib/x",
        ))
        # 方括号内的规格标记被归一化去掉，两版 title_norm 相同
        t_new = _task("Jay Chou - Fantasy [FLAC 24bit]", "flac_24", id="task-0002")
        decision, existing = await decide(test_db, t_new)
        assert decision == ArchiveDecision.UPGRADE
        assert existing is not None and existing.task_id == "old"

    @pytest.mark.asyncio
    async def test_skip_when_not_better(self, test_db):
        t_old = _task("Jay Chou - Fantasy", "flac_24")
        await repo.add_library_item(test_db, LibraryItem(
            watch_id="w1", task_id="old", title_norm=title_norm_of(t_old),
            kind=WatchKind.MUSIC, quality="flac_24", quality_score=95, path="/lib/x",
        ))
        # 同分 → skip
        same = _task("Jay Chou - Fantasy", "flac_24", id="task-0002")
        assert (await decide(test_db, same))[0] == ArchiveDecision.SKIP
        # 更差 → skip
        worse = _task("Jay Chou - Fantasy", "mp3", id="task-0003")
        assert (await decide(test_db, worse))[0] == ArchiveDecision.SKIP

    @pytest.mark.asyncio
    async def test_different_watch_id_does_not_collide(self, test_db):
        t = _task("Fantasy", "flac_16")
        await repo.add_library_item(test_db, LibraryItem(
            watch_id="other", task_id="old", title_norm=title_norm_of(t),
            kind=WatchKind.MUSIC, quality="flac_24", quality_score=95, path="/lib/x",
        ))
        decision, _ = await decide(test_db, t)
        assert decision == ArchiveDecision.NEW


class TestResolveTaskKind:
    @pytest.mark.asyncio
    async def test_prefers_watch_entry_kind(self, test_db):
        wid = await repo.add_watch_entry(test_db, WatchEntry(kind=WatchKind.AUDIOBOOK, query="三体"))
        t = _task("三体 FLAC", "flac_16", watch_id=wid)
        assert await resolve_task_kind(test_db, t) == WatchKind.AUDIOBOOK

    @pytest.mark.asyncio
    async def test_falls_back_to_title_clues(self, test_db):
        assert await resolve_task_kind(test_db, _task("三体 有声书 M4B", None, None)) \
            == WatchKind.AUDIOBOOK
        assert await resolve_task_kind(test_db, _task("Album FLAC", "flac_16", None)) \
            == WatchKind.MUSIC
