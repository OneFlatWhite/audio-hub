"""SA 真实协议：JWT 登录、115 离线、目录解析与 WXBizMsgCrypt 回调。"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import xml.etree.ElementTree as ET

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from audio_hub.lackfill.sa import SAClient, SAError, send_wecom_callback

pytestmark = pytest.mark.asyncio

BASE = "https://sa.example.com"
LOGIN = f"{BASE}/api/v1/login/access-token"
OFFLINE = f"{BASE}/api/v1/plugin/115/offline"
FOLDERS = f"{BASE}/api/v1/plugin/115/offline/folders"
CALLBACK = "https://sa.example.com/wecom/callback"
WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
ED2K = "ed2k://|file|测试视频.mkv|4021117121|6A8A258330DB0DA10B020623AE256150|/"
AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"


class Sleeper:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def _client(**kwargs) -> SAClient:
    options = {
        "callback_url": CALLBACK,
        "callback_token": "callback-secret",
        "callback_aeskey": AES_KEY,
        "callback_corpid": "corp-1",
        "callback_userid": "user-1",
        "callback_agentid": "agent-1",
        "sleep": Sleeper(),
    }
    options.update(kwargs)
    return SAClient(BASE, "bot", "s3cret", **options)


def _login_ok(token="jwt-1"):
    return httpx.Response(200, json={"access_token": token})


def _offline_ok(message="离线成功"):
    return httpx.Response(200, json={"success": True, "message": message})


@respx.mock
async def test_offline_uses_real_login_and_magnets_body():
    login = respx.post(LOGIN).mock(return_value=_login_ok())
    offline = respx.post(OFFLINE).mock(return_value=_offline_ok())
    client = _client()
    assert (await client.offline([ED2K], parent_id="123"))["ok"] is True
    assert (await client.offline([ED2K], parent_id="123"))["ok"] is True
    assert len(login.calls) == 1
    assert len(offline.calls) == 2
    login_body = login.calls.last.request.content.decode()
    assert "username=bot" in login_body and "password=s3cret" in login_body
    assert "access_token" not in login_body
    request = offline.calls.last.request
    assert request.headers["Authorization"] == "Bearer jwt-1"
    assert json.loads(request.content) == {"magnets": [ED2K], "parent_id": "123"}


@respx.mock
async def test_offline_uses_first_folder_when_parent_id_missing():
    respx.post(LOGIN).mock(return_value=_login_ok())
    folders = respx.get(FOLDERS).mock(return_value=httpx.Response(200, json={
        "data": [["云下载", "cid-1"], ["其他", "cid-2"]],
    }))
    offline = respx.post(OFFLINE).mock(return_value=_offline_ok())
    assert (await _client().offline([ED2K]))["ok"] is True
    assert folders.called
    assert json.loads(offline.calls.last.request.content)["parent_id"] == "cid-1"


@respx.mock
async def test_offline_supports_nested_folder_objects():
    respx.post(LOGIN).mock(return_value=_login_ok())
    respx.get(FOLDERS).mock(return_value=httpx.Response(200, json={
        "data": {"folders": [{"file_name": "云下载", "id": "cid-obj"}]},
    }))
    respx.post(OFFLINE).mock(return_value=_offline_ok())
    assert (await _client().offline([ED2K]))["ok"] is True


@respx.mock
async def test_offline_relogins_after_401():
    respx.post(LOGIN).mock(side_effect=[_login_ok("old"), _login_ok("new")])
    offline = respx.post(OFFLINE).mock(side_effect=[
        httpx.Response(401), _offline_ok(),
    ])
    assert (await _client().offline([ED2K], parent_id="123"))["ok"] is True
    assert offline.calls.last.request.headers["Authorization"] == "Bearer new"


@respx.mock
async def test_offline_existing_task_is_idempotent_success():
    respx.post(LOGIN).mock(return_value=_login_ok())
    respx.post(OFFLINE).mock(return_value=httpx.Response(200, json={
        "success": False, "message": "115离线下载失败：任务已存在",
    }))
    assert (await _client().offline([ED2K], parent_id="123"))["ok"] is True


@respx.mock
async def test_offline_real_failure_raises():
    respx.post(LOGIN).mock(return_value=_login_ok())
    respx.post(OFFLINE).mock(return_value=httpx.Response(200, json={
        "success": False, "message": "下载失败：地址无效",
    }))
    with pytest.raises(SAError):
        await _client().offline([ED2K], parent_id="123")


@respx.mock
async def test_login_failure_never_calls_offline():
    respx.post(LOGIN).mock(return_value=httpx.Response(500))
    offline = respx.post(OFFLINE)
    with pytest.raises(SAError):
        await _client().offline([ED2K], parent_id="123")
    assert len(offline.calls) == 0


def _decrypt_callback(request: httpx.Request, token: str):
    params = request.url.params
    assert params["token"] == "symedia"
    root = ET.fromstring(request.content.decode())
    encrypted = root.findtext("Encrypt")
    expected_sig = hashlib.sha1("".join(sorted([
        token, params["timestamp"], params["nonce"], encrypted,
    ])).encode()).hexdigest()
    assert params["msg_signature"] == expected_sig
    key = base64.b64decode(AES_KEY + "=")
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    decryptor = cipher.decryptor()
    plain = decryptor.update(base64.b64decode(encrypted)) + decryptor.finalize()
    plain = plain[16:-plain[-1]]
    size = struct.unpack(">I", plain[:4])[0]
    inner = plain[4:4 + size].decode()
    assert plain[4 + size:] == b"corp-1"
    return inner


@respx.mock
async def test_submit_message_uses_encrypted_symedia_callback():
    respx.post(LOGIN).mock(return_value=_login_ok())
    route = respx.post(CALLBACK).mock(return_value=httpx.Response(200, json={
        "success": True, "message": "消息处理成功",
    }))
    client = _client()
    result = await client.submit_message([ED2K, "https://115.com/s/abc123"], "测试剧")
    assert result["ok"] is True and result["via"] == "http"
    inner = _decrypt_callback(route.calls.last.request, "callback-secret")
    assert "测试剧" in inner
    assert ED2K in inner
    assert "https://115.com/s/abc123" in inner
    assert "corp-1" in inner and "agent-1" in inner


@respx.mock
async def test_submit_message_rejects_received_but_not_processed():
    respx.post(CALLBACK).mock(return_value=httpx.Response(200, json={
        "success": True, "message": "消息已接收",
    }))
    with pytest.raises(SAError):
        await _client().submit_message([ED2K], "测试剧")


@respx.mock
async def test_submit_message_requires_full_callback_config():
    client = SAClient(BASE, "u", "s")
    with pytest.raises(SAError):
        await client.submit_message([ED2K], "测试剧")


@respx.mock
async def test_send_wecom_callback_remains_a_plain_notification():
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    assert await send_wecom_callback(WEBHOOK, "hello") is True
    assert json.loads(route.calls.last.request.content)["text"]["content"] == "hello"


@respx.mock
async def test_aclose_closes_owned_client():
    http_client = httpx.AsyncClient()
    client = _client(client=http_client)
    await client.aclose()
    assert http_client.is_closed
