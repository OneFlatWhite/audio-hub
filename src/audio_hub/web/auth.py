"""认证与会话模块（PRD §12.3 / §12.4）。

设计要点：
- 密码哈希用 **argon2-cffi（Argon2id）**，明确不用 passlib（已停止维护，
  且与 bcrypt 5.0.0 不兼容）。参数用库默认值，argon2-cffi 会随时间自动升级。
- 会话为**服务端 session** + HttpOnly/SameSite=Strict cookie，
  **不用 JWT 存 localStorage**——一旦 XSS，localStorage 里的 token 直接失守。
- 登录限流用滑动窗口，对齐 Navidrome AuthRequestLimit 默认值 5。
  教训来源：Navidrome 自身出过 CVE-2025-27112（认证绕过）与
  GHSA-p994-r776-mw52（未认证暴力破解），故自研认证必须假设自己会写错。
- 首次启动不预设默认密码，强制创建管理员（MTW 的 admin/admin 是反面教材）。
"""

from __future__ import annotations

import secrets
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from loguru import logger

from ..timeutil import iso_utc, parse_iso, utcnow

# argon2-cffi 默认即 Argon2id，参数由库维护，不手调
_hasher = PasswordHasher()

SESSION_COOKIE = "audio_hub_session"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"


class AuthError(Exception):
    """认证层异常。"""


class RateLimited(AuthError):
    """登录尝试超限。"""

    def __init__(self, retry_after_minutes: int):
        self.retry_after_minutes = retry_after_minutes
        super().__init__(f"登录尝试过于频繁，请 {retry_after_minutes} 分钟后重试")


# ── 密码 ────────────────────────────────────────────────────

def hash_password(plaintext: str) -> str:
    """Argon2id 哈希。返回串自带算法与参数，便于未来透明升级。"""
    if not plaintext:
        raise AuthError("密码不能为空")
    return _hasher.hash(plaintext)


