"""scripts/check_qb.py：凭据解析与认证顺序（不触网：probe 与 qb 客户端均 mock）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from audio_hub import registry_store, repo
from audio_hub.config import AppConfig, DownloaderConfig
from audio_hub.db import init_db


def _load_check_qb():
    """scripts/ 不是包，按路径加载模块。"""
    spec = importlib.util.spec_from_file_location(
        "check_qb", Path(__file__).resolve().parents[2] / "scripts" / "check_qb.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_qb = _load_check_qb()


@pytest.fixture(autouse=True)
def _key(tmp_key, monkeypatch):
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(tmp_key))


def _config(db_path: str, **qb_overrides) -> AppConfig:
    qb = {"host": "qb.local", "port": 8085, "api_key": "YAMLKEY", "save_path": "/seeds"}
    qb.update(qb_overrides)
    return AppConfig(dry_run=True, db_path=db_path, qbittorrent=qb, sites=[])


async def _seed_db(db_path: Path, **qb_overrides) -> None:
    db = await init_db(db_path)
    try:
        await registry_store.seed_from_yaml(db, _config(str(db_path), **qb_overrides))
    finally:
        await db.close()


class TestResolveEffectiveDownloader:
    async def test_no_db_falls_back_to_yaml(self, tmp_path):
        config = _config(str(tmp_path / "missing.db"))
        dl, source, _ = check_qb.resolve_effective_downloader(config)
        assert source == "yaml"
        assert dl is config.qbittorrent
        assert not (tmp_path / "missing.db").exists()  # 只读：诊断不得建库

    async def test_seeded_db_default_wins(self, tmp_path):
        db_path = tmp_path / "hub.db"
        await _seed_db(db_path)
        dl, source, _ = check_qb.resolve_effective_downloader(_config(str(db_path)))
        assert source == "db"
        assert dl.id == "default" and dl.is_default
        assert dl.api_key == "YAMLKEY"  # 密文落库、读出解密

    async def test_db_credentials_override_yaml(self, tmp_path):
        """/downloaders 页改过的凭据以表为准，不被 YAML 旧值盖掉。"""
        db_path = tmp_path / "hub.db"
        await _seed_db(db_path)
        db = await init_db(db_path)
        try:
            await repo.upsert_downloader(db, DownloaderConfig(
                id="default", host="nas.local", port=9090,
                username="admin", password="dbpass", is_default=True,
            ))
        finally:
            await db.close()
        dl, source, _ = check_qb.resolve_effective_downloader(_config(str(db_path)))
        assert source == "db"
        assert (dl.host, dl.port) == ("nas.local", 9090)
        assert dl.username == "admin" and dl.password == "dbpass"

    async def test_empty_table_falls_back_to_yaml_with_note(self, tmp_path):
        db_path = tmp_path / "hub.db"
        db = await init_db(db_path)  # 只建库不种子
        await db.close()
        _dl, source, notes = check_qb.resolve_effective_downloader(_config(str(db_path)))
        assert source == "yaml"
        assert notes  # 有说明，避免误以为验的是 downloaders 表


class TestQbAuthKwargs:
    """认证顺序与 main._make_qb_client 一致：账密优先，API Key 兜底。"""

    def test_password_preferred(self):
        dl = DownloaderConfig(username="u", password="p", api_key="K")
        kwargs, label = check_qb.qb_auth_kwargs(dl)
        assert kwargs == {"username": "u", "password": "p"}
        assert label == "账密"

    def test_api_key_fallback(self):
        kwargs, label = check_qb.qb_auth_kwargs(DownloaderConfig(api_key="K"))
        assert kwargs == {"api_key": "K"}
        assert label == "API Key"

    def test_no_credentials(self):
        assert check_qb.qb_auth_kwargs(DownloaderConfig()) == ({}, "")


class TestMainSmoke:
    """main() 端到端冒烟：probe 与 qbittorrentapi.Client 全部 mock，不触网。"""

    def _write_config(self, tmp_path: Path, db_name: str = "missing.db") -> Path:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(f"""
dry_run: true
db_path: "{(tmp_path / db_name).as_posix()}"
qbittorrent:
  host: "qb.local"
  port: 8085
  save_path: "/seeds"
sites: []
""", encoding="utf-8")
        return cfg_file

    def _mock_network(self, monkeypatch, captured: dict | None = None):
        from audio_hub.downloaders import probe
        monkeypatch.setattr(probe, "probe_tcp", lambda h, p: (True, ""))
        monkeypatch.setattr(probe, "probe_http", lambda h, p: (200, "HTTP 200"))

        import qbittorrentapi

        class FakeClient:
            def __init__(self, **kwargs):
                if captured is not None:
                    captured.update(kwargs)

            def auth_log_in(self):
                return None

            @property
            def app(self):
                return SimpleNamespace(version="5.2.0", web_api_version="2.11")

            def app_default_save_path(self):
                return "/volume1/musiclab/downloads"

            def torrents_info(self, limit=20):
                return []

            def auth_log_out(self):
                return None

        monkeypatch.setattr(qbittorrentapi, "Client", FakeClient)

    def test_no_credentials_skips_auth_with_downloaders_guidance(
        self, tmp_path, monkeypatch, capsys,
    ):
        """无凭据环境：跑到第⑤步给出 /downloaders 指引，而不是误报 api_key 未配置。"""
        cfg_file = self._write_config(tmp_path)
        monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg_file))
        self._mock_network(monkeypatch)

        rc = check_qb.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "未配置凭据" in out
        assert "/downloaders" in out
        assert not (tmp_path / "missing.db").exists()  # 全程未建库

    async def test_main_auths_with_db_password_credentials(
        self, tmp_path, monkeypatch, capsys,
    ):
        """DB 有账密默认下载器：main 用账密认证（对齐服务行为），并打印来源。"""
        db_path = tmp_path / "hub.db"
        await _seed_db(db_path, username="admin", password="dbpass", api_key="")
        cfg_file = self._write_config(tmp_path, db_name="hub.db")
        monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg_file))
        captured: dict = {}
        self._mock_network(monkeypatch, captured)

        rc = check_qb.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        # 凭据来自 downloaders 表且账密优先（Client 收到的不是 api_key）
        assert captured.get("username") == "admin"
        assert captured.get("password") == "dbpass"
        assert "api_key" not in captured
        assert "downloaders 表" in out
        assert "认证成功（账密）" in out
