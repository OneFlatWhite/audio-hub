"""bootstrap：CLI / 诊断入口的运行时装配（DB 权威源 + 纯 YAML 降级）。

v0.6 起站点 / 下载器以 DB 为准；本文件锁死两条路径的行为：
- create_db=True（run-once）：库不存在就建，与服务启动一致；
- create_db=False（verify-adapters / check_qb）：库不存在降级纯 YAML，不建库。
"""

from __future__ import annotations

import pytest

from audio_hub import repo
from audio_hub.bootstrap import bootstrap_runtime, default_downloader
from audio_hub.config import AppConfig, DownloaderConfig
from audio_hub.models import AuthStatus

RSS = "https://hhanclub.net/torrentrss.php?rows=50&passkey=TOPSECRET123"


@pytest.fixture(autouse=True)
def _key(tmp_key, monkeypatch):
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(tmp_key))


def _config(db_path: str, **overrides) -> AppConfig:
    base = {
        "dry_run": True,
        "db_path": db_path,
        "qbittorrent": {"host": "qb.local", "port": 8085, "api_key": "QBKEY",
                        "save_path": "/seeds"},
        "sites": [{"name": "hhanclub", "enabled": True, "rss_url": RSS}],
    }
    base.update(overrides)
    return AppConfig(**base)


class TestYamlFallback:
    async def test_missing_db_degrades_to_yaml(self, tmp_path):
        db_path = tmp_path / "missing.db"
        config = _config(str(db_path))
        ctx = await bootstrap_runtime(config)
        assert ctx.db is None
        assert ctx.source == "yaml"
        assert ctx.notes  # 降级说明，供 CLI / 脚本打印
        assert not db_path.exists()  # 诊断路径不得在全新环境凭空建库
        # 站点与下载器保持 YAML 原值（auth_status 恒为默认 unconfigured，
        # 故诊断入口须显式 require_valid_auth=False 绕过门禁）
        assert [s.name for s in ctx.config.sites] == ["hhanclub"]
        assert ctx.config.sites[0].auth_status == "unconfigured"
        assert ctx.downloaders == [config.qbittorrent]

    async def test_create_db_true_initializes(self, tmp_path):
        db_path = tmp_path / "hub.db"
        ctx = await bootstrap_runtime(_config(str(db_path)), create_db=True)
        try:
            assert ctx.db is not None
            assert ctx.source == "db"
            assert db_path.exists()
        finally:
            await ctx.close()


class TestDbSource:
    async def test_sites_seeded_then_reloaded_from_db(self, tmp_path):
        db_path = str(tmp_path / "hub.db")
        ctx = await bootstrap_runtime(_config(db_path), create_db=True)
        try:
            # YAML 种子导入后站点改由 DB 供给（含解密凭据）
            assert [s.name for s in ctx.config.sites] == ["hhanclub"]
            assert ctx.config.sites[0].rss_url == RSS
            # 巡检写回 DB 的认证状态，再次装配能拿到 —— run-once 的门禁依据
            await repo.set_site_auth(ctx.db, "hhanclub", AuthStatus.VALID)
        finally:
            await ctx.close()

        ctx2 = await bootstrap_runtime(_config(db_path))
        try:
            assert ctx2.source == "db"
            assert ctx2.config.sites[0].auth_status == AuthStatus.VALID.value
        finally:
            await ctx2.close()

    async def test_downloaders_seeded_with_decrypted_secret(self, tmp_path):
        ctx = await bootstrap_runtime(_config(str(tmp_path / "hub.db")), create_db=True)
        try:
            assert len(ctx.downloaders) == 1
            dl = ctx.downloaders[0]
            assert dl.id == "default" and dl.is_default
            assert dl.api_key == "QBKEY"  # 库里是密文，装配结果是明文
            row = (await repo.list_downloader_rows(ctx.db))[0]
            assert "QBKEY" not in row["api_key_enc"]
        finally:
            await ctx.close()

    async def test_downloader_table_overrides_yaml(self, tmp_path):
        """/downloaders 页改过的实例以表为准，不被 YAML 旧值盖掉。"""
        db_path = str(tmp_path / "hub.db")
        ctx = await bootstrap_runtime(_config(db_path), create_db=True)
        try:
            await repo.upsert_downloader(ctx.db, DownloaderConfig(
                id="default", host="nas.local", port=9090,
                username="admin", password="dbpass", is_default=True,
            ))
        finally:
            await ctx.close()

        ctx2 = await bootstrap_runtime(_config(db_path))
        try:
            dl = default_downloader(ctx2.downloaders)
            assert (dl.host, dl.port) == ("nas.local", 9090)
            assert dl.username == "admin" and dl.password == "dbpass"
        finally:
            await ctx2.close()


class TestDefaultDownloader:
    def test_prefers_is_default(self):
        a = DownloaderConfig(id="a", priority=99)
        b = DownloaderConfig(id="b", is_default=True)
        assert default_downloader([a, b]).id == "b"

    def test_falls_back_to_highest_priority_enabled(self):
        a = DownloaderConfig(id="a", priority=1)
        b = DownloaderConfig(id="b", priority=9)
        c = DownloaderConfig(id="c", priority=99, enabled=False)
        assert default_downloader([a, b, c]).id == "b"

    def test_empty_or_all_disabled(self):
        assert default_downloader([]) is None
        assert default_downloader([DownloaderConfig(id="a", enabled=False)]) is None
