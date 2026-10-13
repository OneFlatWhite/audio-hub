"""registry_store：YAML → sites / downloaders 的首启种子、旧覆盖项合并、DB → 配置对象。

这些测试同时承接了原 test_site_credentials / test_settings_routes 中「站点凭据只写不读」
的断言（v0.6 起站点配置不再走 app_settings 覆盖层，而是 sites 表）。
"""

from __future__ import annotations

import pytest

from audio_hub import registry_store, repo
from audio_hub import settings_store as store
from audio_hub.config import AppConfig, DownloaderConfig, SiteConfig
from audio_hub.crypto import decrypt_value, encrypt_value
from audio_hub.models import AuthStatus, Task, TaskState

pytestmark = pytest.mark.asyncio

RSS = "https://hhanclub.net/torrentrss.php?rows=50&passkey=TOPSECRET123"


@pytest.fixture(autouse=True)
def _key(tmp_key, monkeypatch):
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(tmp_key))


def _config(**overrides) -> AppConfig:
    base = {
        "dry_run": True,
        "db_path": "/tmp/x.db",
        "qbittorrent": {"host": "qb.local", "port": 8085, "api_key": "QBKEY",
                        "save_path": "/seeds", "category": "audio-hub", "tag": "hub"},
        "sites": [
            {"name": "hhanclub", "enabled": False, "rss_url": RSS, "music_cat_id": "410, 411"},
            {"name": "mteam", "enabled": False, "api_key": "MTOKEN",
             "search_url": "https://api.m-team.cc/api/torrent/search"},
        ],
    }
    base.update(overrides)
    return AppConfig(**base)


async def _raw_site(db, site_id):
    async with db.execute("SELECT * FROM sites WHERE id = ?", (site_id,)) as cur:
        return await cur.fetchone()


# ── 表空：全量导入 ──────────────────────────────────────────

class TestSeedEmpty:
    async def test_imports_all_sites(self, test_db):
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["sites_added"] == 2
        ids = {s.id for s in await repo.list_sites(test_db)}
        assert ids == {"hhanclub", "mteam"}

    async def test_adapter_type_inferred(self, test_db):
        await registry_store.seed_from_yaml(test_db, _config())
        assert (await repo.get_site(test_db, "hhanclub")).adapter_type == "nexusphp"
        assert (await repo.get_site(test_db, "mteam")).adapter_type == "mteam"
        assert registry_store.adapter_type_for("馒头") == "mteam"
        assert registry_store.adapter_type_for("unknown_pt") == "nexusphp"

    async def test_secrets_encrypted_not_plaintext(self, test_db):
        """整条 RSS 地址当凭据加密，明文不落库（原 test_rss_url_stored_encrypted）。"""
        await registry_store.seed_from_yaml(test_db, _config())
        row = await _raw_site(test_db, "hhanclub")
        assert row["rss_url_enc"] and "TOPSECRET123" not in row["rss_url_enc"]
        assert decrypt_value(row["rss_url_enc"]) == RSS
        mt = await _raw_site(test_db, "mteam")
        assert "MTOKEN" not in (mt["api_key_enc"] or "")
        assert decrypt_value(mt["api_key_enc"]) == "MTOKEN"

    async def test_site_record_has_no_plaintext(self, test_db):
        await registry_store.seed_from_yaml(test_db, _config())
        rec = await repo.get_site(test_db, "hhanclub")
        dumped = rec.model_dump_json()
        assert "TOPSECRET123" not in dumped
        assert "passkey" not in dumped
        assert rec.has_rss is True
        assert rec.has_api_key is False
        assert rec.has_cookie is False
        assert rec.auth_status is AuthStatus.UNCONFIGURED
        assert rec.music_cat_ids == ["410", "411"]
        for field in ("rss_url", "api_key", "cookie", "rss_url_enc", "api_key_enc"):
            assert field not in type(rec).model_fields

    async def test_get_site_secret_decrypts(self, test_db):
        await registry_store.seed_from_yaml(test_db, _config())
        assert await repo.get_site_secret(test_db, "hhanclub", "rss_url") == RSS
        assert await repo.get_site_secret(test_db, "mteam", "api_key") == "MTOKEN"
        assert await repo.get_site_secret(test_db, "hhanclub", "cookie") == ""
        assert await repo.get_site_secret(test_db, "nope", "rss_url") == ""
        with pytest.raises(ValueError):
            await repo.get_site_secret(test_db, "hhanclub", "rss_url_enc")

    async def test_load_site_configs_roundtrip(self, test_db):
        await registry_store.seed_from_yaml(test_db, _config())
        sites = {s.name: s for s in await registry_store.load_site_configs(test_db)}
        assert sites["hhanclub"].rss_url == RSS
        assert sites["hhanclub"].music_cat_ids == ["410", "411"]
        assert sites["mteam"].api_key == "MTOKEN"
        assert sites["mteam"].search_url.startswith("https://api.m-team.cc")
        assert sites["mteam"].enabled is False

    async def test_downloader_default_imported(self, test_db):
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["downloader_added"] is True
        d = await repo.get_default_downloader(test_db)
        assert d is not None
        assert d.id == "default"
        assert d.name == "默认 qBittorrent"
        assert d.is_default is True and d.enabled is True
        assert d.host == "qb.local" and d.port == 8085
        assert d.has_api_key is True and d.has_password is False
        assert "QBKEY" not in d.model_dump_json()
        cfgs = await registry_store.load_downloader_configs(test_db)
        assert len(cfgs) == 1 and cfgs[0].api_key == "QBKEY"
        assert await repo.get_downloader_secret(test_db, "default", "api_key") == "QBKEY"

    async def test_tasks_backfilled_with_default_downloader(self, test_db):
        await repo.create_task(test_db, Task(
            info_hash="h1", site="hhanclub", title="t1", state=TaskState.DISPATCHED,
        ))
        await test_db.execute("UPDATE tasks SET downloader_id = NULL")
        await test_db.commit()
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["tasks_backfilled"] == 1
        async with test_db.execute("SELECT downloader_id FROM tasks") as cur:
            assert (await cur.fetchone())[0] == "default"
        assert await repo.count_active_tasks_by_downloader(test_db) == {"default": 1}


