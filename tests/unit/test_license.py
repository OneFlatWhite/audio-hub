"""授权码机制测试：签名往返、篡改拒绝、激活端点、强制门禁。"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from audio_hub import license as lic


@pytest.fixture
def keypair(monkeypatch):
    """生成临时 Ed25519 密钥对并把公钥注入环境；返回 (私钥pem字节, 公钥b64)。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_b64 = base64.urlsafe_b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
    ).rstrip(b"=").decode()
    monkeypatch.setenv("AUDIO_HUB_LICENSE_PUBLIC_KEY", pub_b64)
    return pem, pub_b64


def _issue(pem: bytes, user="starer", days=0) -> str:
    """用测试私钥签一个授权码（与 scripts/issue_license.py 同逻辑）。"""
    key = serialization_load(pem)
    payload = json.dumps({
        "v": 1, "user": user, "repo": lic.DEFAULT_REPO,
        "issued_at": int(time.time()),
        "exp": int(time.time() + days * 86400) if days else None,
    }, sort_keys=True).encode()
    sig = key.sign(payload)
    b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return f"AH1.{b64u(payload)}.{b64u(sig)}"


def serialization_load(pem: bytes):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(pem, password=None)


def test_verify_roundtrip(keypair):
    pem, _ = keypair
    key = _issue(pem, user="alice")
    payload = lic.verify_license(key)
    assert payload is not None and payload["user"] == "alice"


def test_verify_rejects_tampered(keypair):
    pem, _ = keypair
    key = _issue(pem)
    head, payload_b64, sig = key.split(".")
    raw = base64.urlsafe_b64decode(payload_b64 + "==")
    tampered = json.loads(raw)
    tampered["user"] = "hacker"
    bad_payload = base64.urlsafe_b64encode(
        json.dumps(tampered, sort_keys=True).encode()).rstrip(b"=").decode()
    assert lic.verify_license(f"{head}.{bad_payload}.{sig}") is None


def test_verify_rejects_expired(keypair):
    pem, _ = keypair
    key = _issue(pem, days=-1)   # exp 在过去
    assert lic.verify_license(key) is None


def test_verify_without_public_key_returns_none(keypair, monkeypatch):
    pem, _ = keypair
    monkeypatch.delenv("AUDIO_HUB_LICENSE_PUBLIC_KEY")
    assert lic.verify_license(_issue(pem)) is None


def test_required_flag_parsing(monkeypatch):
    for v in ("1", "true", "YES"):
        monkeypatch.setenv("AUDIO_HUB_LICENSE_REQUIRED", v)
        assert lic.license_required() is True
    monkeypatch.setenv("AUDIO_HUB_LICENSE_REQUIRED", "0")
    assert lic.license_required() is False
    monkeypatch.delenv("AUDIO_HUB_LICENSE_REQUIRED")
    assert lic.license_required() is False


# ── 集成：激活页与强制门禁 ──────────────────────────────────

@pytest.fixture
def web_env(tmp_path: Path, monkeypatch):
    from audio_hub.crypto import generate_key

    key_file = tmp_path / "secrets.key"
    key_file.write_bytes(generate_key())
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'dry_run: true\ndb_path: "{(tmp_path / "hub.db").as_posix()}"\n'
        'qbittorrent:\n  host: "localhost"\n  port: 8085\n'
        'web:\n  cookie_secure: false\nsites: []\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg))
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(key_file))
    return tmp_path


@pytest.fixture
def client(web_env):
    from fastapi.testclient import TestClient

    from audio_hub.main import app

    with TestClient(app) as c:
        yield c


def test_activate_page_open_without_login(client):
    resp = client.get("/activate")
    assert resp.status_code == 200
    assert "激活" in resp.text


def test_activate_saves_and_status(client, keypair):
    from audio_hub import license as lic_mod

    pem, _ = keypair
    key = _issue(pem, user="starer")
    resp = client.post("/activate", data={"license_key": key}, follow_redirects=False)
    assert resp.status_code == 303 and "ok=activated" in resp.headers["location"]
    page = client.get("/activate").text
    assert "已激活" in page and "starer" in page
    # check_state 认可
    import asyncio

    state = asyncio.run(lic_mod.check_state(client.app.state.db))
    assert state["valid"] is True and state["payload"]["user"] == "starer"


def test_activate_rejects_bad_key_when_pubkey_set(client, keypair, monkeypatch):
    monkeypatch.setenv("AUDIO_HUB_LICENSE_PUBLIC_KEY", keypair[1])
    resp = client.post("/activate", data={"license_key": "AH1.abc.def"},
                       follow_redirects=False)
    assert "err=invalid" in resp.headers["location"]


def test_gate_redirects_when_required_and_invalid(client, monkeypatch):
    monkeypatch.setenv("AUDIO_HUB_LICENSE_REQUIRED", "1")
    # 依赖在请求时读取环境：直接访问受保护页
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/activate")
    # 放行名单不受限
    assert client.get("/health").status_code == 200
    assert client.get("/activate").status_code == 200
