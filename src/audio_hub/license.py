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


def public_key_b64() -> str:
    """验证公钥（32 字节 raw 的 base64）；未配置返回空串。"""
    return os.environ.get("AUDIO_HUB_LICENSE_PUBLIC_KEY", "").strip()


def license_required() -> bool:
    """是否启用强制门禁（环境变量 AUDIO_HUB_LICENSE_REQUIRED）。"""
    return os.environ.get("AUDIO_HUB_LICENSE_REQUIRED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


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


async def check_state(db) -> dict[str, Any]:
    """当前授权状态（门禁与激活页共用）。

    Returns:
        {"required": bool, "configured": bool(公钥已配置),
         "valid": bool, "payload": dict|None, "key": str}
    """
    required = license_required()
    pub = public_key_b64()
    key = await get_stored_key(db)
    payload = verify_license(key, pub) if key else None
    return {
        "required": required,
        "configured": bool(pub),
        "valid": payload is not None,
        "payload": payload,
        "key": key,
    }