# ── 表非空：不覆盖，只补新站 ─────────────────────────────────

class TestSeedNonEmpty:
    async def test_existing_rows_not_overwritten(self, test_db):
        await registry_store.seed_from_yaml(test_db, _config())
        # 界面改动：启用 + 改凭据
        await repo.set_site_enabled(test_db, "hhanclub", True)
        await repo.upsert_site(test_db, SiteConfig(
            name="hhanclub", enabled=True, rss_url="https://hhanclub.net/rss?passkey=NEW",
        ))
        # 第二次启动：YAML 仍是旧值 + 禁用
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["sites_added"] == 0
        assert stats["downloader_added"] is False
        rec = await repo.get_site(test_db, "hhanclub")
        assert rec.enabled is True, "YAML 把界面改动打回去了"
        secret = await repo.get_site_secret(test_db, "hhanclub", "rss_url")
        assert secret.endswith("passkey=NEW")

    async def test_new_yaml_site_added(self, test_db):
        await registry_store.seed_from_yaml(test_db, _config())
        cfg = _config()
        cfg.sites.append(SiteConfig(name="hdsky", rss_url="https://hdsky.me/rss?passkey=K"))
        stats = await registry_store.seed_from_yaml(test_db, cfg)
        assert stats["sites_added"] == 1
        assert {s.id for s in await repo.list_sites(test_db)} == {"hhanclub", "mteam", "hdsky"}

    async def test_downloader_not_reimported(self, test_db):
        await registry_store.seed_from_yaml(test_db, _config())
        await repo.upsert_downloader(test_db, DownloaderConfig(
            id="default", name="改过名", host="other.host", is_default=True,
        ))
        await registry_store.seed_from_yaml(test_db, _config())
        d = await repo.get_downloader(test_db, "default")
        assert d.name == "改过名" and d.host == "other.host"
        # 留空的凭据保留
        assert await repo.get_downloader_secret(test_db, "default", "api_key") == "QBKEY"


# ── 旧 app_settings 覆盖项合并 ──────────────────────────────

