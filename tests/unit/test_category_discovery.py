"""v0.6 分类发现：NexusPHP torrents.php 复选框、mteam categoryList、本地类型建议、六态纯函数。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from audio_hub.adapters.base import AdapterError, suggest_category_kind
from audio_hub.adapters.mteam import MTeamAdapter, parse_category_list
from audio_hub.adapters.nexusphp import NexusPhpAdapter, parse_torrents_categories
from audio_hub.config import SiteConfig
from audio_hub.models import AuthStatus
from audio_hub.sites.status import (
    STATE_AUTH_FAILED,
    STATE_CIRCUIT_OPEN,
    STATE_DISABLED,
    STATE_OK,
    STATE_UNAUTHENTICATED,
    STATE_UNCONFIGURED,
    compute_state,
    state_view,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestSuggestKind:
    @pytest.mark.parametrize("name,expected", [
        ("音乐 Music", "music"),
        ("Music(Lossless)", "music"),
        ("有声书 Audio Book", "audiobook"),   # 含 Audio 也不能判成音乐
        ("Audiobook", "audiobook"),
        ("电影", "other"),
        ("", "other"),
    ])
    def test_mapping(self, name, expected):
        assert suggest_category_kind(name) == expected


class TestNexusCategories:
    def test_parse_checkboxes(self):
        cats = parse_torrents_categories(load("nexusphp_torrents.html"), "hhanclub")
        by_id = {c.remote_id: c for c in cats}
        # 重复的 cat408 去重
        assert sorted(by_id) == ["401", "402", "408", "410", "411"]
        assert by_id["408"].remote_name == "音乐 Music"
        assert by_id["408"].local_kind == "music"
        assert by_id["410"].local_kind == "audiobook"
        assert by_id["401"].local_kind == "other"
        # 没有 img 时退回 <label for=>
        assert by_id["411"].remote_name == "其他 Misc"
        assert all(c.site_id == "hhanclub" for c in cats)

    def test_sp_cat_inputs_and_icon_name_fallback(self):
        """憨憨新模板：sp_cat[] 是 special 分区（音乐/MV），img alt="cat" 无意义，
        分类名应从图标文件名（icon-music.svg → music）提取。"""
        html = """
        <html><body>
        <div class="top-search-checkbox">
          <input type="checkbox" name="cat[]" value="401" id="cat401">
          <label for="cat401"><a href="/torrents.php?cat[]=401">
            <img src="styles/HHan/icons/icon-movie.svg" alt="cat"></a></label>
        </div>
        <div>
          <input type="checkbox" name="sp_cat[]" value="410" id="cat410">
          <label for="cat410"><a href="/special.php?cat[]=410">
            <img src="styles/HHan/icons/icon-music.svg" alt="cat"></a></label>
        </div>
        </body></html>"""
        cats = parse_torrents_categories(html, "hhanclub")
        by_id = {c.remote_id: c for c in cats}
        assert sorted(by_id) == ["401", "410"]
        assert by_id["410"].remote_name == "music"
        assert by_id["410"].local_kind == "music"
        assert by_id["401"].remote_name == "movie"
        assert by_id["401"].local_kind == "other"

    @respx.mock
    async def test_discover_categories_requests_torrents_php(self):
        adapter = NexusPhpAdapter(SiteConfig(
            name="hhanclub", domain="https://hhanclub.net", cookie="c=1",
        ))
        respx.get("https://hhanclub.net/torrents.php").mock(
            return_value=httpx.Response(200, text=load("nexusphp_torrents.html")))
        cats = await adapter.discover_categories()
        assert len(cats) == 5

    @respx.mock
    async def test_discover_categories_empty_page_raises(self):
        adapter = NexusPhpAdapter(SiteConfig(
            name="hhanclub", domain="https://hhanclub.net", cookie="c=1",
        ))
        respx.get("https://hhanclub.net/torrents.php").mock(
            return_value=httpx.Response(200, text="<html><body>nothing</body></html>"))
        with pytest.raises(AdapterError):
            await adapter.discover_categories()


class TestMteamCategories:
    def test_parse_category_list_fixture(self):
        cats = parse_category_list(json.loads(load("mteam_categories.json")), "mteam")
        by_id = {c.remote_id: c for c in cats}
        assert sorted(by_id) == ["401", "406", "408", "434", "999"]
        assert by_id["406"].remote_name == "無損音樂"
        assert by_id["406"].local_kind == "music"       # 繁体名判不出，靠 nameEng
        assert by_id["408"].local_kind == "music"
        assert by_id["434"].local_kind == "audiobook"
        assert by_id["401"].local_kind == "other"
        # mode 字段优先于名字
        assert by_id["999"].local_kind == "audiobook"
        assert by_id["999"].remote_name == "Edu(Course)"

    def test_parse_flat_list(self):
        cats = parse_category_list({"data": [{"id": 1, "name": "Music"}]}, "mteam")
        assert cats[0].remote_id == "1" and cats[0].local_kind == "music"

    @respx.mock
    async def test_discover_categories_posts(self):
        adapter = MTeamAdapter(SiteConfig(
            name="mteam", api_key="K", search_url="https://api.m-team.cc/api/torrent/search",
        ))
        respx.post("https://api.m-team.cc/api/torrent/categoryList").mock(
            return_value=httpx.Response(200, json=json.loads(load("mteam_categories.json"))))
        cats = await adapter.discover_categories()
        assert {c.remote_id for c in cats} == {"401", "406", "408", "434", "999"}


class TestComputeState:
    def test_six_states(self):
        v = AuthStatus.VALID.value
        assert compute_state(enabled=False, auth_status="unconfigured", has_credentials=False,
                             circuit_tripped=False) == STATE_UNCONFIGURED
        assert compute_state(enabled=False, auth_status="unconfigured", has_credentials=True,
                             circuit_tripped=False) == STATE_UNAUTHENTICATED
        assert compute_state(enabled=True, auth_status="expired", has_credentials=True,
                             circuit_tripped=False) == STATE_AUTH_FAILED
        assert compute_state(enabled=False, auth_status=v, has_credentials=True,
                             circuit_tripped=False) == STATE_DISABLED
        assert compute_state(enabled=True, auth_status=v, has_credentials=True,
                             circuit_tripped=True) == STATE_CIRCUIT_OPEN
        assert compute_state(enabled=True, auth_status=v, has_credentials=True,
                             circuit_tripped=False) == STATE_OK

    def test_enum_accepted(self):
        assert compute_state(enabled=True, auth_status=AuthStatus.VALID, has_credentials=True,
                             circuit_tripped=False) == STATE_OK

    def test_auth_failed_wins_over_enabled(self):
        """认证失效的站即使 enabled=True 也不算正常 —— 与门禁口径一致。"""
        assert compute_state(enabled=True, auth_status="expired", has_credentials=True,
                             circuit_tripped=True) == STATE_AUTH_FAILED

    def test_state_view_labels(self):
        assert state_view(STATE_OK) == ("正常", "high")
        assert state_view(STATE_AUTH_FAILED)[0] == "认证失效"
        assert state_view("garbage")[0] == "未配置"
