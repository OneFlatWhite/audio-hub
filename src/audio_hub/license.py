"""授权码：Ed25519 签名的离线许可证（star 换码，见 docs/LICENSE-ACTIVATION.md）。

格式：AH1.<payload_b64url>.<signature_b64url>
payload（JSON，键序固定）::
    {"v": 1, "user": "<github用户名>", "repo": "OneFlatWhite/audio-hub",
     "issued_at": <unix秒>, "exp": <unix秒|null>}

- 签发：scripts/issue_license.py（Ed25519 私钥只保存在作者手里，不入仓库）
- 验证：公钥经环境变量 AUDIO_HUB_LICENSE_PUBLIC_KEY 注入（32 字节 raw 的 base64）
- 存储：app_settings 表 license_key 键（明文即可——它本身是签名产物，不含秘密）
- 强制模式：AUDIO_HUB_LICENSE_REQUIRED=1 时，无有效授权码则除激活 / 登录 /
  初始化 / 静态资源 / 健康检查外的所有页面跳转 /activate

公钥未配置时视为「未启用授权校验」：verify 一律返回 None，但 key 仍可保存
（作者以后配置了公钥即可生效）。
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from loguru import logger

LICENSE_KEY_PREFIX = "AH1."
LICENSE_SETTINGS_KEY = "license_key"
DEFAULT_REPO = "OneFlatWhite/audio-hub"

# ── 在线复核（吊销）─────────────────────────────────────────
#: 发放端的复核端点（docs/license-issuer/worker.js 的 /api/status）。
#: 配置后，程序会定期核对本地授权码的 jti 是否已被吊销（用户取消 Star）。
VERIFY_URL_ENV = "AUDIO_HUB_LICENSE_VERIFY_URL"
#: 离线宽限：距上次成功复核超过该天数即视为过期（NAS 断网也能用一段时间）
GRACE_ENV = "AUDIO_HUB_LICENSE_GRACE_DAYS"
#: 复核间隔（调度 job）
REVALIDATE_INTERVAL_HOURS = 24

_LAST_CHECK_KEY = "license_last_check"
_REVOKED_KEY = "license_revoked"


def public_key_b64() -> str:
    """验证公钥（32 字节 raw 的 base64）；未配置返回空串。"""
    return os.environ.get("AUDIO_HUB_LICENSE_PUBLIC_KEY", "").strip()


def license_required() -> bool:
    """是否启用强制门禁。**v0.17 起默认开启**：未激活授权码前只放行
    激活 / 登录 / 初始化页。开发或特殊场景可显式关闭：
    AUDIO_HUB_LICENSE_REQUIRED=0（false/no/off 同义）。"""
    raw = os.environ.get("AUDIO_HUB_LICENSE_REQUIRED", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def verify_url() -> str:
    """发放端复核端点；未配置 = 不做在线复核（纯离线模式，无吊销能力）。"""
    return os.environ.get(VERIFY_URL_ENV, "").strip()


def grace_seconds() -> int:
    """复核宽限时长（秒），默认 30 天。"""
    try:
        return int(float(os.environ.get(GRACE_ENV, "30"))) * 86400
    except ValueError:
        return 30 * 86400


def _b64u_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def verify_license(key: str, pub_b64: str | None = None) -> dict[str, Any] | None:
    """校验授权码，返回 payload；无效 / 公钥未配置 / 已过期返回 None。

    签名覆盖 payload 原始字节（不重序列化），避免 JSON 键序差异导致误判。
    """
    pub_b64 = pub_b64 if pub_b64 is not None else public_key_b64()
    if not pub_b64 or not key:
        return None
    parts = key.strip().split(".")
    if len(parts) != 3 or parts[0] != "AH1":
        return None
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        payload_raw = _b64u_decode(parts[1])
        sig = _b64u_decode(parts[2])
        Ed25519PublicKey.from_public_bytes(_b64u_decode(pub_b64)).verify(sig, payload_raw)
        payload = json.loads(payload_raw)
        exp = payload.get("exp")
        if exp and time.time() > float(exp):
            return None
        return payload
    except (InvalidSignature, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        logger.debug(f"授权码校验失败: {e}")
        return None


async def get_stored_key(db) -> str:
    """app_settings 里的授权码（未设置返回空串）。"""
    async with db.execute(
        "SELECT value FROM app_settings WHERE key = ?", (LICENSE_SETTINGS_KEY,)
    ) as cur:
        row = await cur.fetchone()
    return (row["value"] or "").strip() if row else ""


async def set_stored_key(db, key: str) -> None:
    """保存 / 清除（空串清除）授权码。"""
    key = (key or "").strip()
    if key:
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (LICENSE_SETTINGS_KEY, key),
        )
    else:
        await db.execute("DELETE FROM app_settings WHERE key = ?", (LICENSE_SETTINGS_KEY,))
    await db.commit()


async def _get_meta(db, key: str) -> str:
    async with db.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ) as cur:
        row = await cur.fetchone()
    return (row["value"] or "").strip() if row else ""


async def _set_meta(db, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )
    await db.commit()


async def _default_fetch(url: str, params: dict) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def revalidate(db, *, fetch=None) -> dict:
    """在线复核本地授权码是否被吊销（用户取消 Star 后发放端会吊销）。

    - 无 key / 验签不过 / 未配置复核端点 / 无 jti（旧版码）→ 不改吊销状态；
    - 复核失败（网络）→ 保持原状态（宽限期内仍可用，到期再复核）；
    - 结果写 app_settings：license_last_check（unix秒）、license_revoked（0/1）。
    可注入 fetch 供测试。
    """
    now = int(time.time())
    await _set_meta(db, _LAST_CHECK_KEY, str(now))
    key = await get_stored_key(db)
    payload = verify_license(key)
    if not payload:
        return {"checked": False, "reason": "no-valid-key"}
    if not verify_url():
        await _set_meta(db, _REVOKED_KEY, "0")
        return {"checked": False, "reason": "no-verify-url"}
    jti = payload.get("jti")
    if not jti:
        # 旧版无 jti 的授权码无法在线核对：保持有效（grandfathered）
        await _set_meta(db, _REVOKED_KEY, "0")
        return {"checked": False, "reason": "no-jti"}
    fetch = fetch or _default_fetch
    try:
        data = await fetch(verify_url(), {"jti": jti})
        revoked = bool(data.get("revoked"))
    except Exception as e:  # noqa: BLE001 - 网络失败保原状态，宽限期内照常可用
        logger.debug(f"授权复核失败（保持原状态）: {e}")
        return {"checked": False, "reason": f"unreachable: {type(e).__name__}"}
    await _set_meta(db, _REVOKED_KEY, "1" if revoked else "0")
    logger.info(f"授权复核完成: jti={jti} revoked={revoked}")
    return {"checked": True, "revoked": revoked}


async def check_state(db) -> dict[str, Any]:
    """当前授权状态（门禁与激活页共用）。

    valid = 签名有效 AND 未被吊销 AND（未配置在线复核 或 复核未超宽限期）。
    """
    required = license_required()
    pub = public_key_b64()
    key = await get_stored_key(db)
    payload = verify_license(key, pub) if key else None
    revoked = await _get_meta(db, _REVOKED_KEY) == "1"
    stale = False
    if payload and verify_url():
        try:
            last = int(float(await _get_meta(db, _LAST_CHECK_KEY) or 0))
        except ValueError:
            last = 0
        stale = (time.time() - last) > grace_seconds()
    valid = payload is not None and not revoked and not stale
    return {
        "required": required,
        "configured": bool(pub),
        "valid": valid,
        "payload": payload,
        "key": key,
        "revoked": revoked,
        "stale": stale,
    }
