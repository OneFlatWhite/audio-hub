"""下载器中心（v0.6 波次 3）集成测试：列表 / 表单 / 校验 / 测试连接 / 默认切换 / 删除保护。

走真实 ASGI 路由，qb 探测一律 mock（routes_downloaders.probe_qb），不发真实网络。
重点锁住：
1. 凭据只写不读：列表页 / 编辑页不出现密码 / API Key 明文，留空 = 不修改。
2. 连接参数或凭据变更后，上次测试结果作废（last_test_json 置为失败待重测）。
3. 删除三重保护是服务端的：在途任务引用 / 默认下载器 / 最后一个启用实例均被拒。
4. 每次写操作后下载器注册表热重建（app.state.downloaders 立即反映新配置）。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from audio_hub import repo
from audio_hub import settings_store as store
from audio_hub.config import DownloaderConfig
from audio_hub.main import app
from audio_hub.models import Task, TaskState
from audio_hub.web import auth, routes_downloaders


@pytest.fixture
def web_env(tmp_path: Path, monkeypatch):
    """单下载器种子（YAML qbittorrent 段首启导入为 default）；dry_run 不触网。"""
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

sites: []
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
    page = client.get("/downloaders")
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


def _record(downloader_id: str):
    return _run(repo.get_downloader(app.state.db, downloader_id))


def _secret(downloader_id: str, field: str) -> str:
    return _run(repo.get_downloader_secret(app.state.db, downloader_id, field))


def _add_downloader(did="nas", *, password="", api_key="", enabled=True, is_default=False,
                    name=None, host="192.168.1.10", port=9090, save_path="/volume1/music",
                    priority=0) -> None:
    """直接经 repo 写一台下载器（测试前置数据），并热重建注册表保持运行时一致。"""
    cfg = DownloaderConfig(
        id=did, name=name or did, host=host, port=port,
        username="admin" if password else "", password=password, api_key=api_key,
        save_path=save_path, enabled=enabled, is_default=is_default, priority=priority,
    )
    _run(repo.upsert_downloader(app.state.db, cfg))
    _run(store.rebuild_downloaders(app))


def _mock_probe(monkeypatch, result: dict) -> list:
    """把 routes 层引用的 probe_downloader 换成假实现（不触网）；返回收到过的 cfg 列表。"""
    calls: list = []

    async def fake(cfg):
        calls.append(cfg)
        return dict(result)

    monkeypatch.setattr(routes_downloaders, "probe_downloader", fake)
    return calls


#: 「测试连接」通过的假探测结果
_PROBE_OK = {
    "ok": True, "version": "5.2.0", "api_version": "2.11.0",
    "default_save_path": "/volume1/downloads", "free_space_bytes": 1073741824,
    "active_torrents": 3, "error": None,
}
_PROBE_FAIL = {
    "ok": False, "version": None, "api_version": None, "default_save_path": None,
    "free_space_bytes": None, "active_torrents": None, "error": "TCP 不可达: 连接被拒绝",
}

#: 新建表单的一份合法基线，校验用例各改一个字段
_VALID_FORM = {
    "id": "nas", "name": "NAS qB", "host": "192.168.1.10", "port": "9090",
    "save_path": "/volume1/music", "priority": "0",
}


# ── 列表页：空态 / 行字段 / 状态机 / 凭据脱敏 ────────────────

def _row(html: str, did: str) -> str:
    """取列表页中某下载器那一 <tr>；页脚图例含全部五态文案，断言必须限定在行内。"""
    m = re.search(
        rf'<tr>\s*<td><a class="mono" href="/downloaders/{did}/edit">.*?</tr>',
        html, re.DOTALL,
    )
    assert m, f"页面没有 {did} 这一行"
    return m.group(0)


class TestDownloadersPage:
    def test_empty_state(self, client):
        """删掉首启种子的 default 后，空库显示空态而非报错。"""
        _run(repo.delete_downloader(app.state.db, "default"))
        page = client.get("/downloaders")
        assert page.status_code == 200
        assert "尚未配置下载器" in page.text
        assert 'href="/downloaders" class="active"' in page.text

    def test_seeded_default_row(self, client):
        """YAML qbittorrent 段首启种子出 default：默认、启用、未配置凭据。"""
        page = client.get("/downloaders")
        assert page.status_code == 200
        row = _row(page.text, "default")
        assert "默认 qBittorrent" in row
        assert "localhost:8085" in row
        assert "无凭据" in row                       # 凭据类型提示
        assert "未配置凭据" in row                    # 状态 pill
        assert '<span class="pill high">默认</span>' in row
        assert "✓" in row                            # 已启用

    def test_row_state_machine(self, client):
        """未测试 → 连接失败 → 正常 → 已禁用，状态列与最近测试随行渲染。"""
        _add_downloader("nas", password="pw-secret")
        assert "未测试" in _row(client.get("/downloaders").text, "nas")

        _run(repo.set_downloader_test(app.state.db, "nas", _PROBE_FAIL))
        row = _row(client.get("/downloaders").text, "nas")
        assert "连接失败" in row and "TCP 不可达" in row
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", row), "行内应有最近测试时间"

        _run(repo.set_downloader_test(app.state.db, "nas", _PROBE_OK))
        row = _row(client.get("/downloaders").text, "nas")
        assert "正常" in row and "5.2.0" in row and "API 2.11.0" in row
        assert "1.0 GB" in row and "/volume1/downloads" in row

        _post(client, "/downloaders/nas/toggle", {"enabled": "0"})
        assert "已禁用" in _row(client.get("/downloaders").text, "nas")

    def test_active_tasks_counted(self, client):
        _add_downloader("nas", password="pw-secret")
        _run(repo.create_task(app.state.db, Task(
            info_hash="ab" * 20, site="hhanclub", title="T",
            downloader_id="nas", state=TaskState.DOWNLOADING,
        )))
        assert '<td class="num">1</td>' in _row(client.get("/downloaders").text, "nas")

    def test_credentials_never_rendered(self, client):
        _add_downloader("nas", password="pw-secret-123", api_key="key-secret-456")
        for path in ("/downloaders", "/downloaders/nas/edit"):
            text = client.get(path).text
            assert "pw-secret-123" not in text, path
            assert "key-secret-456" not in text, path
        edit = client.get("/downloaders/nas/edit").text
        assert "已配置" in edit and "留空 = 不修改" in edit
        assert "用户名 / 密码" in _row(client.get("/downloaders").text, "nas")


# ── 新建 / 编辑表单页 ───────────────────────────────────────

class TestFormPages:
    def test_new_page(self, client):
        page = client.get("/downloaders/new")
        assert page.status_code == 200
        assert "新建下载器" in page.text and 'name="id"' in page.text

    def test_new_page_requires_admin(self, client):
        _login_as_viewer(client)
        resp = client.get("/downloaders/new", follow_redirects=False)
        assert resp.status_code == 303 and "err=need_admin" in resp.headers["location"]

    def test_edit_page(self, client):
        page = client.get("/downloaders/default/edit")
        assert page.status_code == 200
        assert 'value="default" disabled' in page.text     # id 保存后不可改
        assert 'value="默认 qBittorrent"' in page.text
        assert "未配置" in page.text                       # 密码 / API Key 均未配置
        assert "测试与状态" in page.text                    # 管理员可见测试面板

    def test_edit_notfound(self, client):
        resp = client.get("/downloaders/ghost/edit", follow_redirects=False)
        assert resp.headers["location"] == "/downloaders?err=notfound"
        assert "下载器不存在" in client.get(resp.headers["location"]).text


# ── 新建：校验分支 / 重复 id / 首台自动默认 ──────────────────

class TestCreate:
    def test_create_ok(self, client):
        resp = _post(client, "/downloaders", {
            **_VALID_FORM, "username": "admin", "password": "pw-secret",
            "category": "audio-hub", "tag": "hub", "priority": "5",
        })
        assert resp.status_code == 303, resp.text
        assert resp.headers["location"] == "/downloaders/nas/edit?ok=created"
        rec = _record("nas")
        assert rec.name == "NAS qB" and rec.port == 9090 and rec.priority == 5
        assert rec.has_password is True and rec.has_api_key is False
        assert rec.enabled is True and rec.is_default is False   # default 已存在
        assert _secret("nas", "password") == "pw-secret"
        # 注册表热重建，无需重启
        assert "nas" in app.state.downloaders
        assert app.state.downloaders.get("nas").config.port == 9090

    def test_create_normalizes_id(self, client):
        """id 去空白并转小写后再校验。"""
        resp = _post(client, "/downloaders", {**_VALID_FORM, "id": " NAS-Main "})
        assert resp.headers["location"] == "/downloaders/nas-main/edit?ok=created"
        assert _record("nas-main") is not None

    def test_first_downloader_becomes_default(self, client):
        """表空时新建的首台自动成为默认（保证「有实例就有默认」）。"""
        _run(repo.delete_downloader(app.state.db, "default"))
        _run(store.rebuild_downloaders(app))
        resp = _post(client, "/downloaders", _VALID_FORM)
        assert resp.headers["location"] == "/downloaders/nas/edit?ok=created"
        assert _record("nas").is_default is True
        assert app.state.downloaders.default().id == "nas"

    def test_create_duplicate_id(self, client):
        resp = _post(client, "/downloaders", {**_VALID_FORM, "id": "default"})
        assert resp.headers["location"] == "/downloaders/default/edit?err=exists"
        assert len(_run(repo.list_downloaders(app.state.db))) == 1

    @pytest.mark.parametrize("field,value,err", [
        ("id", "bad id", "bad_id"),
        ("id", "-nas", "bad_id"),
        ("id", "a" * 33, "bad_id"),
        ("name", "   ", "bad_name"),
        ("host", "", "bad_host"),
        ("host", "http://nas", "bad_host"),
        ("host", "nas/media", "bad_host"),
        ("port", "abc", "bad_port"),
        ("port", "0", "bad_port"),
        ("port", "65536", "bad_port"),
        ("save_path", "  ", "bad_save_path"),
        ("priority", "high", "bad_priority"),
    ])
    def test_create_validation(self, client, field, value, err):
        resp = _post(client, "/downloaders", {**_VALID_FORM, field: value})
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/downloaders/new?err={err}"
        # 校验失败不落库（表里仍只有首启种子的 default）
        assert [r.id for r in _run(repo.list_downloaders(app.state.db))] == ["default"]


# ── 保存：连接变更作废测试结果 / 凭据留空不修改 ───────────────

class TestSave:
    def test_save_ok(self, client):
        _add_downloader("nas", password="pw-secret")
        resp = _post(client, "/downloaders/nas", {
            "name": "NAS 二号", "host": "192.168.1.10", "port": "9091",
            "save_path": "/volume2/music", "category": "music", "tag": "hub2", "priority": "7",
        })
        assert resp.headers["location"] == "/downloaders/nas/edit?ok=saved"
        rec = _record("nas")
        assert rec.name == "NAS 二号" and rec.port == 9091 and rec.priority == 7
        assert rec.save_path == "/volume2/music" and rec.category == "music"
        assert app.state.downloaders.get("nas").config.port == 9091

    def test_save_blank_credentials_kept(self, client):
        """凭据留空 = 不修改：库中密文原样保留。"""
        _add_downloader("nas", password="pw-secret", api_key="key-secret")
        resp = _post(client, "/downloaders/nas", {
            "name": "NAS", "host": "192.168.1.10", "port": "9090", "username": "admin",
            "password": "", "api_key": "", "save_path": "/volume1/music",
        })
        assert resp.headers["location"] == "/downloaders/nas/edit?ok=saved"
        assert _secret("nas", "password") == "pw-secret"
        assert _secret("nas", "api_key") == "key-secret"
        rec = _record("nas")
        assert rec.has_password and rec.has_api_key

    def test_save_connection_change_invalidates_last_test(self, client):
        """连接参数或凭据变了，上次测试结果不再可信；只改名称等则保留。"""
        _add_downloader("nas", password="pw-secret")
        _run(repo.set_downloader_test(app.state.db, "nas", _PROBE_OK))

        # 只改名称 / 优先级：连接四要素与凭据未动 → 测试结果保留
        resp = _post(client, "/downloaders/nas", {
            "name": "新名字", "host": "192.168.1.10", "port": "9090", "username": "admin",
            "save_path": "/volume1/music", "priority": "9",
        })
        assert resp.headers["location"] == "/downloaders/nas/edit?ok=saved"
        assert _record("nas").last_test["ok"] is True

        # 改 host → 作废为「配置已更改，请重新测试」
        resp = _post(client, "/downloaders/nas", {
            "name": "新名字", "host": "192.168.1.11", "port": "9090", "username": "admin",
            "save_path": "/volume1/music",
        })
        assert resp.headers["location"] == "/downloaders/nas/edit?ok=saved"
        assert _record("nas").last_test == {"ok": False, "error": "配置已更改，请重新测试"}

        # 填新密码同样算连接变更
        _run(repo.set_downloader_test(app.state.db, "nas", _PROBE_OK))
        _post(client, "/downloaders/nas", {
            "name": "新名字", "host": "192.168.1.11", "port": "9090", "username": "admin",
            "password": "new-pw", "save_path": "/volume1/music",
        })
        assert _record("nas").last_test["ok"] is False
        assert _secret("nas", "password") == "new-pw"

    def test_save_notfound(self, client):
        resp = _post(client, "/downloaders/ghost", _VALID_FORM)
        assert resp.headers["location"] == "/downloaders?err=notfound"

    def test_save_validation_redirects_back_to_edit(self, client):
        _add_downloader("nas")
        resp = _post(client, "/downloaders/nas", {**_VALID_FORM, "port": "70000"})
        assert resp.headers["location"] == "/downloaders/nas/edit?err=bad_port"
        assert _record("nas").port == 9090                    # 校验失败不落库


# ── 测试连接：mock probe_qb，结果落 last_test_json ───────────

class TestTestConnection:
    def test_probe_ok(self, client, monkeypatch):
        _add_downloader("nas", password="pw-secret")
        calls = _mock_probe(monkeypatch, _PROBE_OK)
        resp = _post(client, "/downloaders/nas/test")
        assert resp.status_code == 303
        loc = unquote(resp.headers["location"])
        assert loc.startswith("/downloaders?ok=test_ok")
        assert "qB 5.2.0 / API 2.11.0" in loc
        # probe 拿到的是从 downloaders 表解密出的完整配置（凭据明文不过页面）
        assert calls[0].id == "nas" and calls[0].password == "pw-secret"
        rec = _record("nas")
        assert rec.last_test["ok"] is True and rec.last_test["version"] == "5.2.0"
        assert rec.last_test["elapsed_ms"] >= 0 and rec.last_test_at is not None
        row = _row(client.get("/downloaders").text, "nas")
        assert "正常" in row and "5.2.0" in row

    def test_probe_fail(self, client, monkeypatch):
        _add_downloader("nas", password="pw-secret")
        _mock_probe(monkeypatch, _PROBE_FAIL)
        resp = _post(client, "/downloaders/nas/test")
        loc = unquote(resp.headers["location"])
        assert "err=test_failed" in loc and "TCP 不可达" in loc
        rec = _record("nas")
        assert rec.last_test["ok"] is False and "TCP 不可达" in rec.last_test["error"]
        assert "连接失败" in _row(client.get("/downloaders").text, "nas")
        # 跟随重定向能看到失败 flash 与详情
        page = client.get(resp.headers["location"])
        assert "连接测试失败" in page.text and "TCP 不可达" in page.text

    def test_probe_redirects_back_to_edit_when_came_from_edit(self, client, monkeypatch):
        _add_downloader("nas", password="pw-secret")
        _mock_probe(monkeypatch, _PROBE_OK)
        resp = _post(client, "/downloaders/nas/test",
                     headers={"referer": "http://testserver/downloaders/nas/edit"})
        assert resp.headers["location"].startswith("/downloaders/nas/edit?ok=test_ok")

    def test_probe_notfound(self, client, monkeypatch):
        calls = _mock_probe(monkeypatch, _PROBE_OK)
        resp = _post(client, "/downloaders/ghost/test")
        assert resp.headers["location"] == "/downloaders?err=notfound"
        assert calls == [], "不存在的下载器不该发起探测"


# ── 默认切换 / 启用禁用 ─────────────────────────────────────

class TestSetDefaultAndToggle:
    def test_set_default(self, client):
        _add_downloader("nas", password="pw-secret")
        resp = _post(client, "/downloaders/nas/set-default")
        assert resp.headers["location"] == "/downloaders?ok=default_set"
        assert _record("nas").is_default is True
        assert _record("default").is_default is False
        assert app.state.downloaders.default().id == "nas"

    def test_set_default_disabled_rejected(self, client):
        """已禁用的下载器不能设为默认（「默认却关着」语义混乱）。"""
        _add_downloader("nas", enabled=False)
        resp = _post(client, "/downloaders/nas/set-default")
        assert "err=disabled_default" in resp.headers["location"]
        assert _record("nas").is_default is False
        assert app.state.downloaders.default().id == "default"

    def test_set_default_notfound(self, client):
        resp = _post(client, "/downloaders/ghost/set-default")
        assert resp.headers["location"] == "/downloaders?err=notfound"

    def test_disable_and_enable(self, client):
        _add_downloader("nas", password="pw-secret")
        resp = _post(client, "/downloaders/nas/toggle", {"enabled": "0"})
        assert resp.headers["location"] == "/downloaders?ok=disabled"
        assert _record("nas").enabled is False
        assert app.state.downloaders.get("nas").config.enabled is False

        resp = _post(client, "/downloaders/nas/toggle", {"enabled": "1"},
                     headers={"referer": "http://testserver/downloaders/nas/edit"})
        assert resp.headers["location"] == "/downloaders/nas/edit?ok=enabled"
        assert _record("nas").enabled is True
        assert app.state.downloaders.get("nas").config.enabled is True

    def test_disable_default_promotes_next(self, client):
        """v0.12：禁用默认 → 自动继任给其它已启用者，不再死锁。"""
        _add_downloader("nas", password="pw-secret", priority=5)
        resp = _post(client, "/downloaders/default/toggle", {"enabled": "0"})
        assert "ok=disabled" in resp.headers["location"]
        assert "detail=" in resp.headers["location"]      # flash 携带继任说明
        assert _record("default").enabled is False
        assert _record("default").is_default is False
        assert _record("nas").is_default is True

    def test_disable_default_last_enabled_rejected(self, client):
        """默认且是唯一已启用 → 禁用仍拒绝（关了就没地方派发）。"""
        resp = _post(client, "/downloaders/default/toggle", {"enabled": "0"})
        assert "err=last_enabled" in resp.headers["location"]
        assert _record("default").enabled is True

    def test_toggle_notfound(self, client):
        resp = _post(client, "/downloaders/ghost/toggle", {"enabled": "0"})
        assert resp.headers["location"] == "/downloaders?err=notfound"


# ── 删除：三重保护 + 正常路径 ────────────────────────────────

class TestDelete:
    def test_delete_ok(self, client):
        _add_downloader("nas", password="pw-secret")
        resp = _post(client, "/downloaders/nas/delete")
        assert resp.headers["location"] == "/downloaders?ok=deleted"
        assert _record("nas") is None
        assert "nas" not in app.state.downloaders
        assert client.get("/downloaders/nas/edit", follow_redirects=False).headers["location"] \
            == "/downloaders?err=notfound"

    def test_delete_notfound(self, client):
        resp = _post(client, "/downloaders/ghost/delete")
        assert resp.headers["location"] == "/downloaders?err=notfound"

    def test_delete_default_promotes_next(self, client):
        """v0.12：删除默认 → 自动继任，不再要求先手动切换默认。"""
        _add_downloader("nas", password="pw-secret", priority=5)
        resp = _post(client, "/downloaders/default/delete")
        assert "ok=deleted" in resp.headers["location"]
        assert _record("default") is None
        assert _record("nas").is_default is True

    def test_delete_default_last_enabled_rejected(self, client):
        """默认且是唯一已启用 → 删除仍拒绝（last_enabled 兜底）。"""
        resp = _post(client, "/downloaders/default/delete")
        assert "err=last_enabled" in resp.headers["location"]
        assert _record("default") is not None

    def test_delete_last_enabled_rejected(self, client):
        """最后一个已启用的下载器不可删（删了就没地方派发）。

        「唯一启用却非默认」是服务端兜底分支：正常界面路径到不了这个状态
        （禁用 / 删除默认都会被先一步拦下），这里直接改库模拟。
        """
        async def strip_default_flag():
            await app.state.db.execute(
                "UPDATE downloaders SET is_default = 0 WHERE id = 'default'")
            await app.state.db.commit()
        _run(strip_default_flag())

        resp = _post(client, "/downloaders/default/delete")
        assert "err=last_enabled" in resp.headers["location"]
        assert _record("default") is not None

    def test_delete_with_inflight_task_rejected(self, client):
        """在途任务引用的下载器不可删；任务进终态后放行。"""
        _add_downloader("nas", password="pw-secret")
        task_id = _run(repo.create_task(app.state.db, Task(
            info_hash="cd" * 20, site="hhanclub", title="周杰伦 叶惠美",
            downloader_id="nas", state=TaskState.DOWNLOADING,
        )))
        resp = _post(client, "/downloaders/nas/delete")
        loc = unquote(resp.headers["location"])
        assert "err=has_tasks" in loc and "1 个在途任务" in loc
        assert _record("nas") is not None

        _run(repo.set_task_state(app.state.db, task_id, TaskState.FAILED))
        resp = _post(client, "/downloaders/nas/delete")
        assert resp.headers["location"] == "/downloaders?ok=deleted"

    def test_delete_with_completed_task_allowed(self, client):
        """v0.12.1：已完成任务不再拦删除——历史记录保留，downloader_id 仅存档。"""
        _add_downloader("nas", password="pw-secret")
        _run(repo.create_task(app.state.db, Task(
            info_hash="ef" * 20, site="hhanclub", title="周杰伦 八度空间",
            downloader_id="nas", state=TaskState.COMPLETED,
        )))
        resp = _post(client, "/downloaders/nas/delete")
        assert "ok=deleted" in resp.headers["location"]
        assert _record("nas") is None
        assert _record("nas") is None


# ── 权限：非管理员只读，写操作一律 403 ───────────────────────

class TestPermissions:
    def test_pages_require_login(self, client):
        # 不带会话 cookie 的匿名 client（app.state 已由外层 client 的 lifespan 装配）
        anon = TestClient(app)
        resp = anon.get("/downloaders", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")

    def test_write_endpoints_require_csrf(self, client):
        resp = client.post("/downloaders/default/toggle", data={"enabled": "0"},
                           follow_redirects=False)
        assert resp.status_code == 403

    def test_write_endpoints_require_admin(self, client):
        _login_as_viewer(client)
        for path in ("/downloaders", "/downloaders/default", "/downloaders/default/test",
                     "/downloaders/default/set-default", "/downloaders/default/toggle",
                     "/downloaders/default/delete"):
            resp = _post(client, path, {"id": "x", "enabled": "0"})
            assert resp.status_code == 403, path
        # 全部被拒后库里不该有改动
        assert _record("default") is not None

    def test_viewer_read_only_view(self, client):
        _login_as_viewer(client)
        page = client.get("/downloaders")
        assert page.status_code == 200
        assert "操作" not in page.text                     # 无操作列
        assert "新建下载器" not in page.text                # 无添加入口
        edit = client.get("/downloaders/default/edit")
        assert edit.status_code == 200
        assert "测试与状态" not in edit.text                # 测试面板仅管理员可见
