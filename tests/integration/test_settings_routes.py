"""设置覆盖层测试：界面改动落库、热生效、需重启项不半应用、重启后仍在。

这是 v0.3 设置功能的核心验收。重点不是「能保存」，而是三件容易做错的事：

1. **需重启的项绝不能半应用**：dry_run 若只改了 config 而 dispatcher
   仍持旧值，使用者会以为空跑保护还开着、实际已在真实下载。
2. **未提交的字段＝不修改**，不是「按缺省值处理」。若把缺失当 False，
   任何漏带 dry_run 字段的请求都会静默关掉空跑保护。
3. **覆盖层真的持久化**：重启后（重走 lifespan）配置仍是界面改过的值。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from audio_hub import registry_store, repo
from audio_hub import settings_store as store
from audio_hub.main import app
from audio_hub.models import AuthStatus
from audio_hub.web import auth


@pytest.fixture
def cfg_dir(tmp_path: Path, monkeypatch):
    """配置 + 密钥；两个站点（一禁一禁，便于验证开关）；不触网。"""
    from audio_hub.crypto import generate_key

    key_file = tmp_path / "secrets.key"
    key_file.write_bytes(generate_key())

    config_text = f"""
dry_run: true
db_path: "{(tmp_path / 'hub.db').as_posix()}"
log_level: "INFO"
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
    rss_url: "https://hhanclub.net/torrentrss.php?passkey=FAKE"
    music_cat_id: "410"
  - name: "hdsky"
    enabled: false
    rss_url: "https://hdsky.me/torrentrss.php?passkey=FAKE"
    music_cat_id: "401"
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg_file))
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(key_file))
    return tmp_path


@pytest.fixture
def client(cfg_dir):
    with TestClient(app) as c:
        yield c


def _setup_admin(client, username="ops", password="strongpass123"):
    resp = client.post("/setup", data={
        "username": username, "password": password, "password2": password,
    }, follow_redirects=False)
    assert resp.status_code in (303, 200), resp.text
    return resp


def _csrf(client) -> str:
    import re
    page = client.get("/settings")
    assert page.status_code == 200, page.text[:300]
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m, "settings 页未下发 CSRF meta"
    return m.group(1)


async def _make_normal_user(username="viewer", password="strongpass123") -> str:
    """直接插库造一个非管理员用户（create_admin 一律 is_admin=1）。"""
    uid = str(uuid.uuid4())
    await app.state.db.execute(
        """INSERT INTO users (id, username, password_hash, is_admin, created_at, updated_at)
           VALUES (?,?,?,?,?,?)""",
        (uid, username, auth.hash_password(password), 0,
         auth.iso_utc(), auth.iso_utc()),
    )
    await app.state.db.commit()
    return uid


def _post_settings(client, action, data, token=None):
    payload = dict(data)
    payload["csrf_token"] = token if token is not None else _csrf(client)
    return client.post(action, data=payload, follow_redirects=False)


def _set_site_enabled(site_id: str, enabled: bool) -> None:
    """v0.6：站点开关在 sites 表（站点中心页），保存后经 rebuild_adapters 热生效。

    认证门禁（2A）：auth_status != valid 的站点不会被 build_adapters 构造，故启用前
    先标记为 valid（等于站点中心「测试认证」通过），本文件关心的是开关热生效本身。
    """
    async def _go():
        if enabled:
            await repo.set_site_auth(app.state.db, site_id, AuthStatus.VALID)
        await repo.set_site_enabled(app.state.db, site_id, enabled)
        await store.rebuild_adapters(app)
    asyncio.run(_go())


def _set_site_hr(site_id: str, **fields) -> None:
    """站点 H&R 覆盖：读现状 → 改字段 → upsert（None 即清除）→ 重建。"""
    async def _go():
        db = app.state.db
        cur = {s.name: s for s in await registry_store.load_site_configs(db)}[site_id]
        await repo.upsert_site(db, cur.model_copy(update=fields), site_id=site_id)
        await store.rebuild_adapters(app)
    asyncio.run(_go())


# ── 权限与 CSRF ─────────────────────────────────────────────

