"""115 直连转存链(M2 主通道):分享链接解析、snap 验包、cookie receive 转存。"""
from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_SNAP_URL = "https://webapi.115.com/share/snap"
_RECEIVE_URL = "https://webapi.115.com/share/receive"
_SHARE_RE = re.compile(r"https?://(?:115\.com|115cdn\.com|115cdn\.net)/s/([A-Za-z0-9]+)")
_PASSWORD_RE = re.compile(r"[?&]password=([A-Za-z0-9]+)")
_MAX_DEPTH = 3
_MAX_TOTAL = 3000
_PAGE_LIMIT = 1000


class P115Error(RuntimeError):
    """115 接口返回 state 为假或响应结构异常。"""


def parse_share_url(url: str) -> tuple[str, str | None]:
    """从 115 分享链接提取 (share_code, password);非法链接抛 ValueError。"""
    match = _SHARE_RE.search(url)
    if not match:
        raise ValueError(f"非法 115 分享链接: {url!r}")
    pwd = _PASSWORD_RE.search(url)
    return match.group(1), pwd.group(1) if pwd else None


class P115Client:
    """115 分享快照验包与 cookie 转存客户端。"""

    def __init__(
        self,
        cookie: str,
        *,
        delay_min: float = 3.0,
        delay_max: float = 6.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cookie = cookie
        self._delay_min = delay_min
        self._delay_max = delay_max
        self._sleep = sleep
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": _UA}, trust_env=False, timeout=30.0
        )

    async def snap(self, share_code: str, receive_code: str = "") -> list[dict[str, Any]]:
        """递归拉取分享包文件清单(目录 ≤3 层、总数 ≤3000),state 为假抛 P115Error。"""
        out: list[dict[str, Any]] = []
        await self._snap_dir(share_code, receive_code, None, 1, out)
        return out

    async def _snap_dir(
        self,
        share_code: str,
        receive_code: str,
        cid: str | None,
        depth: int,
        out: list[dict[str, Any]],
    ) -> None:
        start = len(out)
        offset = 0
        while True:
            params: dict[str, str] = {
                "share_code": share_code,
                "receive_code": receive_code,
                "limit": str(_PAGE_LIMIT),
                "offset": str(offset),
            }
            if cid:
                params["cid"] = cid
            resp = await self._client.get(_SNAP_URL, params=params)
            payload = resp.json()
            if not payload.get("state"):
                raise P115Error(f"snap 失败: {payload.get('error') or payload}")
            data = payload.get("data") or {}
            entries = data.get("list") or []
            count = data.get("count")
            for item in entries:
                if len(out) >= _MAX_TOTAL:
                    return
                fid = item.get("fid")
                dir_cid = item.get("cid")
                if fid is not None:
                    out.append({"name": item.get("n", ""), "file_id": str(fid), "is_dir": False})
                elif dir_cid is not None:
                    out.append({"name": item.get("n", ""), "file_id": str(dir_cid), "is_dir": True})
            offset += len(entries)
            if (
                not entries
                or (count is not None and offset >= int(count))
                or (count is None and len(entries) < _PAGE_LIMIT)
            ):
                break
        if depth >= _MAX_DEPTH:
            return
        for item in out[start:]:
            if len(out) >= _MAX_TOTAL:
                return
            if item["is_dir"]:
                await self._snap_dir(share_code, receive_code, item["file_id"], depth + 1, out)

    async def receive(
        self,
        share_code: str,
        receive_code: str,
        file_ids: list[str],
        cid: str,
    ) -> bool:
        """cookie 转存;调用前随机延迟(每次调用一次),失败重试一次,成败以 bool 返回。"""
        await self._sleep(random.uniform(self._delay_min, self._delay_max))
        form = {
            "share_code": share_code,
            "receive_code": receive_code,
            "file_id": ",".join(file_ids),
            "cid": cid,
        }
        for _ in range(2):
            try:
                resp = await self._client.post(
                    _RECEIVE_URL, data=form, headers={"Cookie": self._cookie}
                )
                if resp.json().get("state"):
                    return True
            except (httpx.HTTPError, ValueError):
                pass
        return False

    async def aclose(self) -> None:
        await self._client.aclose()
