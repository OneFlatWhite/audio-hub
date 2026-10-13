"""下载器连通性探测（只读）：TCP → HTTP → qb API。

供两处复用：
- 下载器中心页「测试连接」按钮（routes_downloaders → probe_qb）
- scripts/check_qb.py 诊断脚本（probe_tcp / probe_http 原本就在脚本里，v0.6 迁入此处）

三层探测分开报错的原因：把「网络不通」「qb 没在这个端口」「凭据不对」三类问题
区分开，否则用户只看到一行「qb 客户端不可用」，不知道该查网线还是查密码。

本模块**只读**：不添加种子、不改 qb 任何设置。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any

from loguru import logger

#: 一次 qb 探测的总预算；超时视为连接失败而非卡死请求线程
PROBE_TIMEOUT_SECONDS = 15.0

#: qb WebUI 常见状态码 → 诊断建议（脚本与页面共用文案）
QB_HTTP_HINTS = {
    403: (
        "qb 拒绝访问。常见原因（qb WebUI → 选项 → Web用户界面 → 安全）："
        "「启用 Host 头验证」开着、或访问方 IP 不在白名单、或 CSRF 保护拦截了跨主机请求。"
    ),
    401: "凭据被拒：用户名 / 密码或 API Key 无效，或 qb 版本 < 5.2 不支持 API Key。",
    404: "路径不对：qb 的 WebUI 可能配了「备用 WebUI」或子路径前缀。",
}


def probe_tcp(host: str, port: int, timeout: float = 6.0) -> tuple[bool, str]:
    """TCP 层可达性。先把「网络不通」与「凭据不对」分开，避免混为一谈。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except TimeoutError:
        return False, (
            f"连接超时（{timeout}s）。可能：IP 写错、NAS 防火墙拦截、"
            "或 qb 只监听在 NAS 本机回环地址"
        )
    except ConnectionRefusedError:
        return False, (
            "连接被拒绝：端口上没有服务在监听。可能 qb 未运行、端口不是这个、"
            "或 qb WebUI 绑定在 127.0.0.1（需改成 0.0.0.0 或 NAS 内网 IP）"
        )
    except socket.gaierror as e:
        return False, f"主机名解析失败: {e}"
    except OSError as e:
        return False, f"网络错误: {type(e).__name__}: {e}"


def probe_http(host: str, port: int) -> tuple[int | None, str]:
    """裸 HTTP 探测，拿到 qb 的真实响应码（比库的异常信息更能定位问题）。"""
    import httpx

    last = ""
    for scheme in ("http", "https"):
        url = f"{scheme}://{host}:{port}/api/v2/app/version"
        try:
            r = httpx.get(url, timeout=8.0, verify=False)
            return r.status_code, f"{scheme} → {r.status_code} {r.text[:120]!r}"
        except Exception as e:  # noqa: BLE001
            last = f"{scheme} 失败: {type(e).__name__}: {e}"
    return None, last


def _empty_result() -> dict[str, Any]:
    return {
        "ok": False,
        "version": None,
        "api_version": None,
        "default_save_path": None,
        "free_space_bytes": None,
        "active_torrents": None,
        "error": None,
    }


def _make_client(cfg):
    """按 DownloaderConfig 构造 qbittorrentapi.Client（username 优先，否则 api_key）。"""
    import qbittorrentapi

    if cfg.username:
        return qbittorrentapi.Client(
            host=cfg.host, port=cfg.port, username=cfg.username, password=cfg.password,
        )
    return qbittorrentapi.Client(host=cfg.host, port=cfg.port, api_key=cfg.api_key)