class TestGuards:
    def test_settings_page_requires_login(self, client):
        with TestClient(app) as anon:
            resp = anon.get("/settings", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers["location"]

    def test_write_requires_csrf(self, client):
        _setup_admin(client)
        resp = client.post("/settings/general",
                           data={"poll_interval_minutes": "45"},
                           follow_redirects=False)
        assert resp.status_code == 403

    def test_write_rejects_bad_csrf(self, client):
        _setup_admin(client)
        resp = _post_settings(client, "/settings/general",
                              {"poll_interval_minutes": "45"}, token="bogus")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_write_requires_admin(self, client):
        """非管理员：设置页被弹回总览，直接打写端点仍 403。

        在**同一** client 上登出再登录，避免并发第二个 lifespan
        （两个 TestClient 同时持有同一 SQLite 会让结果依赖时序而非逻辑）。
        """
        import re

        _setup_admin(client)
        await _make_normal_user()

        client.post("/logout", data={"csrf_token": _csrf(client)},
                    follow_redirects=False)
        resp = client.post("/login", data={
            "username": "viewer", "password": "strongpass123",
        }, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

        # 页面：重定向到总览，而非裸 JSON 403
        page = client.get("/settings", follow_redirects=False)
        assert page.status_code == 303, f"实际 {page.status_code}"
        assert "/dashboard" in page.headers.get("location", "")

        # 写端点：仍必须 403（页面友好不等于接口放开）
        m = re.search(r'name="csrf-token" content="([^"]+)"',
                      client.get("/dashboard").text)
        r = client.post("/settings/general", data={
            "poll_interval_minutes": "45",
            "csrf_token": m.group(1) if m else "x",
        }, follow_redirects=False)
        assert r.status_code == 403, f"非管理员不应能改配置，实际 {r.status_code}"


# ── 热生效项 ────────────────────────────────────────────────

class TestHotSettings:
    def test_poll_interval_applies_immediately(self, client):
        _setup_admin(client)
        resp = _post_settings(client, "/settings/general",
                              {"poll_interval_minutes": "45"})
        assert resp.status_code == 303, resp.text[:200]
        assert app.state.config.poll_interval_minutes == 45
        assert "ok=saved_general" in resp.headers["location"]
        # 没有需重启项，不应挂待重启标记
        assert resp.headers["location"].find("restart") == -1

    def test_poll_interval_below_floor_rejected(self, client):
        """低于安全下限应被拒且不落库（防误配打爆 PT 站）。"""
        _setup_admin(client)
        before = app.state.config.poll_interval_minutes
        resp = _post_settings(client, "/settings/general",
                              {"poll_interval_minutes": "1"})
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]
        assert app.state.config.poll_interval_minutes == before

    def test_poll_interval_non_integer_rejected(self, client):
        _setup_admin(client)
        resp = _post_settings(client, "/settings/general",
                              {"poll_interval_minutes": "abc"})
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]

    def test_qb_group_endpoint_removed(self, client):
        """v0.6.0：qb 组端点已撤下，连接与路径配置在下载器中心。

        旧双轨的陷阱是设置页存一份、下载器中心又存一份，两边漂移后
        没人知道哪份生效。端点必须 404，而不是静默收进旧键。
        """
        _setup_admin(client)
        before = app.state.config.qbittorrent.save_path
        resp = _post_settings(client, "/settings/qbittorrent",
                              {"qb_save_path": "/musiclab/new-seeds"})
        assert resp.status_code == 404, resp.status_code
        assert app.state.config.qbittorrent.save_path == before
        assert "qb_save_path" not in asyncio.run(store.load_overrides(app.state.db))

    def test_legacy_qb_keys_no_longer_hydrate(self, client):
        """旧库里的 qb_* 覆盖键不再进入启动覆盖层（已被收编进下载器注册表）。"""
        _setup_admin(client)
        before = app.state.config.qbittorrent.save_path
        resp = _post_settings(client, "/settings/qbittorrent", {"qb_save_path": "   "})
        assert resp.status_code == 404
        assert app.state.config.qbittorrent.save_path == before

    def test_site_enable_rebuilds_adapters(self, client):
        """站点开关热生效：sites 表改后重建，runner.adapters 立即包含该站。

        v0.6：开关不再走 /settings/sites 覆盖层，而是 sites 表 + rebuild_adapters。
        """
        _setup_admin(client)
        assert "hhanclub" not in app.state.runner.adapters

        _set_site_enabled("hhanclub", True)
        site = next(s for s in app.state.config.sites if s.name == "hhanclub")
        assert site.enabled is True
        assert "hhanclub" in app.state.runner.adapters, \
            "适配器未重建，站点开关未热生效"
        # 做种监视器的站点快照（H&R 覆盖）必须同步刷新，否则改阈值要等重启
        assert app.state.runner.seeding_monitor._site_cfg["hhanclub"].enabled is True

    def test_site_disable_rebuilds_adapters(self, client):
        _setup_admin(client)
        _set_site_enabled("hhanclub", True)
        assert "hhanclub" in app.state.runner.adapters

        _set_site_enabled("hhanclub", False)
        site = next(s for s in app.state.config.sites if s.name == "hhanclub")
        assert site.enabled is False
        assert "hhanclub" not in app.state.runner.adapters

    def test_site_toggle_persists_across_restart(self, cfg_dir):
        """sites 表是权威源：重启后 YAML 的 enabled=false 不得把界面开关打回。"""
        with TestClient(app) as first:
            _setup_admin(first)
            _set_site_enabled("hhanclub", True)
        with TestClient(app) as second:
            assert second.get("/health").status_code == 200
            site = next(s for s in app.state.config.sites if s.name == "hhanclub")
            assert site.enabled is True
            assert "hhanclub" in app.state.runner.adapters

    def test_log_level_choice_validated(self, client):
        _setup_admin(client)
        resp = _post_settings(client, "/settings/general",
                              {"log_level": "TRACE_ME"})
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]


