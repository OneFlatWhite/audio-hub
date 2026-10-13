"""做种页集成测试：真渲染 + 四个 POST 动作 + CSRF，qb 客户端用假对象 monkeypatch。

fixture 与 test_web_routes 同构（文件内自带副本，避免跨文件 import 测试模块）。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from audio_hub import repo
from audio_hub.main import app
from audio_hub.models import SeedRecord, Task, TaskState
from audio_hub.seeding.lifecycle import SeedingMonitor

HASH_A = "a" * 40


@pytest.fixture
def web_env(tmp_path: Path, monkeypatch):
    """配置 + 密钥文件；站点全禁用（不触网）；dry_run=false 让 pause/resume 真的走 qb 假对象。"""
    from audio_hub.crypto import generate_key
    key_file = tmp_path / "secrets.key"
    key_file.write_bytes(generate_key())

    config_text = f"""
dry_run: false
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

seeding:
  enabled: true
  check_interval_minutes: 15
  default_min_seed_hours: 72
  default_min_ratio: 1.0
  on_satisfied: "keep"

web:
  cookie_secure: false
  auth_request_limit: 5
  auth_window_minutes: 15

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


class FakeQb:
    def __init__(self, torrents=None, *, fail=False):
        self.torrents = list(torrents or [])
        self.fail = fail
        self.paused: list = []
        self.resumed: list = []

    def torrents_info(self, tag=None, **_):
        if self.fail:
            raise ConnectionError("qb down")
        return list(self.torrents)

    def torrents_pause(self, torrent_hashes=None, **_):
        if self.fail:
            raise ConnectionError("qb down")
        self.paused.append(torrent_hashes)

    def torrents_resume(self, torrent_hashes=None, **_):
        if self.fail:
            raise ConnectionError("qb down")
        self.resumed.append(torrent_hashes)


@pytest.fixture
def fake_qb(monkeypatch):
    """把 SeedingMonitor 拿客户端的入口整体替换掉：无论 runner 内还是路由按需构造都命中。"""
    qb = FakeQb()
    monkeypatch.setattr(SeedingMonitor, "_get_client", lambda self: qb)
    return qb


@pytest.fixture
def client(web_env, fake_qb):
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


def _seed_task(*, status="seeding", seed_seconds=7200, ratio=0.5, hr_satisfied_at=None) -> str:
    """建一条 task + seed_record，返回 task_id。"""
    db = app.state.db

    async def go():
        tid = await repo.create_task(db, Task(
            info_hash=HASH_A, site="hhanclub", title="Jay Chou - Fantasy [FLAC]",
            state=TaskState.COMPLETED,
        ))
        await repo.upsert_seed_record(db, SeedRecord(
            task_id=tid, info_hash=HASH_A, site="hhanclub",
            seed_seconds=seed_seconds, ratio=ratio,
            required_seconds=72 * 3600, required_ratio=1.0,
            status=status, hr_satisfied_at=hr_satisfied_at,
        ))
        return tid

    return asyncio.run(go())


def _record(task_id: str) -> SeedRecord | None:
    return asyncio.run(repo.get_seed_record(app.state.db, task_id))


# ── 渲染 ────────────────────────────────────────────────────

class TestSeedingPageRender:
    def test_empty_state(self, client):
        _setup_admin(client)
        html = client.get("/seeding").text
        assert "做种与 H&amp;R" in html
        assert "暂无做种记录" in html
        assert "立即检查" in html
        assert "72 小时" in html and "每 15 分钟" in html

    def test_row_shows_title_progress_and_actions(self, client):
        _setup_admin(client)
        tid = _seed_task(seed_seconds=36 * 3600, ratio=0.25)
        html = client.get("/seeding").text

        assert "Jay Chou - Fantasy [FLAC]" in html
        assert tid[:8] in html
        assert "36.0 h" in html and "0.25" in html
        assert 'style="width:50%"' in html              # 取时长 50% 与分享率 25% 的较大者
        assert f"/seeding/{tid}/pause" in html
        assert f"/seeding/{tid}/mark-satisfied" in html
        assert f"/seeding/{tid}/resume" not in html

    def test_paused_row_offers_resume(self, client):
        _setup_admin(client)
        tid = _seed_task(status="paused")
        html = client.get("/seeding?status=paused").text
        assert f"/seeding/{tid}/resume?status=paused" in html
        assert f"/seeding/{tid}/pause" not in html

    def test_status_counts_and_filter(self, client):
        _setup_admin(client)
        _seed_task(status="satisfied", ratio=1.2)
        html = client.get("/seeding?status=seeding").text
        assert "暂无做种记录" in html                   # 筛 seeding 时无行
        html_all = client.get("/seeding").text
        assert "Jay Chou" in html_all

    def test_flash_mapping(self, client):
        _setup_admin(client)
        assert "已暂停做种" in client.get("/seeding?ok=paused").text
        assert "不可达" in client.get("/seeding?err=qb_unreachable").text
        # 未知 key 回落到全局表（原样显示）
        assert "whatever" in client.get("/seeding?ok=whatever").text


