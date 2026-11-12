"""TMDB 客户端:剧集详情与单季播出集数的薄封装。

认证方式自动二选一:api_key 以 ``eyJ`` 开头视为 v4 read access token,
走 ``Authorization: Bearer`` 请求头;否则视为 v3 key,拼 ``api_key`` 查询参数。
节流(调用间隔)由 service 层统一控制,本类不做限速。
"""

from __future__ import annotations

from datetime import date

import httpx

#: TMDB API v3 根地址
BASE = "https://api.themoviedb.org/3"

#: 默认请求超时(秒)
DEFAULT_TIMEOUT = 15.0


class TmdbClient:
    """TMDB REST 客户端。

    Attributes:
        language: 查询语言,默认 zh-CN。
    """

    def __init__(
        self,
        api_key: str,
        language: str = "zh-CN",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.language = language
        self._api_key = api_key
        self._use_bearer = api_key.startswith("eyJ")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        """关闭自行创建的 HTTP 客户端;外部传入的客户端由调用方自己管理。"""
        if self._owns_client:
            await self._client.aclose()

    def _auth_kwargs(self) -> dict:
        """按 key 形态返回认证用的请求头或查询参数。"""
        if self._use_bearer:
            return {"headers": {"Authorization": f"Bearer {self._api_key}"}}
        return {"params": {"api_key": self._api_key}}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """发请求并校验状态码;返回 JSON 响应。"""
        url = f"{BASE}{path}"
        auth = self._auth_kwargs()
        headers = {**auth.get("headers", {}), **kwargs.pop("headers", {})}
        params = {**auth.get("params", {}), **kwargs.pop("params", {})}
        resp = await self._client.request(method, url, headers=headers, params=params, **kwargs)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"TMDB 请求失败: {method} {url} -> HTTP {resp.status_code}")
        return resp.json()

    async def tv(self, tmdb_id: int) -> dict:
        """取剧集详情(含 seasons 列表)。

        Args:
            tmdb_id: TMDB 剧集 ID。

        Returns:
            TMDB /tv/{id} 响应原文。
        """
        return await self._request("GET", f"/tv/{tmdb_id}", params={"language": self.language})

    async def trending_tv_week(self) -> list[dict]:
        """本周热门剧集,按 TMDB 排序取前 20。

        新剧自动检索的发现源;返回 [{id, name, first_air_date}]。
        """
        data = await self._request("GET", "/trending/tv/week", params={"language": self.language})
        return [
            {
                "id": item.get("id"),
                "name": item.get("name") or "",
                "first_air_date": item.get("first_air_date") or "",
            }
            for item in (data.get("results") or [])[:20]
            if item.get("id")
        ]

    async def season_aired_episode_numbers(self, tmdb_id: int, season: int, today: date) -> list[int]:
        """取某季「截至 today 已播出」的集号列表。

        过滤规则:air_date 非空且 <= today,episode_number > 0;
        结果升序去重。

        Args:
            tmdb_id: TMDB 剧集 ID。
            season: 季号。
            today: 判定「已播出」的基准日期。

        Returns:
            已播出集号列表(升序,无重复)。
        """
        data = await self._request(
            "GET",
            f"/tv/{tmdb_id}/season/{season}",
            params={"language": self.language},
        )
        numbers: set[int] = set()
        for ep in data.get("episodes") or []:
            air_date_raw = ep.get("air_date")
            number = ep.get("episode_number")
            if not air_date_raw or number is None or number <= 0:
                continue
            try:
                if date.fromisoformat(air_date_raw) > today:
                    continue
            except ValueError:
                continue
            numbers.add(int(number))
        return sorted(numbers)