# ── 需重启项：不得半应用 ────────────────────────────────────

class TestRestartScopedSettings:
    def test_dry_run_not_applied_at_runtime(self, client):
        """改 dry_run 只落库并标记待重启，运行时 config 不变。

        这是安全关键：若 config.dry_run 立刻变 False 而 dispatcher
        仍持旧值，界面显示与实际行为就不一致了。
        """
        _setup_admin(client)
        # select 提交单值：0 = 实跑，1 = 空跑（见 settings_store 的 labels）
        resp = _post_settings(client, "/settings/general", {"dry_run": "0"})
        assert resp.status_code == 303, resp.text[:200]

        # 运行时未被改动
        assert app.state.config.dry_run is True
        assert app.state.runner.dispatcher.dry_run is True

        # 但已落库
        overrides = asyncio.run(store.load_overrides(app.state.db))
        assert overrides.get("dry_run") == "0", \
            f"应存实跑，实际 {overrides.get('dry_run')!r}"

    def test_dry_run_sets_restart_flag(self, client):
        _setup_admin(client)
        _post_settings(client, "/settings/general", {"dry_run": "0"})
        assert asyncio.run(store.is_restart_pending(app.state.db)) is True

        page = client.get("/settings")
        assert page.status_code == 200
        assert "重启" in page.text, "页面应挂待重启横幅"

    def test_restart_flag_survives_reload(self, cfg_dir):
        """完整验证：改 -> 关 -> 重开，覆盖层生效且待重启标记被清除。"""
        with TestClient(app) as first:
            _setup_admin(first)
            _post_settings(first, "/settings/general", {
                "poll_interval_minutes": "55",
            })
            _post_settings(first, "/settings/general", {"dry_run": "0"})
            assert app.state.config.poll_interval_minutes == 55

        # 重新进入 lifespan = 模拟重启
        with TestClient(app) as second:
            assert second.get("/health").status_code == 200
            cfg = app.state.config
            assert cfg.poll_interval_minutes == 55, "轮询间隔覆盖未持久化"
            assert cfg.dry_run is False, "dry_run 覆盖未在重启后生效"
            # 重启后 dispatcher 用的就是新值了
            assert app.state.runner.dispatcher.dry_run is False
            # 重启后全部覆盖项已生效，待重启标记必须被清除 ——
            # 否则横幅永久挂着，「待重启」提示退化成噪音
            assert asyncio.run(store.is_restart_pending(app.state.db)) is False
            page = second.get("/settings")
            assert "有待重启的改动" not in page.text


