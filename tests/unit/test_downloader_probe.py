"""downloaders/probe.py：三层失败分层（TCP → HTTP → 登录）与异步超时语义。

全程 monkeypatch mock 掉 probe_tcp / probe_http / qbittorrentapi.Client，
不发任何真实网络请求。
"""

from __future__ import annotations

import socket
import time
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
import qbittorrentapi

from audio_hub.config import DownloaderConfig
from audio_hub.downloaders import probe


def _cfg(**kw) -> DownloaderConfig:
    """一份「合法到足以走到 TCP 探测」的配置，按需覆盖字段。"""
    base = {"id": "nas", "host": "qb.local", "port": 8085, "username": "admin", "password": "pw"}
    base.update(kw)
    return DownloaderConfig(**base)


# ── 假 qb 客户端 ────────────────────────────────────────────

class FakeClient:
    """qbittorrentapi.Client 假身：按类属性表演登录成败与参考信息。

    instances 记录每次构造收到的 kwargs，供断言认证参数选择（账密 vs API Key）。
    """

    instances: ClassVar[list[FakeClient]] = []
    login_error: ClassVar[Exception | None] = None
    app_version: ClassVar[str] = "5.2.0"
    web_api_version: ClassVar[str] = "2.11.0"
    save_path: ClassVar[str | Exception] = "/volume1/downloads"
    maindata: ClassVar[dict | Exception] = {"server_state": {"free_space_on_disk": 1073741824}}
    torrents: ClassVar[list | Exception] = ["t1", "t2"]

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.logged_out = 0
        FakeClient.instances.append(self)

    def auth_log_in(self):
        if FakeClient.login_error is not None:
            raise FakeClient.login_error

    @property
    def app(self):
        return SimpleNamespace(
            version=FakeClient.app_version, web_api_version=FakeClient.web_api_version,
        )

    def app_default_save_path(self):
        if isinstance(FakeClient.save_path, Exception):
            raise FakeClient.save_path
        return FakeClient.save_path

    def sync_maindata(self):
        if isinstance(FakeClient.maindata, Exception):
            raise FakeClient.maindata
        return FakeClient.maindata

    def torrents_info(self, status_filter=None):
        if isinstance(FakeClient.torrents, Exception):
            raise FakeClient.torrents
        return FakeClient.torrents

    def auth_log_out(self):
        self.logged_out += 1


@pytest.fixture
def fake_qb_client(monkeypatch):
    """装好 FakeClient 并重置表演参数；TCP 默认可达。"""
    FakeClient.instances = []
    FakeClient.login_error = None
    FakeClient.save_path = "/volume1/downloads"
    FakeClient.maindata = {"server_state": {"free_space_on_disk": 1073741824}}
    FakeClient.torrents = ["t1", "t2"]
    monkeypatch.setattr(qbittorrentapi, "Client", FakeClient)
    monkeypatch.setattr(probe, "probe_tcp", lambda host, port: (True, ""))
    return FakeClient


# ── probe_tcp：socket 错误 → 中文诊断 ───────────────────────

class TestProbeTcp:
    def test_success(self, monkeypatch):
        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(socket, "create_connection", lambda addr, timeout: _Conn())
        assert probe.probe_tcp("qb.local", 8085) == (True, "")

    @pytest.mark.parametrize("exc,keyword", [
        (TimeoutError(), "连接超时"),
        (ConnectionRefusedError(), "连接被拒绝"),
        (socket.gaierror("nx"), "主机名解析失败"),
        (OSError("no route"), "网络错误"),
    ])
    def test_failures(self, monkeypatch, exc, keyword):
        def _raise(addr, timeout):
            raise exc

        monkeypatch.setattr(socket, "create_connection", _raise)
        ok, err = probe.probe_tcp("qb.local", 8085)
        assert ok is False and keyword in err


# ── probe_http：http → https 回退 ───────────────────────────

class TestProbeHttp:
    def test_http_ok(self, monkeypatch):
        monkeypatch.setattr(
            httpx, "get", lambda url, **kw: SimpleNamespace(status_code=200, text="v5.2.0"),
        )
        code, detail = probe.probe_http("qb.local", 8085)
        assert code == 200 and "http → 200" in detail

    def test_fallback_to_https(self, monkeypatch):
        def _get(url, **kw):
            if url.startswith("http://"):
                raise httpx.ConnectError("refused")
            return SimpleNamespace(status_code=401, text="")

        monkeypatch.setattr(httpx, "get", _get)
        code, detail = probe.probe_http("qb.local", 8085)
        assert code == 401 and "https → 401" in detail

    def test_both_schemes_fail(self, monkeypatch):
        def _get(url, **kw):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "get", _get)
        code, detail = probe.probe_http("qb.local", 8085)
        assert code is None and "https 失败" in detail


# ── probe_qb_sync：分层失败与成功 ────────────────────────────

