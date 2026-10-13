"""FastAPI lifespan 装配测试：验证 main.py 的资源装配路径真实可用。

这是端到端的最后一环——CLI 已验证过，但 lifespan（容器实际启动路径）
涉及 config 加载、DB 初始化、adapter 构造、scheduler 启停、崩溃恢复对账。

不发任何网络请求：配置中站点全部 enabled=false。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audio_hub import __version__
from audio_hub.main import app


@pytest.fixture
def env_config(tmp_path: Path, monkeypatch):
    """写入一份站点全禁用的测试配置，并设置环境变量。"""
    db_path = tmp_path / "hub.db"
    config_text = f"""
dry_run: true
db_path: "{db_path.as_posix()}"
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
  - name: "mteam"
    enabled: false
    search_url: "https://api.m-team.cc/api/torrent/search"
    api_key_encrypted: ""
    music_cat_id: "music"
    max_rows: 50
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(config_text, encoding="utf-8")

    key_file = tmp_path / "secrets.key"
    from audio_hub.crypto import generate_key
    key_file.write_bytes(generate_key())

    monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg_file))
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(key_file))
    return tmp_path


@pytest.fixture
def admin_client(env_config):
    """已登录管理员的 TestClient。

    v0.2 起调试端点收敛到 /api/ 前缀并要求登录（PRD §12.3），
    故本文件凡打 /api/* 的用例都走这个 fixture。
    """
    with TestClient(app) as client:
        resp = client.post("/setup", data={
            "username": "ops",
            "password": "strongpass123",
            "password2": "strongpass123",
        }, follow_redirects=False)
        assert resp.status_code in (303, 200), resp.text
        yield client


def _csrf_token(client: TestClient) -> str:
    """从已登录页面抓 CSRF token（写操作必须带）。"""
    import re

    page = client.get("/dashboard")
    assert page.status_code == 200, page.text[:300]
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m, "dashboard 页面未找到 CSRF meta 标签"
    return m.group(1)


def _api_run_once(client: TestClient):
    """带 CSRF 头调用 JSON 版 run-once。"""
    return client.post("/api/run-once",
                       headers={"X-CSRF-Token": _csrf_token(client)})


