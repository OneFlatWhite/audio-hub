"""v0.6 站点认证：NexusPHP index / userdetails 解析、mteam profile 解析、check_auth 行为。

HTML / JSON 全部走 fixtures，网络用 respx 模拟，不触网。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from audio_hub.adapters.base import AdapterAuthError, AdapterError
from audio_hub.adapters.mteam import MTeamAdapter, parse_profile
from audio_hub.adapters.nexusphp import (
    NexusPhpAdapter,
    merge_userdetails,
    parse_index_userinfo,
    parse_userdetails,
)
from audio_hub.config import SiteConfig

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def nexus(**kw) -> NexusPhpAdapter:
    base = {
        "name": "hhanclub", "enabled": True, "domain": "https://hhanclub.net",
        "rss_url": "https://hhanclub.net/torrentrss.php?passkey=PK", "music_cat_id": "408",
        "cookie": "uid=12345; pass=abc",
    }
    base.update(kw)
    return NexusPhpAdapter(SiteConfig(**base))


def mteam(**kw) -> MTeamAdapter:
    base = {
        "name": "mteam", "enabled": True, "api_key": "MTKEY",
        "search_url": "https://api.m-team.cc/api/torrent/search",
    }
    base.update(kw)
    return MTeamAdapter(SiteConfig(**base))


# ── NexusPHP 解析 ───────────────────────────────────────────

class TestNexusIndexParsing:
    def test_userinfo_and_uid(self):
        info, uid = parse_index_userinfo(load("nexusphp_index.html"))
        assert uid == "12345"
        assert info.username == "audiofan"
        assert info.uploaded == "1.23 TB"
        assert info.downloaded == "384.50 GB"
        assert info.ratio == "3.21"
        assert info.bonus == "12,345.6"
        assert info.seeding == 42
        assert info.leeching == 1

    def test_login_page_has_no_uid(self):
        info, uid = parse_index_userinfo(load("nexusphp_login_redirect.html"))
        assert uid == ""
        assert info.username == ""

    def test_userdetails_level_and_merge(self):
        details = parse_userdetails(load("nexusphp_userdetails.html"))
        assert details["level"] == "Power User"
        assert details["seeding"] == 42

        info, _ = parse_index_userinfo(load("nexusphp_index.html"))
        merged = merge_userdetails(info, {"level": "Power User", "uploaded": "SHOULD NOT WIN"})
        assert merged.level == "Power User"
        # 信息栏已有的字段不被 userdetails 覆盖
        assert merged.uploaded == "1.23 TB"


class TestNexusCheckAuth:
    @respx.mock
    async def test_valid_cookie_returns_user_info(self):
        respx.get("https://hhanclub.net/index.php").mock(
            return_value=httpx.Response(200, text=load("nexusphp_index.html")))
        respx.get("https://hhanclub.net/userdetails.php?id=12345").mock(
            return_value=httpx.Response(200, text=load("nexusphp_userdetails.html")))
        info = await nexus().check_auth()
        assert info.username == "audiofan"
        assert info.level == "Power User"

    @respx.mock
    async def test_redirect_to_login_is_auth_error(self):
        respx.get("https://hhanclub.net/index.php").mock(
            return_value=httpx.Response(302, headers={"location": "/login.php?returnto=index.php"}))
        with pytest.raises(AdapterAuthError):
            await nexus().check_auth()

    @respx.mock
    async def test_login_page_body_is_auth_error(self):
        respx.get("https://hhanclub.net/index.php").mock(
            return_value=httpx.Response(200, text=load("nexusphp_login_redirect.html")))
        with pytest.raises(AdapterAuthError):
            await nexus().check_auth()

    @respx.mock
    async def test_userdetails_failure_only_degrades(self):
        respx.get("https://hhanclub.net/index.php").mock(
            return_value=httpx.Response(200, text=load("nexusphp_index.html")))
        respx.get("https://hhanclub.net/userdetails.php?id=12345").mock(
            return_value=httpx.Response(500))
        info = await nexus().check_auth()
        assert info.username == "audiofan"
        assert info.level == ""

    async def test_missing_cookie_is_auth_error(self):
        with pytest.raises(AdapterAuthError):
            await nexus(cookie="").check_auth()

    async def test_missing_domain_is_adapter_error(self):
        adapter = NexusPhpAdapter(SiteConfig(name="hhanclub", cookie="x"))
        with pytest.raises(AdapterError):
            await adapter.check_auth()

    @respx.mock
    async def test_cookie_header_sent(self):
        route = respx.get("https://hhanclub.net/index.php").mock(
            return_value=httpx.Response(200, text=load("nexusphp_index.html")))
        respx.get("https://hhanclub.net/userdetails.php?id=12345").mock(
            return_value=httpx.Response(200, text=load("nexusphp_userdetails.html")))
        await nexus(user_agent="UA-X").check_auth()
        req = route.calls[0].request
        assert req.headers["cookie"] == "uid=12345; pass=abc"
        assert req.headers["user-agent"] == "UA-X"


# ── mteam ───────────────────────────────────────────────────

class TestMteamProfile:
    def test_parse_profile_fixture(self):
        info = parse_profile(json.loads(load("mteam_profile.json")))
        assert info.username == "mt_audiofan"
        assert info.level == "资深用户"
        assert info.uploaded.endswith("TB")
        assert info.downloaded.endswith("GB")
        assert info.ratio == "3.28"
        assert info.bonus == "98765.43"
        assert info.seeding == 57
        assert info.leeching == 0

    def test_parse_profile_without_data_is_auth_error(self):
        with pytest.raises(AdapterAuthError):
            parse_profile({"code": "0", "data": None})

    @respx.mock
    async def test_check_auth_posts_profile(self):
        route = respx.post("https://api.m-team.cc/api/member/profile").mock(
            return_value=httpx.Response(200, json=json.loads(load("mteam_profile.json"))))
        info = await mteam().check_auth()
        assert info.username == "mt_audiofan"
        assert route.calls[0].request.headers["x-api-key"] == "MTKEY"

    @respx.mock
    async def test_business_error_is_auth_error(self):
        respx.post("https://api.m-team.cc/api/member/profile").mock(
            return_value=httpx.Response(200, json={"code": "401", "message": "invalid key"}))
        with pytest.raises(AdapterAuthError):
            await mteam().check_auth()

    @respx.mock
    async def test_http_403_is_auth_error(self):
        respx.post("https://api.m-team.cc/api/member/profile").mock(
            return_value=httpx.Response(403))
        with pytest.raises(AdapterAuthError):
            await mteam().check_auth()
