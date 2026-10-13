"""资料库 / 隔离区页面与动作的 Web 集成测试：渲染、CSRF、restore / purge / release / discard。

fixture 复制自 test_web_routes.py（web_env / client / _setup_admin / _csrf），只多了
paths 配置指向 tmp_path，让动作真实移动文件。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audio_hub import repo
from audio_hub.main import app
from audio_hub.models import (
    LibraryItem,
    QuarantineItem,
    RecycleEntry,
    Task,
    TaskState,
    WatchEntry,
    WatchKind,
)
from audio_hub.web import auth


@pytest.fixture
def web_env(tmp_path: Path, monkeypatch):
    from audio_hub.crypto import generate_key
    key_file = tmp_path / "secrets.key"
    key_file.write_bytes(generate_key())

    for sub in ("seeds", "scratch", "library/music", "library/audiobook", "recycle", "quarantine"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    config_text = f"""
dry_run: true
db_path: "{(tmp_path / 'hub.db').as_posix()}"
log_level: "INFO"
poll_interval_minutes: 30

qbittorrent:
  host: "localhost"
  port: 8085
  api_key_encrypted: ""
  category: "audio-hub"
  tag: "hub"
  save_path: "{(tmp_path / 'seeds').as_posix()}"

web:
  cookie_secure: false
  auth_request_limit: 5
  auth_window_minutes: 15

paths:
  scratch_inbox: "{(tmp_path / 'scratch').as_posix()}"
  library_music: "{(tmp_path / 'library' / 'music').as_posix()}"
  library_audiobook: "{(tmp_path / 'library' / 'audiobook').as_posix()}"
  recycle_dir: "{(tmp_path / 'recycle').as_posix()}"
  quarantine_dir: "{(tmp_path / 'quarantine').as_posix()}"
  recycle_retention_days: 30

sites:
  - name: "hhanclub"
    enabled: false
    rss_url: "https://hhanclub.net/torrentrss.php?passkey=FAKE"
    music_cat_id: "410"
    max_rows: 50
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg_file))
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(key_file))
    return tmp_path


@pytest.fixture
def client(web_env):
    with TestClient(app) as c:
        yield c


def _setup_admin(client: TestClient, username="ops", password="strongpass123"):
    resp = client.post("/setup", data={
        "username": username, "password": password, "password2": password,
    }, follow_redirects=False)
    assert resp.status_code in (303, 200), resp.text
    return resp


def _csrf(client: TestClient) -> str:
    page = client.get("/dashboard")
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m, f"页面未找到 CSRF meta: {page.status_code}"
    return m.group(1)


def _run(coro):
    return asyncio.run(coro)