class TestLifespan:
    def test_app_starts_and_stops(self, env_config):
        """lifespan 应能完整启停，不抛异常。"""
        with TestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200
        # 退出 with 块后 scheduler 应已关闭（不残留后台任务）

    def test_health_reports_dry_run(self, env_config):
        with TestClient(app) as client:
            data = client.get("/health").json()
            assert data["status"] == "ok"
            # 断言取自 __version__ 而非硬编码字面量：版本号只有
            # src/audio_hub/__init__.py 一个真源，改版本不该改测试
            assert data["version"] == __version__
            assert data["dry_run"] is True

    def test_health_reports_no_adapters_when_all_disabled(self, env_config):
        """站点全禁用时应报告空适配器列表，而非崩溃。"""
        with TestClient(app) as client:
            data = client.get("/health").json()
            # main.py 用 list(runner.adapters) 暴露站点名列表
            assert data["adapters"] == []

    def test_status_endpoint(self, admin_client):
        data = admin_client.get("/api/status").json()
        assert data["dry_run"] is True
        assert data["watchlist_count"] == 0
        assert data["tasks_by_state"] == {}

    def test_api_status_requires_login(self, env_config):
        """未登录打 /api/status 应返回 401 JSON，而非重定向到登录页。"""
        with TestClient(app) as anon:
            resp = anon.get("/api/status", follow_redirects=False)
            assert resp.status_code == 401
            assert resp.json() == {"detail": "not_authenticated"}

    def test_run_once_endpoint_empty_watchlist(self, admin_client):
        """无订阅项时 run-once 应返回 watchlist_empty（最廉价检查在前）。"""
        resp = _api_run_once(admin_client)
        assert resp.status_code == 200
        assert resp.json().get("watchlist_empty") is True

    def test_api_run_once_requires_csrf(self, admin_client):
        """写操作缺 CSRF token 应 403（PRD §12.3，与页面版一致）。"""
        resp = admin_client.post("/api/run-once")
        assert resp.status_code == 403

    def test_api_run_once_rejects_bad_csrf(self, admin_client):
        """伪造的 CSRF token 同样应 403。"""
        resp = admin_client.post("/api/run-once",
                                 headers={"X-CSRF-Token": "bogus-token"})
        assert resp.status_code == 403

    def test_run_once_reports_no_adapters_when_watchlist_nonempty(self, admin_client):
        """有订阅项但无可用适配器时，应报告 no_adapters 而非 500。"""
        import asyncio

        from audio_hub import repo
        from audio_hub.models import WatchEntry, WatchKind

        # 复用 app 已打开的连接写入：另开一个 SQLite 连接会与 lifespan
        # 持有的连接争锁，测试环境下容易偶发 "database is locked"
        asyncio.run(repo.add_watch_entry(app.state.db, WatchEntry(
            id="seeded-1", query="周杰伦", kind=WatchKind.MUSIC,
        )))

        resp = _api_run_once(admin_client)
        assert resp.status_code == 200
        assert resp.json().get("no_adapters") is True

    def test_db_file_created(self, env_config):
        """启动后 SQLite 文件应真实存在（WAL 模式会额外生成 -wal/-shm）。"""
        with TestClient(app):
            pass
        assert (env_config / "hub.db").exists()

    def test_state_attached_to_app(self, env_config):
        """lifespan 应把 db / config / runner / scheduler 挂到 app.state。"""
        with TestClient(app):
            assert app.state.config is not None
            assert app.state.db is not None
            assert app.state.runner is not None
            assert app.state.scheduler is not None
            assert app.state.config.dry_run is True

    def test_monitor_shares_runner_notifier(self, env_config):
        """CompletionMonitor 必须拿到 runner 的 notifier，否则归档事件不进 events 表。

        监听器的 notifier 是可选参数；漏传不会报错，只是 routed / quarantined 事件
        静默退化成纯日志 —— 事件页与推送渠道都看不到归档结果。这里锁死装配关系。
        """
        with TestClient(app):
            runner = app.state.runner
            assert runner.monitor.notifier is runner.notifier
            assert runner.monitor.notifier is app.state.notifier
            assert runner.monitor.db is app.state.db
            assert runner.monitor.config is app.state.config


class TestConfigSecurity:
    def test_no_plaintext_passkey_in_loaded_config(self, env_config):
        """验收标准：加载后的配置对象中不应残留明文 passkey 字段名。

        注：本用例配置里 api_key_encrypted 为空串，故不会触发解密；
        重点是验证 _encrypted 后缀字段被正确移除的机制（见下条）。
        """
        from audio_hub.config import load_config
        cfg = load_config()
        # QB 配置不应有 api_key_encrypted 这个属性（已被 _decrypt_sensitive 移除）
        assert not hasattr(cfg.qbittorrent, "api_key_encrypted")

    def test_encrypted_field_decrypted_and_removed(self, tmp_path: Path, monkeypatch):
        """_encrypted 字段应被解密到明文字段，且原字段被移除。"""
        from audio_hub.config import load_config
        from audio_hub.crypto import encrypt_value, generate_key

        key_file = tmp_path / "k.key"
        key_file.write_bytes(generate_key())
        monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(key_file))

        token = encrypt_value("MY_SECRET_PASSKEY", key_file)
        cfg_text = f"""
dry_run: true
db_path: "{(tmp_path / 'x.db').as_posix()}"
qbittorrent:
  host: "localhost"
  port: 8085
  api_key_encrypted: "{token}"
sites: []
"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(cfg_text, encoding="utf-8")
        monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg_file))

        cfg = load_config()
        assert cfg.qbittorrent.api_key == "MY_SECRET_PASSKEY"   # 已解密
        assert not hasattr(cfg.qbittorrent, "api_key_encrypted")  # 原字段已移除