class TestProbeQbSync:
    def test_missing_host_or_port(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            probe, "probe_tcp", lambda host, port: called.append((host, port)) or (True, ""),
        )
        for cfg in (_cfg(host=""), _cfg(port=0)):
            result = probe.probe_qb_sync(cfg)
            assert result["ok"] is False and "host / port 未配置" in result["error"]
        assert called == [], "配置不全时不该发起 TCP 探测"

    def test_no_credentials(self, monkeypatch):
        monkeypatch.setattr(probe, "probe_tcp", lambda host, port: (True, ""))
        result = probe.probe_qb_sync(_cfg(username="", password="", api_key=""))
        assert result["ok"] is False and "未配置凭据" in result["error"]

    def test_tcp_unreachable(self, fake_qb_client, monkeypatch):
        """第一层就失败：不再构建客户端，错误带 TCP 前缀。"""
        monkeypatch.setattr(probe, "probe_tcp", lambda host, port: (False, "连接被拒绝：无服务"))
        result = probe.probe_qb_sync(_cfg())
        assert result["ok"] is False
        assert result["error"] == "TCP 不可达: 连接被拒绝：无服务"
        assert fake_qb_client.instances == []

    def test_login_failed(self, fake_qb_client):
        fake_qb_client.login_error = qbittorrentapi.LoginFailed("bad creds")
        result = probe.probe_qb_sync(_cfg())
        assert result["ok"] is False
        assert "认证失败" in result["error"] and "bad creds" in result["error"]

    def test_api_connection_error_includes_http_hint(self, fake_qb_client, monkeypatch):
        """登录抛连接错误时补一次裸 HTTP 探测，403 附加已知排查提示。"""
        fake_qb_client.login_error = qbittorrentapi.APIConnectionError("conn reset")
        monkeypatch.setattr(
            probe, "probe_http", lambda host, port: (403, "http → 403 'Forbidden'"),
        )
        result = probe.probe_qb_sync(_cfg())
        assert result["ok"] is False
        assert "连接失败: conn reset" in result["error"]
        assert "http → 403" in result["error"]
        assert "Host 头验证" in result["error"]

    def test_api_connection_error_without_hint(self, fake_qb_client, monkeypatch):
        """非已知状态码（或两 scheme 都失败）只附探测详情，不附提示。"""
        fake_qb_client.login_error = qbittorrentapi.APIConnectionError("conn reset")
        monkeypatch.setattr(probe, "probe_http", lambda host, port: (None, "http 失败: x"))
        result = probe.probe_qb_sync(_cfg())
        assert result["ok"] is False
        assert "http 失败" in result["error"] and "Host 头验证" not in result["error"]

    def test_unexpected_error(self, fake_qb_client):
        fake_qb_client.login_error = RuntimeError("boom")
        result = probe.probe_qb_sync(_cfg())
        assert result["ok"] is False and result["error"] == "RuntimeError: boom"

    def test_success_full(self, fake_qb_client):
        result = probe.probe_qb_sync(_cfg())
        assert result["ok"] is True and result["error"] is None
        assert result["version"] == "5.2.0" and result["api_version"] == "2.11.0"
        assert result["default_save_path"] == "/volume1/downloads"
        assert result["free_space_bytes"] == 1073741824
        assert result["active_torrents"] == 2
        assert fake_qb_client.instances[-1].logged_out == 1, "探测结束应登出会话"

    def test_success_survives_partial_failures(self, fake_qb_client):
        """登录成功即 ok；参考信息单项失败只留 None，不翻错误。"""
        fake_qb_client.save_path = RuntimeError("no pref")
        fake_qb_client.maindata = RuntimeError("sync fail")
        fake_qb_client.torrents = RuntimeError("list fail")
        result = probe.probe_qb_sync(_cfg())
        assert result["ok"] is True and result["version"] == "5.2.0"
        assert result["default_save_path"] is None
        assert result["free_space_bytes"] is None
        assert result["active_torrents"] is None

    def test_free_space_absent_in_maindata(self, fake_qb_client):
        fake_qb_client.maindata = {"server_state": {}}
        result = probe.probe_qb_sync(_cfg())
        assert result["ok"] is True and result["free_space_bytes"] is None


class TestMakeClient:
    """认证参数选择：username 非空走账密，否则 API Key（与 main._make_qb_client 同序）。"""

    def test_username_password_preferred(self, fake_qb_client):
        probe.probe_qb_sync(_cfg(username="admin", password="pw", api_key="K"))
        assert FakeClient.instances[-1].kwargs == {
            "host": "qb.local", "port": 8085, "username": "admin", "password": "pw",
        }

    def test_api_key_fallback(self, fake_qb_client):
        probe.probe_qb_sync(_cfg(username="", password="", api_key="K"))
        assert FakeClient.instances[-1].kwargs == {
            "host": "qb.local", "port": 8085, "api_key": "K",
        }


# ── probe_qb（异步入口）：结果透传与超时 ─────────────────────

class TestProbeQbAsync:
    async def test_passthrough(self, monkeypatch):
        sentinel = {"ok": True, "version": "5.2.0"}
        monkeypatch.setattr(probe, "probe_qb_sync", lambda cfg: sentinel)
        result = await probe.probe_qb(_cfg())
        assert result is sentinel

    async def test_timeout(self, monkeypatch):
        """同步探测超时：整体限时返回失败结果，而不是卡住请求线程。"""
        monkeypatch.setattr(probe, "probe_qb_sync", lambda cfg: time.sleep(0.5))
        result = await probe.probe_qb(_cfg(), timeout=0.05)
        assert result["ok"] is False and "探测超时" in result["error"]

    def test_default_timeout_is_module_constant(self):
        assert probe.probe_qb.__kwdefaults__["timeout"] == probe.PROBE_TIMEOUT_SECONDS
