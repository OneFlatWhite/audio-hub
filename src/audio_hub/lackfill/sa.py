"""Symedia 真实通道：JWT 离线下载与 WXBizMsgCrypt 加密消息回调。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import random
import struct
import time
from collections.abc import Awaitable, Callable

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_JWT_TTL_SECONDS = 25 * 86400


class SAError(RuntimeError):
    """SA 登录、离线提交或消息回调未真正被接受。"""


def _cdata(value: object) -> str:
    text = str(value).replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{text}]]>"


class SAClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        secret: str,
        *,
        callback_url: str = "",
        callback_token: str = "",
        callback_aeskey: str = "",
        callback_corpid: str = "",
        callback_userid: str = "",
        callback_agentid: str = "",
        delay_min: float = 3.0,
        delay_max: float = 6.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._secret = secret
        self._callback_url = callback_url
        self._callback_token = callback_token
        self._callback_aeskey = callback_aeskey
        self._callback_corpid = callback_corpid
        self._callback_userid = callback_userid
        self._callback_agentid = callback_agentid
        self._delay_min = delay_min
        self._delay_max = delay_max
        self._sleep = sleep
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": _UA}, trust_env=False, timeout=30.0
        )
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._parent_id: str = ""

    async def _login(self, *, force: bool = False) -> str:
        if not force and self._token and self._clock() < self._token_expires_at:
            return self._token
        try:
            resp = await self._client.post(
                f"{self._base}/api/v1/login/access-token",
                data={"username": self._username, "password": self._secret},
            )
            if resp.status_code != 200:
                raise SAError(f"SA 登录失败：HTTP {resp.status_code}")
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SAError("SA 登录请求失败") from exc
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise SAError("SA 登录响应缺少 access_token")
        self._token = token
        self._token_expires_at = self._clock() + _JWT_TTL_SECONDS
        return token

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(2):
            token = await self._login(force=attempt == 1)
            try:
                resp = await self._client.request(
                    method,
                    f"{self._base}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
                if resp.status_code == 401 and attempt == 0:
                    continue
                if resp.status_code != 200:
                    raise SAError(f"SA API 返回 HTTP {resp.status_code}")
                return resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise SAError("SA API 请求或响应解析失败") from exc
        raise SAError("SA API 鉴权重试后仍失败")

    @staticmethod
    def _folder_pairs(payload: dict) -> list[tuple[str, str]]:
        def pairs(items) -> list[tuple[str, str]]:
            if not isinstance(items, list):
                return []
            result = []
            for item in items:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    name, cid = item[0], item[1]
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("file_name") or item.get("title") or ""
                    cid = item.get("cid") or item.get("id") or item.get("file_id") or ""
                else:
                    continue
                if str(cid or "").strip():
                    result.append((str(name or ""), str(cid).strip()))
            return result

        data = (payload or {}).get("data")
        return pairs(data) or (pairs(data.get("folders")) if isinstance(data, dict) else [])

    async def _resolve_parent_id(self, configured: str) -> str:
        if configured:
            return configured
        if self._parent_id:
            return self._parent_id
        payload = await self._request("GET", "/api/v1/plugin/115/offline/folders")
        folders = self._folder_pairs(payload)
        if not folders:
            raise SAError("SA 未返回可用的离线目录")
        self._parent_id = folders[0][1]
        return self._parent_id

    async def offline(self, links: list[str], parent_id: str = "") -> dict:
        """ed2k/magnet 直连离线；115 分享链接严禁走这里。"""
        if not links:
            raise SAError("离线链接为空")
        parent = await self._resolve_parent_id(parent_id)
        await self._sleep(random.uniform(self._delay_min, self._delay_max))
        payload = await self._request(
            "POST",
            "/api/v1/plugin/115/offline",
            json={"magnets": links, "parent_id": parent},
        )
        message = str((payload or {}).get("message") or "")
        if "任务已存在" in message or "已经转存过" in message:
            return {"ok": True, "via": "api", "sent": len(links)}
        if (payload or {}).get("success") and "失败" not in message:
            return {"ok": True, "via": "api", "sent": len(links)}
        raise SAError("SA 离线提交未被接受")

    def _callback_ready(self) -> bool:
        return all((
            self._callback_url,
            self._callback_token,
            self._callback_aeskey,
            self._callback_corpid,
            self._callback_userid,
            self._callback_agentid,
        ))

    def _encrypted_callback(self, content: str) -> tuple[str, dict[str, str], bytes]:
        timestamp = str(int(time.time()))
        nonce = "".join(random.choices("0123456789", k=10))
        msg_id = random.randint(10**9, 10**10 - 1)
        inner_xml = (
            f"<xml><ToUserName>{_cdata(self._callback_corpid)}</ToUserName>"
            f"<FromUserName>{_cdata(self._callback_userid)}</FromUserName>"
            f"<CreateTime>{timestamp}</CreateTime><MsgType>{_cdata('text')}</MsgType>"
            f"<Content>{_cdata(content)}</Content><MsgId>{msg_id}</MsgId>"
            f"<AgentID>{_cdata(self._callback_agentid)}</AgentID></xml>"
        ).encode()
        key = base64.b64decode(self._callback_aeskey + "=")
        if len(key) != 32:
            raise SAError("SA EncodingAESKey 格式错误")
        corpid = self._callback_corpid.encode()
        plain = os.urandom(16) + struct.pack(">I", len(inner_xml)) + inner_xml + corpid
        pad_len = 32 - len(plain) % 32
        plain += bytes([pad_len]) * pad_len
        encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
        encrypted = base64.b64encode(encryptor.update(plain) + encryptor.finalize()).decode()
        signature = hashlib.sha1("".join(sorted([
            self._callback_token, timestamp, nonce, encrypted,
        ])).encode()).hexdigest()
        outer = (
            f"<xml><ToUserName>{_cdata(self._callback_corpid)}</ToUserName>"
            f"<Encrypt>{_cdata(encrypted)}</Encrypt>"
            f"<AgentID>{_cdata(self._callback_agentid)}</AgentID></xml>"
        ).encode()
        params = {
            "token": "symedia",
            "msg_signature": signature,
            "timestamp": timestamp,
            "nonce": nonce,
        }
        return signature, params, outer

    async def submit_message(self, links: list[str], title: str) -> dict:
        """115 分享及离线失败后的链接，经 SA 加密消息通道提交。"""
        if not links:
            raise SAError("消息链接为空")
        if not self._callback_ready():
            raise SAError("SA 加密回调配置不完整")
        await self._sleep(random.uniform(self._delay_min, self._delay_max))
        content = title + "\n" + "\n".join(links)
        _, params, body = self._encrypted_callback(content)
        try:
            resp = await self._client.post(
                self._callback_url,
                params=params,
                content=body,
                headers={"Content-Type": "text/xml"},
            )
            if resp.status_code != 200:
                raise SAError(f"SA 消息回调返回 HTTP {resp.status_code}")
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SAError("SA 消息回调请求失败") from exc
        if payload.get("success") and payload.get("message") == "消息处理成功":
            return {"ok": True, "via": "http", "sent": len(links)}
        raise SAError("SA 只接收消息但未实际处理")

    async def receive(
        self, share_code: str, receive_code: str, file_ids: list[str], cid: str
    ) -> bool:
        """兼容 M2 旧调用：分享转存走消息通道，不走离线下载。"""
        url = f"https://115.com/s/{share_code}"
        if receive_code:
            url += f"?password={receive_code}"
        await self.submit_message([url], "audio-hub 缺集补齐")
        return True

    async def aclose(self) -> None:
        await self._client.aclose()


async def send_wecom_callback(
    webhook_url: str,
    content: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """普通企微机器人通知；与 SA 加密回调无关。"""
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
