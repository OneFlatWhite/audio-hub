"""站点凭据测试：加密入库、留空不覆盖、热重建适配器、状态如实反映。

覆盖用户选择的「界面填完整 RSS 地址」方案，并锁住两个已修 bug：
1. init_secrets 曾收集 passkey 输出 `<site>_passkey_encrypted`，但
   SiteConfig 没有 passkey 字段 → Pydantic 静默丢弃，照做等于白做
2. 界面凭据标签曾用 `bool(s.rss_url or s.api_key)` 一律判断，
   对 mteam（必需 api_key）判断错了必需项

v0.6 起站点配置的权威源是 sites 表（registry_store），不再走 app_settings 覆盖层与
POST /settings/sites。本文件的写操作改为直接经 repo.upsert_site +
settings_store.rebuild_adapters（即站点中心页保存后的同一条路径），断言仍落在同一组行为上。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audio_hub import registry_store, repo
from audio_hub import settings_store as store
from audio_hub.adapters.registry import site_credential_kinds
from audio_hub.main import app
from audio_hub.models import AuthStatus


@pytest.fixture
def cfg_dir(tmp_path: Path, monkeypatch):
    """四站配置：三个 NexusPHP 系 + mteam，全部禁用且凭据留空。"""
    from audio_hub.crypto import generate_key

    key_file = tmp_path / "secrets.key"
    key_file.write_bytes(generate_key())

    config_text = f"""
dry_run: true
db_path: "{(tmp_path / 'hub.db').as_posix()}"
log_level: "WARNING"
poll_interval_minutes: 30

web:
  cookie_secure: false
  auth_request_limit: 100
  auth_window_minutes: 15

qbittorrent:
  host: "localhost"
  port: 8085
  api_key_encrypted: ""
  category: "audio-hub"
  tag: "hub"
  save_path: "{(tmp_path / 'seeds').as_posix()}"

sites:
  - name: "hhanclub"
    enabled: false
    rss_url: ""
  - name: "mteam"
    enabled: false
    rss_url: ""
    api_key: ""
    search_url: "https://api.m-team.cc/api/torrent/search"
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg_file))
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(key_file))
    (tmp_path / "seeds").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def client(cfg_dir):
    with TestClient(app) as c:
        yield c


def _setup_admin(client):
    resp = client.post("/setup", data={
        "username": "ops", "password": "strongpass123", "password2": "strongpass123",
    }, follow_redirects=False)
    assert resp.status_code in (303, 200), resp.text
    return resp


def _csrf(client) -> str:
    page = client.get("/settings")
    assert page.status_code == 200, page.text[:300]
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m, "settings 页未下发 CSRF meta"
    return m.group(1)


def _save_site(site_id: str, **fields):
    """模拟站点中心「只改提交的字段」：读现状 → 覆盖字段 → upsert → 热重建适配器。

    凭据字段留空（""）时 upsert_site 保留库中已有值，与界面「留空＝不修改」一致。
    启用时先把认证状态置为 valid（v0.6 门禁：auth 未通过的站点不构建适配器），
    本文件只关心凭据与热重建，门禁本身由 test_sites_routes 覆盖。
    """
    async def _go():
        db = app.state.db
        current = {s.name: s for s in await registry_store.load_site_configs(db)}[site_id]
        await repo.upsert_site(db, current.model_copy(update=fields), site_id=site_id)
        if fields.get("enabled"):
            await repo.set_site_auth(db, site_id, AuthStatus.VALID)
        await store.rebuild_adapters(app)
    asyncio.run(_go())


def _stored(site_id: str, column: str) -> str | None:
    async def _go():
        async with app.state.db.execute(
            f"SELECT {column} FROM sites WHERE id = ?", (site_id,),
        ) as cur:
            r = await cur.fetchone()
        return r[0] if r else None
    return asyncio.run(_go())


def _secret(site_id: str, field: str) -> str:
    return asyncio.run(repo.get_site_secret(app.state.db, site_id, field))


# ── 凭据需求声明 ────────────────────────────────────────────

