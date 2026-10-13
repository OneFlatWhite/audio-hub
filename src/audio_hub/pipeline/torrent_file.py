"""从 .torrent 字节 / magnet 链接解析真实 BT info_hash。

为什么需要它：RSS / 搜索适配器拿到的往往是站点内部 ID（馒头 infoHash 为 null、
NexusPHP 搜索页不暴露哈希），v0.7.6 用站点 ID 合成哈希保证任务不坍缩。
但 qBittorrent 入库后只认 .torrent 文件里的真实 info_hash —— 完成监控与启动
对账都按哈希关联任务与 qb 种子，合成哈希永远查不到，任务会被误判
「qb 中不存在」。所以在派发前下载 .torrent（PT 的 download URL 本身带
passkey / 临时 token，无需 cookie），算出真实哈希并以字节形式投递给 qb。

bencode 解析只取顶层 ``info`` 键的原始字节切片做 sha1 —— 不重编码，
因为哈希按定义就是 info 字典原始字节的摘要。
"""

from __future__ import annotations

import base64
import hashlib
import re

import httpx
from loguru import logger

#: magnet xt 参数里的 btih：40 位十六进制或 32 位 base32
_MAGNET_BTIH = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})(?:&|$)")

#: 拉取 .torrent 的预算；PT 站响应小，超时即回落为 URL 投递
FETCH_TIMEOUT_SECONDS = 30.0

#: .torrent 体积上限（防异常响应撑爆内存；正常种子文件远小于此）
MAX_TORRENT_BYTES = 16 * 1024 * 1024


def _skip_value(data: bytes, i: int) -> int:
    """跳过从 i 开始的一个 bencode 值，返回结束位置（不含）。非法输入抛 ValueError。"""
    if i >= len(data):
        raise ValueError("bencode 截断")
    c = data[i : i + 1]
    if c == b"i":
        j = data.find(b"e", i)
        if j < 0:
            raise ValueError("整数缺少结束符")
        int(data[i + 1 : j])  # 校验是合法整数
        return j + 1
    if c in (b"l", b"d"):
        i += 1
        while True:
            if i >= len(data):
                raise ValueError("容器缺少结束符")
            if data[i : i + 1] == b"e":
                return i + 1
            i = _skip_value(data, i)
    if not c.isdigit():
        raise ValueError(f"非法 bencode 标记: {c!r}")
    j = data.find(b":", i)
    if j < 0:
        raise ValueError("字节串缺少分隔符")
    n = int(data[i:j])
    end = j + 1 + n
    if end > len(data):
        raise ValueError("字节串超出数据长度")
    return end


def info_hash_from_torrent(data: bytes) -> str:
    """从 .torrent 字节算出真实 info_hash（sha1(info 字典原始字节)）。

    Raises:
        ValueError: 数据不是合法 bencode，或顶层字典没有 info 键
    """
    if not data or data[:1] != b"d":
        raise ValueError("不是有效的 .torrent（顶层必须是字典）")
    i = 1
    while True:
        if i >= len(data):
            raise ValueError("bencode 截断")
        if data[i : i + 1] == b"e":
            raise ValueError("顶层字典缺少 info 键")
        j = data.find(b":", i)
        if j < 0:
            raise ValueError("字典键缺少分隔符")
        n = int(data[i:j])
        key = data[j + 1 : j + 1 + n]
        if len(key) != n:
            raise ValueError("bencode 截断")
        i = j + 1 + n
        if key == b"info":
            end = _skip_value(data, i)
            return hashlib.sha1(data[i:end]).hexdigest()
        i = _skip_value(data, i)


def info_hash_from_magnet(url: str) -> str | None:
    """从 magnet 链接提取 btih（统一转 40 位小写十六进制）；非 magnet 返回 None。"""
    m = _MAGNET_BTIH.search(url or "")
    if not m:
        return None
    raw = m.group(1)
    if len(raw) == 40:
        return raw.lower()
    return base64.b32decode(raw.upper()).hex()


async def fetch_torrent_bytes(url: str) -> bytes | None:
    """GET 下载 .torrent 字节；非 http(s) / 请求失败 / 内容不像种子返回 None。

    返回 None 时调用方回落为把 URL 直接交给 qb（保持旧行为），仅损失真实哈希。
    """
    if not url.startswith(("http://", "https://")):
        return None
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True,
        ) as client:
            r = await client.get(url)
        if r.status_code != 200 or not r.content or r.content[:1] != b"d":
            logger.warning(
                f".torrent 拉取内容无效（status={r.status_code} len={len(r.content)}），"
                "回落为 URL 投递"
            )
            return None
        if len(r.content) > MAX_TORRENT_BYTES:
            logger.warning(f".torrent 超大（{len(r.content)}B），回落为 URL 投递")
            return None
        return r.content
    except Exception as e:  # noqa: BLE001 - 拉取失败不应阻断派发，回落 URL 投递
        logger.warning(f".torrent 拉取失败（{type(e).__name__}: {e}），回落为 URL 投递")
        return None


async def fetch_and_hash(url: str) -> tuple[str | None, bytes | None]:
    """给定下载直链，尽量拿到 (真实 info_hash, .torrent 字节)。

    magnet 直接解析哈希（无字节）；http(s) 下载字节并算哈希；
    任何失败都返回 (None, None)，由调用方回落 URL 投递。
    """
    magnet_hash = info_hash_from_magnet(url)
    if magnet_hash:
        return magnet_hash, None
    blob = await fetch_torrent_bytes(url)
    if blob is None:
        return None, None
    try:
        return info_hash_from_torrent(blob), blob
    except ValueError as e:
        logger.warning(f".torrent 解析失败（{e}），回落为 URL 投递")
        return None, None