def verify_password(password_hash: str, plaintext: str) -> bool:
    """校验密码。

    失败一律返回 False，**不区分「用户不存在」与「密码错误」**——
    避免通过响应差异枚举用户名（Navidrome CVE-2025-27112 的教训之一）。
    """
    if not password_hash or not plaintext:
        return False
    try:
        return _hasher.verify(password_hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception as e:  # noqa: BLE001 - 认证失败不得抛出未预期异常
        logger.error(f"密码校验异常，按失败处理: {type(e).__name__}")
        return False


def needs_rehash(password_hash: str) -> bool:
    """argon2 参数升级后需要重新哈希（登录成功时透明完成）。"""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:  # noqa: BLE001
        return False


# ── 用户 ────────────────────────────────────────────────────

async def create_admin(db, username: str, password: str) -> str:
    """创建管理员账号。用户名不建议用 admin（避开扫描字典第一批）。"""
    username = username.strip()
    if not username:
        raise AuthError("用户名不能为空")
    if len(password) < 8:
        raise AuthError("密码至少 8 位")

    existing = await get_user_by_username(db, username)
    if existing:
        raise AuthError(f"用户名 {username!r} 已存在")

    user_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO users (id, username, password_hash, is_admin, created_at, updated_at)
           VALUES (?,?,?,?,?,?)""",
        (user_id, username, hash_password(password), 1, iso_utc(), iso_utc()),
    )
    await db.commit()
    logger.info(f"已创建管理员账号: {username}")
    return user_id


async def get_user_by_username(db, username: str) -> dict | None:
    async with db.execute(
        "SELECT * FROM users WHERE username = ?", (username.strip(),)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_id(db, user_id: str) -> dict | None:
    async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def count_users(db) -> int:
    async with db.execute("SELECT COUNT(*) AS n FROM users") as cur:
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def change_password(db, user_id: str, new_password: str) -> None:
    """改密后吊销该用户全部会话（PRD §12.3「登出所有设备」）。"""
    if len(new_password) < 8:
        raise AuthError("密码至少 8 位")
    await db.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (hash_password(new_password), iso_utc(), user_id),
    )
    await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    await db.commit()
    logger.info(f"用户 {user_id[:8]} 已改密，全部会话已吊销")


# ── 会话 ────────────────────────────────────────────────────

async def create_session(db, user_id: str, session_days: int = 7) -> tuple[str, str]:
    """创建服务端会话。

    Returns:
        (session_token, csrf_token) — token 写入 HttpOnly cookie，
        csrf_token 需下发给前端（放页面 meta 或隐藏字段）。
    """
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = utcnow()
    expires = now + _days_delta(session_days)

    await db.execute(
        """INSERT INTO sessions (id, user_id, csrf_token, created_at, expires_at, last_seen_at)
           VALUES (?,?,?,?,?,?)""",
        (session_id, user_id, csrf_token, iso_utc(),
         expires.isoformat(timespec="seconds"), iso_utc()),
    )
    await db.commit()
    return session_id, csrf_token


async def resolve_session(db, session_token: str | None) -> tuple[dict, str] | None:
    """校验会话 token，返回 (user_row, csrf_token)；无效/过期返回 None。

    有效时滑动续期 last_seen_at。
    """
    if not session_token:
        return None

    async with db.execute(
        """SELECT s.csrf_token, s.expires_at, u.id, u.username, u.is_admin
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.id = ?""",
        (session_token,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None

    expires = parse_iso(row["expires_at"])
    if expires is None or expires < utcnow():
        await revoke_session(db, session_token)
        return None

    await db.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
        (iso_utc(), session_token),
    )
    await db.commit()

    user = {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}
    return user, row["csrf_token"]


async def revoke_session(db, session_token: str) -> None:
    await db.execute("DELETE FROM sessions WHERE id = ?", (session_token,))
    await db.commit()


async def revoke_all_sessions(db, user_id: str) -> int:
    cur = await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    await db.commit()
    return cur.rowcount or 0


async def purge_expired_sessions(db) -> int:
    """清理过期会话。定时任务调用，避免 sessions 表无限增长。"""
    cur = await db.execute("DELETE FROM sessions WHERE expires_at < ?", (iso_utc(),))
    await db.commit()
    return cur.rowcount or 0


# ── 登录限流（滑动窗口）──────────────────────────────────────

async def check_rate_limit(
    db, ip: str, *, limit: int = 5, window_minutes: int = 15
) -> None:
    """超限则抛 RateLimited。

    用 DB 而非内存计数：容器重启后限流状态不丢失，
    否则重启即等于重置爆破计数器。
    """
    from datetime import timedelta
    since = (utcnow() - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
    async with db.execute(
        "SELECT COUNT(*) AS n FROM login_attempts WHERE ip = ? AND success = 0 AND attempted_at > ?",
        (ip, since),
    ) as cur:
        row = await cur.fetchone()
    failures = int(row["n"]) if row else 0
    if failures >= limit:
        raise RateLimited(window_minutes)


async def record_attempt(db, ip: str, username: str | None, success: bool) -> None:
    await db.execute(
        "INSERT INTO login_attempts (ip, username, success, attempted_at) VALUES (?,?,?,?)",
        (ip, username, int(success), iso_utc()),
    )
    # 成功登录时清除该 IP 的失败计数，避免正常使用被历史失败拖累
    if success:
        await db.execute(
            "DELETE FROM login_attempts WHERE ip = ? AND success = 0", (ip,)
        )
    await db.commit()


async def authenticate(db, username: str, password: str) -> dict | None:
    """校验用户名密码，成功返回 user row。失败返回 None（不泄露原因）。"""
    user = await get_user_by_username(db, username)
    if user is None:
        # 对不存在的用户也跑一次哈希校验，使响应时间一致，
        # 防止通过时延差异枚举用户名
        verify_password(hash_password("dummy-timing-equalizer"), "wrong")
        return None
    if not verify_password(user["password_hash"], password):
        return None
    if needs_rehash(user["password_hash"]):
        await db.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(password), iso_utc(), user["id"]),
        )
        await db.commit()
    return user


# ── CSRF ────────────────────────────────────────────────────

def csrf_ok(expected: str, provided: str | None) -> bool:
    """常量时间比较，防时序侧信道。"""
    if not expected or not provided:
        return False
    return secrets.compare_digest(expected, provided)


def _days_delta(days: int):
    from datetime import timedelta
    return timedelta(days=max(1, days))