class TestCredentialKinds:
    def test_nexusphp_needs_rss_url(self):
        """NexusPHP 系：passkey 内联在 URL 里，故凭据就是 rss_url。"""
        for name in ("hhanclub", "hdsky", "audiences"):
            kinds = site_credential_kinds(name)
            assert kinds.get("rss_url") is True, f"{name} 应必需 rss_url"
            assert "api_key" not in kinds, f"{name} 不用 api_key"

    def test_mteam_needs_api_key_not_rss(self):
        """mteam 用 Authorization header，api_key 必需；rss_url 可选。"""
        kinds = site_credential_kinds("mteam")
        assert kinds.get("api_key") is True
        assert kinds.get("rss_url") is False

    def test_site_alias_resolved(self):
        assert site_credential_kinds("馒头") == site_credential_kinds("mteam")

    def test_unknown_site_falls_back_to_nexusphp(self):
        """v0.12：未注册站点按 NexusPHP 处理（与 build_adapter 回退一致）。"""
        assert site_credential_kinds("nonexistent_site") == {"rss_url": True}


# ── 凭据写入与加密（sites 表）────────────────────────────────

class TestSiteCredentialStore:
    def test_yaml_sites_seeded_into_db(self, client):
        """首启：YAML 两站进 sites 表，禁用、无凭据、认证未配置。"""
        _setup_admin(client)
        recs = {s.id: s for s in asyncio.run(repo.list_sites(app.state.db))}
        assert set(recs) == {"hhanclub", "mteam"}
        assert recs["hhanclub"].enabled is False and recs["hhanclub"].has_rss is False
        assert recs["mteam"].adapter_type == "mteam" and recs["mteam"].has_api_key is False
        # 旧的站点覆盖层端点已下线
        resp = client.post("/settings/sites", data={"csrf_token": _csrf(client)},
                           follow_redirects=False)
        assert resp.status_code in (404, 405)

    def test_settings_page_has_no_site_form(self, client):
        _setup_admin(client)
        page = client.get("/settings")
        assert page.status_code == 200
        assert 'action="/settings/sites"' not in page.text
        assert 'name="site_rss:hhanclub"' not in page.text

    def test_rss_url_stored_encrypted(self, client):
        """整条 RSS 地址当凭据加密，明文不落库。"""
        _setup_admin(client)
        secret_url = "https://hhanclub.net/torrentrss.php?rows=50&passkey=TOPSECRET123"
        _save_site("hhanclub", rss_url=secret_url)

        stored = _stored("hhanclub", "rss_url_enc")
        assert stored and "TOPSECRET123" not in stored, "passkey 明文入库"
        assert stored != secret_url

        from audio_hub.crypto import decrypt_value
        assert decrypt_value(stored) == secret_url
        assert _secret("hhanclub", "rss_url") == secret_url

    def test_rss_url_never_rendered(self, client):
        _setup_admin(client)
        secret_url = "https://hhanclub.net/torrentrss.php?passkey=TOPSECRET123"
        _save_site("hhanclub", rss_url=secret_url)
        page = client.get("/settings")
        assert "TOPSECRET123" not in page.text
        # 页面模型（SiteRecord）本身就不带明文字段
        rec = asyncio.run(repo.get_site(app.state.db, "hhanclub"))
        assert "TOPSECRET123" not in rec.model_dump_json()
        assert rec.has_rss is True

    def test_empty_secret_does_not_overwrite(self, client):
        _setup_admin(client)
        url = "https://hhanclub.net/torrentrss.php?passkey=KEEPME"
        _save_site("hhanclub", rss_url=url)
        _save_site("hhanclub", rss_url="")
        assert _secret("hhanclub", "rss_url") == url, "留空把已有凭据覆盖了"

    def test_mteam_api_key_stored_encrypted(self, client):
        _setup_admin(client)
        _save_site("mteam", api_key="MTOKEN_ABC123")
        stored = _stored("mteam", "api_key_enc")
        assert stored and "MTOKEN_ABC123" not in stored
        assert _secret("mteam", "api_key") == "MTOKEN_ABC123"

    def test_legacy_overrides_not_in_settings_layer(self, client):
        """站点键不再进 app_settings：写凭据后覆盖层里不得出现 site_* 键。"""
        _setup_admin(client)
        _save_site("hhanclub", rss_url="https://hhanclub.net/torrentrss.php?passkey=X")
        overrides = asyncio.run(store.load_overrides(app.state.db))
        assert not any(k.startswith(p) for k in overrides for p in store.SITE_KEY_PREFIXES)


# ── 热生效：适配器重建 ──────────────────────────────────────

