"""事件页集成测试：渲染、kind/level 筛选、分页、清理（含 CSRF）。

fixture 与 test_web_routes 同构（独立复制：conftest 不在本模块可改范围）。
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audio_hub import repo
from audio_hub.main import app
from audio_hub.models import EventRecord
from audio_hub.timeutil import utcnow
from audio_hub.web import auth
from audio_hub.web.routes_events import PAGE_SIZE


@pytest.fixture
def web_env(tmp_path: Path, monkeypatch):
    from audio_hub.crypto import generate_key
    key_file = tmp_path / "secrets.key"
    key_file.write_bytes(generate_key())

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


def _seed(events: list[EventRecord]) -> None:
    import asyncio

    async def go():
        for ev in events:
            await repo.add_event(app.state.db, ev)

    asyncio.run(go())


def _login_as_viewer(client: TestClient, username="viewer", password="strongpass123") -> None:
    """直接插库造一个非管理员，在同一 client 上登出管理员再以它登入。"""
    import asyncio

    async def insert():
        await app.state.db.execute(
            """INSERT INTO users (id, username, password_hash, is_admin, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (str(uuid.uuid4()), username, auth.hash_password(password), 0,
             auth.iso_utc(), auth.iso_utc()),
        )
        await app.state.db.commit()

    asyncio.run(insert())
    client.post("/logout", data={"csrf_token": _csrf(client)}, follow_redirects=False)
    resp = client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)
    assert resp.status_code == 303, resp.text[:200]


# ── 渲染 ────────────────────────────────────────────────────

class TestEventsRender:
    def test_empty_state(self, client):
        _setup_admin(client)
        html = client.get("/events").text
        assert "事件流" in html and "暂无事件" in html
        # 筛选下拉含「全部」与基线种类
        assert 'id="flt-kind"' in html and 'id="flt-level"' in html
        assert "<option value=\"\" selected>全部</option>" in html
        assert 'value="dispatched"' in html and 'value="adapter_error"' in html
        # 清理表单带 CSRF
        assert 'action="/events/clear"' in html and 'name="csrf_token"' in html

    def test_rows_render_with_level_and_expandable_detail(self, client):
        _setup_admin(client)
        _seed([
            EventRecord(kind="dispatched", level="info", site="hhanclub",
                        title="周杰伦 - 范特西", detail='{"size": "500 MB", "quality": "flac_16"}'),
            EventRecord(kind="adapter_error", level="error", site="mteam",
                        detail='{"reason": "HTTP 503"}'),
            EventRecord(kind="round_summary", level="warning", detail="not json at all"),
        ])
        html = client.get("/events").text
        assert "周杰伦 - 范特西" in html
        # 级别着色 dot
        assert 'class="dot err"' in html and 'class="dot ok"' in html and 'class="dot off"' in html
        # detail 摘要 + <details> 展开的美化全文
        assert "<details>" in html
        assert "size=500 MB" in html and "quality=flac_16" in html
        assert "&#34;reason&#34;: &#34;HTTP 503&#34;" in html or '"reason": "HTTP 503"' in html
        # 非 JSON 的 detail 原样显示，不 500
        assert "not json at all" in html
        # kind 计数出现在下拉里（展示层走 kind_zh 中文化，value 仍是英文 key）
        assert "已派发 (1)" in html and "适配器错误 (1)" in html

    def test_requires_login(self, client):
        _setup_admin(client)
        with TestClient(app) as anon:
            resp = anon.get("/events", follow_redirects=False)
            assert resp.status_code == 303 and "/login" in resp.headers["location"]


# ── 筛选 ────────────────────────────────────────────────────

