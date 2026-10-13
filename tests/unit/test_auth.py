"""认证模块单元测试：Argon2id 哈希、会话、限流、CSRF、用户名枚举防护。

这是安全关键模块（PRD §12.3），测试覆盖：
- 密码哈希用 Argon2id（不是明文、不是 MD5）
- 失败不区分「用户不存在」与「密码错误」（防枚举）
- 会话过期/吊销
- 登录限流（DB 持久化，重启不重置）
- CSRF 常量时间比较
"""

from __future__ import annotations

import pytest

from audio_hub.web import auth

# ── 密码哈希 ────────────────────────────────────────────────

class TestPasswordHashing:
    def test_hash_is_argon2id(self):
        """必须是 Argon2id，不是明文/MD5/bcrypt。"""
        h = auth.hash_password("correct-horse-battery")
        assert h.startswith("$argon2id$")

    def test_hash_not_plaintext(self):
        h = auth.hash_password("secret123")
        assert "secret123" not in h

    def test_verify_roundtrip(self):
        h = auth.hash_password("s3cret!")
        assert auth.verify_password(h, "s3cret!") is True

    def test_verify_wrong_password(self):
        h = auth.hash_password("s3cret!")
        assert auth.verify_password(h, "wrong") is False

    def test_verify_empty_inputs(self):
        h = auth.hash_password("x" * 8)
        assert auth.verify_password(h, "") is False
        assert auth.verify_password("", "x") is False

    def test_verify_invalid_hash_returns_false(self):
        """哈希串损坏时返回 False，不抛异常（防 500）。"""
        assert auth.verify_password("not-a-valid-hash", "x") is False

    def test_hash_empty_raises(self):
        with pytest.raises(auth.AuthError):
            auth.hash_password("")

    def test_different_hashes_for_same_password(self):
        """Argon2 自带随机 salt，同密码两次哈希不同。"""
        assert auth.hash_password("same") != auth.hash_password("same")


# ── 用户与会话 ──────────────────────────────────────────────

class TestUsersAndSessions:
    @pytest.mark.asyncio
    async def test_create_admin_and_count(self, test_db):
        assert await auth.count_users(test_db) == 0
        await auth.create_admin(test_db, "ops", "strongpass123")
        assert await auth.count_users(test_db) == 1

    @pytest.mark.asyncio
    async def test_duplicate_username_rejected(self, test_db):
        await auth.create_admin(test_db, "ops", "strongpass123")
        with pytest.raises(auth.AuthError, match="已存在"):
            await auth.create_admin(test_db, "ops", "anotherpass1")

    @pytest.mark.asyncio
    async def test_short_password_rejected(self, test_db):
        with pytest.raises(auth.AuthError, match="至少 8"):
            await auth.create_admin(test_db, "ops", "short")

    @pytest.mark.asyncio
    async def test_get_user_by_username(self, test_db):
        await auth.create_admin(test_db, "ops", "strongpass123")
        u = await auth.get_user_by_username(test_db, "ops")
        assert u is not None
        assert u["username"] == "ops"
        assert u["is_admin"] == 1
        # password_hash 不应是明文
        assert u["password_hash"].startswith("$argon2id$")

    @pytest.mark.asyncio
    async def test_session_roundtrip(self, test_db):
        uid = await auth.create_admin(test_db, "ops", "strongpass123")
        token, csrf = await auth.create_session(test_db, uid)
        assert token and csrf
        result = await auth.resolve_session(test_db, token)
        assert result is not None
        user, got_csrf = result
        assert user["username"] == "ops"
        assert got_csrf == csrf

    @pytest.mark.asyncio
    async def test_invalid_session_token(self, test_db):
        assert await auth.resolve_session(test_db, "bogus-token") is None
        assert await auth.resolve_session(test_db, None) is None

    @pytest.mark.asyncio
    async def test_revoke_session(self, test_db):
        uid = await auth.create_admin(test_db, "ops", "strongpass123")
        token, _ = await auth.create_session(test_db, uid)
        await auth.revoke_session(test_db, token)
        assert await auth.resolve_session(test_db, token) is None

    @pytest.mark.asyncio
    async def test_expired_session_rejected(self, test_db):
        """过期会话应被拒绝并清理。"""
        from datetime import timedelta

        from audio_hub.timeutil import utcnow
        uid = await auth.create_admin(test_db, "ops", "strongpass123")
        token, _ = await auth.create_session(test_db, uid, session_days=1)
        # 手动把 expires_at 改成过去
        past = (utcnow() - timedelta(hours=1)).isoformat(timespec="seconds")
        await test_db.execute("UPDATE sessions SET expires_at=? WHERE id=?", (past, token))
        await test_db.commit()
        assert await auth.resolve_session(test_db, token) is None

    @pytest.mark.asyncio
    async def test_change_password_revokes_sessions(self, test_db):
        """改密后所有会话吊销（PRD §12.3 登出所有设备）。"""
        uid = await auth.create_admin(test_db, "ops", "strongpass123")
        token, _ = await auth.create_session(test_db, uid)
        await auth.change_password(test_db, uid, "newstrongpass")
        assert await auth.resolve_session(test_db, token) is None
        # 新密码可用
        u = await auth.authenticate(test_db, "ops", "newstrongpass")
        assert u is not None