# ── 未提交＝不修改（安全关键）───────────────────────────────

class TestMissingFieldSemantics:
    def test_absent_dry_run_field_does_not_change_it(self, client):
        """表单没带 dry_run 字段时，绝不能把它当成 False。

        若实现把「缺失」当默认值，任何漏带该字段的请求都会
        静默关掉空跑保护、开启真实下载。
        """
        _setup_admin(client)
        assert app.state.config.dry_run is True
        resp = _post_settings(client, "/settings/general",
                              {"poll_interval_minutes": "40"})
        assert resp.status_code == 303
        assert app.state.config.dry_run is True, "缺失字段被当成 False 了"
        overrides = asyncio.run(store.load_overrides(app.state.db))
        assert "dry_run" not in overrides, "不该写入未提交的键"

    def test_empty_secret_does_not_overwrite(self, client):
        """凭据留空表示不修改（只写不读约定）。"""
        _setup_admin(client)
        _post_settings(client, "/settings/notify",
                       {"wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=FIRST"})
        first = asyncio.run(store.load_overrides(app.state.db))
        assert "wecom_webhook" in first

        # 再提交一次，secret 留空（带上另一字段避免「无变更」短路）
        _post_settings(client, "/settings/notify",
                       {"wecom_webhook": "", "telegram_chat_id": "12345"})
        second = asyncio.run(store.load_overrides(app.state.db))
        assert second.get("wecom_webhook") == first["wecom_webhook"], \
            "留空的凭据被覆盖了"

    def test_secret_stored_encrypted_not_plaintext(self, client):
        """凭据在库里必须是 Fernet token，明文不落盘。"""
        _setup_admin(client)
        _post_settings(client, "/settings/notify",
                       {"wecom_webhook": "https://hooks.example.com/SUPER_SECRET_KEY_123"})
        overrides = asyncio.run(store.load_overrides(app.state.db))
        stored = overrides["wecom_webhook"]
        assert "SUPER_SECRET_KEY_123" not in stored, "凭据以明文入库"

        # 且能解回原值（启动时 hydrate 依赖这一点）
        from audio_hub.crypto import decrypt_value
        assert decrypt_value(stored) == "https://hooks.example.com/SUPER_SECRET_KEY_123"

    def test_secret_never_rendered_in_page(self, client):
        """设置页不得回显凭据明文或密文。"""
        _setup_admin(client)
        _post_settings(client, "/settings/notify",
                       {"wecom_webhook": "https://hooks.example.com/SUPER_SECRET_KEY_123"})
        page = client.get("/settings")
        assert "SUPER_SECRET_KEY_123" not in page.text
        overrides = asyncio.run(store.load_overrides(app.state.db))
        assert overrides["wecom_webhook"] not in page.text

    def test_no_change_submission_reports_error(self, client):
        """什么都没改就提交，应给出提示而非静默成功。"""
        _setup_admin(client)
        resp = _post_settings(client, "/settings/general", {})
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]


# ── 页面渲染 ────────────────────────────────────────────────

class TestSettingsPageRender:
    def test_page_shows_editable_forms(self, client):
        _setup_admin(client)
        page = client.get("/settings")
        assert page.status_code == 200
        assert 'action="/settings/general"' in page.text
        # v0.6：站点组已迁出设置页
        assert 'action="/settings/sites"' not in page.text
        # v0.6.0：qBittorrent 组也迁出，只留指向下载器中心的只读指引卡
        assert 'action="/settings/qbittorrent"' not in page.text
        assert "/downloaders" in page.text, "缺少 qBittorrent 指引卡"

    def test_page_marks_scope_per_field(self, client):
        """每个字段都应标注热生效 / 需重启，不含糊。"""
        _setup_admin(client)
        page = client.get("/settings")
        assert "热生效" in page.text
        assert "需重启" in page.text

    def test_readonly_db_path_still_shown(self, client):
        """db_path 不在覆盖层内，但应只读展示（避免使用者以为漏做）。"""
        _setup_admin(client)
        page = client.get("/settings")
        assert "hub.db" in page.text

    def test_site_rows_rendered(self, client):
        _setup_admin(client)
        page = client.get("/settings")
        assert "hhanclub" in page.text
        assert "hdsky" in page.text


