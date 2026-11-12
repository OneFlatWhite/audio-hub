"""lackfill 连通性自检:外部通道必须可一键验证(FRONTEND-RULES)。"""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from audio_hub.config import LackfillConfig
from audio_hub.lackfill import probe

pytestmark = pytest.mark.asyncio


def _cfg(**kw) -> LackfillConfig:
    base = {
        "enabled": True,
        "emby_url": "http://emby.test",
        "emby_api_key": "emby-key",
        "tmdb_api_key": "tmdb-key",
        "p115_cookie": "UID=1&CID=2&SEID=3",
        "sa_base_url": "http://sa.test",
        "sa_username": "sa-user",
        "sa_secret": "sa-pass",
        "cloud_staging_dir": "/tmp/probe-staging",
        "cloud_feed_dir": "/tmp/probe-feed",
    }
    base.update(kw)
    return LackfillConfig(**base)


class TestProbeUnits:
    async def test_emby_ok_and_401(self):
        with respx.mock:
            respx.get("http://emby.test/System/Info").mock(
                return_value=Response(200, json={"ServerName": "NAS-Emby", "Version": "4.9"})
            )
            result = await probe.probe("emby", _cfg())
        assert result["ok"] is True and "NAS-Emby" in result["detail"]

        with respx.mock:
            respx.get("http://emby.test/System/Info").mock(return_value=Response(401))
            result = await probe.probe("emby", _cfg())
        assert result["ok"] is False and "401" in result["detail"]

    async def test_tmdb_bearer_and_unconfigured(self):
        with respx.mock:
            route = respx.get("https://api.themoviedb.org/3/configuration").mock(
                return_value=Response(200, json={"images": {"base_url": "http://img"}})
            )
            result = await probe.probe("tmdb", _cfg(tmdb_api_key="eyJv4"))
        assert result["ok"] is True
        assert "Bearer" in route.calls.last.request.headers["Authorization"]

        result = await probe.probe("tmdb", _cfg(tmdb_api_key=""))
        assert result["ok"] is False and "未配置" in result["detail"]

    @respx.mock
    async def test_p115_reachable_json_and_blocked(self):
        respx.post("https://webapi.115.com/share/snap").mock(
            return_value=Response(200, json={"state": False, "message": "分享不存在"})
        )
        result = await probe.probe("p115", _cfg())
        assert result["ok"] is True and "连通" in result["detail"]

        respx.post("https://webapi.115.com/share/snap").mock(
            return_value=Response(200, text="<html>风控</html>")
        )
        result = await probe.probe("p115", _cfg())
        assert result["ok"] is True and "不是 JSON" in result["detail"]

    @respx.mock
    async def test_sa_login_success_and_rejected(self):
        respx.post("http://sa.test/api/v1/login/access-token").mock(
            return_value=Response(200, json={"access_token": "jwt"})
        )
        result = await probe.probe("sa", _cfg())
        assert result["ok"] is True and "凭据正确" in result["detail"]

        respx.post("http://sa.test/api/v1/login/access-token").mock(
            return_value=Response(401)
        )
        result = await probe.probe("sa", _cfg())
        assert result["ok"] is False and "401" in result["detail"]

    async def test_cd2_mount_missing_reported(self, tmp_path, monkeypatch):
        cfg = _cfg(cloud_staging_dir=str(tmp_path / "nope"), cloud_feed_dir=str(tmp_path))
        result = await probe.probe("cd2", cfg)
        assert result["ok"] is False and "云下载目录不存在" in result["detail"]

        cfg = _cfg(cloud_staging_dir=str(tmp_path), cloud_feed_dir=str(tmp_path))
        result = await probe.probe("cd2", cfg)
        assert result["ok"] is True

    async def test_unknown_target_rejected(self):
        with pytest.raises(ValueError):
            await probe.probe("ssh", _cfg())
