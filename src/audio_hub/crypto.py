"""Fernet 对称加密模块，用于 passkey / API Key 的安全存储。

密钥文件路径由环境变量 AUDIO_HUB_SECRETS_KEY 指定。
首次运行时若密钥文件不存在，自动生成并设置权限 600。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _key_path() -> Path:
    """从环境变量获取密钥文件路径。"""
    raw = os.environ.get("AUDIO_HUB_SECRETS_KEY")
    if not raw:
        raise RuntimeError(
            "环境变量 AUDIO_HUB_SECRETS_KEY 未设置。"
            "请将其指向密钥文件路径，例如 /config/secrets.key"
        )
    return Path(raw)


def generate_key() -> bytes:
    """生成新的 Fernet 密钥（32 字节 URL-safe base64）。"""
    return Fernet.generate_key()


def ensure_key_file(path: str | Path | None = None) -> Path:
    """确保密钥文件存在；不存在则自动生成并设权限 600。

    接受 str 或 Path：serve_dev.sh 等脚本以字符串传路径，早期版本只收
    Path，`"...".exists()` 抛 AttributeError 且被脚本静默吞掉，
    导致密钥文件从未生成、保存凭据时才报错。

    Returns:
        密钥文件路径
    """
    p = Path(path) if path else _key_path()
    if not p.exists():
        key = generate_key()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(key)
        # 尝试设置权限 600（Windows 下可能不支持，忽略）
        try:
            p.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return p


def load_fernet(path: Path | None = None) -> Fernet:
    """加载 Fernet 实例。"""
    p = path or _key_path()
    if not p.exists():
        raise FileNotFoundError(
            f"密钥文件不存在: {p}\n"
            "请运行 scripts/init_secrets.py 或手动生成密钥文件。"
        )
    return Fernet(p.read_bytes())


def encrypt_value(plaintext: str, path: Path | None = None) -> str:
    """加密明文字符串，返回 Fernet token。"""
    f = load_fernet(path)
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_value(token: str, path: Path | None = None) -> str:
    """解密 Fernet token，返回明文。

    Raises:
        InvalidToken: token 无效或密钥不匹配
    """
    f = load_fernet(path)
    try:
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise InvalidToken("解密失败：token 无效或密钥文件不匹配。请检查密钥文件或重新加密凭据。")
