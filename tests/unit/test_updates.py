"""新版本检测测试：版本比较、拉取写入 state、opt-out、失败静默。"""

from __future__ import annotations

from types import SimpleNamespace

from audio_hub import updates


def test_parse_version():
    assert updates.parse_version("v0.14.1") == (0, 14, 1)
    assert updates.parse_version("0.14") == (0, 14, 0)
    assert updates.parse_version("v1.2.3") > updates.parse_version("v0.99.9")


def test_is_newer():
    assert updates.is_newer("v0.15.0", "v0.14.1") is True
    assert updates.is_newer("v0.14.1", "v0.14.1") is False
    assert updates.is_newer("v0.13", "v0.14.1") is False


async def test_check_writes_state_and_flags_newer(monkeypatch):
    async def fake_fetch():
        return {
            "tag_name": "v99.0.0",
            "html_url": "https://github.com/OneFlatWhite/audio-hub/releases/tag/v99.0.0",
            "body": "说明文本",
        }

    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.delenv("AUDIO_HUB_NO_UPDATE_CHECK", raising=False)
    info = await updates.check(app, fetch=fake_fetch)
    assert info is not None and info["newer"] is True and info["latest"] == "99.0.0"
    assert app.state.update_info is info


async def test_check_not_newer(monkeypatch):
    async def fake_fetch():
        return {"tag_name": "v0.0.1", "html_url": "https://x", "body": ""}

    app = SimpleNamespace(state=SimpleNamespace())
    info = await updates.check(app, fetch=fake_fetch)
    assert info is not None and info["newer"] is False


async def test_check_opt_out(monkeypatch):
    monkeypatch.setenv("AUDIO_HUB_NO_UPDATE_CHECK", "1")
    called = False

    async def fake_fetch():
        nonlocal called
        called = True

    assert await updates.check(SimpleNamespace(state=SimpleNamespace()), fetch=fake_fetch) is None
    assert called is False


async def test_check_failure_returns_none(monkeypatch):
    async def fake_fetch():
        raise ConnectionError("offline")

    app = SimpleNamespace(state=SimpleNamespace())
    assert await updates.check(app, fetch=fake_fetch) is None