class TestLegacyOverrides:
    async def _seed_legacy(self, db, pairs: dict[str, str]):
        for k, v in pairs.items():
            await store.save_override(db, k, v)

    async def test_overrides_merged_then_deleted(self, test_db):
        legacy_rss = "https://hhanclub.net/torrentrss.php?passkey=FROMUI"
        await self._seed_legacy(test_db, {
            f"{store.SITE_ENABLED_PREFIX}hhanclub": "1",
            f"{store.SITE_RSS_PREFIX}hhanclub": encrypt_value(legacy_rss),
            f"{store.SITE_APIKEY_PREFIX}mteam": encrypt_value("UI_MTOKEN"),
            f"{store.SITE_HR_HOURS_PREFIX}hhanclub": "48",
            f"{store.SITE_HR_RATIO_PREFIX}hhanclub": "",
            "poll_interval_minutes": "45",     # 非站点键必须原样保留
        })
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["overrides_merged"] == 2

        hh = await repo.get_site(test_db, "hhanclub")
        assert hh.enabled is True
        assert hh.hr_min_seed_hours == 48
        assert hh.hr_min_ratio is None, "空串覆盖应还原为 None"
        assert await repo.get_site_secret(test_db, "hhanclub", "rss_url") == legacy_rss
        assert await repo.get_site_secret(test_db, "mteam", "api_key") == "UI_MTOKEN"

        overrides = await store.load_overrides(test_db)
        assert not any(k.startswith(p) for k in overrides for p in store.SITE_KEY_PREFIXES), \
            f"旧站点键未删除: {sorted(overrides)}"
        assert overrides["poll_interval_minutes"] == "45"

    async def test_override_for_unknown_site_dropped(self, test_db):
        await self._seed_legacy(test_db, {f"{store.SITE_ENABLED_PREFIX}ghost": "1"})
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["overrides_merged"] == 0
        assert await repo.get_site(test_db, "ghost") is None
        assert f"{store.SITE_ENABLED_PREFIX}ghost" not in await store.load_overrides(test_db)

    async def test_merge_is_one_shot(self, test_db):
        await self._seed_legacy(test_db, {f"{store.SITE_ENABLED_PREFIX}hhanclub": "1"})
        await registry_store.seed_from_yaml(test_db, _config())
        await repo.set_site_enabled(test_db, "hhanclub", False)
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["overrides_merged"] == 0
        assert (await repo.get_site(test_db, "hhanclub")).enabled is False


# ── repo 站点写入语义：留空不覆盖、H&R 可清除 ───────────────

class TestUpsertSiteSemantics:
    async def test_empty_secret_does_not_overwrite(self, test_db):
        """留空＝不修改（原 test_empty_secret_does_not_overwrite）。"""
        await repo.upsert_site(test_db, SiteConfig(name="hhanclub", rss_url=RSS))
        await repo.upsert_site(test_db, SiteConfig(name="hhanclub", rss_url=""))
        assert await repo.get_site_secret(test_db, "hhanclub", "rss_url") == RSS
        await repo.upsert_site(
            test_db, SiteConfig(name="hhanclub", rss_url=""), overwrite_empty_secrets=True,
        )
        assert await repo.get_site_secret(test_db, "hhanclub", "rss_url") == ""
        assert (await repo.get_site(test_db, "hhanclub")).has_rss is False

    async def test_hr_override_set_and_clear(self, test_db):
        """站点 H&R：填值生效；None 清除（原 test_site_hr_nullable_set_and_clear）。"""
        await repo.upsert_site(test_db, SiteConfig(
            name="hhanclub", hr_min_seed_hours=48, hr_min_ratio=0.8,
        ))
        s = (await registry_store.load_site_configs(test_db))[0]
        assert s.hr_min_seed_hours == 48 and s.hr_min_ratio == 0.8

        await repo.upsert_site(test_db, SiteConfig(
            name="hhanclub", hr_min_seed_hours=None, hr_min_ratio=0.8,
        ))
        s = (await registry_store.load_site_configs(test_db))[0]
        assert s.hr_min_seed_hours is None
        assert s.hr_min_ratio == 0.8

    async def test_update_keeps_auth_fields(self, test_db):
        await repo.upsert_site(test_db, SiteConfig(name="hhanclub", rss_url=RSS))
        await repo.set_site_auth(test_db, "hhanclub", AuthStatus.VALID)
        await repo.upsert_site(test_db, SiteConfig(name="hhanclub", enabled=True))
        rec = await repo.get_site(test_db, "hhanclub")
        assert rec.auth_status is AuthStatus.VALID
        assert rec.enabled is True

    async def test_delete_site_removes_categories(self, test_db):
        from audio_hub.models import SiteCategory

        await repo.upsert_site(test_db, SiteConfig(name="hhanclub"))
        await repo.replace_site_categories(test_db, "hhanclub", [
            SiteCategory(site_id="hhanclub", remote_id="410", remote_name="Music",
                         local_kind="music"),
        ])
        assert len(await repo.list_site_categories(test_db, "hhanclub")) == 1
        assert await repo.delete_site(test_db, "hhanclub") is True
        assert await repo.list_site_categories(test_db, "hhanclub") == []
        assert await repo.get_site(test_db, "hhanclub") is None