class TestCredentialHotReload:
    def test_credential_and_enable_together(self, client):
        """同时填凭据 + 启用：适配器应构建成功。"""
        _setup_admin(client)
        assert "hhanclub" not in app.state.runner.adapters

        _save_site("hhanclub", rss_url="https://hhanclub.net/torrentrss.php?passkey=X",
                   enabled=True)

        site = next(s for s in app.state.config.sites if s.name == "hhanclub")
        assert site.enabled is True
        assert "passkey=X" in site.rss_url
        assert "hhanclub" in app.state.runner.adapters, "适配器未重建"

    def test_disable_removes_adapter(self, client):
        _setup_admin(client)
        _save_site("hhanclub", rss_url="https://hhanclub.net/torrentrss.php?passkey=X",
                   enabled=True)
        assert "hhanclub" in app.state.runner.adapters
        _save_site("hhanclub", enabled=False)
        assert "hhanclub" not in app.state.runner.adapters
        assert next(s for s in app.state.config.sites if s.name == "hhanclub").enabled is False

    def test_mteam_adapter_built_after_api_key(self, client):
        """mteam 缺 api_key 时构造会抛错被跳过；填上后应能构建。

        这条锁住「只改 config 不重建适配器」的隐患 —— mteam 在
        __init__ 里把 api_key 缓存成 self._api_key，不重建就用旧值。
        """
        _setup_admin(client)
        # 先启用但没 api_key：构造失败被 build_adapters 跳过
        _save_site("mteam", enabled=True)
        assert "mteam" not in app.state.runner.adapters

        # 补上 api_key：应重建成功
        _save_site("mteam", api_key="MTOKEN_XYZ")
        assert "mteam" in app.state.runner.adapters, \
            "补 api_key 后适配器仍未构建，说明没重建"

    def test_credential_persists_across_restart(self, cfg_dir):
        """重启后从 sites 表加载：凭据仍在、YAML 的 enabled=false 不会把它打回。"""
        with TestClient(app) as first:
            _setup_admin(first)
            _save_site("hhanclub",
                       rss_url="https://hhanclub.net/torrentrss.php?passkey=PERSIST",
                       enabled=True)

        with TestClient(app) as second:
            assert second.get("/health").status_code == 200
            site = next(s for s in app.state.config.sites if s.name == "hhanclub")
            assert "passkey=PERSIST" in site.rss_url, "凭据未在重启后生效"
            assert site.enabled is True
            assert "hhanclub" in app.state.runner.adapters


# ── 凭据状态如实反映 ────────────────────────────────────────

class TestCredentialStatus:
    def test_status_flags_missing_required(self, client):
        _setup_admin(client)
        status = store.credential_status(app.state.config)
        by_name = {s["name"]: s for s in status["sites"]}

        assert by_name["hhanclub"]["ready"] is False
        assert by_name["mteam"]["ready"] is False
        # mteam 缺的是 api_key（必需），rss_url 可选不影响 ready
        mteam_items = {i["field"]: i for i in by_name["mteam"]["creds"]}  # creds 而非 items，避开 dict.items() 撞名
        assert mteam_items["api_key"]["required"] is True
        assert mteam_items["rss_url"]["required"] is False

    def test_status_ready_after_fill(self, client):
        _setup_admin(client)
        _save_site("hhanclub", rss_url="https://hhanclub.net/torrentrss.php?passkey=X")
        status = store.credential_status(app.state.config)
        by_name = {s["name"]: s for s in status["sites"]}
        assert by_name["hhanclub"]["ready"] is True

    def test_mteam_not_ready_with_only_rss(self, client):
        """mteam 只填 rss_url 仍不算就绪 —— 原 bug 会误判为已配置。"""
        _setup_admin(client)
        _save_site("mteam", rss_url="https://rss.m-team.cc/api/rss/fetch?dl=1")
        status = store.credential_status(app.state.config)
        by_name = {s["name"]: s for s in status["sites"]}
        assert by_name["mteam"]["ready"] is False

    def test_page_shows_missing_credential_warning(self, client):
        """启用但缺凭据时，页面应明确警告熔断风险。"""
        _setup_admin(client)
        _save_site("hhanclub", enabled=True)
        page = client.get("/settings")
        assert "熔断" in page.text

    def test_page_does_not_leak_stored_token(self, client):
        _setup_admin(client)
        _save_site("hhanclub", rss_url="https://hhanclub.net/torrentrss.php?passkey=LEAKTEST")
        page = client.get("/settings")
        stored = _stored("hhanclub", "rss_url_enc")
        assert stored and stored not in page.text
        assert "LEAKTEST" not in page.text
