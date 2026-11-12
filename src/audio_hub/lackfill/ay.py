"""爱影 HTTP API 客户端、资源覆盖解析与按缺集贪心选包。"""

from __future__ import annotations

import json
import re

import httpx


class AYError(RuntimeError):
    """AY 请求、鉴权或响应解析失败。"""


class AYClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_url = api_url
        self._token = token
        self._client = client or httpx.AsyncClient(trust_env=False, timeout=15.0)

    async def query(self, tmdb_id: int, tg_id: str) -> dict:
        try:
            resp = await self._client.post(
                self._api_url,
                json={
                    "tg_id": str(tg_id),
                    "type": "tv",
                    "tmdb_id": str(tmdb_id),
                    "token": self._token,
                },
            )
        except httpx.HTTPError as exc:
            raise AYError("AY API 请求失败") from exc
        if resp.status_code != 200:
            raise AYError(f"AY API 返回 HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise AYError("AY API 响应不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise AYError("AY API 响应必须是 JSON 对象")
        data = payload.get("data")
        resources = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        return {
            "resources": resources,
            "quota_left": payload.get("times"),
            "http_status": resp.status_code,
            "message": payload.get("message") or payload.get("mesage") or "",
        }

    async def aclose(self) -> None:
        await self._client.aclose()


def episode_cover(files: list[dict]) -> set[tuple[int, int]]:
    """从真实快照路径提取季集；不认识的文件不按全集猜测。"""
    covered = set()
    for item in files:
        if item.get("is_dir"):
            continue
        path = str(item.get("path") or item.get("name") or "")
        direct = {
            (int(match.group(1)), int(match.group(2)))
            for match in re.finditer(r"S(\d{1,2})E(\d{1,3})", path, re.IGNORECASE)
        }
        if direct:
            covered |= direct
            continue
        seasons = re.findall(r"(?<![A-Za-z0-9])S(\d{1,2})(?!\d)", path, re.IGNORECASE)
        episodes = re.findall(r"(?<![A-Za-z0-9])E(\d{1,3})(?!\d)", path, re.IGNORECASE)
        if seasons and episodes:
            covered.add((int(seasons[-1]), int(episodes[-1])))
    return covered


def _whole(season_eps: dict[int, list[int]]) -> set[tuple[int, int]]:
    return {(season, episode) for season, episodes in season_eps.items() for episode in episodes}


def resource_cover(text: str, season_eps: dict[int, list[int]]) -> set[tuple[int, int]]:
    covered: set[tuple[int, int]] = set()
    if re.search(r"全集|全\s*\d+\s*集|complete", text, re.IGNORECASE):
        return _whole(season_eps)

    def _episode_range(match: re.Match) -> str:
        start_season, start_episode = int(match.group(1)), int(match.group(2))
        end_season = int(match.group(3)) if match.group(3) else start_season
        end_episode = int(match.group(4))
        if (end_season, end_episode) < (start_season, start_episode):
            start_season, end_season = end_season, start_season
            start_episode, end_episode = end_episode, start_episode
        for season in range(start_season, end_season + 1):
            lo = start_episode if season == start_season else 1
            hi = end_episode if season == end_season else max(season_eps.get(season) or [end_episode])
            for episode in range(lo, hi + 1):
                covered.add((season, episode))
        return " "

    text = re.sub(
        r"S(\d{1,2})E(\d{1,3})\s*[-~–]\s*(?:S(\d{1,2}))?E(\d{1,3})",
        _episode_range,
        text,
        flags=re.IGNORECASE,
    )
    for match in re.finditer(r"S(\d{1,2})E(\d{1,3})", text, re.IGNORECASE):
        covered.add((int(match.group(1)), int(match.group(2))))
    for match in re.finditer(r"S(\d{1,2})\s*[-~–]\s*S?(\d{1,2})(?!\d*E)", text, re.IGNORECASE):
        first, second = sorted((int(match.group(1)), int(match.group(2))))
        for season in range(first, second + 1):
            covered |= {(season, episode) for episode in season_eps.get(season) or []}
    for match in re.finditer(r"S(\d{1,2})(?!\d*\s*[-~–E])|第\s*(\d{1,2})\s*季", text, re.IGNORECASE):
        season = int(match.group(1) or match.group(2))
        covered |= {(season, episode) for episode in season_eps.get(season) or []}
    if not covered:
        covered = _whole(season_eps)
    return covered


def _size_gb(resource: dict) -> float:
    try:
        return float(resource.get("size") or 0)
    except (TypeError, ValueError):
        return 0.0


def pick_ay_links(
    resources: list[dict],
    max_links: int,
    lack_eps: set[tuple[int, int]] | None = None,
    season_eps: dict[int, list[int]] | None = None,
) -> tuple[list[dict], str]:
    if not lack_eps:
        def _whole_package(resource: dict) -> bool:
            text = f"{resource.get('notes') or ''} {resource.get('name') or ''}"
            return bool(re.search(
                r"全集|全\s*\d+\s*集|complete|S\d{1,2}\s*[-~–]\s*S?\d{1,2}|S\d{2}\b",
                text,
                re.IGNORECASE,
            ))

        whole = sorted((r for r in resources if _whole_package(r)), key=_size_gb, reverse=True)
        rest = sorted((r for r in resources if not _whole_package(r)), key=_size_gb, reverse=True)
        return (whole + rest)[:max(1, max_links)], ""

    season_eps = season_eps or {}
    pool = []
    for resource in resources:
        text = f"{resource.get('name') or ''} {resource.get('notes') or ''}"
        covered = resource_cover(text, season_eps)
        valid = covered & lack_eps
        if not valid:
            continue
        pool.append({
            "res": resource,
            "covered": covered,
            "valid": valid,
            "overflow": covered - lack_eps,
            "is_ed2k": 1 if str(resource.get("link") or "").lower().startswith("ed2k") else 0,
            "size": _size_gb(resource),
        })

    picks: list[dict] = []
    labels: list[str] = []
    covered_so_far: set[tuple[int, int]] = set()
    remaining = list(pool)
    while remaining and len(picks) < max(1, max_links) and covered_so_far < lack_eps:
        best = max(
            remaining,
            key=lambda item: (
                len(item["valid"] - covered_so_far),
                -item["is_ed2k"],
                -len(item["overflow"]),
                -item["size"],
            ),
        )
        new_cover = best["valid"] - covered_so_far
        if not new_cover:
            break
        picks.append(best["res"])
        covered_so_far |= new_cover
        text = f"{best['res'].get('name') or ''} {best['res'].get('notes') or ''}"
        match = re.search(
            r"S\d{1,2}E\d{1,3}\s*[-~–]\s*(?:S\d{1,2})?E\d{1,3}"
            r"|S\d{1,2}\s*[-~–]\s*S?\d{1,2}|S\d{1,2}E\d{1,3}"
            r"|全集|全\s*\d+\s*集|S\d{2}",
            text,
            re.IGNORECASE,
        )
        labels.append(match.group(0) if match else text.strip()[:16])
        remaining.remove(best)
    reason = (
        f"按缺集选包：缺 {len(lack_eps)} 集，选 {'+'.join(labels) or '无'}，"
        f"预计覆盖 {len(covered_so_far & lack_eps)}/{len(lack_eps)}"
    )
    return picks, reason


_QUOTA_KEY = "__lackfill_ay_quota"


async def save_quota(conn, quota_left, now: str) -> None:
    payload = json.dumps(
        {"quota_left": quota_left, "updated_at": now},
        ensure_ascii=False,
    )
    await conn.execute(
        """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
             value = excluded.value, updated_at = excluded.updated_at""",
        (_QUOTA_KEY, payload, now),
    )
    await conn.commit()


async def quota_status(conn) -> dict:
    cur = await conn.execute("SELECT value FROM app_settings WHERE key = ?", (_QUOTA_KEY,))
    row = await cur.fetchone()
    if row is None:
        return {"quota_left": None, "updated_at": None}
    try:
        value = json.loads(row["value"])
        if not isinstance(value, dict):
            raise TypeError("quota is not an object")
        return {
            "quota_left": value.get("quota_left"),
            "updated_at": value.get("updated_at"),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"quota_left": None, "updated_at": None}
