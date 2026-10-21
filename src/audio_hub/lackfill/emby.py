"""Emby 客户端:剧集列表、已有集数、媒体库刷新的薄封装。

只做三件事:拼 URL、带上 X-Emby-Token 认证头、把响应整形成本包的字典结构;
不做重试、不做缓存。非 2xx 一律抛带状态码的 RuntimeError,由上层决定去留。
"""

from __future__ import annotations

import httpx

#: 默认请求超时(秒)
DEFAULT_TIMEOUT = 15.0


class EmbyClient:
    """Emby 服务器 REST 客户端。

    Attributes:
        base_url: 服务器地址,自动去掉尾部斜杠。
        api_key: Emby API Key,经 ``X-Emby-Token`` 请求头传递。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            headers={"X-Emby-Token": api_key},
        )

    async def aclose(self) -> None:
        """关闭自行创建的 HTTP 客户端;外部传入的客户端由调用方自己管理。"""
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict | None:
        """发请求并校验状态码;返回 JSON 响应(无响应体时返回 None)。"""
        url = f"{self.base_url}{path}"
        resp = await self._client.request(method, url, **kwargs)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"Emby 请求失败: {method} {url} -> HTTP {resp.status_code}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def list_series(self) -> list[dict]:
        """列出媒体库中全部剧集。

        Returns:
            每部剧一个字典:``emby_id``、``name``、``year``、
            ``tmdb_id``(取 ProviderIds.Tmdb,缺失或非法时为 None)。
        """
        data = await self._request(
            "GET",
            "/emby/Items",
            params={
                "IncludeItemTypes": "Series",
                "Recursive": "true",
                "Fields": "ProviderIds,ProductionYear",
            },
        ) or {}
        result: list[dict] = []
        for item in data.get("Items") or []:
            providers = item.get("ProviderIds") or {}
            raw_tmdb = providers.get("Tmdb")
            try:
                tmdb_id = int(raw_tmdb) if raw_tmdb else None
            except (TypeError, ValueError):
                tmdb_id = None
            result.append(
                {
                    "emby_id": str(item.get("Id", "")),
                    "name": item.get("Name") or "",
                    "year": item.get("ProductionYear"),
                    "tmdb_id": tmdb_id,
                }
            )
        return result

    async def episode_map(self, series_id: str) -> dict[int, set[int]]:
        """取某部剧已有的全部分集,按季聚合。

        Args:
            series_id: Emby 剧集条目 ID。

        Returns:
            季号(ParentIndexNumber)→ 集号(IndexNumber)集合;
            缺编号的条目直接跳过。
        """
        data = await self._request(
            "GET",
            "/emby/Items",
            params={
                "ParentId": series_id,
                "Recursive": "true",
                "IncludeItemTypes": "Episode",
                "Fields": "IndexNumber,ParentIndexNumber",
            },
        ) or {}
        result: dict[int, set[int]] = {}
        for item in data.get("Items") or []:
            season = item.get("ParentIndexNumber")
            episode = item.get("IndexNumber")
            if season is None or episode is None:
                continue
            result.setdefault(int(season), set()).add(int(episode))
        return result

    async def refresh_library(self) -> None:
        """触发 Emby 全库扫描刷新(POST /emby/Library/Refresh)。"""
        await self._request("POST", "/emby/Library/Refresh")