# ── v0.4 新分组：paths / validation / scrape / seeding / notify ────────

_V4_ACTIONS = (
    "/settings/paths", "/settings/validation", "/settings/scrape",
    "/settings/seeding", "/settings/notify",
)


class TestV4Groups:
    def test_page_renders_new_forms(self, client):
        _setup_admin(client)
        page = client.get("/settings")
        assert page.status_code == 200
        for action in _V4_ACTIONS:
            assert f'action="{action}"' in page.text, f"缺少表单 {action}"
        # 各组的代表性字段真的渲染成了控件
        for name in ("library_music", "allowed_exts", "abs_token",
                     "default_min_ratio", "notify_kinds"):
            assert f'name="{name}"' in page.text, f"缺少字段 {name}"
        # 站点级 H&R 已迁至站点中心，设置页不再渲染
        assert 'name="site_hr_hours:hhanclub"' not in page.text
        # list 字段以逗号串回显
        assert "dispatched, completed" in page.text

    def test_paths_are_hot_scoped(self, client, cfg_dir):
        """路径组 v0.11 起热生效：运行时立即变、目录即建、不挂待重启标记。

        依据：归档/回收/隔离模块用时现读 config.paths（Path 在函数内构造），
        启动期没有固化点；保存后 apply_hot 顺带 mkdir 提前暴露权限问题。
        """
        _setup_admin(client)
        target = Path(cfg_dir) / "lib2-hot"
        resp = _post_settings(client, "/settings/paths",
                              {"library_music": str(target)})
        assert resp.status_code == 303, resp.text[:200]
        assert "ok=saved_paths" in resp.headers["location"]
        assert app.state.config.paths.library_music == str(target)
        assert target.is_dir(), "热生效路径应立即建目录"
        assert asyncio.run(store.is_restart_pending(app.state.db)) is False
        assert asyncio.run(store.load_overrides(app.state.db))["library_music"] == str(target)

    def test_validation_hot_and_list_roundtrip(self, client):
        """list 类型：表单逗号串 -> 运行时 list；库里存逗号串。"""
        _setup_admin(client)
        resp = _post_settings(client, "/settings/validation", {
            "allowed_exts": " flac, ape ,,mp3 ",
            "validation_on_fail": "pass_through",
            "validation_enabled": "0",
        })
        assert resp.status_code == 303, resp.text[:200]
        cfg = app.state.config.validation
        assert cfg.allowed_exts == ["flac", "ape", "mp3"]
        assert cfg.on_fail == "pass_through"
        assert cfg.enabled is False
        overrides = asyncio.run(store.load_overrides(app.state.db))
        assert overrides["allowed_exts"] == "flac,ape,mp3"

    def test_notify_kinds_rejects_unknown_kind(self, client):
        _setup_admin(client)
        before = list(app.state.config.notify.kinds)
        resp = _post_settings(client, "/settings/notify",
                              {"notify_kinds": "dispatched,typo_kind"})
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]
        assert app.state.config.notify.kinds == before

    def test_notify_kinds_empty_means_none(self, client):
        _setup_admin(client)
        resp = _post_settings(client, "/settings/notify", {"notify_kinds": ""})
        assert resp.status_code == 303, resp.text[:200]
        assert "ok=saved_notify" in resp.headers["location"]
        assert app.state.config.notify.kinds == []

    def test_seeding_float_and_restart_split(self, client):
        """seeding 组：ratio 是 float 热生效；check_interval 需重启。"""
        _setup_admin(client)
        resp = _post_settings(client, "/settings/seeding", {
            "default_min_ratio": "1.5",
            "seeding_check_interval_minutes": "10",
        })
        assert resp.status_code == 303, resp.text[:200]
        cfg = app.state.config.seeding
        assert cfg.default_min_ratio == 1.5
        assert cfg.check_interval_minutes == 30, "需重启项不应半应用"
        assert "ok=saved_seeding" in resp.headers["location"]
        assert asyncio.run(store.is_restart_pending(app.state.db)) is True

    def test_float_rejects_garbage(self, client):
        _setup_admin(client)
        for bad in ("abc", "nan", "-1"):
            resp = _post_settings(client, "/settings/seeding", {"default_min_ratio": bad})
            assert "err=" in resp.headers["location"], f"{bad!r} 不应被接受"

    def test_site_hr_nullable_set_and_clear(self, client):
        """站点 H&R 覆盖（sites 表）：填值生效；None＝清除；不让禁用站进适配器。"""
        _setup_admin(client)
        site = next(s for s in app.state.config.sites if s.name == "hhanclub")
        assert site.hr_min_seed_hours is None

        _set_site_hr("hhanclub", hr_min_seed_hours=48, hr_min_ratio=0.8)
        site = next(s for s in app.state.config.sites if s.name == "hhanclub")
        assert site.hr_min_seed_hours == 48
        assert site.hr_min_ratio == 0.8
        # 站点仍禁用，H&R 改动不应让它进入适配器
        assert "hhanclub" not in app.state.runner.adapters

        _set_site_hr("hhanclub", hr_min_seed_hours=None)
        site = next(s for s in app.state.config.sites if s.name == "hhanclub")
        assert site.hr_min_seed_hours is None
        assert site.hr_min_ratio == 0.8, "未改的字段不应被动"
        rec = asyncio.run(repo.get_site(app.state.db, "hhanclub"))
        assert rec.hr_min_seed_hours is None and rec.hr_min_ratio == 0.8
        # 旧覆盖层不再承载站点键
        overrides = asyncio.run(store.load_overrides(app.state.db))
        assert not any(k.startswith(store.SITE_HR_HOURS_PREFIX) for k in overrides)

    def test_site_hr_clear_survives_reload(self, cfg_dir):
        """清除后的 None 与设定值都从 sites 表还原，重启不被 YAML 打回。"""
        with TestClient(app) as first:
            _setup_admin(first)
            _set_site_hr("hhanclub", hr_min_seed_hours=48)
            _set_site_hr("hhanclub", hr_min_seed_hours=None)
            _set_site_hr("hdsky", hr_min_ratio=2.5)
        with TestClient(app) as second:
            assert second.get("/health").status_code == 200
            sites = {s.name: s for s in app.state.config.sites}
            assert sites["hhanclub"].hr_min_seed_hours is None
            assert sites["hdsky"].hr_min_ratio == 2.5

    def test_legacy_site_overrides_migrated_on_startup(self, cfg_dir):
        """v0.4/0.5 库里的 site_* 覆盖项：首启合并进 sites 表并从 app_settings 删除。"""
        from audio_hub.crypto import encrypt_value
        from audio_hub.db import init_db

        async def _plant():
            db = await init_db(cfg_dir / "hub.db")
            try:
                await store.save_override(db, f"{store.SITE_ENABLED_PREFIX}hhanclub", "1")
                await store.save_override(db, f"{store.SITE_HR_HOURS_PREFIX}hhanclub", "72")
                await store.save_override(
                    db, f"{store.SITE_RSS_PREFIX}hdsky",
                    encrypt_value("https://hdsky.me/torrentrss.php?passkey=LEGACY"),
                )
            finally:
                await db.close()
        asyncio.run(_plant())

        with TestClient(app) as c:
            assert c.get("/health").status_code == 200
            sites = {s.name: s for s in app.state.config.sites}
            assert sites["hhanclub"].enabled is True
            assert sites["hhanclub"].hr_min_seed_hours == 72
            assert "passkey=LEGACY" in sites["hdsky"].rss_url
            # 认证门禁（2A）：迁移进来的站点 auth_status 仍是 unconfigured，不会被构造；
            # 这里模拟认证通过后重建，验证迁移过来的 enabled / 凭据确实能成为可用适配器。
            assert "hhanclub" not in app.state.runner.adapters

            async def _mark_valid():
                await repo.set_site_auth(app.state.db, "hhanclub", AuthStatus.VALID)
                await store.rebuild_adapters(app)
            asyncio.run(_mark_valid())
            assert "hhanclub" in app.state.runner.adapters
            overrides = asyncio.run(store.load_overrides(app.state.db))
            assert not any(k.startswith(p) for k in overrides for p in store.SITE_KEY_PREFIXES)

    def test_scrape_secret_write_only(self, client):
        """ABS token 走 secret：加密入库、热生效到 config、页面不回显。"""
        _setup_admin(client)
        resp = _post_settings(client, "/settings/scrape", {
            "abs_token": "ABS_TOKEN_PLAINTEXT_42",
            "abs_url": "http://abs.local:13378",
        })
        assert resp.status_code == 303, resp.text[:200]
        assert app.state.config.scrape.abs_token == "ABS_TOKEN_PLAINTEXT_42"
        overrides = asyncio.run(store.load_overrides(app.state.db))
        assert "ABS_TOKEN_PLAINTEXT_42" not in overrides["abs_token"]
        page = client.get("/settings")
        assert "ABS_TOKEN_PLAINTEXT_42" not in page.text
        assert overrides["abs_token"] not in page.text
        # 凭据状态表显示已配置
        assert "Audiobookshelf" in page.text

    def test_scrape_abs_url_must_be_url(self, client):
        _setup_admin(client)
        resp = _post_settings(client, "/settings/scrape", {"abs_url": "abs.local"})
        assert "err=" in resp.headers["location"]

    def test_group_cannot_write_other_groups_keys(self, client):
        """往 /settings/notify 塞 paths 的键应被忽略（组间互斥）。"""
        _setup_admin(client)
        resp = _post_settings(client, "/settings/notify",
                              {"library_music": "/evil"})
        assert "err=" in resp.headers["location"]   # 没有本组的改动
        assert "library_music" not in asyncio.run(store.load_overrides(app.state.db))