# ── authenticate：防用户名枚举 ──────────────────────────────

class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_correct_credentials(self, test_db):
        await auth.create_admin(test_db, "ops", "strongpass123")
        u = await auth.authenticate(test_db, "ops", "strongpass123")
        assert u is not None and u["username"] == "ops"

    @pytest.mark.asyncio
    async def test_wrong_password_returns_none(self, test_db):
        await auth.create_admin(test_db, "ops", "strongpass123")
        assert await auth.authenticate(test_db, "ops", "wrong") is None

    @pytest.mark.asyncio
    async def test_nonexistent_user_returns_none(self, test_db):
        """用户不存在与密码错误返回相同的 None（不泄露哪个错）。"""
        assert await auth.authenticate(test_db, "ghost", "whatever") is None


# ── 登录限流 ────────────────────────────────────────────────

class TestRateLimit:
    @pytest.mark.asyncio
    async def test_under_limit_ok(self, test_db):
        for _ in range(3):
            await auth.record_attempt(test_db, "1.2.3.4", "ops", success=False)
        # limit=5，3 次失败不触发
        await auth.check_rate_limit(test_db, "1.2.3.4", limit=5, window_minutes=15)

    @pytest.mark.asyncio
    async def test_over_limit_raises(self, test_db):
        for _ in range(5):
            await auth.record_attempt(test_db, "1.2.3.4", "ops", success=False)
        with pytest.raises(auth.RateLimited):
            await auth.check_rate_limit(test_db, "1.2.3.4", limit=5, window_minutes=15)

    @pytest.mark.asyncio
    async def test_success_clears_failures(self, test_db):
        """成功登录清除该 IP 失败计数，避免正常使用被历史失败拖累。"""
        for _ in range(4):
            await auth.record_attempt(test_db, "1.2.3.4", "ops", success=False)
        await auth.record_attempt(test_db, "1.2.3.4", "ops", success=True)
        # 清除后不应触发限流
        await auth.check_rate_limit(test_db, "1.2.3.4", limit=5, window_minutes=15)

    @pytest.mark.asyncio
    async def test_limit_per_ip(self, test_db):
        """限流按 IP 隔离，A 的失败不影响 B。"""
        for _ in range(5):
            await auth.record_attempt(test_db, "1.1.1.1", "ops", success=False)
        await auth.check_rate_limit(test_db, "2.2.2.2", limit=5, window_minutes=15)  # 不抛


# ── CSRF ────────────────────────────────────────────────────

class TestCsrf:
    def test_matching_tokens(self):
        assert auth.csrf_ok("abc123", "abc123") is True

    def test_mismatched_tokens(self):
        assert auth.csrf_ok("abc123", "xyz789") is False

    def test_empty_tokens_rejected(self):
        assert auth.csrf_ok("", "abc") is False
        assert auth.csrf_ok("abc", "") is False
        assert auth.csrf_ok("", "") is False
        assert auth.csrf_ok("abc", None) is False