# ── 动作 ────────────────────────────────────────────────────

class TestSeedingActions:
    def test_pause_requires_csrf(self, client):
        _setup_admin(client)
        tid = _seed_task()
        resp = client.post(f"/seeding/{tid}/pause", follow_redirects=False)
        assert resp.status_code == 403
        assert _record(tid).status == "seeding"

    def test_actions_require_login(self, client):
        _setup_admin(client)
        tid = _seed_task()
        with TestClient(app) as anon:
            resp = anon.post(f"/seeding/{tid}/pause", follow_redirects=False)
            assert resp.status_code == 303 and "/login" in resp.headers["location"]

    def test_pause_then_resume(self, client, fake_qb):
        _setup_admin(client)
        token = _csrf(client)
        tid = _seed_task()

        resp = client.post(f"/seeding/{tid}/pause?status=seeding",
                           data={"csrf_token": token}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/seeding?ok=paused&status=seeding"
        assert fake_qb.paused == [HASH_A]
        assert _record(tid).status == "paused"

        resp = client.post(f"/seeding/{tid}/resume",
                           data={"csrf_token": token}, follow_redirects=False)
        assert resp.headers["location"] == "/seeding?ok=resumed"
        assert fake_qb.resumed == [HASH_A]
        assert _record(tid).status == "seeding"

    def test_pause_unknown_task(self, client):
        _setup_admin(client)
        token = _csrf(client)
        resp = client.post("/seeding/nope/pause", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/seeding?err=notfound"

    def test_pause_when_qb_down_flashes_error_not_500(self, client, fake_qb):
        _setup_admin(client)
        token = _csrf(client)
        tid = _seed_task()
        fake_qb.fail = True
        resp = client.post(f"/seeding/{tid}/pause", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/seeding?err=qb_unreachable"
        assert _record(tid).status == "seeding"
        # 跟随重定向后页面正常渲染并显示错误文案
        assert "不可达" in client.get(resp.headers["location"]).text

    def test_mark_satisfied_emits_event(self, client, fake_qb):
        _setup_admin(client)
        token = _csrf(client)
        tid = _seed_task()
        resp = client.post(f"/seeding/{tid}/mark-satisfied", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/seeding?ok=marked"
        rec = _record(tid)
        assert rec.status == "satisfied" and rec.hr_satisfied_at is not None
        assert fake_qb.paused == []

        events = asyncio.run(repo.list_events(app.state.db, kind="hr_satisfied"))
        assert len(events) == 1
        assert events[0].site == "hhanclub" and "Jay Chou" in events[0].title
        assert "hr_satisfied" in client.get("/events").text

    def test_check_now_polls_qb_and_creates_record(self, client, fake_qb):
        _setup_admin(client)
        token = _csrf(client)
        db = app.state.db
        tid = asyncio.run(repo.create_task(db, Task(
            info_hash=HASH_A, site="hhanclub", title="Fresh", state=TaskState.COMPLETED,
        )))
        fake_qb.torrents.append(SimpleNamespace(
            hash=HASH_A, seeding_time=3 * 3600, ratio=0.3, state="uploading", name="Fresh",
        ))

        resp = client.post("/seeding/check-now", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.headers["location"] == "/seeding?ok=checked"
        rec = _record(tid)
        assert rec is not None and rec.seed_seconds == 3 * 3600
        html = client.get("/seeding").text
        assert "Fresh" in html and "3.0 h" in html

    def test_check_now_when_qb_down(self, client, fake_qb):
        _setup_admin(client)
        token = _csrf(client)
        fake_qb.fail = True
        resp = client.post("/seeding/check-now", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/seeding?err=check_skipped"
        assert "未能连接" in client.get(resp.headers["location"]).text


# ── 调度器注册 ───────────────────────────────────────────────

class TestSchedulerJob:
    def test_seeding_check_job_registered(self, client):
        sched = app.state.scheduler
        job = sched.get_job("seeding_check")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 15 * 60
        assert job.max_instances == 1 and job.coalesce is True
        assert sched.get_job("pipeline_round") is not None

    def test_run_seeding_once_exposed_on_runner(self, client, fake_qb):
        runner = app.state.runner
        summary = asyncio.run(runner.run_seeding_once())
        assert summary["checked"] == 0 and summary["skipped"] is None