def _login_as_viewer(client: TestClient, username="viewer", password="strongpass123") -> None:
    """直接插库造一个非管理员，在**同一** client 上登出管理员再以它登入。

    create_admin 一律 is_admin=1，没有建普通用户的公开入口；不开第二个 TestClient
    是为了避免两个 lifespan 同时持有同一 SQLite。
    """
    async def insert():
        await app.state.db.execute(
            """INSERT INTO users (id, username, password_hash, is_admin, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (str(uuid.uuid4()), username, auth.hash_password(password), 0,
             auth.iso_utc(), auth.iso_utc()),
        )
        await app.state.db.commit()

    _run(insert())
    client.post("/logout", data={"csrf_token": _csrf(client)}, follow_redirects=False)
    resp = client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)
    assert resp.status_code == 303, resp.text[:200]


def _fill(d: Path, name: str = "01.flac") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"fLaC" + b"\x00" * 64)
    return d


# ── 资料库 ─────────────────────────────────────────────────

class TestLibraryPage:
    def test_empty_state(self, client):
        _setup_admin(client)
        html = client.get("/library").text
        assert "资料库为空" in html
        assert "回收区为空" in html

    def test_renders_items_and_recycle_entries(self, client, web_env):
        _setup_admin(client)
        db = app.state.db

        async def seed():
            iid = await repo.add_library_item(db, LibraryItem(
                watch_id="w1", title_norm="jay chou fantasy", kind=WatchKind.MUSIC,
                quality="flac_24", quality_score=95, path=str(web_env / "library/music/jay"),
            ))
            await repo.add_recycle_entry(db, RecycleEntry(
                library_item_id=iid, original_path=str(web_env / "library/music/jay"),
                recycle_path=str(web_env / "recycle/20260101" / str(iid)), reason="upgrade:abc",
            ))

        _run(seed())
        html = client.get("/library").text
        assert "jay chou fantasy" in html and ">95<" in html
        assert "upgrade:abc" in html
        assert "/restore" in html and "/purge" in html
        assert 'name="csrf_token"' in html
        assert "回收区" in html and "保留 30 天" in html

    def test_kind_filter(self, client, web_env):
        _setup_admin(client)
        db = app.state.db

        async def seed():
            await repo.add_library_item(db, LibraryItem(
                title_norm="music one", kind=WatchKind.MUSIC, path="/m"))
            await repo.add_library_item(db, LibraryItem(
                title_norm="book one", kind=WatchKind.AUDIOBOOK, path="/a"))

        _run(seed())
        html = client.get("/library?kind=audiobook").text
        assert "book one" in html and "music one" not in html

    def test_flash_messages(self, client):
        _setup_admin(client)
        assert "已从回收区恢复到原路径" in client.get("/library?ok=restored").text
        assert "回收区条目不存在" in client.get("/library?err=notfound").text


class TestRecycleActions:
    def _seed_upgrade(self, web_env: Path):
        """库内有新版（24bit）占着原路径，回收区里有旧版（16bit）。"""
        db = app.state.db
        original = web_env / "library" / "music" / "album"
        _fill(original, "new.flac")
        recycled = web_env / "recycle" / "20260101" / "1"
        _fill(recycled, "old.flac")

        async def seed():
            iid = await repo.add_library_item(db, LibraryItem(
                watch_id="w1", title_norm="album", quality="flac_24", quality_score=95,
                path=str(original),
            ))
            eid = await repo.add_recycle_entry(db, RecycleEntry(
                library_item_id=iid, original_path=str(original),
                recycle_path=str(recycled), reason="upgrade:t24",
            ))
            return iid, eid

        iid, eid = _run(seed())
        return original, recycled, iid, eid

    def test_restore_requires_csrf(self, client, web_env):
        _setup_admin(client)
        _, _, _, eid = self._seed_upgrade(web_env)
        resp = client.post(f"/library/recycle/{eid}/restore", follow_redirects=False)
        assert resp.status_code == 403

    def test_restore_displaces_occupant(self, client, web_env):
        _setup_admin(client)
        original, recycled, iid, eid = self._seed_upgrade(web_env)
        token = _csrf(client)

        resp = client.post(f"/library/recycle/{eid}/restore", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/library?ok=restored"

        # 旧版回到原路径
        assert (original / "old.flac").exists() and not (original / "new.flac").exists()
        assert not recycled.exists()
        # 新版被让位进回收区，带条目
        db = app.state.db
        pending = _run(repo.list_recycle_entries(db, restored=False))
        assert len(pending) == 1
        assert pending[0].reason == f"displaced_by_restore:{eid}"
        assert (Path(pending[0].recycle_path) / "new.flac").exists()
        assert Path(pending[0].recycle_path).is_relative_to(web_env / "recycle")
        restored = _run(repo.get_recycle_entry(db, eid))
        assert restored.restored_at is not None
        item = _run(repo.get_library_item(db, iid))
        assert item.path == str(original)

    def test_restore_twice_is_rejected(self, client, web_env):
        _setup_admin(client)
        _, _, _, eid = self._seed_upgrade(web_env)
        token = _csrf(client)
        client.post(f"/library/recycle/{eid}/restore", data={"csrf_token": token},
                    follow_redirects=False)
        resp = client.post(f"/library/recycle/{eid}/restore", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/library?err=already_restored"

    def test_restore_rejects_path_outside_recycle_dir(self, client, web_env):
        _setup_admin(client)
        victim = _fill(web_env / "seeds" / "precious")
        db = app.state.db
        eid = _run(repo.add_recycle_entry(db, RecycleEntry(
            library_item_id=None, original_path=str(web_env / "library/music/x"),
            recycle_path=str(victim), reason="tampered",
        )))
        token = _csrf(client)
        resp = client.post(f"/library/recycle/{eid}/restore", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/library?err=outside"
        assert (victim / "01.flac").exists()

    def test_purge_removes_dir_and_entry(self, client, web_env):
        _setup_admin(client)
        original, recycled, _, eid = self._seed_upgrade(web_env)
        token = _csrf(client)
        resp = client.post(f"/library/recycle/{eid}/purge", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/library?ok=purged"
        assert not recycled.exists()
        assert (original / "new.flac").exists()
        assert _run(repo.get_recycle_entry(app.state.db, eid)) is None

    def test_purge_refuses_outside_path(self, client, web_env):
        _setup_admin(client)
        victim = _fill(web_env / "seeds" / "precious")
        eid = _run(repo.add_recycle_entry(app.state.db, RecycleEntry(
            original_path="/x", recycle_path=str(victim), reason="tampered",
        )))
        token = _csrf(client)
        resp = client.post(f"/library/recycle/{eid}/purge", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/library?err=outside"
        assert (victim / "01.flac").exists()
        assert _run(repo.get_recycle_entry(app.state.db, eid)) is not None

    def test_purge_unknown_entry(self, client):
        _setup_admin(client)
        token = _csrf(client)
        resp = client.post("/library/recycle/9999/purge", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/library?err=notfound"

    def test_purge_requires_admin(self, client, web_env):
        """彻底删除不可逆：普通用户带有效 CSRF 也 403，目录与条目原样。"""
        _setup_admin(client)
        _, recycled, _, eid = self._seed_upgrade(web_env)
        _login_as_viewer(client)
        token = _csrf(client)
        resp = client.post(f"/library/recycle/{eid}/purge", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 403
        assert (recycled / "old.flac").exists()
        assert _run(repo.get_recycle_entry(app.state.db, eid)) is not None

    def test_restore_allowed_for_normal_user(self, client, web_env):
        """恢复可逆，仍对所有已登录用户开放。"""
        _setup_admin(client)
        original, _, _, eid = self._seed_upgrade(web_env)
        _login_as_viewer(client)
        resp = client.post(f"/library/recycle/{eid}/restore", data={"csrf_token": _csrf(client)},
                           follow_redirects=False)
        assert resp.headers["location"] == "/library?ok=restored"
        assert (original / "old.flac").exists()


# ── 隔离区 ─────────────────────────────────────────────────

def _seed_quarantine(web_env: Path, *, title="Bad Album [FLAC]", quality="flac_16"):
    """造一个 held 的隔离条目 + 对应 quarantined 任务 + 隔离目录。"""
    db = app.state.db
    qdir = _fill(web_env / "quarantine" / "task-q1")

    async def seed():
        wid = await repo.add_watch_entry(db, WatchEntry(kind=WatchKind.MUSIC, query="Bad"))
        tid = await repo.create_task(db, Task(
            id="task-q1", watch_entry_id=wid, info_hash="qh", site="hhanclub",
            title=title, quality=quality, state=TaskState.QUARANTINED,
        ))
        qid = await repo.add_quarantine_item(db, QuarantineItem(
            task_id=tid, scratch_path=str(qdir), reasons=["suspect_lossless:01.flac"],
        ))
        return tid, qid

    tid, qid = _run(seed())
    return qdir, tid, qid


class TestQuarantinePage:
    def test_empty_state(self, client):
        _setup_admin(client)
        html = client.get("/quarantine").text
        assert "没有待处理的隔离内容" in html
        assert "后续版本提供" not in html

    def test_renders_reasons_and_actions(self, client, web_env):
        _setup_admin(client)
        qdir, _, qid = _seed_quarantine(web_env)
        html = client.get("/quarantine").text
        assert str(qdir) in html
        assert "suspect_lossless:01.flac" in html
        assert f"/quarantine/{qid}/release" in html and f"/quarantine/{qid}/discard" in html
        assert 'name="csrf_token"' in html

    def test_status_tabs(self, client, web_env):
        _setup_admin(client)
        _seed_quarantine(web_env)
        assert "该状态下无记录" in client.get("/quarantine?status=released").text
        assert "task-q1"[:8] in client.get("/quarantine?status=all").text
        # 非法值回落到 held
        assert "suspect_lossless" in client.get("/quarantine?status=bogus").text

    def test_resolved_rows_have_no_actions(self, client, web_env):
        _setup_admin(client)
        _, _, qid = _seed_quarantine(web_env)
        _run(repo.set_quarantine_status(app.state.db, qid, "discarded"))
        html = client.get("/quarantine?status=discarded").text
        assert f"/quarantine/{qid}/release" not in html

    def test_flash_messages(self, client):
        _setup_admin(client)
        assert "已放行并归档入库" in client.get("/quarantine?ok=released").text
        assert "隔离条目不存在" in client.get("/quarantine?err=notfound").text


class TestQuarantineActions:
    def test_release_requires_csrf(self, client, web_env):
        _setup_admin(client)
        _, _, qid = _seed_quarantine(web_env)
        assert client.post(f"/quarantine/{qid}/release", follow_redirects=False).status_code == 403

    def test_release_archives_to_library(self, client, web_env):
        _setup_admin(client)
        qdir, tid, qid = _seed_quarantine(web_env)
        token = _csrf(client)

        resp = client.post(f"/quarantine/{qid}/release", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/quarantine?status=held&ok=released"

        db = app.state.db
        assert not qdir.exists()
        item = _run(repo.get_quarantine_item(db, qid))
        assert item.status == "released" and item.resolved_at is not None
        task = _run(repo.get_task(db, tid))
        assert task.state == TaskState.ROUTED and task.archive_decision == "new"
        assert Path(task.archived_path).is_relative_to(web_env / "library" / "music")
        assert (Path(task.archived_path) / "01.flac").exists()
        lib = _run(repo.list_library_items(db))
        assert len(lib) == 1 and lib[0].task_id == tid
        # scratch 副本保留给刮削
        scratch_copies = list((web_env / "scratch").iterdir())
        assert len(scratch_copies) == 1 and (scratch_copies[0] / "01.flac").exists()
        # 事件已进 events 表（app.state.notifier 含 DbNotifier）
        assert _run(repo.count_events(db, kind="routed")) == 1

    def test_release_skip_when_library_has_better(self, client, web_env):
        _setup_admin(client)
        qdir, tid, qid = _seed_quarantine(web_env, quality="flac_16")
        db = app.state.db
        task = _run(repo.get_task(db, tid))
        _run(repo.add_library_item(db, LibraryItem(
            watch_id=task.watch_entry_id, title_norm="bad album", quality="flac_24",
            quality_score=95, path=str(web_env / "library/music/bad album"),
        )))
        token = _csrf(client)
        resp = client.post(f"/quarantine/{qid}/release", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/quarantine?status=held&ok=released_skip"
        assert _run(repo.get_task(db, tid)).state == TaskState.ARCHIVED_SKIPPED
        assert not qdir.exists()
        assert list((web_env / "scratch").iterdir()) == [], "skip 应删除 scratch 副本"
        assert _run(repo.get_quarantine_item(db, qid)).status == "released"

    def test_release_twice_rejected(self, client, web_env):
        _setup_admin(client)
        _, _, qid = _seed_quarantine(web_env)
        token = _csrf(client)
        client.post(f"/quarantine/{qid}/release", data={"csrf_token": token},
                    follow_redirects=False)
        resp = client.post(f"/quarantine/{qid}/release", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/quarantine?status=held&err=not_held"

    def test_release_rejects_outside_path(self, client, web_env):
        _setup_admin(client)
        victim = _fill(web_env / "seeds" / "precious")
        qid = _run(repo.add_quarantine_item(app.state.db, QuarantineItem(
            task_id="task-q1", scratch_path=str(victim), reasons=["x"],
        )))
        token = _csrf(client)
        resp = client.post(f"/quarantine/{qid}/release", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/quarantine?status=held&err=outside"
        assert (victim / "01.flac").exists()

    def test_release_missing_dir(self, client, web_env):
        _setup_admin(client)
        qdir, _, qid = _seed_quarantine(web_env)
        (qdir / "01.flac").unlink()
        qdir.rmdir()
        token = _csrf(client)
        resp = client.post(f"/quarantine/{qid}/release", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/quarantine?status=held&err=missing"

    def test_discard_removes_dir_and_rejects_task(self, client, web_env):
        _setup_admin(client)
        qdir, tid, qid = _seed_quarantine(web_env)
        token = _csrf(client)
        resp = client.post(f"/quarantine/{qid}/discard", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/quarantine?status=held&ok=discarded"
        assert not qdir.exists()
        db = app.state.db
        assert _run(repo.get_quarantine_item(db, qid)).status == "discarded"
        task = _run(repo.get_task(db, tid))
        assert task.state == TaskState.REJECTED
        assert task.reject_reason == "quarantine_discarded"
        assert _run(repo.list_library_items(db)) == []

    def test_discard_refuses_outside_path(self, client, web_env):
        _setup_admin(client)
        victim = _fill(web_env / "seeds" / "precious")
        qid = _run(repo.add_quarantine_item(app.state.db, QuarantineItem(
            scratch_path=str(victim), reasons=["x"],
        )))
        token = _csrf(client)
        resp = client.post(f"/quarantine/{qid}/discard", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/quarantine?status=held&err=outside"
        assert (victim / "01.flac").exists()
        assert _run(repo.get_quarantine_item(app.state.db, qid)).status == "held"

    def test_discard_unknown(self, client):
        _setup_admin(client)
        token = _csrf(client)
        resp = client.post("/quarantine/4242/discard", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/quarantine?status=held&err=notfound"

    def test_discard_requires_admin(self, client, web_env):
        """丢弃不可逆：普通用户带有效 CSRF 也 403，隔离目录与任务状态不变。"""
        _setup_admin(client)
        qdir, tid, qid = _seed_quarantine(web_env)
        _login_as_viewer(client)
        resp = client.post(f"/quarantine/{qid}/discard", data={"csrf_token": _csrf(client)},
                           follow_redirects=False)
        assert resp.status_code == 403
        assert (qdir / "01.flac").exists()
        db = app.state.db
        assert _run(repo.get_quarantine_item(db, qid)).status == "held"
        assert _run(repo.get_task(db, tid)).state == TaskState.QUARANTINED

    def test_release_allowed_for_normal_user(self, client, web_env):
        """放行不删东西，仍对所有已登录用户开放。"""
        _setup_admin(client)
        _, tid, qid = _seed_quarantine(web_env)
        _login_as_viewer(client)
        resp = client.post(f"/quarantine/{qid}/release", data={"csrf_token": _csrf(client)},
                           follow_redirects=False)
        assert resp.headers["location"] == "/quarantine?status=held&ok=released"
        assert _run(repo.get_task(app.state.db, tid)).state == TaskState.ROUTED