# ── 密钥文件缺失：友好错误而非 500 ───────────────────────────

class TestSecretsKeyMissing:
    def test_saving_secret_without_key_file_flashes_error(self, client, cfg_dir, monkeypatch):
        """密钥文件不存在时保存凭据：303 + 可读 err，且不落库。

        这是 serve_dev.sh 上的真实事故：密钥生成静默失败后，每次填凭据都是裸 500。
        """
        _setup_admin(client)
        token = _csrf(client)
        monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(cfg_dir / "missing" / "secrets.key"))

        resp = client.post("/settings/notify", data={
            "wecom_webhook": "https://hooks.example.com/SOME_KEY", "csrf_token": token,
        }, follow_redirects=False)
        assert resp.status_code == 303, f"应重定向而非 {resp.status_code}: {resp.text[:200]}"
        assert "err=secrets_key" in resp.headers["location"]
        assert "wecom_webhook" not in asyncio.run(store.load_overrides(app.state.db))

        # 页面把键翻译成可操作的文案
        page = client.get("/settings?err=secrets_key")
        assert page.status_code == 200
        assert "密钥文件不存在" in page.text

    def test_non_secret_save_still_works_without_key_file(self, client, cfg_dir, monkeypatch):
        """非凭据字段不走加密，密钥缺失不应影响。"""
        _setup_admin(client)
        token = _csrf(client)
        monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(cfg_dir / "missing.key"))
        resp = client.post("/settings/general", data={
            "poll_interval_minutes": "50", "csrf_token": token,
        }, follow_redirects=False)
        assert resp.status_code == 303
        assert "ok=saved_general" in resp.headers["location"]


