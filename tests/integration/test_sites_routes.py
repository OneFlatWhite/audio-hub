"""站点中心（v0.6 波次 2A）集成测试：六态渲染、凭据不回显、三步向导端点、启用门禁。

走真实 ASGI 路由，站点外网请求全部用 respx 模拟。重点锁住：
1. 门禁是服务端的：auth_status != valid 的站点 build_adapters 不构建、
   POST /sites/{id}/toggle 启用被拒并回显提示，页面灰按钮只是辅助。
2. 凭据只写不读：列表页 / 编辑页都不出现 passkey / Cookie 明文。
3. 测试端点（认证 / 分类 / RSS / 搜索）的结果如实写回或回显，包括 degraded 标记。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from audio_hub import registry_store, repo
from audio_hub import settings_store as store
from audio_hub.adapters.registry import build_adapters
from audio_hub.config import SiteConfig
from audio_hub.main import app
from audio_hub.models import AuthStatus
from audio_hub.sites.auth_check import EVENT_SITE_AUTH_EXPIRED, run_auth_check_once
from audio_hub.web import auth

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PASSKEY = "SECRETPASSKEY123"
COOKIE = "uid=12345; pass=deadbeefcookie"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def web_env(tmp_path: Path, monkeypatch):
    """两站：hhanclub 有 RSS（未认证）、mteam 无任何凭据（未配置）；全部禁用不触网。"""
    from audio_hub.crypto import generate_key
    key_file = tmp_path / "secrets.key"
    key_file.write_bytes(generate_key())

    config_text = f"""
dry_run: true
db_path: "{(tmp_path / 'hub.db').as_posix()}"
log_level: "WARNING"
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
  auth_request_limit: 100
  auth_window_minutes: 15

sites:
  - name: "hhanclub"
    enabled: false
    domain: "https://hhanclub.net"
    rss_url: "https://hhanclub.net/torrentrss.php?passkey={PASSKEY}"
    music_cat_id: "408"
  - name: "mteam"
    enabled: false
    api_key: ""
    search_url: "https://api.m-team.cc/api/torrent/search"
