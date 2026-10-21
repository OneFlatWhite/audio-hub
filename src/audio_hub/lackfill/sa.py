"""SA 备链(M2 第二级):SA 直连 API(JWT 内存缓存 + 过期/401 重登)与企微回调兜底。"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

import httpx

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_TOKEN_SKEW = 30.0


class SAError(RuntimeError):
    """SA 登录失败或转存两次尝试后仍失败。"""


class SAClient:
    """SA 转存客户端:登录态内存缓存,401 重登,终败抛 SAError。"""

    def __init__(
        self,
        base_url: str,
        username: str,
        secret: str,
        *,
        delay_min: float = 3.0,
        delay_max: float = 6.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._secret = secret
        self._delay_min = delay_min
        self._delay_max = delay_max
        self._sleep = sleep
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": _UA}, trust_env=False, timeout=30.0
        )
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def _login(self) -> None:
        try:
            resp = await self._client.post(
                f"{self._base}/auth/login",
                json={"username": self._username, "secret": self._secret},
            )
            if resp.status_code != 200:
                raise SAError(f"SA 登录失败: HTTP {resp.status_code}")
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SAError(f"SA 登录失败: {exc}") from exc
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise SAError(f"SA 登录响应缺 token: {payload}")
        expires_in = float(payload.get("expires_in") or 3600)
        self._token = token
        self._token_expires_at = self._clock() + expires_in - _TOKEN_SKEW

    async def _ensure_token(self) -> str:
        if self._token is None or self._clock() >= self._token_expires_at:
            await self._login()
        return str(self._token)

    async def receive(
        self,
        share_code: str,
        receive_code: str,
        file_ids: list[str],
        cid: str,
    ) -> bool:
        """SA 转存;调用前随机延迟一次,401 重登后再试,两次皆败抛 SAError。"""
        await self._sleep(random.uniform(self._delay_min, self._delay_max))
        token = await self._ensure_token()
        body = {
            "share_code": share_code,
            "receive_code": receive_code,
            "file_ids": file_ids,
            "cid": cid,
        }
        for _ in range(2):
            try:
                resp = await self._client.post(
                    f"{self._base}/api/transfer",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 401:
                    await self._login()
                    token = str(self._token)
                    continue
                if resp.status_code == 200 and resp.json().get("state"):
                    return True
            except (httpx.HTTPError, ValueError):
                pass
        raise SAError("SA 转存两次尝试后仍失败")

    async def aclose(self) -> None:
        await self._client.aclose()


async def send_wecom_callback(
    webhook_url: str,
    content: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """企微机器人回调兜底:发送文本通知,errcode==0 为成功。

    TODO: 企微 inner_xml / AES 加密细节待 Ob 确认后补齐。
    """
    http = client or httpx.AsyncClient(trust_env=False, timeout=15.0)
    try:
        resp = await http.post(
            webhook_url,
            json={"msgtype": "text", "text": {"content": content}},
        )
        return resp.status_code == 200 and resp.json().get("errcode") == 0
    except (httpx.HTTPError, ValueError):
        return False
    finally:
        if client is None:
            await http.aclose()
