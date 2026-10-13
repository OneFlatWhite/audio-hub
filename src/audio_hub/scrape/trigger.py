"""刮削触发：归档落位后通知外部刮削器。

两条通道都是"尽力而为"：失败只返回 (False, message)，绝不抛出 —— 刮削是归档
的下游旁路，MTW / ABS 挂了不应把任务打成 failed，更不应阻塞轮询。

- 音乐：POST mtw_webhook_url，JSON {path, kind:"music", title}
- 有声书：POST {abs_url}/api/libraries/{abs_library_id}/scan，Bearer abs_token
"""

from __future__ import annotations

from pathlib import Path

import httpx
from loguru import logger

from ..config import ScrapeConfig
from ..models import WatchKind


async def trigger_music(cfg: ScrapeConfig, path: str | Path, title: str) -> tuple[bool, str]:
    """通知 MusicTag Web 刮削 scratch 副本。未配置 webhook 视为跳过（ok=True）。"""
    url = (cfg.mtw_webhook_url or "").strip()
    if not url:
        return True, "mtw_webhook_url 未配置，跳过"
    payload = {"path": str(path), "kind": "music", "title": title}
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
            resp = await client.post(url, json=payload)
        if resp.is_success:
            return True, f"MTW webhook {resp.status_code}"
        return False, f"MTW webhook HTTP {resp.status_code}"
    except Exception as e:  # noqa: BLE001 - 旁路调用，任何异常都只记录不外溢
        logger.warning(f"MTW webhook 调用失败: {type(e).__name__}: {e}")
        return False, f"MTW webhook 异常: {type(e).__name__}: {e}"


async def trigger_audiobook(
    cfg: ScrapeConfig, path: str | Path | None = None, title: str = "",
) -> tuple[bool, str]:
    """让 Audiobookshelf 重扫有声书库。path/title 仅用于日志，ABS 扫的是整个库。"""
    base = (cfg.abs_url or "").strip().rstrip("/")
    if not base:
        return True, "abs_url 未配置，跳过"
    if not cfg.abs_library_id:
        return False, "abs_library_id 未配置"
    url = f"{base}/api/libraries/{cfg.abs_library_id}/scan"
    headers = {"Authorization": f"Bearer {cfg.abs_token}"} if cfg.abs_token else {}
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
            resp = await client.post(url, headers=headers)
        if resp.is_success:
            return True, f"ABS scan {resp.status_code}"
        return False, f"ABS scan HTTP {resp.status_code}"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ABS 扫库触发失败 ({title!r}): {type(e).__name__}: {e}")
        return False, f"ABS scan 异常: {type(e).__name__}: {e}"


async def trigger_for_kind(
    cfg: ScrapeConfig, kind: WatchKind | str, path: str | Path, title: str,
) -> tuple[bool, str]:
    """按类型分流到对应刮削器。"""
    kind_val = kind.value if isinstance(kind, WatchKind) else str(kind)
    if kind_val == WatchKind.AUDIOBOOK.value:
        return await trigger_audiobook(cfg, path, title)
    return await trigger_music(cfg, path, title)