# ── 通知推送：保存后热重建 sink + 测试通知端点 ────────────────────────

_WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TESTKEY"
_GENERIC = "https://hooks.example.com/audio-hub"


class TestNotifyPush:
    def test_page_has_test_button_form(self, client):
        _setup_admin(client)
        page = client.get("/settings").text
        assert 'action="/settings/notify/test"' in page
        assert "发送测试通知" in page

    def test_save_notify_rebuilds_push_sinks(self, client):
        """保存通知组后 app.state.notifier 的推送 sink 立即按新配置重建。"""
        from audio_hub.notify import GenericWebhookNotifier, WecomNotifier

        _setup_admin(client)
        notifier = app.state.notifier
        assert notifier.push_sinks == []

        resp = _post_settings(client, "/settings/notify", {"wecom_webhook": _WECOM})
        assert resp.status_code == 303, resp.text[:200]
        assert "ok=saved_notify" in resp.headers["location"]
        assert [type(s) for s in notifier.push_sinks] == [WecomNotifier]
        assert notifier.push_sinks[0].webhook_url == _WECOM

        # 再加一个通用 webhook：整体替换而非累加，基础 sink（日志 / DB）仍在
        base_before = [s for s in notifier.sinks if not getattr(s, "is_push", False)]
        _post_settings(client, "/settings/notify", {"generic_webhook_url": _GENERIC})
        assert [type(s) for s in notifier.push_sinks] == [WecomNotifier, GenericWebhookNotifier]
        assert [s for s in notifier.sinks if not getattr(s, "is_push", False)] == base_before

    def test_notify_test_requires_csrf_and_admin(self, client):
        _setup_admin(client)
        resp = client.post("/settings/notify/test", follow_redirects=False)
        assert resp.status_code == 403

    def test_notify_test_without_channels(self, client):
        _setup_admin(client)
        resp = _post_settings(client, "/settings/notify/test", {})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings?err=notify_none"
        page = client.get(resp.headers["location"])
        assert "尚未配置任何推送渠道" in page.text

    @respx.mock
    def test_notify_test_all_success(self, client):
        wecom = respx.post(_WECOM).mock(return_value=httpx.Response(200, json={"errcode": 0}))
        generic = respx.post(_GENERIC).mock(return_value=httpx.Response(204))
        _setup_admin(client)
        _post_settings(client, "/settings/notify",
                       {"wecom_webhook": _WECOM, "generic_webhook_url": _GENERIC})

        resp = _post_settings(client, "/settings/notify/test", {})
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/settings?ok=notify_test&detail=")
        assert wecom.called and generic.called

        page = client.get(loc).text
        assert "测试通知已发送" in page
        assert "企业微信: HTTP 200" in page and "通用 Webhook: HTTP 204" in page
        # 凭据不得出现在 URL 或页面中
        assert "TESTKEY" not in loc and "TESTKEY" not in page

    @respx.mock
    def test_notify_test_partial_failure(self, client):
        respx.post(_WECOM).mock(return_value=httpx.Response(500))
        respx.post(_GENERIC).mock(return_value=httpx.Response(200))
        _setup_admin(client)
        _post_settings(client, "/settings/notify",
                       {"wecom_webhook": _WECOM, "generic_webhook_url": _GENERIC})

        resp = _post_settings(client, "/settings/notify/test", {})
        loc = resp.headers["location"]
        assert loc.startswith("/settings?err=notify_test_failed&detail=")

        page = client.get(loc).text
        assert "测试通知未全部成功" in page
        assert "企业微信: HTTP 500" in page and "通用 Webhook: HTTP 200" in page
        assert "TESTKEY" not in page

    @respx.mock
    def test_notify_test_ignores_kinds_filter(self, client):
        """kinds 留空（全部不推）时测试通知仍要真发出去。"""
        generic = respx.post(_GENERIC).mock(return_value=httpx.Response(200))
        _setup_admin(client)
        _post_settings(client, "/settings/notify",
                       {"generic_webhook_url": _GENERIC, "notify_kinds": ""})
        assert app.state.config.notify.kinds == []
        resp = _post_settings(client, "/settings/notify/test", {})
        assert "ok=notify_test" in resp.headers["location"]
        assert generic.called
