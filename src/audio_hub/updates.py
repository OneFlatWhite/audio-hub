"""新版本检测：每日拉一次 GitHub Releases 最新 tag，界面提示「有新版本」。

原则：
- **只提示，绝不自动升级**——NAS 上自动动容器/环境是事故之源（watchtower 式
  的静默升级可能带来配置迁移断层），升级永远由用户手动执行（见 docs/UPGRADE.md）；
- 数据来自 GitHub 公开 API（releases/latest），不带任何用户信息，单次请求；
- 关闭方式：环境变量 AUDIO_HUB_NO_UPDATE_CHECK=1（隐私 / 离线场景）；
- 网络失败静默跳过：提示条是锦上添花，不能变成噪音。
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx
from loguru import logger

from . import __version__

RELEASES_API = "https://api.github.com/repos/OneFlatWhite/audio-hub/releases/latest"


def opt_out() -> bool:
    return os.environ.get("AUDIO_HUB_NO_UPDATE_CHECK", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def parse_version(v: str) -> tuple[int, ...]:
    """'v0.14.1' / '0.14' → (0, 14, 1)；非数字段忽略。"""
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts[:3]) + (0,) * (3 - len(parts[:3]))


def is_newer(latest: str, current: str) -> bool:
    try:
        return parse_version(latest) > parse_version(current)
    except ValueError:
        return False


async def check(app, *, fetch=None) -> dict[str, Any] | None:
    """拉取最新 release，结果写 app.state.update_info 并返回。

    fetch 可注入供测试。任何失败返回 None 且不影响旧值。
    """
    if opt_out():
        return None

    async def _default() -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(RELEASES_API, headers={"User-Agent": "audio-hub"})
            r.raise_for_status()
            return r.json()

    try:
        data = await (fetch or _default)()
        tag = str(data.get("tag_name") or "").strip()
        if not tag:
            return None
        info = {
            "latest": tag.lstrip("v"),
            "current": __version__,
            "newer": is_newer(tag, __version__),
            "url": data.get("html_url") or f"{RELEASES_API.rsplit('/', 1)[0].replace('api.github.com/repos', 'github.com')}/releases",
            "notes": (data.get("body") or "")[:400],
        }
        app.state.update_info = info
        if info["newer"]:
            logger.info(f"发现新版本 v{info['latest']}（当前 v{__version__}），升级步骤见 docs/UPGRADE.md")
        return info
    except Exception as e:  # noqa: BLE001 - 检测失败静默跳过
        logger.debug(f"新版本检测失败（跳过）: {e}")
        return None
