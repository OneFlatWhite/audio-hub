"""AY HTTP API 协议、覆盖解析与贪心选包。"""

from __future__ import annotations

import importlib
import json

import httpx
import pytest
import respx


def _ay():
    return importlib.import_module("audio_hub.lackfill.ay")


@respx.mock
@pytest.mark.asyncio
async def test_query_posts_real_body_and_returns_resources_and_quota():
    route = respx.post("https://ay.example.com/api/user").mock(
        return_value=httpx.Response(200, json={
            "data": [{"name": "S01 整季", "notes": "", "link": "https://115.com/s/abc123", "size": 10}],
            "times": 97,
            "message": "ok",
        })
    )
    result = await _ay().AYClient("https://ay.example.com/api/user", "secret").query(1416, "12345")
    assert result["quota_left"] == 97
    assert result["http_status"] == 200
    assert result["resources"][0]["name"] == "S01 整季"
    body = json.loads(route.calls.last.request.content)
    assert body == {"tg_id": "12345", "type": "tv", "tmdb_id": "1416", "token": "secret"}
    assert "Authorization" not in route.calls.last.request.headers


@respx.mock
@pytest.mark.asyncio
async def test_query_empty_data_is_empty_resources():
    respx.post("https://ay.example.com/api/user").mock(
        return_value=httpx.Response(200, json={"data": {}, "times": 96, "mesage": "无资源"})
    )
    result = await _ay().AYClient("https://ay.example.com/api/user", "secret").query(1, "12345")
    assert result["resources"] == []
    assert result["quota_left"] == 96
    assert result["message"] == "无资源"


@respx.mock
@pytest.mark.asyncio
async def test_query_http_error_raises_ay_error():
    respx.post("https://ay.example.com/api/user").mock(return_value=httpx.Response(401))
    with pytest.raises(_ay().AYError):
        await _ay().AYClient("https://ay.example.com/api/user", "secret").query(1, "12345")


@respx.mock
@pytest.mark.asyncio
async def test_query_non_object_json_raises_ay_error():
    respx.post("https://ay.example.com/api/user").mock(
        return_value=httpx.Response(200, json=[])
    )
    with pytest.raises(_ay().AYError, match="JSON 对象"):
        await _ay().AYClient("https://ay.example.com/api/user", "secret").query(1, "12345")


def test_episode_cover_reads_real_snapshot_paths():
    files = [
        {"path": "测试剧/S01/E03.mkv"},
        {"path": "测试剧/S02/S02E04.mkv"},
        {"path": "测试剧/预告片.mkv"},
    ]
    assert _ay().episode_cover(files) == {(1, 3), (2, 4)}


@pytest.mark.asyncio
async def test_client_ignores_environment_proxy_and_closes():
    client = _ay().AYClient("https://ay.example.com/api/user", "secret")
    assert client._client.trust_env is False
    await client.aclose()
    assert client._client.is_closed


def test_resource_cover_expands_episode_and_cross_season_ranges():
    ay = _ay()
    covered = ay.resource_cover("S01E40-S02E03", {1: [1, 40], 2: [1, 2, 3, 4]})
    assert covered == {(1, 40), (2, 1), (2, 2), (2, 3)}


def test_resource_cover_season_range_single_season_and_complete():
    ay = _ay()
    season_eps = {1: [1, 2], 2: [1, 2, 3], 3: [1]}
    assert ay.resource_cover("S1-S2", season_eps) == {(1, 1), (1, 2), (2, 1), (2, 2), (2, 3)}
    assert ay.resource_cover("第2季", season_eps) == {(2, 1), (2, 2), (2, 3)}
    assert ay.resource_cover("全集 Complete", season_eps) == {
        (1, 1), (1, 2), (2, 1), (2, 2), (2, 3), (3, 1),
    }


def test_pick_greedy_covers_missing_and_discards_irrelevant_packages():
    ay = _ay()
    resources = [
        {"name": "S01 整季", "notes": "", "link": "https://115.com/s/a", "size": 10},
        {"name": "S02 整季", "notes": "", "link": "https://115.com/s/b", "size": 12},
        {"name": "S03 整季", "notes": "", "link": "https://115.com/s/c", "size": 99},
    ]
    season_eps = {1: [1, 2, 3, 4], 2: [1, 2], 3: [1]}
    picks, reason = ay.pick_ay_links(resources, 2, {(1, 3), (1, 4), (2, 1)}, season_eps)
    assert [p["link"] for p in picks] == ["https://115.com/s/a", "https://115.com/s/b"]
    assert "预计覆盖 3/3" in reason


def test_pick_tie_prefers_115_over_ed2k():
    ay = _ay()
    resources = [
        {"name": "S01", "notes": "", "link": "ed2k://|file|a|1|6A8A258330DB0DA10B020623AE256150|/", "size": 10},
        {"name": "S01", "notes": "", "link": "https://115.com/s/a", "size": 10},
    ]
    picks, _ = ay.pick_ay_links(resources, 1, {(1, 1)}, {1: [1]})
    assert picks[0]["link"] == "https://115.com/s/a"


def test_pick_rejects_completely_irrelevant_resources():
    ay = _ay()
    resources = [{"name": "S09", "notes": "", "link": "https://115.com/s/a", "size": 1}]
    picks, reason = ay.pick_ay_links(resources, 3, {(1, 1)}, {1: [1], 9: [1]})
    assert picks == []
    assert "预计覆盖 0/1" in reason