class TestEventsFilter:
    def _seed_mixed(self):
        _seed([
            EventRecord(kind="dispatched", level="info", title="A-dispatched"),
            EventRecord(kind="completed", level="info", title="B-completed"),
            EventRecord(kind="failed", level="error", title="C-failed"),
            EventRecord(kind="adapter_error", level="error", title="D-adapter"),
        ])

    def test_filter_by_kind(self, client):
        _setup_admin(client)
        self._seed_mixed()
        html = client.get("/events?kind=completed").text
        assert "B-completed" in html
        for other in ("A-dispatched", "C-failed", "D-adapter"):
            assert other not in html
        assert '<option value="completed" selected>' in html

    def test_filter_by_level(self, client):
        _setup_admin(client)
        self._seed_mixed()
        html = client.get("/events?level=error").text
        assert "C-failed" in html and "D-adapter" in html
        assert "A-dispatched" not in html and "B-completed" not in html

    def test_filter_kind_and_level_combined(self, client):
        _setup_admin(client)
        self._seed_mixed()
        html = client.get("/events?kind=failed&level=error").text
        assert "C-failed" in html and "D-adapter" not in html

    def test_invalid_level_treated_as_all(self, client):
        _setup_admin(client)
        self._seed_mixed()
        html = client.get("/events?level=bogus").text
        assert "A-dispatched" in html and "C-failed" in html

    def test_error_card_counts_all_errors_regardless_of_filter(self, client):
        _setup_admin(client)
        self._seed_mixed()
        html = client.get("/events?kind=dispatched").text
        # 错误卡片始终是全库错误数（2），当前筛选计数是 1
        assert '<div class="n">2</div><div class="l">错误</div>' in html
        assert '<div class="n">1</div><div class="l">当前筛选</div>' in html


# ── 分页 ────────────────────────────────────────────────────

class TestEventsPaging:
    def test_page_size_and_next_prev_links(self, client):
        _setup_admin(client)
        _seed([EventRecord(kind="dispatched", title=f"evt-{i:03d}") for i in range(PAGE_SIZE + 5)])

        p1 = client.get("/events").text
        # 标题在每行出现两次（title 属性 + 文本），按文本节点计数
        assert p1.count(">evt-") == PAGE_SIZE
        assert 'href="/events?page=2"' in p1
        assert "上一页" not in p1

        p2 = client.get("/events?page=2").text
        assert p2.count(">evt-") == 5
        assert 'href="/events?page=1"' in p2
        assert "下一页" not in p2
        assert f"第 2 页，每页 {PAGE_SIZE} 条" in p2

    def test_paging_links_keep_filter(self, client):
        _setup_admin(client)
        _seed([EventRecord(kind="failed", level="error", title=f"f-{i}") for i in range(PAGE_SIZE + 1)])
        html = client.get("/events?kind=failed&level=error").text
        # Jinja 自动转义：属性里的 & 渲染为 &amp;
        assert 'href="/events?page=2&amp;kind=failed&amp;level=error"' in html

    def test_page_zero_rejected(self, client):
        _setup_admin(client)
        assert client.get("/events?page=0").status_code == 422


# ── 清理 ────────────────────────────────────────────────────

class TestEventsClear:
    def test_clear_requires_csrf(self, client):
        _setup_admin(client)
        resp = client.post("/events/clear", follow_redirects=False)
        assert resp.status_code == 403

    def test_clear_purges_only_old_events(self, client):
        _setup_admin(client)
        now = utcnow()
        _seed([
            EventRecord(kind="dispatched", title="old-1", created_at=now - timedelta(days=40)),
            EventRecord(kind="dispatched", title="old-2", created_at=now - timedelta(days=31)),
            EventRecord(kind="dispatched", title="recent", created_at=now - timedelta(days=29)),
            EventRecord(kind="dispatched", title="today"),
        ])
        token = _csrf(client)
        resp = client.post("/events/clear", data={"csrf_token": token}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/events?ok=events_cleared&n=2"

        html = client.get(resp.headers["location"]).text
        assert "已清理 30 天前的事件（2 条）" in html
        assert "recent" in html and "today" in html
        assert "old-1" not in html and "old-2" not in html

    def test_clear_on_empty_db_reports_zero(self, client):
        _setup_admin(client)
        token = _csrf(client)
        resp = client.post("/events/clear", data={"csrf_token": token}, follow_redirects=False)
        assert resp.headers["location"].endswith("&n=0")
        assert "（0 条）" in client.get(resp.headers["location"]).text

    def test_clear_requires_admin(self, client):
        """清理事件不可逆：普通用户带有效 CSRF 也 403，老事件仍在。"""
        _setup_admin(client)
        _seed([EventRecord(kind="dispatched", title="old-1",
                           created_at=utcnow() - timedelta(days=40))])
        _login_as_viewer(client)
        resp = client.post("/events/clear", data={"csrf_token": _csrf(client)},
                           follow_redirects=False)
        assert resp.status_code == 403
        assert "old-1" in client.get("/events").text
