"""pytest fixtures：测试用 DB、临时密钥文件等。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from audio_hub.crypto import generate_key
from audio_hub.db import init_db


@pytest.fixture(scope="session")
def event_loop():
    """为 pytest-asyncio 提供 session 级事件循环。"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def tmp_key(tmp_path: Path) -> Path:
    """生成临时 Fernet 密钥文件。"""
    key_path = tmp_path / "test_secrets.key"
    key_path.write_bytes(generate_key())
    return key_path


@pytest.fixture
async def test_db(tmp_path: Path):
    """初始化测试用 SQLite 数据库。"""
    db_path = tmp_path / "test_hub.db"
    conn = await init_db(db_path)
    yield conn
    await conn.close()
