"""115 真实验包：包名、目标季、空包与快照路径。"""

from __future__ import annotations

import httpx
import pytest
import respx

from audio_hub.lackfill import p115, refill
from audio_hub.lackfill.p115 import P115Error


def _snapshot(package_name="测试剧 2024", files=None):
    if files is None:
        files = [
            {"name": "E03.mkv", "path": "测试剧/S01/E03.mkv", "file_id": "f1", "is_dir": False},
            {"name": "E03.mkv", "path": "测试剧/S02/E03.mkv", "file_id": "f2", "is_dir": False},
        ]
    return p115.ShareSnapshot(package_name=package_name, files=files)


def test_validate_accepts_named_package_with_all_target_seasons():
    snapshot = _snapshot()
    assert refill.validate_share_package(snapshot, "测试剧", {1: [3], 2: [3]}) == "测试剧 2024"


def test_validate_rejects_wrong_show():
    with pytest.raises(P115Error, match="剧名"):
        refill.validate_share_package(_snapshot("别的剧 2024"), "测试剧", {1: [3]})


def test_validate_rejects_missing_target_season():
    files = [
        {"name": "E03.mkv", "path": "测试剧/S01/E03.mkv", "file_id": "f1", "is_dir": False},
    ]
    with pytest.raises(P115Error, match="目标季"):
        refill.validate_share_package(_snapshot(files=files), "测试剧", {1: [3], 2: [3]})


def test_validate_rejects_empty_or_unnamed_package():
    with pytest.raises(P115Error, match="空包"):
        refill.validate_share_package(_snapshot(files=[]), "测试剧", {1: [3]})
    with pytest.raises(P115Error, match="包名"):
        refill.validate_share_package(_snapshot(package_name=""), "测试剧", {1: [3]})


def test_validate_rejects_unseasoned_legacy_request():
    with pytest.raises(P115Error, match="季号"):
        refill.validate_share_package(_snapshot(), "测试剧", [3], season=None)


@pytest.mark.asyncio
@respx.mock
async def test_inspect_share_preserves_package_name_and_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cid") == "222":
            return httpx.Response(200, json={
                "state": True,
                "data": {"count": 1, "list": [{"n": "E03.mkv", "fid": 111}]},
            })
        return httpx.Response(200, json={
            "state": True,
            "data": {"share_name": "测试剧 2024", "count": 1, "list": [{"n": "S01", "cid": 222}]},
        })

    respx.get("https://webapi.115.com/share/snap").mock(side_effect=handler)
    snapshot = await p115.P115Client("cookie").inspect_share("abc123")
    assert snapshot.package_name == "测试剧 2024"
    assert snapshot.files[1]["path"] == "S01/E03.mkv"
