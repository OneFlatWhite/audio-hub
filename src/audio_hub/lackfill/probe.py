"""lackfill 外部依赖连通性探测。

FRONTEND-RULES 约定:凡是与外部程序打通的通道(Emby/TMDB/115/SA/CD2 挂载),
必须能从界面一键验证「连没连通」——参照站点中心「测试认证」与下载器中心
「测试连接」的既有语义。全部只读探测,不产生转存/解锁等副作用:
- emby: GET /System/Info(X-Emby-Token),200 即认证通过
- tmdb: GET /3/configuration(v3 key 或 v4 Bearer,与 tmdb.py 同规则)
- p115: 向生产同款 snap 端点发一次格式合法的假分享码——只验证
  Web API 可达性;cookie 有效性只能在真实验包时确认,如实告知
- sa:   POST /api/v1/login/access-token(与 sa.py 生产端点一致),拿到
  access_token 即「连通且凭据正确」
- cd2:  检查容器内 staging/feed 目录存在性——挂载缺失是最常见的
  「配置全对但不工作」根因
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .p115 import _SNAP_URL

_TIMEOUT = httpx.Timeout(12.0)
# 格式合法、几乎不可能命中的分享码,仅用于探测接口可达
_PROBE_SHARE = "probe0000000000000"


async def _emby(cfg) -> dict:
    if not cfg.emby_url:
        return {"ok": False, "detail": "未配置 Emby 地址"}
    try:
        async with httpx.AsyncClient(
            headers={"X-Emby-Token": cfg.emby_api_key}, trust_env=False, timeout=_TIMEOUT
        ) as client:
            resp = await client.get(f"{cfg.emby_url.rstrip('/')}/System/Info")
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"连不上: {type(exc).__name__}"}
    if resp.status_code == 401:
        return {"ok": False, "detail": "可达,但 API Key 被拒绝(401)"}
    if resp.status_code != 200:
        return {"ok": False, "detail": f"HTTP {resp.status_code}"}
    info = resp.json()
    server = info.get("ServerName") or info.get("Server") or "Emby"
    version = info.get("Version") or ""
    return {"ok": True, "detail": f"{server} {version}".strip() + " 认证通过"}


async def _tmdb(cfg) -> dict:
    if not cfg.tmdb_api_key:
        return {"ok": False, "detail": "未配置 TMDB API Key"}
    headers, params = (
        ({"Authorization": f"Bearer {cfg.tmdb_api_key}"}, {})
        if cfg.tmdb_api_key.startswith("eyJ")
        else ({}, {"api_key": cfg.tmdb_api_key})
    )
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://api.themoviedb.org/3/configuration", headers=headers, params=params
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"连不上: {type(exc).__name__}"}
    if resp.status_code == 401:
        return {"ok": False, "detail": "可达,但 Key 无效(401)"}
    if resp.status_code != 200:
        return {"ok": False, "detail": f"HTTP {resp.status_code}"}
    images = resp.json().get("images", {}).get("base_url", "")
    return {"ok": True, "detail": "Key 有效" + (f"({images})" if images else "")}


async def _p115(cfg) -> dict:
    if not cfg.p115_cookie:
        return {"ok": False, "detail": "未配置 115 Cookie"}
    try:
        async with httpx.AsyncClient(
            headers={"Cookie": cfg.p115_cookie, "User-Agent": "audio-hub-probe"},
            trust_env=False,
            timeout=_TIMEOUT,
        ) as client:
            resp = await client.post(_SNAP_URL, data={"share_code": _PROBE_SHARE, "receive_code": ""})
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"连不上 115 Web API: {type(exc).__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "detail": f"HTTP {resp.status_code}"}
    try:
        resp.json()
        shape = "响应为 JSON"
    except ValueError:
        shape = "响应不是 JSON(可能被风控页拦截)"
    return {
        "ok": True,
        "detail": f"115 Web API 连通,{shape};Cookie 有效性在真实验包时确认",
    }


async def _sa(cfg) -> dict:
    if not cfg.sa_base_url:
        return {"ok": False, "detail": "未配置 SA 服务地址(仅主链 cookie 模式可不配)"}
    if not (cfg.sa_username and cfg.sa_secret):
        return {"ok": False, "detail": "SA 用户名/密码未配齐"}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{cfg.sa_base_url.rstrip('/')}/api/v1/login/access-token",
                data={"username": cfg.sa_username, "password": cfg.sa_secret},
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"连不上: {type(exc).__name__}"}
    if resp.status_code == 401:
        return {"ok": False, "detail": "可达,但用户名/密码被拒绝(401)"}
    if resp.status_code != 200:
        return {"ok": False, "detail": f"HTTP {resp.status_code}"}
    return {"ok": True, "detail": "登录成功,拿到 access_token(连通且凭据正确)"}


async def _cd2(cfg) -> dict:
    if not cfg.cloud_verify_enabled:
        return {"ok": False, "detail": "CD2 落盘验证未启用"}
    staging = Path(cfg.cloud_staging_dir)
    feed = Path(cfg.cloud_feed_dir)
    problems = []
    if not staging.is_dir():
        problems.append(f"云下载目录不存在: {staging}")
    if not feed.is_dir():
        problems.append(f"集合爱影目录不存在: {feed}")
    if problems:
        return {"ok": False, "detail": ";".join(problems) + "(容器内视角——多为 NAS 未挂载 /CloudNAS)"}
    return {"ok": True, "detail": f"staging 与 feed 目录均可访问(容器内 {staging} / {feed})"}


_TARGETS = {"emby": _emby, "tmdb": _tmdb, "p115": _p115, "sa": _sa, "cd2": _cd2}


async def probe(target: str, lackfill_cfg) -> dict:
    """探测一个通道。返回 {"target","ok","detail"};未知目标抛 ValueError。"""
    func = _TARGETS.get(target)
    if func is None:
        raise ValueError(f"未知探测目标: {target}")
    result = await func(lackfill_cfg)
    return {"target": target, "ok": bool(result["ok"]), "detail": str(result["detail"])[:200]}
