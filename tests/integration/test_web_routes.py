"""Web 层集成测试：登录守卫、CSRF、页面渲染、待审采纳/拒绝。

用 TestClient 走真实 ASGI 路由（含认证依赖），不发任何外部网络请求。
站点全禁用，适配器为空，故流水线轮次不会触网。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from audio_hub import repo
from audio_hub.main import app
from audio_hub.models import (
    EventRecord,
    LibraryItem,
    PartEntry,
    QuarantineItem,
    SeedRecord,
    SeriesEntry,
    Task,
    TaskState,
)


@pytest.fixture
def web_env(tmp_path: Path, monkeypatch):
    """配置 + 密钥文件；站点全禁用（不触网）；cookie_secure=False（HTTP 测试）。"""
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
    """通过 /setup 创建管理员（返回后已登录，cookie 已设置）。"""
    resp = client.post("/setup", data={
        "username": username, "password": password, "password2": password,
    }, follow_redirects=False)
    assert resp.status_code in (303, 200), resp.text
    return resp


def _csrf(client: TestClient) -> str:
    """从任意已登录页面抓取 CSRF token。"""
    page = client.get("/dashboard")
    # 从 meta 标签提取
    import re
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m, f"页面未找到 CSRF meta: {page.status_code}"
    return m.group(1)


# ── 首次初始化 ──────────────────────────────────────────────

class TestSetup:
    def test_setup_page_shown_when_no_user(self, client):
        resp = client.get("/setup")
        assert resp.status_code == 200
        assert "创建管理员" in resp.text

    def test_setup_creates_admin_and_logs_in(self, client):
        _setup_admin(client)
        # 已登录：能访问 dashboard
        assert client.get("/dashboard").status_code == 200

    def test_setup_blocked_after_user_exists(self, client):
        _setup_admin(client)
        resp = client.get("/setup", follow_redirects=False)
        assert resp.status_code == 303     # 已有用户，重定向走

    def test_setup_password_mismatch(self, client):
        resp = client.post("/setup", data={
            "username": "ops", "password": "strongpass123", "password2": "differentpass",
        })
        assert resp.status_code == 400
        assert "不一致" in resp.text


# ── 登录守卫 ────────────────────────────────────────────────

class TestLoginGuard:
    def test_dashboard_requires_login(self, client):
        _setup_admin(client)
        # 新客户端（无 cookie）访问受保护页 → 重定向到登录
        with TestClient(app) as anon:
            resp = anon.get("/dashboard", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers["location"]

    def test_login_success(self, client):
        _setup_admin(client)
        with TestClient(app) as c:
            resp = c.post("/login", data={"username": "ops", "password": "strongpass123"},
                          follow_redirects=False)
            assert resp.status_code == 303
            assert c.get("/dashboard").status_code == 200

    def test_login_wrong_password(self, client):
        _setup_admin(client)
        with TestClient(app) as c:
            resp = c.post("/login", data={"username": "ops", "password": "wrong"})
            assert resp.status_code == 401
            assert "用户名或密码错误" in resp.text

    def test_login_error_does_not_leak_user_existence(self, client):
        """用户不存在与密码错误返回相同文案（防枚举）。"""
        _setup_admin(client)
        with TestClient(app) as c:
            r1 = c.post("/login", data={"username": "ghost", "password": "whateverpass"})
            r2 = c.post("/login", data={"username": "ops", "password": "wrongpass"})
            assert r1.status_code == r2.status_code == 401
            assert "用户名或密码错误" in r1.text
            assert "用户名或密码错误" in r2.text

    def test_login_rate_limited(self, client):
        _setup_admin(client)
        with TestClient(app) as c:
            last = None
            for _ in range(6):
                last = c.post("/login", data={"username": "ops", "password": "wrong"})
            assert last.status_code == 429

    def test_open_redirect_blocked(self, client):
        _setup_admin(client)
        with TestClient(app) as c:
            resp = c.post("/login",
                          data={"username": "ops", "password": "strongpass123",
                                "next": "//evil.com"},
                          follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"] == "/"   # 恶意 next 被中和

    def test_health_no_auth(self, client):
        """健康检查无需登录（容器探针）。"""
        assert client.get("/health").status_code == 200


# ── 登出 ────────────────────────────────────────────────────

class TestLogout:
    def test_logout_requires_csrf(self, client):
        _setup_admin(client)
        # 缺 CSRF token 的登出应被拒
        resp = client.post("/logout", follow_redirects=False)
        assert resp.status_code == 403

    def test_logout_with_csrf(self, client):
        _setup_admin(client)
        token = _csrf(client)
        resp = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
        assert resp.status_code == 303
        # 登出后访问受保护页 → 重定向登录
        assert client.get("/dashboard", follow_redirects=False).status_code == 303


# ── CSRF 保护 ───────────────────────────────────────────────

class TestCsrfProtection:
    def test_write_without_csrf_rejected(self, client):
        _setup_admin(client)
        resp = client.post("/watchlist", data={"query": "周杰伦"}, follow_redirects=False)
        assert resp.status_code == 403

    def test_write_with_bad_csrf_rejected(self, client):
        _setup_admin(client)
        resp = client.post("/watchlist",
                           data={"query": "周杰伦", "csrf_token": "bogus"},
                           follow_redirects=False)
        assert resp.status_code == 403

    def test_write_with_valid_csrf_ok(self, client):
        _setup_admin(client)
        token = _csrf(client)
        resp = client.post("/watchlist",
                           data={"query": "周杰伦", "csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303


# ── 页面渲染 ────────────────────────────────────────────────

class TestPagesRender:
    def test_all_pages_200_when_logged_in(self, client):
        _setup_admin(client)
        for path in ["/dashboard", "/watchlist", "/records", "/review", "/sites", "/settings"]:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} → {resp.status_code}"

    def test_index_redirects_to_dashboard(self, client):
        _setup_admin(client)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert "/dashboard" in resp.headers["location"]

    def test_sites_page_shows_circuit_open_until(self, client, monkeypatch):
        """站点熔断中：/sites 与总览都渲染「熔断至 HH:MM」文案。

        配置里 hhanclub 是禁用的（禁用优先于熔断显示），这里临时置为启用；
        适配器列表为空，启用不会触网。
        """
        import asyncio

        _setup_admin(client)
        site_cfg = app.state.config.sites[0]
        monkeypatch.setattr(site_cfg, "enabled", True)

        async def trip():
            # threshold=1：一次失败即熔断 30 分钟
            opened = await repo.record_site_failure(
                app.state.db, site_cfg.name, "HTTP 503", threshold=1, open_minutes=30,
            )
            assert opened is True

        asyncio.run(trip())

        html = client.get("/sites").text
        assert "熔断中" in html
        assert "熔断至" in html
        assert "HTTP 503" in html
        assert "熔断中" in client.get("/dashboard").text

        # 成功一次即恢复：文案消失
        asyncio.run(repo.record_site_success(app.state.db, site_cfg.name, rows=3))
        html = client.get("/sites").text
        assert "熔断至" not in html

    def test_nav_badge_shows_review_count(self, client):
        _setup_admin(client)
        # 插入一条待审任务
        db = app.state.db
        import asyncio
        asyncio.run(repo.create_task(db, Task(
            id=str(uuid.uuid4()), info_hash="h1", site="hhanclub",
            title="待审专辑 FLAC", state=TaskState.SCORED,
            confidence="medium", reject_reason="pending_review",
        )))
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        # 导航徽标应出现
        assert "badge" in resp.text


# ── v0.4 五页骨架：资料库 / 隔离区 / 连载 / 做种 / 事件 ───────────

# (路径, 页面标题关键词, 空态文案关键词)
_V4_PAGES = [
    ("/library", "资料库", "资料库为空"),
    ("/quarantine", "隔离区", "没有待处理的隔离内容"),
    ("/series", "连载追踪", "尚无连载"),
    ("/seeding", "做种与 H&amp;R", "暂无做种记录"),
    ("/events", "事件流", "暂无事件"),
]


class TestV4PagesRender:
    def test_pages_200_with_title_and_empty_state(self, client):
        _setup_admin(client)
        for path, title, empty in _V4_PAGES:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} → {resp.status_code}"
            assert title in resp.text, f"{path} 缺少标题 {title!r}"
            assert empty in resp.text, f"{path} 缺少空态文案 {empty!r}"

    def test_pages_redirect_to_login_when_anonymous(self, client):
        _setup_admin(client)
        with TestClient(app) as anon:
            for path, _, _ in _V4_PAGES:
                resp = anon.get(path, follow_redirects=False)
                assert resp.status_code == 303, f"{path} → {resp.status_code}"
                assert "/login" in resp.headers["location"]

    def test_nav_links_present(self, client):
        _setup_admin(client)
        html = client.get("/dashboard").text
        for path, _, _ in _V4_PAGES:
            assert f'href="{path}"' in html

    def test_pages_render_seeded_rows(self, client):
        """每表各塞一行，确认模板字段访问与 repo 模型一致（真渲染，非空态）。"""
        import asyncio

        _setup_admin(client)
        db = app.state.db

        async def seed():
            await repo.add_library_item(db, LibraryItem(
                watch_id="w1", title_norm="jay chou fantasy", quality="FLAC 16/44",
                quality_score=70, path="/musiclab/library/Jay/Fantasy"))
            await repo.add_quarantine_item(db, QuarantineItem(
                task_id="t-q", scratch_path="/scratch/inbox/bad", reasons=["no_audio_files"]))
            sid = await repo.add_series(db, SeriesEntry(watch_id="w2", name="三体", total_parts=3))
            await repo.add_series_part(db, PartEntry(series_id=sid, part_no=1, title="三体 第一部"))
            await repo.upsert_seed_record(db, SeedRecord(
                task_id="t-seed-1234", info_hash="abc", site="hhanclub",
                seed_seconds=7200, ratio=0.5, required_seconds=72 * 3600, required_ratio=1.0))
            await repo.add_event(db, EventRecord(
                kind="adapter_error", level="error", site="mteam", detail='{"reason": "503"}'))

        asyncio.run(seed())

        assert "jay chou fantasy" in client.get("/library").text
        q = client.get("/quarantine").text
        assert "/scratch/inbox/bad" in q and "no_audio_files" in q
        s = client.get("/series").text
        assert "三体" in s and ">1<" in s          # 已登记 1 部
        sd = client.get("/seeding").text
        assert "t-seed-1" in sd and "2.0 h" in sd
        ev = client.get("/events?level=error").text
        assert "adapter_error" in ev and "mteam" in ev

    def test_quarantine_badge_counts_held_only(self, client):
        import asyncio

        _setup_admin(client)
        db = app.state.db

        async def seed():
            await repo.add_quarantine_item(db, QuarantineItem(scratch_path="/a"))
            await repo.add_quarantine_item(db, QuarantineItem(scratch_path="/b"))
            await repo.add_quarantine_item(db, QuarantineItem(scratch_path="/c", status="released"))

        asyncio.run(seed())
        html = client.get("/dashboard").text
        assert '隔离区<span class="badge">2</span>' in html


# ── 总览新增卡片（模块 E）─────────────────────────────────────────

class TestDashboardCards:
    def test_empty_db_renders_new_cards(self, client):
        """空库安全：新卡片全部渲染为 0 / 空态，不报错。"""
        _setup_admin(client)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        html = resp.text
        for kw in ("隔离区待处理", "H&amp;R 未达标", "活跃连载", "最近事件"):
            assert kw in html, f"总览缺少卡片 {kw!r}"
        assert "尚无完结连载" in html
        assert "最近没有事件" in html
        # 卡片链接到对应页面
        for href in ('href="/quarantine"', 'href="/seeding"', 'href="/series"', 'href="/events"'):
            assert href in html

    def test_cards_reflect_seeded_counts(self, client):
        import asyncio

        _setup_admin(client)
        db = app.state.db

        async def seed():
            await repo.add_quarantine_item(db, QuarantineItem(scratch_path="/held"))
            await repo.add_quarantine_item(db, QuarantineItem(scratch_path="/rel", status="released"))
            await repo.upsert_seed_record(db, SeedRecord(
                task_id="t-hr-1", info_hash="h-hr-1", site="hhanclub",
                seed_seconds=10, ratio=0.1, required_seconds=3600, required_ratio=1.0))
            await repo.add_series(db, SeriesEntry(watch_id="w-a", name="连载进行中", total_parts=5))
            await repo.add_series(db, SeriesEntry(
                watch_id="w-b", name="已完结的书", total_parts=2, status="completed"))
            await repo.add_event(db, EventRecord(
                kind="dispatched", level="info", site="hhanclub", title="最近派发的专辑"))

        asyncio.run(seed())
        html = client.get("/dashboard").text
        assert "已完结的书" in html         # 最近完结一条
        assert "最近派发的专辑" in html       # 最近事件列表
        assert "最近没有事件" not in html
        assert "尚无完结连载" not in html


# ── watchlist 增删改 ────────────────────────────────────────

class TestWatchlistCRUD:
    def test_add_and_list(self, client):
        _setup_admin(client)
        token = _csrf(client)
        client.post("/watchlist", data={"query": "周杰伦", "kind": "music",
                                        "aliases": "Jay Chou", "csrf_token": token})
        resp = client.get("/watchlist")
        assert "周杰伦" in resp.text
        assert "Jay Chou" in resp.text

    def test_toggle(self, client):
        _setup_admin(client)
        token = _csrf(client)
        client.post("/watchlist", data={"query": "林俊杰", "csrf_token": token})
        # 取 id
        import asyncio
        entries = asyncio.run(repo.list_watch_entries(app.state.db, enabled_only=False))
        eid = next(e.id for e in entries if e.query == "林俊杰")
        resp = client.post(f"/watchlist/{eid}/toggle", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        entries2 = asyncio.run(repo.list_watch_entries(app.state.db, enabled_only=False))
        e2 = next(e for e in entries2 if e.query == "林俊杰")
        assert e2.enabled is False

    def test_delete(self, client):
        _setup_admin(client)
        token = _csrf(client)
        client.post("/watchlist", data={"query": "临时", "csrf_token": token})
        import asyncio
        entries = asyncio.run(repo.list_watch_entries(app.state.db, enabled_only=False))
        eid = next(e.id for e in entries if e.query == "临时")
        resp = client.post(f"/watchlist/{eid}/delete", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        entries2 = asyncio.run(repo.list_watch_entries(app.state.db, enabled_only=False))
        assert not any(e.query == "临时" for e in entries2)


# ── 待审队列：采纳 / 拒绝 ───────────────────────────────────

def _seed_review_task(title="待审 FLAC 专辑", reason="pending_review") -> str:
    import asyncio
    tid = str(uuid.uuid4())
    asyncio.run(repo.create_task(app.state.db, Task(
        id=tid, info_hash=uuid.uuid4().hex, site="hhanclub", title=title,
        state=TaskState.SCORED, confidence="medium", reject_reason=reason,
        download_url="https://hhanclub.net/download.php?id=1&passkey=X",
        size_bytes=500 * 1024**2, quality="flac_16",
    )))
    return tid


class TestReviewQueue:
    def test_review_shows_pending(self, client):
        _setup_admin(client)
        _seed_review_task()
        resp = client.get("/review")
        assert resp.status_code == 200
        assert "待审 FLAC 专辑" in resp.text

    def test_accept_in_dryrun_marks_accepted(self, client):
        _setup_admin(client)
        token = _csrf(client)
        tid = _seed_review_task()
        resp = client.post(f"/review/{tid}/accept", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        import asyncio
        task = asyncio.run(_get_task(tid))
        assert task["reject_reason"] == "accepted_dry_run"

    def test_reject_requires_valid_reason(self, client):
        _setup_admin(client)
        token = _csrf(client)
        tid = _seed_review_task()
        resp = client.post(f"/review/{tid}/reject",
                           data={"reason": "not_a_real_reason", "csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert "bad_reason" in resp.headers["location"]

    def test_reject_records_decision(self, client):
        _setup_admin(client)
        token = _csrf(client)
        tid = _seed_review_task(title="将被拒绝的")
        resp = client.post(f"/review/{tid}/reject",
                           data={"reason": "wrong_version", "note": "是合辑",
                                 "csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        import asyncio
        task = asyncio.run(_get_task(tid))
        assert task["state"] == "rejected"
        assert "wrong_version" in task["reject_reason"]
        # 决策已记录
        async def count_decisions():
            async with app.state.db.execute(
                "SELECT COUNT(*) AS n FROM review_decisions WHERE decision='reject'"
            ) as cur:
                return (await cur.fetchone())["n"]
        assert asyncio.run(count_decisions()) >= 1

    def test_accept_requires_csrf(self, client):
        _setup_admin(client)
        tid = _seed_review_task()
        resp = client.post(f"/review/{tid}/accept", follow_redirects=False)
        assert resp.status_code == 403


# ── 待审采纳（实跑）：馒头占位 URL 经 genDlToken 换取直链后才交给 qb ───────

_MTEAM_PLACEHOLDER = "https://api.m-team.cc/api/torrent/genDlToken?id=123"
_MTEAM_DIRECT = "https://api.m-team.cc/api/rss/dl?credential=TOKEN&id=123"


class _FakeQb:
    """记录 torrents_add 入参的假 qb 客户端。"""

    def __init__(self):
        self.calls: list[dict] = []

    def torrents_add(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def live_mteam(client):
    """把已启动的 app 临时切到实跑 + 注入馒头适配器 + 假 qb；结束后还原。

    不改 config.yaml：dry_run 在 lifespan 就固化进 runner，此处只需 /review 路由
    走实跑分支，改 app.state.config.dry_run 即可。适配器通过 runner.adapters
    注入 —— dispatcher.adapter_lookup 是回调，每次按站点名从该 dict 取。

    v0.6.0：采纳派发的 qb 客户端改从下载器注册表解析（不再读 app.state.qb_client
    别名），故假客户端直接塞进默认下载器句柄的缓存。注册表是 dry_run 构造的，
    句柄的 _dry_run 一并临时关掉。
    """
    from audio_hub.adapters.mteam import MTeamAdapter
    from audio_hub.config import SiteConfig

    state = app.state
    old_dry = state.config.dry_run
    fake = _FakeQb()
    handle = state.downloaders.default()
    state.config.dry_run = False
    handle._dry_run = False
    handle._client = fake
    state.runner.adapters["mteam"] = MTeamAdapter(SiteConfig(
        name="mteam", api_key="FAKE-KEY",
        search_url="https://api.m-team.cc/api/torrent/search",
    ))
    try:
        yield fake
    finally:
        state.config.dry_run = old_dry
        handle._dry_run = True
        handle.reset_client()
        state.runner.adapters.pop("mteam", None)


def _seed_mteam_task() -> str:
    import asyncio
    tid = str(uuid.uuid4())
    asyncio.run(repo.create_task(app.state.db, Task(
        id=tid, info_hash=uuid.uuid4().hex, site="mteam", title="馒头待审 FLAC",
        state=TaskState.SCORED, confidence="medium", reject_reason="pending_review",
        download_url=_MTEAM_PLACEHOLDER,
        size_bytes=300 * 1024**2, quality="flac_16",
    )))
    return tid


class TestReviewAcceptResolvesMteam:
    @respx.mock
    def test_accept_passes_direct_link_to_qb(self, client, live_mteam):
        route = respx.post("https://api.m-team.cc/api/torrent/genDlToken").mock(
            return_value=httpx.Response(200, json={"code": "0", "data": _MTEAM_DIRECT}),
        )
        _setup_admin(client)
        token = _csrf(client)
        tid = _seed_mteam_task()

        resp = client.post(f"/review/{tid}/accept", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/review?ok=accepted"

        # genDlToken 被调且带了 id；qb 收到的是直链而非占位 URL
        assert route.called
        assert route.calls.last.request.content == b"id=123"
        assert len(live_mteam.calls) == 1
        assert live_mteam.calls[0]["urls"] == _MTEAM_DIRECT
        assert "mteam" in live_mteam.calls[0]["tags"]

        import asyncio
        task = asyncio.run(_get_task(tid))
        assert task["state"] == "dispatched"

    @respx.mock
    def test_accept_resolve_failure_marks_failed(self, client, live_mteam):
        """genDlToken 返回非 0 码 → AdapterError → 任务 failed，qb 未被调，flash 报错。"""
        respx.post("https://api.m-team.cc/api/torrent/genDlToken").mock(
            return_value=httpx.Response(200, json={"code": "1", "message": "token expired"}),
        )
        _setup_admin(client)
        token = _csrf(client)
        tid = _seed_mteam_task()

        resp = client.post(f"/review/{tid}/accept", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/review?err=resolve_failed"
        assert live_mteam.calls == []

        import asyncio
        task = asyncio.run(_get_task(tid))
        assert task["state"] == "failed"
        assert "换取下载直链失败" in task["error_message"]
        # flash 文案已经 _FLASH_ERR 翻译到页面
        assert "换取下载直链失败" in client.get("/review?err=resolve_failed").text

    def test_accept_non_mteam_url_passed_through(self, client, live_mteam):
        """无适配器的站点（hhanclub 已禁用）：download_url 原样交给 qb，不触网。"""
        _setup_admin(client)
        token = _csrf(client)
        tid = _seed_review_task()
        resp = client.post(f"/review/{tid}/accept", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert live_mteam.calls[0]["urls"] == "https://hhanclub.net/download.php?id=1&passkey=X"

    def test_accept_uses_tasks_downloader_settings(self, client, live_mteam):
        """采纳派发按 task.downloader_id 落到对应实例的 save_path/category/tag。

        v0.6.0 陷阱：旧代码回落到 config.qbittorrent（即默认实例），多台
        下载器时「指定实例」形同虚设——种子会进错目录、打错分类。
        """
        import asyncio

        from audio_hub import registry_store
        from audio_hub.config import DownloaderConfig

        _setup_admin(client)
        token = _csrf(client)
        asyncio.run(repo.upsert_downloader(app.state.db, DownloaderConfig(
            id="nas2", name="NAS 二号", host="nas2.local", port=8080,
            save_path="/nas2/seeds", category="music-2", tag="tag2",
        )))
        state = app.state
        state.downloaders.replace(
            asyncio.run(registry_store.load_downloader_configs(state.db)))
        fake2 = _FakeQb()
        h2 = state.downloaders.get("nas2")
        h2._dry_run = False
        h2._client = fake2
        try:
            tid = str(uuid.uuid4())
            asyncio.run(repo.create_task(state.db, Task(
                id=tid, info_hash=uuid.uuid4().hex, site="hhanclub",
                title="指定 nas2 的待审 FLAC", state=TaskState.SCORED,
                confidence="medium", reject_reason="pending_review",
                download_url="https://hhanclub.net/download.php?id=2&passkey=X",
                size_bytes=300 * 1024**2, quality="flac_16",
                downloader_id="nas2",
            )))
            resp = client.post(f"/review/{tid}/accept", data={"csrf_token": token},
                               follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"] == "/review?ok=accepted"

            assert len(fake2.calls) == 1, "任务指定 nas2，却派到了别的实例"
            call = fake2.calls[0]
            assert call["save_path"] == "/nas2/seeds"
            assert call["category"] == "music-2"
            assert call["tags"] == ["tag2", "hhanclub"]
            assert live_mteam.calls == [], "默认实例不应被碰"
        finally:
            h2._dry_run = True
            h2.reset_client()
            state.downloaders.replace(
                asyncio.run(registry_store.load_downloader_configs(state.db)))


async def _get_task(tid):
    async with app.state.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)) as cur:
        return dict(await cur.fetchone())