# ── 旧设置页 qBittorrent 组键收编（v0.6.0）─────────────────────

class TestLegacyQbKeys:
    """存量 app_settings 的 qb_* 键必须收编进默认下载器行并删除。

    陷阱：v0.6.0 撤下设置页 qb 组后 hydrate 不再认识这组键，若不收编，
    老用户在旧设置页改过的连接参数会静默失效——界面没有入口，运行时也不读。
    """

    async def _seed_legacy(self, db, pairs: dict[str, str]):
        for k, v in pairs.items():
            await store.save_override(db, k, v)

    async def test_qb_keys_merged_into_default_downloader(self, test_db):
        await self._seed_legacy(test_db, {
            "qb_host": "qb-nas.local",
            "qb_port": "9090",
            "qb_save_path": "/ui/seeds",
            "qb_category": "ui-cat",
            "qb_tag": "ui-tag",
            "qb_api_key": encrypt_value("UI_QBKEY"),
            "poll_interval_minutes": "45",   # 非 qb 键必须原样保留
        })
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["qb_overrides_merged"] == 6

        row = await repo.get_default_downloader(test_db)
        assert row.host == "qb-nas.local"
        assert row.port == 9090
        assert row.save_path == "/ui/seeds"
        assert row.category == "ui-cat"
        assert row.tag == "ui-tag"
        assert await repo.get_downloader_secret(test_db, row.id, "api_key") == "UI_QBKEY"

        overrides = await store.load_overrides(test_db)
        assert not any(k.startswith("qb_") for k in overrides), \
            f"旧 qb_* 键未删除: {sorted(overrides)}"
        assert overrides["poll_interval_minutes"] == "45"

    async def test_bad_port_dropped_but_key_deleted(self, test_db):
        """端口不是数字：丢弃该值（不阻断其他键），但旧键照样删除。"""
        await self._seed_legacy(test_db, {"qb_port": "not-a-port"})
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["qb_overrides_merged"] == 1
        row = await repo.get_default_downloader(test_db)
        assert row.port == 8085, "坏端口不应覆盖 YAML 种子值"
        assert "qb_port" not in await store.load_overrides(test_db)

    async def test_merge_is_one_shot(self, test_db):
        """幂等：收编并删除后，第二次种子不再动下载器行。"""
        await self._seed_legacy(test_db, {"qb_host": "qb-nas.local"})
        await registry_store.seed_from_yaml(test_db, _config())
        assert (await repo.get_default_downloader(test_db)).host == "qb-nas.local"

        # 用户在下载器中心改过之后，重跑种子不得再覆盖
        dl = await repo.get_default_downloader(test_db)
        await repo.upsert_downloader(
            test_db,
            DownloaderConfig(id=dl.id, name=dl.name, host="edited.local", port=dl.port),
        )
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["qb_overrides_merged"] == 0
        assert (await repo.get_default_downloader(test_db)).host == "edited.local"

    async def test_no_legacy_keys_is_noop(self, test_db):
        stats = await registry_store.seed_from_yaml(test_db, _config())
        assert stats["qb_overrides_merged"] == 0
