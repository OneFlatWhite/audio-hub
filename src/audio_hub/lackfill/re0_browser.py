"""Re0 NAS 浏览器 CDP 健康检查与主源可用性；不暴露调试 URL。"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

import httpx


async def inspect(cdp_url: str, client: httpx.AsyncClient | None = None) -> dict:
    own = client is None
    if own:
        client = httpx.AsyncClient(trust_env=False, timeout=3.0)
    base = cdp_url.rstrip("/")
    try:
        version_response = await client.get(f"{base}/json/version")
        pages_response = await client.get(f"{base}/json/list")
        if version_response.status_code != 200 or pages_response.status_code != 200:
            raise RuntimeError("CDP HTTP 状态异常")
        version = version_response.json()
        raw_pages = pages_response.json()
        pages = raw_pages if isinstance(raw_pages, list) else []
        re0_open = any(
            isinstance(page, dict)
            and (urlsplit(str(page.get("url") or "")).hostname or "") in {"re0.me", "www.re0.me"}
            for page in pages
        )
        return {
            "cdp_ok": True,
            "ready": re0_open,
            "state": "ready" if re0_open else "no_page",
            "browser": str(version.get("Browser") or "")[:100],
            "page_count": len(pages),
            "re0_open": re0_open,
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 —— 健康检查统一降级，不回显网络详情
        return {
            "cdp_ok": False,
            "ready": False,
            "state": "offline",
            "browser": "",
            "page_count": 0,
            "re0_open": False,
            "error": type(exc).__name__,
        }
    finally:
        if own:
            await client.aclose()


def primary_mode(cdp_health: dict, worker: dict) -> Literal["available", "manual", "unavailable"]:
    if worker.get("online"):
        if worker.get("status") in {"challenge", "manual", "quota"}:
            return "manual"
        if worker.get("auto_running"):
            return "available"
    return "unavailable"


def public_ui_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.rsplit("@", 1)[-1]
    return f"{parsed.scheme}://{host}{parsed.path}"
