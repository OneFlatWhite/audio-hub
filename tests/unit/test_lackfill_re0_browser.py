"""Re0 浏览器 CDP 健康与主源状态，不泄露调试地址或页面 URL。"""

from __future__ import annotations

import importlib

import httpx
import pytest
import respx


def _browser():
    return importlib.import_module("audio_hub.lackfill.re0_browser")


@pytest.mark.asyncio
@respx.mock
async def test_cdp_health_reports_ready_without_sensitive_urls():
    respx.get("http://127.0.0.1:9222/json/version").mock(
        return_value=httpx.Response(200, json={"Browser": "Chromium/150.0", "Protocol-Version": "1.3"})
    )
    respx.get("http://127.0.0.1:9222/json/list").mock(
        return_value=httpx.Response(200, json=[{
            "type": "page", "url": "https://re0.me/search?session=secret",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/secret",
        }])
    )
    result = await _browser().inspect("http://127.0.0.1:9222")
    assert result["cdp_ok"] is True
    assert result["state"] == "ready"
    assert result["ready"] is True
    assert result["re0_open"] is True
    assert result["page_count"] == 1
    assert result["browser"] == "Chromium/150.0"
    assert "secret" not in str(result)
    assert "webSocketDebuggerUrl" not in result


@pytest.mark.asyncio
@respx.mock
async def test_cdp_without_re0_page_is_not_ready():
    respx.get("http://127.0.0.1:9222/json/version").mock(
        return_value=httpx.Response(200, json={"Browser": "Chromium/150.0"})
    )
    respx.get("http://127.0.0.1:9222/json/list").mock(
        return_value=httpx.Response(200, json=[{"type": "page", "url": "about:blank"}])
    )
    result = await _browser().inspect("http://127.0.0.1:9222")
    assert result["cdp_ok"] is True
    assert result["ready"] is False
    assert result["state"] == "no_page"


@pytest.mark.asyncio
@respx.mock
async def test_cdp_failure_is_sanitized():
    respx.get("http://127.0.0.1:9222/json/version").mock(
        side_effect=httpx.ConnectError("secret-url-and-token")
    )
    result = await _browser().inspect("http://127.0.0.1:9222")
    assert result["cdp_ok"] is False
    assert result["state"] == "offline"
    assert result["ready"] is False
    assert result["error"] == "ConnectError"
    assert "secret" not in str(result)


def test_primary_mode_requires_live_script_or_manual_action():
    browser = _browser()
    healthy = {"cdp_ok": True, "ready": True}
    running = {"online": True, "auto_running": True, "status": "running"}
    challenge = {"online": True, "auto_running": False, "status": "challenge"}
    offline = {"online": False, "auto_running": True, "status": "running"}
    assert browser.primary_mode(healthy, running) == "available"
    assert browser.primary_mode(healthy, challenge) == "manual"
    assert browser.primary_mode({"cdp_ok": False}, running) == "available"
    assert browser.primary_mode(healthy, offline) == "unavailable"
    assert browser.primary_mode(healthy, {"online": True, "auto_running": False, "status": "failed"}) == "unavailable"


def test_remote_desktop_url_strips_credentials_and_query():
    browser = _browser()
    assert browser.public_ui_url(
        "https://gui:secret-password@127.0.0.1:3001/?token=secret#private"
    ) == "https://127.0.0.1:3001/"
    assert "secret-password" not in browser.public_ui_url(
        "https://gui:secret-password@127.0.0.1:3001/?token=secret#private"
    )
