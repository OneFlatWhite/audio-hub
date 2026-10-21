"""sa 模块测试:SA 备链登录态缓存、过期/401 重登、转存重试与企微回调。

覆盖:
- receive 成功请求体与 Bearer 头、JWT 缓存(两次转存一次登录)、随机延迟注入
- token 过期自动重登、401 重登后重发、state 假两次抛 SAError、网络异常抛 SAError
- 登录失败抛 SAError 且不发转存请求
- aclose 关闭外部传入客户端
- send_wecom_callback 成功/errcode 非零/网络异常
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from audio_hub.lackfill.sa import SAClient, SAError, send_wecom_callback

_BASE = "https://sa.example.com"
_LOGIN = f"{_BASE}/auth/login"
_TRANSFER = f"{_BASE}/api/transfer"
_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"


class _Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _client(sleeper: _Sleeper | None = None, clock=None, **kwargs) -> SAClient:
    return SAClient(
        _BASE,
        "bot",
        "s3cret",
        sleep=sleeper or _Sleeper(),
        clock=clock or time.monotonic,
        **kwargs,
    )


def _login_ok(token: str, expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(200, json={"token": token, "expires_in": expires_in})


@respx.mock
async def test_receive_caches_jwt_across_calls() -> None:
    """连续两次转存只登录一次;请求体四字段齐全,Bearer 头与随机延迟正确。"""
    login = respx.post(_LOGIN).mock(return_value=_login_ok("t1"))
    transfer = respx.post(_TRANSFER).mock(return_value=httpx.Response(200, json={"state": True}))
    sleeper = _Sleeper()
    client = _client(sleeper)
    assert await client.receive("sc", "rc", ["1", "2"], "9") is True
    assert await client.receive("sc", "rc", ["3"], "9") is True
    assert len(login.calls) == 1
    assert len(transfer.calls) == 2
    request = transfer.calls.last.request
    assert request.headers["Authorization"] == "Bearer t1"
    body = json.loads(request.content)
    assert body == {"share_code": "sc", "receive_code": "rc", "file_ids": ["3"], "cid": "9"}
    assert len(sleeper.calls) == 2


@respx.mock
async def test_receive_relogins_when_token_expires() -> None:
    """token 过期(过期点=签发时刻+expires_in-30s)后自动重新登录并用新 token。"""
    login = respx.post(_LOGIN).mock(side_effect=[_login_ok("t1", 60), _login_ok("t2", 60)])
    transfer = respx.post(_TRANSFER).mock(return_value=httpx.Response(200, json={"state": True}))
    clock = _Clock()
    client = _client(clock=clock)
    await client.receive("sc", "", ["1"], "9")
    clock.now += 100  # 过期点 1000+60-30=1030,现在 1100 → 重登
    await client.receive("sc", "", ["2"], "9")
    assert len(login.calls) == 2
    assert transfer.calls.last.request.headers["Authorization"] == "Bearer t2"


@respx.mock
async def test_receive_relogins_on_401() -> None:
    """转存 401 立即重登,用新 token 重发,最终成功。"""
    login = respx.post(_LOGIN).mock(side_effect=[_login_ok("t1"), _login_ok("t2")])
    transfer = respx.post(_TRANSFER).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"state": True}),
        ]
    )
    assert await _client().receive("sc", "", ["1"], "9") is True
    assert len(login.calls) == 2
    assert transfer.calls.last.request.headers["Authorization"] == "Bearer t2"


@respx.mock
async def test_receive_raises_after_two_failures() -> None:
    """state 两次皆假抛 SAError,恰好请求两次。"""
    respx.post(_LOGIN).mock(return_value=_login_ok("t1"))
    transfer = respx.post(_TRANSFER).mock(return_value=httpx.Response(200, json={"state": False}))
    with pytest.raises(SAError):
        await _client().receive("sc", "", ["1"], "9")
    assert len(transfer.calls) == 2


@respx.mock
async def test_receive_network_error_raises() -> None:
    """网络异常重试后仍败,抛 SAError。"""
    respx.post(_LOGIN).mock(return_value=_login_ok("t1"))
    respx.post(_TRANSFER).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(SAError):
        await _client().receive("sc", "", ["1"], "9")


@respx.mock
async def test_login_failure_raises_without_transfer() -> None:
    """登录失败直接抛 SAError,不发任何转存请求。"""
    respx.post(_LOGIN).mock(return_value=httpx.Response(500))
    transfer = respx.post(_TRANSFER)
    with pytest.raises(SAError):
        await _client().receive("sc", "", ["1"], "9")
    assert len(transfer.calls) == 0


@respx.mock
async def test_receive_sleeps_configured_delay() -> None:
    """每次转存调用前按 [delay_min, delay_max] 随机睡眠一次(两端相等便于断言)。"""
    respx.post(_LOGIN).mock(return_value=_login_ok("t1"))
    respx.post(_TRANSFER).mock(return_value=httpx.Response(200, json={"state": True}))
    sleeper = _Sleeper()
    client = SAClient(_BASE, "u", "s", delay_min=2.5, delay_max=2.5, sleep=sleeper)
    await client.receive("sc", "", ["1"], "9")
    assert sleeper.calls == [2.5]


async def test_aclose_closes_owned_client() -> None:
    """外部传入的 httpx 客户端由 aclose 负责关闭。"""
    http_client = httpx.AsyncClient()
    client = SAClient(_BASE, "u", "s", sleep=_Sleeper(), client=http_client)
    await client.aclose()
    assert http_client.is_closed


@respx.mock
async def test_wecom_callback_success() -> None:
    """errcode==0 返回 True,报文为 msgtype=text 且 content 原样。"""
    route = respx.post(_WEBHOOK).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    assert await send_wecom_callback(_WEBHOOK, "hello") is True
    body = json.loads(route.calls.last.request.content)
    assert body["msgtype"] == "text"
    assert body["text"]["content"] == "hello"


@respx.mock
async def test_wecom_callback_errcode_nonzero() -> None:
    """errcode 非零返回 False。"""
    respx.post(_WEBHOOK).mock(return_value=httpx.Response(200, json={"errcode": 1}))
    assert await send_wecom_callback(_WEBHOOK, "x") is False


@respx.mock
async def test_wecom_callback_network_error() -> None:
    """网络异常不抛出,返回 False。"""
    respx.post(_WEBHOOK).mock(side_effect=httpx.ConnectError("down"))
    assert await send_wecom_callback(_WEBHOOK, "x") is False
