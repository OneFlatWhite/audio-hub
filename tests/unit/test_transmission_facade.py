"""Transmission 下载器适配：Facade 词表翻译 / 工厂分流 / 探测分流 / 表单与持久化。

全程用假 transmission 客户端（FakeTrClient），不发任何真实网络请求。
核心约定：下游（dispatcher / monitor / lifecycle）只认 qbittorrentapi 的方法名、
状态词表与异常类 —— TransmissionFacade 负责把 Transmission 的语义翻译过去，
例如 duplicate → Conflict409Error（复用 qb 异常类保持 dispatcher 幂等分支不变）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import qbittorrentapi

from audio_hub.config import DownloaderConfig
from audio_hub.downloaders import probe

# ── 假 transmission 客户端 ──────────────────────────────────

class FakeTrClient:
    """transmission_rpc.Client 假身：记录调用，按类属性表演异常。"""

    def __init__(self, torrents=(), add_error=None, session=None, free_space=1073741824):
        self.added: list[dict] = []
        self.stopped: list = []
        self.started: list = []
        self._torrents = list(torrents)
        self.add_error = add_error
        self.session = session or SimpleNamespace(version="4.0.6", download_dir="/downloads", rpc_version=17)
        self._free_space = free_space

    def add_torrent(self, torrent, download_dir=None, labels=None, paused=False, **kw):
        if self.add_error is not None:
            raise self.add_error
        self.added.append({
            "torrent": torrent, "download_dir": download_dir,
            "labels": list(labels or []), "paused": paused,
        })
        return SimpleNamespace(id=1)

    def get_torrents(self):
        return list(self._torrents)

    def get_session(self):
        return self.session

    def stop_torrent(self, ids):
        self.stopped.append(ids)

    def start_torrent(self, ids):
        self.started.append(ids)

    def free_space(self, path):
        return self._free_space


def _tr_torrent(**kw) -> SimpleNamespace:
    """形似 transmission_rpc.Torrent 的对象（属性词表与 7.x 一致）。"""
    base = {
        "hash_string": "ABCDEF0123456789",
        "name": "Album (2024)",
        "status": "downloading",
        "percent_done": 0.5,
        "download_dir": "/downloads",
        "error": 0,
        "seconds_seeding": 0,
        "upload_ratio": 0.0,
        "labels": ["hub"],
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _cfg(**kw) -> DownloaderConfig:
    base = {"id": "nas-tr", "name": "NAS Transmission", "host": "192.168.31.40",
            "port": 9091, "username": "admin", "password": "pw",
            "save_path": "/downloads/hub"}
    base.update(kw)
    return DownloaderConfig(**base)


@pytest.fixture
def _key(tmp_key, monkeypatch):
    """repo.upsert 的加密链路需要密钥文件环境（与 test_registry_store 同款）。"""
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(tmp_key))


# ── 配置与持久化：type 字段 ─────────────────────────────────

def test_downloader_config_type_default():
    cfg = DownloaderConfig()
    assert cfg.type == "qbittorrent"


@pytest.mark.asyncio
async def test_upsert_and_load_roundtrip_type(test_db, _key):
    from audio_hub import repo

    await repo.upsert_downloader(test_db, _cfg(type="transmission"))
    rows = await repo.list_downloader_rows(test_db)
    cfgs = [repo.downloader_config_from_row(r) for r in rows]
    assert [c.type for c in cfgs] == ["transmission"]
    recs = await repo.list_downloaders(test_db)
    assert recs[0].type == "transmission"


@pytest.mark.asyncio
async def test_upsert_qb_keeps_legacy_rows_compatible(test_db, _key):
    """不传 type 时落库仍是 qbittorrent（旧表单 / 旧代码路径不受影响）。"""
    from audio_hub import repo

    await repo.upsert_downloader(test_db, _cfg(id="nas-qb"))
    recs = await repo.list_downloaders(test_db)
    assert recs[0].type == "qbittorrent"


# ── 表单解析 ────────────────────────────────────────────────

def test_parse_form_accepts_transmission():
    from audio_hub.web.routes_downloaders import _parse_form

    cfg = _parse_form(
        downloader_id="nas-tr", name="TR", host="192.168.31.40", port="9091",
        username="admin", password="pw", api_key="", save_path="/downloads/hub",
        category="", tag="hub", priority="0", dl_type="transmission", current=None,
    )
    assert cfg.type == "transmission"


def test_parse_form_rejects_unknown_type():
    from audio_hub.web.routes_downloaders import _FormError, _parse_form

    with pytest.raises(_FormError) as ei:
        _parse_form(
            downloader_id="x", name="X", host="h", port="9091", username="", password="",
            api_key="", save_path="/d", category="", tag="", priority="0",
            dl_type="aria2", current=None,
        )
    assert ei.value.key == "bad_type"


def test_parse_form_defaults_to_qbittorrent():
    from audio_hub.web.routes_downloaders import _parse_form

    cfg = _parse_form(
        downloader_id="nas-qb", name="QB", host="192.168.31.40", port="8085",
        username="admin", password="pw", api_key="", save_path="/musiclab/seeds",
        category="audio-hub", tag="hub", priority="0", dl_type="", current=None,
    )
    assert cfg.type == "qbittorrent"


# ── TransmissionFacade：torrents_add ────────────────────────

def _facade(**fk) -> tuple[object, FakeTrClient]:
    from audio_hub.downloaders.client import TransmissionFacade

    tr = FakeTrClient(**fk)
    return TransmissionFacade(tr), tr


def test_facade_add_translates_params():
    fac, tr = _facade()
    fac.torrents_add(
        torrent_files=b"torrent-bytes", save_path="/downloads/hub",
        category="audio-hub", tags=["hub", "music"], is_paused=False,
    )
    assert len(tr.added) == 1
    call = tr.added[0]
    assert call["torrent"] == b"torrent-bytes"
    assert call["download_dir"] == "/downloads/hub"
    # 分类并入标签（Transmission 无分类概念），去重后顺序稳定
    assert call["labels"] == ["audio-hub", "hub", "music"]
    assert call["paused"] is False


def test_facade_add_by_url():
    fac, tr = _facade()
    fac.torrents_add(urls="https://example.org/dl.php?id=1", save_path="/d")
    assert tr.added[0]["torrent"] == "https://example.org/dl.php?id=1"


def test_facade_add_duplicate_raises_conflict409():
    from transmission_rpc import TransmissionError

    fac, _ = _facade(add_error=TransmissionError("duplicate torrent already exists"))
    with pytest.raises(qbittorrentapi.Conflict409Error):
        fac.torrents_add(urls="https://x/1.torrent", save_path="/d")


def test_facade_add_auth_error_raises_loginfailed():
    from transmission_rpc import TransmissionAuthError

    fac, _ = _facade(add_error=TransmissionAuthError("401: Unauthorized"))
    with pytest.raises(qbittorrentapi.LoginFailed):
        fac.torrents_add(urls="https://x/1.torrent", save_path="/d")


def test_facade_add_connect_error_raises_apiconnectionerror():
    from transmission_rpc import TransmissionConnectError

    fac, _ = _facade(add_error=TransmissionConnectError("connection refused"))
    with pytest.raises(qbittorrentapi.APIConnectionError):
        fac.torrents_add(urls="https://x/1.torrent", save_path="/d")


# ── TransmissionFacade：torrents_info ───────────────────────

def test_facade_info_maps_states():
    fac, tr = _facade()
    tr._torrents = [
        _tr_torrent(name="a", status="downloading"),
        _tr_torrent(name="b", status="download pending"),
        _tr_torrent(name="c", status="seeding"),
        _tr_torrent(name="d", status="seed pending"),
        _tr_torrent(name="e", status="stopped", percent_done=1.0),
        _tr_torrent(name="f", status="stopped", percent_done=0.2),
        _tr_torrent(name="g", status="checking", percent_done=0.5),
        _tr_torrent(name="h", status="check pending", percent_done=1.0),
        _tr_torrent(name="i", status="downloading", error=3),
        _tr_torrent(name="j", status="downloading", error=1),  # tracker 警告不算错误态
    ]
    by_name = {t.name: t for t in fac.torrents_info()}
    assert by_name["a"].state == "downloading"
    assert by_name["b"].state == "queuedDL"
    assert by_name["c"].state == "uploading"
    assert by_name["d"].state == "queuedUP"
    assert by_name["e"].state == "pausedUP"
    assert by_name["f"].state == "pausedDL"
    assert by_name["g"].state == "checkingDL"
    assert by_name["h"].state == "checkingUP"
    assert by_name["i"].state == "error"
    assert by_name["j"].state == "downloading"


def test_facade_info_field_mapping():
    fac, tr = _facade()
    tr._torrents = [
        _tr_torrent(hash_string="ABC123", name="Album (2024)", download_dir="/downloads",
                    seconds_seeding=3600, upload_ratio=2.5, labels=["hub", "music"]),
    ]
    (t,) = fac.torrents_info()
    assert t.hash == "abc123"                       # qb 侧哈希一律小写
    assert t.name == "Album (2024)"
    assert t.save_path == "/downloads"
    assert t.content_path == "/downloads/Album (2024)"  # tr 无 contentPath，按 dir+name 拼
    assert t.seeding_time == 3600
    assert t.ratio == 2.5


def test_facade_info_filters_by_tag():
    fac, tr = _facade()
    tr._torrents = [
        _tr_torrent(name="mine", labels=["hub"]),
        _tr_torrent(name="other", labels=["personal"]),
        _tr_torrent(name="nolabel", labels=[]),
    ]
    names = {t.name for t in fac.torrents_info(tag="hub")}
    assert names == {"mine"}


def test_facade_pause_resume():
    fac, tr = _facade()
    fac.torrents_pause(torrent_hashes="abc123")
    fac.torrents_resume(torrent_hashes=["abc123", "def456"])
    assert tr.stopped == ["abc123"]
    assert tr.started == [["abc123", "def456"]]


def test_facade_app_version_for_logging():
    fac, _tr = _facade()
    assert "4.0.6" in str(fac.app.version)


# ── build_client 工厂 ───────────────────────────────────────

def test_build_client_qb_path():
    from audio_hub.downloaders.client import build_client

    cfg = _cfg(id="nas-qb", host="192.168.31.40", port=8085)
    client = build_client(cfg, login=False)
    assert isinstance(client, qbittorrentapi.Client)


def test_build_client_qb_without_credentials_returns_none():
    from audio_hub.downloaders.client import build_client

    cfg = _cfg(id="nas-qb", username="", password="")
    assert build_client(cfg, login=False) is None


def test_build_client_transmission_path(monkeypatch):
    from audio_hub.downloaders import client as dl_client

    made: list[dict] = []

    class FakeCtor(FakeTrClient):
        def __init__(self, **kw):
            made.append(kw)
            super().__init__()

    import transmission_rpc
    monkeypatch.setattr(transmission_rpc, "Client", FakeCtor)
    fac = dl_client.build_client(_cfg(type="transmission"), login=False)
    assert isinstance(fac, dl_client.TransmissionFacade)
    assert made[0]["host"] == "192.168.31.40"
    assert made[0]["port"] == 9091
    assert made[0]["username"] == "admin"


def test_build_client_defaults_to_qb_for_unknown_type():
    from audio_hub.downloaders.client import build_client

    cfg = _cfg(id="x", type="aria2", username="a", password="b")
    client = build_client(cfg, login=False)
    assert isinstance(client, qbittorrentapi.Client)


# ── 探测 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_downloader_dispatches_by_type(monkeypatch):
    calls: list[str] = []

    def fake_qb(cfg, **kw):
        calls.append("qb")
        return {"ok": True}

    def fake_tr_sync(cfg):
        calls.append("tr")
        return {"ok": True}

    monkeypatch.setattr(probe, "probe_qb_sync", fake_qb)
    monkeypatch.setattr(probe, "probe_transmission_sync", fake_tr_sync)
    await probe.probe_downloader(_cfg(type="qbittorrent"))
    await probe.probe_downloader(_cfg(type="transmission"))
    assert calls == ["qb", "tr"]


@pytest.mark.asyncio
async def test_probe_downloader_timeout(monkeypatch):
    import time

    def slow(cfg):
        time.sleep(1)

    monkeypatch.setattr(probe, "probe_transmission_sync", slow)
    result = await probe.probe_downloader(_cfg(type="transmission"), timeout=0.05)
    assert result["ok"] is False
    assert "超时" in (result["error"] or "")


def test_probe_transmission_sync_success(monkeypatch):
    fake = FakeTrClient(
        torrents=[_tr_torrent(status="downloading"), _tr_torrent(status="stopped")],
    )
    import transmission_rpc
    monkeypatch.setattr(transmission_rpc, "Client", lambda **kw: fake)
    monkeypatch.setattr(probe, "probe_tcp", lambda host, port, timeout=6.0: (True, ""))
    result = probe.probe_transmission_sync(_cfg())
    assert result["ok"] is True
    assert result["version"] == "4.0.6"
    assert result["default_save_path"] == "/downloads"
    assert result["free_space_bytes"] == 1073741824
    assert result["active_torrents"] == 1


def test_probe_transmission_sync_auth_failure(monkeypatch):
    from transmission_rpc import TransmissionAuthError

    def boom(**kw):
        raise TransmissionAuthError("401: Unauthorized")

    import transmission_rpc
    monkeypatch.setattr(transmission_rpc, "Client", boom)
    monkeypatch.setattr(probe, "probe_tcp", lambda host, port, timeout=6.0: (True, ""))
    result = probe.probe_transmission_sync(_cfg())
    assert result["ok"] is False
    assert "认证" in (result["error"] or "")


def test_probe_transmission_sync_connect_failure(monkeypatch):
    from transmission_rpc import TransmissionConnectError

    def boom(**kw):
        raise TransmissionConnectError("all connection attempts failed")

    import transmission_rpc
    monkeypatch.setattr(transmission_rpc, "Client", boom)
    monkeypatch.setattr(probe, "probe_tcp", lambda host, port, timeout=6.0: (True, ""))
    result = probe.probe_transmission_sync(_cfg())
    assert result["ok"] is False
    assert "连接" in (result["error"] or "")


def test_probe_transmission_sync_missing_host():
    result = probe.probe_transmission_sync(_cfg(host="", port=9091))
    assert result["ok"] is False
    assert "host" in (result["error"] or "")