"""
    (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("AUDIO_HUB_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(key_file))
    (tmp_path / "seeds").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def client(web_env):
    with TestClient(app) as c:
        _setup_admin(c)
        yield c


def _setup_admin(client: TestClient, username="ops", password="strongpass123"):
    resp = client.post("/setup", data={
        "username": username, "password": password, "password2": password,
    }, follow_redirects=False)
    assert resp.status_code in (303, 200), resp.text


def _csrf(client: TestClient) -> str:
    page = client.get("/sites")
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m, f"页面未找到 CSRF meta: {page.status_code}"
    return m.group(1)


def _login_as_viewer(client: TestClient, username="viewer", password="strongpass123") -> None:
    """插库造一个非管理员，在同一 client 上登出管理员再以它登入。"""
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


def _post(client: TestClient, path: str, data: dict | None = None, **kw):
    data = dict(data or {})
    data.setdefault("csrf_token", _csrf(client))
    return client.post(path, data=data, follow_redirects=False, **kw)


def _run(coro):
    return asyncio.run(coro)


def _record(site_id: str):
    return _run(repo.get_site(app.state.db, site_id))


def _set_auth(site_id: str, status: AuthStatus, **kw):
    _run(repo.set_site_auth(app.state.db, site_id, status, **kw))


def _set_enabled(site_id: str, enabled: bool):
    _run(repo.set_site_enabled(app.state.db, site_id, enabled))


def _rebuild():
    _run(store.rebuild_adapters(app))


def _save_cookie(site_id: str, cookie: str = COOKIE):
    """直接经 repo 写 Cookie（等价于编辑页第 ① 步保存），不改 auth_status。"""
    async def _go():
        db = app.state.db
        cur = {s.name: s for s in await registry_store.load_site_configs(db)}[site_id]
        await repo.upsert_site(db, cur.model_copy(update={"cookie": cookie}), site_id=site_id)
        await store.rebuild_adapters(app)
    _run(_go())


def _mock_nexus_auth_ok():
    respx.get("https://hhanclub.net/index.php").mock(
        return_value=httpx.Response(200, text=load("nexusphp_index.html")))
    respx.get("https://hhanclub.net/userdetails.php?id=12345").mock(
        return_value=httpx.Response(200, text=load("nexusphp_userdetails.html")))


def _mock_nexus_auth_expired():
    respx.get("https://hhanclub.net/index.php").mock(
        return_value=httpx.Response(302, headers={"location": "/login.php?returnto=index.php"}))


# ── 列表页：六态与凭据脱敏 ──────────────────────────────────

def _row(html: str, site_id: str) -> str:
    """取列表页中某站点那一 <tr>；页脚图例含全部六态文案，断言必须限定在行内。"""
    m = re.search(rf'<tr>\s*<td><a class="mono" href="/sites/{site_id}/edit">.*?</tr>', html, re.DOTALL)
    assert m, f"页面没有 {site_id} 这一行"
    return m.group(0)


def _state(html: str, site_id: str) -> str:
    """站点行内第一个 pill 就是主状态标签。"""
    m = re.search(r'<span class="pill [^"]*">([^<]+)</span>', _row(html, site_id))
    assert m, "行内没有状态 pill"
    return m.group(1).strip()


class TestSitesPage:
    def test_six_states_rendered(self, client):
        page = client.get("/sites")
        assert page.status_code == 200
        # hhanclub 有 RSS 但未认证；mteam 什么凭据都没有
        assert _state(page.text, "hhanclub") == "未认证"
        assert _state(page.text, "mteam") == "未配置"

        _set_auth("hhanclub", AuthStatus.EXPIRED, error="Cookie 已失效")
        text = client.get("/sites").text
        assert _state(text, "hhanclub") == "认证失效"
        assert "Cookie 已失效" in _row(text, "hhanclub")

        _set_auth("hhanclub", AuthStatus.VALID)
        assert _state(client.get("/sites").text, "hhanclub") == "已禁用"

        _set_enabled("hhanclub", True)
        assert _state(client.get("/sites").text, "hhanclub") == "正常"

        # 连续失败触发熔断 → 熔断中
        async def trip():
            for i in range(10):
                await repo.record_site_failure(app.state.db, "hhanclub", f"HTTP 503 #{i}")
        _run(trip())
        text = client.get("/sites").text
        assert _state(text, "hhanclub") == "熔断中"
        assert "熔断至" in _row(text, "hhanclub")

    def test_circuit_shown_as_secondary_hint_when_unauthenticated(self, client):
        """主状态「未认证」时熔断仍作次级提示渲染，最后错误照旧可见。"""
        async def trip():
            for i in range(10):
                await repo.record_site_failure(app.state.db, "hhanclub", "HTTP 503")
        _run(trip())
        text = client.get("/sites").text
        row = _row(text, "hhanclub")
        assert _state(text, "hhanclub") == "未认证"
        assert "熔断至" in row
        assert "HTTP 503" in row

    def test_credentials_not_leaked(self, client):
        _save_cookie("hhanclub")
        for path in ("/sites", "/sites/hhanclub/edit"):
            text = client.get(path).text
            assert PASSKEY not in text, path
            assert "deadbeefcookie" not in text, path
        edit = client.get("/sites/hhanclub/edit").text
        assert "已配置" in edit
        assert "留空 = 不修改" in edit
        assert "使用常见 Chrome UA" in edit

    def test_user_info_rendered_after_auth(self, client):
        from audio_hub.models import SiteUserInfo
        _set_auth("hhanclub", AuthStatus.VALID,
                  user_info=SiteUserInfo(username="audiofan", level="Power User"))
        text = client.get("/sites").text
        assert "audiofan" in text and "Power User" in text

    def test_dashboard_uses_same_states(self, client):
        _set_auth("hhanclub", AuthStatus.EXPIRED)
        text = client.get("/dashboard").text
        m = re.search(r'href="/sites/hhanclub/edit">hhanclub</a></td>\s*<td>\s*<span class="pill [^"]*">([^<]+)<', text)
        assert m and m.group(1) == "认证失效"

    def test_new_page_requires_admin(self, client):
        assert client.get("/sites/new").status_code == 200
        _login_as_viewer(client)
        assert client.get("/sites").status_code == 200
        resp = client.get("/sites/new", follow_redirects=False)
        assert resp.status_code == 303 and "err=need_admin" in resp.headers["location"]

    def test_write_endpoints_require_admin_and_csrf(self, client):
        # 管理员但没 CSRF
        resp = client.post("/sites/hhanclub/toggle", data={"enabled": "0"}, follow_redirects=False)
        assert resp.status_code == 403
        # 非管理员带 CSRF
        _login_as_viewer(client)
        for path in ("/sites/hhanclub/toggle", "/sites/hhanclub/test-auth",
                     "/sites/hhanclub/delete", "/sites"):
            resp = _post(client, path, {"enabled": "0", "name": "hdsky"})
            assert resp.status_code == 403, path


# ── 门禁：auth 非 valid 不得构建 / 启用 ─────────────────────

class TestAuthGate:
    @pytest.mark.parametrize("status", [AuthStatus.UNCONFIGURED, AuthStatus.EXPIRED])
    def test_build_adapters_skips_non_valid(self, client, status):
        """反向断言：启用但认证未通过的站点，build_adapters 默认不构建。"""
        _set_auth("hhanclub", status)
        _set_enabled("hhanclub", True)
        _rebuild()
        assert "hhanclub" not in app.state.runner.adapters

        cfg = next(s for s in app.state.config.sites if s.name == "hhanclub")
        assert cfg.enabled is True and cfg.auth_status == status.value
        assert build_adapters([cfg]) == {}
        # 显式关掉门禁才构建（测试 / 诊断用途）
        assert "hhanclub" in build_adapters([cfg], require_valid_auth=False)

    def test_build_adapters_direct_config(self):
        """不依赖 app：纯 SiteConfig 列表也受门禁约束。"""
        cfgs = [
            SiteConfig(name="hhanclub", enabled=True, auth_status="expired",
                       rss_url="https://hhanclub.net/rss?passkey=K"),
            SiteConfig(name="hdsky", enabled=True, auth_status="unconfigured",
                       rss_url="https://hdsky.me/rss?passkey=K"),
            SiteConfig(name="audiences", enabled=True, auth_status="valid",
                       rss_url="https://audiences.me/rss?passkey=K"),
        ]
        assert set(build_adapters(cfgs)) == {"audiences"}

    @pytest.mark.parametrize("status", [AuthStatus.UNCONFIGURED, AuthStatus.EXPIRED])
    def test_toggle_enable_rejected_server_side(self, client, status):
        """反向断言：POST toggle 启用被服务端拒绝、回显提示、DB 与适配器都不变。"""
        _set_auth("hhanclub", status)
        resp = _post(client, "/sites/hhanclub/toggle", {"enabled": "1"},
                     headers={"referer": "http://testserver/sites/hhanclub/edit"})
        assert resp.status_code == 303
        loc = unquote(resp.headers["location"])
        assert loc.startswith("/sites/hhanclub/edit?")
        assert "err=auth_required" in loc
        assert f"当前状态：{status.value}" in loc

        assert _record("hhanclub").enabled is False
        assert "hhanclub" not in app.state.runner.adapters

        page = client.get(resp.headers["location"])
        assert page.status_code == 200
        assert "启用前必须先通过" in page.text
        assert f"当前状态：{status.value}" in page.text

    def test_toggle_enable_rejected_from_list_page(self, client):
        resp = _post(client, "/sites/hhanclub/toggle", {"enabled": "1"})
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/sites?err=auth_required")
        assert "启用前必须先通过" in client.get(resp.headers["location"]).text

    def test_toggle_enable_allowed_when_valid(self, client):
        _set_auth("hhanclub", AuthStatus.VALID)
        resp = _post(client, "/sites/hhanclub/toggle", {"enabled": "1"})
        assert resp.status_code == 303 and "ok=enabled" in resp.headers["location"]
        assert _record("hhanclub").enabled is True
        assert "hhanclub" in app.state.runner.adapters

        resp = _post(client, "/sites/hhanclub/toggle", {"enabled": "0"})
        assert "ok=disabled" in resp.headers["location"]
        assert "hhanclub" not in app.state.runner.adapters

    def test_edit_page_enable_button_disabled_until_valid(self, client):
        text = client.get("/sites/hhanclub/edit").text
        assert "服务端强制" in text
        _set_auth("hhanclub", AuthStatus.VALID)
        text = client.get("/sites/hhanclub/edit").text
        assert "认证已通过，可以启用" in text

    def test_engine_skips_auth_invalid_site(self, client):
        """第二道闸：站点在 DB 里已失效、适配器尚未重建时，RSS 捕获整轮跳过它。

        v0.6：门禁由订阅引擎的 _usable_adapters 承担（以 sites 表的 auth_status 为准），
        不再经过旧 search_round。
        """
        from audio_hub.models import WatchEntry, WatchKind
        from audio_hub.subscription import engine as sub_engine

        _set_auth("hhanclub", AuthStatus.VALID)
        _set_enabled("hhanclub", True)
        _rebuild()
        adapters = dict(app.state.runner.adapters)
        assert "hhanclub" in adapters

        # 巡检把状态写回 expired，但适配器还是按旧状态构建的（未重建）
        _set_auth("hhanclub", AuthStatus.EXPIRED)
        _run(repo.add_watch_entry(app.state.db, WatchEntry(
            id=str(uuid.uuid4()), query="周杰伦", kind=WatchKind.MUSIC,
        )))

        summary = _run(sub_engine.run_rss_capture_once(
            app.state.db, adapters, config=app.state.config, dry_run=True,
        ))
        assert summary.sites_polled == []
        assert summary.skipped_sites == {"hhanclub": "auth_expired"}


# ── 向导：新建 / 保存 / 校验 ────────────────────────────────

class TestEditFlow:
    def test_create_site(self, client):
        resp = _post(client, "/sites", {
            "name": "hdsky", "domain": "https://hdsky.me/",
            "rss_url": "https://hdsky.me/torrentrss.php?passkey=NEW", "max_rows": "50",
        })
        assert resp.status_code == 303, resp.text
        assert resp.headers["location"] == "/sites/hdsky/edit?ok=created"
        rec = _record("hdsky")
        assert rec is not None and rec.has_rss and rec.enabled is False
        assert rec.auth_status is AuthStatus.UNCONFIGURED
        cfg = next(s for s in app.state.config.sites if s.name == "hdsky")
        assert cfg.domain == "https://hdsky.me"      # 归一：去尾斜杠
        assert "hdsky" not in app.state.runner.adapters

    def test_create_rejects_duplicate_and_unknown(self, client):
        resp = _post(client, "/sites", {"name": "hhanclub", "max_rows": "50"})
        assert "err=exists" in resp.headers["location"]
        resp = _post(client, "/sites", {"name": "fakesite", "max_rows": "50"})
        assert "err=bad_name" in resp.headers["location"]

    @pytest.mark.parametrize("field,value,err", [
        ("rss_url", "hhanclub.net/rss?passkey=X", "bad_rss_url"),
        ("rss_url", "ftp://hhanclub.net/rss", "bad_rss_url"),
        ("domain", "hhanclub.net", "bad_domain"),
        ("proxy", "127.0.0.1:7890", "bad_proxy"),
        ("search_url", "javascript:alert(1)", "bad_search_url"),
        ("max_rows", "500", "bad_rows"),
        ("max_rows", "abc", "bad_rows"),
        ("priority", "high", "bad_priority"),
        ("hr_min_ratio", "x", "bad_hr"),
    ])
    def test_save_validation(self, client, field, value, err):
        data = {"max_rows": "50", field: value}
        resp = _post(client, "/sites/hhanclub", data)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/sites/hhanclub/edit?err={err}"
        # 校验失败不落库
        assert _run(repo.get_site_secret(app.state.db, "hhanclub", "rss_url")).endswith(PASSKEY)

    def test_save_keeps_secrets_when_blank(self, client):
        _set_auth("hhanclub", AuthStatus.VALID)
        resp = _post(client, "/sites/hhanclub", {
            "max_rows": "30", "priority": "5", "rss_url": "", "cookie": "",
            "user_agent": "UA-TEST",
        })
        assert resp.headers["location"] == "/sites/hhanclub/edit?ok=saved"
        assert _run(repo.get_site_secret(app.state.db, "hhanclub", "rss_url")).endswith(PASSKEY)
        rec = _record("hhanclub")
        assert rec.max_rows == 30 and rec.priority == 5 and rec.user_agent == "UA-TEST"
        # 没碰凭据 → 认证状态不动
        assert rec.auth_status is AuthStatus.VALID

    def test_save_new_cookie_resets_auth(self, client):
        _set_auth("hhanclub", AuthStatus.VALID)
        resp = _post(client, "/sites/hhanclub", {"max_rows": "50", "cookie": COOKIE})
        assert resp.headers["location"] == "/sites/hhanclub/edit?ok=saved_recheck"
        rec = _record("hhanclub")
        assert rec.has_cookie and rec.auth_status is AuthStatus.UNCONFIGURED
        assert _run(repo.get_site_secret(app.state.db, "hhanclub", "cookie")) == COOKIE
        # 门禁随之生效
        assert "hhanclub" not in app.state.runner.adapters

    def test_delete_site(self, client):
        resp = _post(client, "/sites/hhanclub/delete")
        assert resp.headers["location"] == "/sites?ok=deleted"
        assert _record("hhanclub") is None
        assert "hhanclub" not in app.state.runner.adapters
        assert client.get("/sites/hhanclub/edit", follow_redirects=False).headers["location"] \
            == "/sites?err=notfound"


# ── 向导：测试认证 / 分类 / RSS / 搜索 ──────────────────────

class TestSiteActions:
    def test_test_auth_requires_cookie(self, client):
        resp = _post(client, "/sites/hhanclub/test-auth")
        assert resp.headers["location"] == "/sites/hhanclub/edit?err=need_cookie"
        resp = _post(client, "/sites/mteam/test-auth")
        assert resp.headers["location"] == "/sites/mteam/edit?err=need_api_key"

    @respx.mock
    def test_test_auth_success_marks_valid(self, client):
        _save_cookie("hhanclub")
        _mock_nexus_auth_ok()
        resp = _post(client, "/sites/hhanclub/test-auth")
        assert resp.headers["location"] == "/sites/hhanclub/edit?ok=auth_ok"
        rec = _record("hhanclub")
        assert rec.auth_status is AuthStatus.VALID
        assert rec.auth_checked_at is not None
        assert rec.user_info.username == "audiofan" and rec.user_info.level == "Power User"
        page = client.get("/sites/hhanclub/edit").text
        assert "认证通过，站点已标记为有效" not in page      # flash 只在带 ?ok 时出现
        assert "audiofan" in page and "认证已通过，可以启用" in page

    @respx.mock
    def test_test_auth_failure_marks_expired_and_removes_adapter(self, client):
        _save_cookie("hhanclub")
        _set_auth("hhanclub", AuthStatus.VALID)
        _set_enabled("hhanclub", True)
        _rebuild()
        assert "hhanclub" in app.state.runner.adapters

        _mock_nexus_auth_expired()
        resp = _post(client, "/sites/hhanclub/test-auth")
        loc = unquote(resp.headers["location"])
        assert "err=auth_failed" in loc and "detail=" in loc
        rec = _record("hhanclub")
        assert rec.auth_status is AuthStatus.EXPIRED
        assert "登录页" in (rec.auth_error or "")
        assert "hhanclub" not in app.state.runner.adapters
        assert "认证失败" in client.get(resp.headers["location"]).text

    @respx.mock
    def test_test_auth_network_error_keeps_status(self, client):
        _save_cookie("hhanclub")
        _set_auth("hhanclub", AuthStatus.VALID)
        respx.get("https://hhanclub.net/index.php").mock(side_effect=httpx.ConnectTimeout("t/o"))
        resp = _post(client, "/sites/hhanclub/test-auth")
        assert "err=auth_error" in resp.headers["location"]
        rec = _record("hhanclub")
        assert rec.auth_status is AuthStatus.VALID
        assert "超时" in (rec.auth_error or "")

    @respx.mock
    def test_discover_and_save_categories(self, client):
        _save_cookie("hhanclub")
        respx.get("https://hhanclub.net/torrents.php").mock(
            return_value=httpx.Response(200, text=load("nexusphp_torrents.html")))
        resp = _post(client, "/sites/hhanclub/discover-categories")
        loc = unquote(resp.headers["location"])
        assert "ok=cats_found" in loc and "5 个分类" in loc
        cats = {c.remote_id: c for c in _run(repo.list_site_categories(app.state.db, "hhanclub"))}
        assert cats["408"].local_kind == "music" and cats["410"].local_kind == "audiobook"

        page = client.get("/sites/hhanclub/edit").text
        assert "音乐 Music" in page and 'name="kind_408"' in page

        # 人工改映射：410 也算音乐，408 不抓
        resp = _post(client, "/sites/hhanclub/categories", {
            "kind_401": "other", "kind_402": "other", "kind_408": "other",
            "kind_410": "music", "kind_411": "audiobook",
        })
        assert resp.headers["location"] == "/sites/hhanclub/edit?ok=cats_saved"
        cats = {c.remote_id: c for c in _run(repo.list_site_categories(app.state.db, "hhanclub"))}
        assert cats["408"].local_kind == "other" and cats["410"].local_kind == "music"
        cfg = next(s for s in app.state.config.sites if s.name == "hhanclub")
        assert cfg.music_cat_id == "410" and cfg.audiobook_cat_id == "411"
        # 同步分类不丢凭据
        assert _run(repo.get_site_secret(app.state.db, "hhanclub", "cookie")) == COOKIE

    def test_save_categories_without_discovery(self, client):
        resp = _post(client, "/sites/hhanclub/categories", {"kind_999": "music"})
        assert resp.headers["location"] == "/sites/hhanclub/edit?err=cats_empty"

    @respx.mock
    def test_test_rss_writes_result(self, client):
        respx.get(url__startswith="https://hhanclub.net/torrentrss.php").mock(
            return_value=httpx.Response(200, text=load("hhanclub_rss.xml")))
        resp = _post(client, "/sites/hhanclub/test-rss")
        loc = unquote(resp.headers["location"])
        assert "ok=rss_ok" in loc and "5 条" in loc
        rec = _record("hhanclub")
        assert rec.last_rss_test is not None and rec.last_rss_test.ok
        assert rec.last_rss_test.count == 5
        page = client.get("/sites/hhanclub/edit").text
        assert "上次 RSS 测试" in page and "通过" in page

    @respx.mock
    def test_test_rss_failure(self, client):
        respx.get(url__startswith="https://hhanclub.net/torrentrss.php").mock(
            return_value=httpx.Response(403))
        resp = _post(client, "/sites/hhanclub/test-rss")
        assert "err=rss_failed" in resp.headers["location"]
        rec = _record("hhanclub")
        assert rec.last_rss_test is not None and rec.last_rss_test.ok is False
        assert "403" in (rec.last_rss_test.error or "")

    def test_test_rss_mteam_without_key(self, client):
        resp = _post(client, "/sites/mteam/test-rss")
        assert "err=rss_failed" in resp.headers["location"]

    @respx.mock
    def test_test_search_html(self, client):
        _save_cookie("hhanclub")
        respx.get(url__startswith="https://hhanclub.net/torrents.php").mock(
            return_value=httpx.Response(
                200, text=load("nexusphp_search_result.html").replace("page=1", "page=9")))
        resp = _post(client, "/sites/hhanclub/test-search", {"keyword": "周杰伦", "kind": "music"})
        assert resp.status_code == 200
        assert "成功" in resp.text and "叶惠美" in resp.text
        assert "降级（RSS）" not in resp.text
        assert ">html<" in resp.text

    @respx.mock
    def test_test_search_degraded_without_cookie(self, client):
        respx.get(url__startswith="https://hhanclub.net/torrentrss.php").mock(
            return_value=httpx.Response(200, text=load("hhanclub_rss.xml")))
        resp = _post(client, "/sites/hhanclub/test-search", {"keyword": "周杰伦"})
        assert resp.status_code == 200
        assert "降级（RSS）" in resp.text
        assert PASSKEY not in resp.text

    @respx.mock
    def test_test_search_error_rendered(self, client):
        _save_cookie("hhanclub")
        respx.get(url__startswith="https://hhanclub.net/torrents.php").mock(
            return_value=httpx.Response(302, headers={"location": "/login.php"}))
        resp = _post(client, "/sites/hhanclub/test-search", {"keyword": "x"})
        assert resp.status_code == 200
        assert "失败" in resp.text and "Cookie 已失效" in resp.text

    def test_test_search_empty_keyword(self, client):
        resp = _post(client, "/sites/hhanclub/test-search", {"keyword": "  "})
        assert resp.headers["location"] == "/sites/hhanclub/edit?err=empty_keyword"


# ── 认证巡检 run_auth_check_once ─────────────────────────────

class _Notifier:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class TestAuthCheckOnce:
    @respx.mock
    def test_valid_then_expired_with_event_and_rebuild(self, client):
        _save_cookie("hhanclub")
        _mock_nexus_auth_ok()
        notifier = _Notifier()

        summary = _run(run_auth_check_once(app.state.db, app.state.config, notifier, app=app))
        assert summary["checked"] == 1
        assert summary["skipped"] == ["mteam"]          # 无 API Key → 跳过不写
        assert summary["results"] == {"hhanclub": "valid"}
        assert summary["changed"] == ["hhanclub"]
        assert notifier.events == []                     # 变好不吵
        assert _record("hhanclub").auth_status is AuthStatus.VALID

        _set_enabled("hhanclub", True)
        _rebuild()
        assert "hhanclub" in app.state.runner.adapters

        respx.reset()
        _mock_nexus_auth_expired()
        summary = _run(run_auth_check_once(app.state.db, app.state.config, notifier, app=app))
        assert summary["results"] == {"hhanclub": "expired"}
        assert summary["changed"] == ["hhanclub"]
        assert [e.kind for e in notifier.events] == [EVENT_SITE_AUTH_EXPIRED]
        assert notifier.events[0].site == "hhanclub"
        # app 传入 → 门禁即时生效
        assert "hhanclub" not in app.state.runner.adapters

        # 已经 expired 再失败：不重复发事件
        summary = _run(run_auth_check_once(app.state.db, app.state.config, notifier, app=app))
        assert summary["changed"] == []
        assert len(notifier.events) == 1

    @respx.mock
    def test_network_error_keeps_status(self, client):
        _save_cookie("hhanclub")
        _set_auth("hhanclub", AuthStatus.VALID)
        _rebuild()
        respx.get("https://hhanclub.net/index.php").mock(side_effect=httpx.ConnectError("boom"))
        summary = _run(run_auth_check_once(app.state.db, app.state.config))
        assert summary["results"] == {"hhanclub": "valid"}
        assert summary["changed"] == []
        rec = _record("hhanclub")
        assert rec.auth_status is AuthStatus.VALID and rec.auth_error

    def test_no_capable_sites(self, client):
        summary = _run(run_auth_check_once(app.state.db, app.state.config))
        assert summary["checked"] == 0
        assert sorted(summary["skipped"]) == ["hhanclub", "mteam"]