def probe_qb_sync(cfg) -> dict[str, Any]:
    """同步版探测：TCP → 登录 → 版本 / 默认路径 / 剩余空间 / 活跃种子数。

    任何一步失败都把 error 填上并尽早返回；成功登录后的附加信息（路径、空间）
    单项失败不影响 ok —— 它们只是参考数据。
    """
    result = _empty_result()
    if not cfg.host or not cfg.port:
        result["error"] = "host / port 未配置"
        return result
    if not cfg.username and not cfg.api_key:
        result["error"] = "未配置凭据（用户名 / 密码或 API Key）"
        return result

    ok, err = probe_tcp(cfg.host, int(cfg.port))
    if not ok:
        result["error"] = f"TCP 不可达: {err}"
        return result

    try:
        import qbittorrentapi
    except ImportError as e:  # pragma: no cover - 依赖缺失属环境问题
        result["error"] = f"qbittorrentapi 未安装: {e}"
        return result

    client = None
    try:
        client = _make_client(cfg)
        client.auth_log_in()
        result["version"] = str(client.app.version)
        result["api_version"] = str(client.app.web_api_version)
        result["ok"] = True
    except qbittorrentapi.LoginFailed as e:
        result["error"] = f"认证失败（凭据无效？）: {e}"
        return result
    except qbittorrentapi.APIConnectionError as e:
        code, detail = probe_http(cfg.host, int(cfg.port))
        hint = QB_HTTP_HINTS.get(code or 0)
        result["error"] = f"连接失败: {e}；HTTP 探测 {detail}" + (f"；{hint}" if hint else "")
        return result
    except Exception as e:  # noqa: BLE001 - 探测不得抛出
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    # 以下为参考信息，单项失败只记日志
    try:
        result["default_save_path"] = str(client.app_default_save_path())
    except Exception as e:  # noqa: BLE001
        logger.debug(f"下载器 {cfg.id} 取默认保存路径失败: {e}")
    try:
        info = client.sync_maindata()
        free = (info.get("server_state") or {}).get("free_space_on_disk")
        if free is not None:
            result["free_space_bytes"] = int(free)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"下载器 {cfg.id} 取剩余空间失败: {e}")
    try:
        result["active_torrents"] = len(client.torrents_info(status_filter="active"))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"下载器 {cfg.id} 取活跃种子数失败: {e}")
    with contextlib.suppress(Exception):
        client.auth_log_out()
    return result


async def probe_qb(cfg, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """异步入口：在线程里跑同步探测，整体限时，供 Web 请求调用不阻塞事件循环。

    Returns:
        dict(ok, version, api_version, default_save_path, free_space_bytes,
             active_torrents, error)
    """
    try:
        return await asyncio.wait_for(asyncio.to_thread(probe_qb_sync, cfg), timeout=timeout)
    except TimeoutError:
        result = _empty_result()
        result["error"] = f"探测超时（>{timeout:.0f}s）"
        return result


def probe_transmission_sync(cfg) -> dict[str, Any]:
    """Transmission 版探测：TCP → 构造客户端（构造即握手，兼做认证）→ 会话信息。

    transmission-rpc 的异常已按层次分类（Auth / Connect / Timeout / 通用），
    比裸 HTTP 探测更能定位问题；会话版本、下载目录、剩余空间、活跃种子数
    与 qb 版探测同字段返回，页面无需区分展示。
    """
    result = _empty_result()
    if not cfg.host or not cfg.port:
        result["error"] = "host / port 未配置"
        return result

    ok, err = probe_tcp(cfg.host, int(cfg.port))
    if not ok:
        result["error"] = f"TCP 不可达: {err}"
        return result

    try:
        import transmission_rpc
    except ImportError as e:  # pragma: no cover - 依赖缺失属环境问题
        result["error"] = f"transmission-rpc 未安装: {e}"
        return result

    client = None
    session = None
    try:
        client = transmission_rpc.Client(
            host=cfg.host, port=int(cfg.port),
            username=cfg.username or None, password=cfg.password or None,
            timeout=10,
        )
        session = client.get_session()
        result["version"] = str(session.version)
        result["ok"] = True
    except transmission_rpc.TransmissionAuthError as e:
        result["error"] = f"认证失败（用户名 / 密码无效？）: {e}"
        return result
    except transmission_rpc.TransmissionConnectError as e:
        result["error"] = (
            f"连接失败（端口不对？WebUI 默认 9091；或 RPC 白名单拦了本机 IP）: {e}"
        )
        return result
    except Exception as e:  # noqa: BLE001 - 探测不得抛出
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    # 以下为参考信息，单项失败只记日志
    try:
        result["default_save_path"] = str(session.download_dir)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"下载器 {cfg.id} 取下载目录失败: {e}")
    try:
        free = client.free_space(result["default_save_path"] or "/downloads")
        if free is not None:
            result["free_space_bytes"] = int(free)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"下载器 {cfg.id} 取剩余空间失败: {e}")
    try:
        result["active_torrents"] = sum(
            1 for t in client.get_torrents()
            if str(getattr(getattr(t, "status", ""), "value", getattr(t, "status", "")))
            != "stopped"
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"下载器 {cfg.id} 取种子数失败: {e}")
    return result


async def probe_downloader(cfg, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """按下载器类型分发的异步探测入口（Web「测试连接」统一走这里）。"""
    dl_type = (getattr(cfg, "type", "") or "qbittorrent").strip().lower()
    fn = probe_transmission_sync if dl_type == "transmission" else probe_qb_sync
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, cfg), timeout=timeout)
    except TimeoutError:
        result = _empty_result()
        result["error"] = f"探测超时（>{timeout:.0f}s）"
        return result
