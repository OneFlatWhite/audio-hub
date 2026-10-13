"""crypto 模块单元测试：Fernet 加解密往返 + 边界情况。"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken

from audio_hub.crypto import (
    decrypt_value,
    encrypt_value,
    ensure_key_file,
    generate_key,
    load_fernet,
)


class TestGenerateKey:
    def test_returns_bytes(self):
        key = generate_key()
        assert isinstance(key, bytes)
        assert len(key) == 44  # Fernet key 是 32 字节 base64 编码


class TestEnsureKeyFile:
    def test_creates_new_file(self, tmp_path: Path):
        p = tmp_path / "new_key.key"
        result = ensure_key_file(p)
        assert result == p
        assert p.exists()
        assert len(p.read_bytes()) == 44

    def test_preserves_existing_file(self, tmp_path: Path):
        p = tmp_path / "existing.key"
        original = generate_key()
        p.write_bytes(original)
        ensure_key_file(p)
        assert p.read_bytes() == original

    def test_accepts_str_path(self, tmp_path: Path):
        """传字符串路径仍可生成密钥（serve_dev.sh 以 str 调用）。

        早期版本只接受 Path，字符串会在 `.exists()` 处抛 AttributeError，
        脚本静默吞掉导致密钥从未生成。
        """
        p = tmp_path / "sub" / "from_str.key"
        result = ensure_key_file(str(p))
        assert isinstance(result, Path)
        assert result == p
        assert p.exists()
        assert len(p.read_bytes()) == 44


class TestEncryptDecrypt:
    def test_roundtrip(self, tmp_key: Path):
        plaintext = "my_secret_passkey_12345"
        token = encrypt_value(plaintext, tmp_key)
        assert token != plaintext
        result = decrypt_value(token, tmp_key)
        assert result == plaintext

    def test_unicode_roundtrip(self, tmp_key: Path):
        plaintext = "中文密码テスト🎵"
        token = encrypt_value(plaintext, tmp_key)
        assert decrypt_value(token, tmp_key) == plaintext

    def test_empty_string(self, tmp_key: Path):
        token = encrypt_value("", tmp_key)
        assert decrypt_value(token, tmp_key) == ""

    def test_wrong_key_raises(self, tmp_path: Path):
        key1 = tmp_path / "key1.key"
        key2 = tmp_path / "key2.key"
        key1.write_bytes(generate_key())
        key2.write_bytes(generate_key())

        token = encrypt_value("secret", key1)
        with pytest.raises(InvalidToken):
            decrypt_value(token, key2)

    def test_invalid_token_raises(self, tmp_key: Path):
        with pytest.raises(InvalidToken):
            decrypt_value("not_a_valid_token", tmp_key)


class TestLoadFernet:
    def test_missing_file_raises(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.key"
        with pytest.raises(FileNotFoundError):
            load_fernet(missing)
