"""发现墙封面层：iTunes Search API 补图 + 磁盘缓存 + 负缓存。

PT 站列表不提供封面；iTunes Search API 免费无 key。流程：搜索接口取第一条
artworkUrl100 → 换成 600x600 → 抓图落盘。任何网络 / 解析异常都静默降级为
None（前端 onerror 换占位卡），日志只记 debug —— 封面是装饰，挂了不能影响墙。

缓存布局（cache_dir = covers_dir(config)）：
- ``<sha1(key)>.img``：图片字节，永久有效（专辑封面几乎不变）
- ``<sha1(key)>.json``：元信息（源 URL / 命中与否 / 抓取时间 / content-type）；
  未命中也写（负缓存 24h），避免同一冷门专辑每次开页都外呼
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import httpx
from loguru import logger

from ..pipeline.title_parser import normalize_key
from ..timeutil import iso_utc, parse_iso, utcnow

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

#: 封面外呼超时（秒）：比站点适配器短，封面慢不应拖住整页渲染
HTTP_TIMEOUT = 10.0

#: 负缓存有效期：iTunes 上没有的专辑 24h 内不重试
NEGATIVE_TTL_SECONDS = 24 * 3600

_ENTITY = {"music": "album", "audiobook": "audiobook"}

#: /covers/{key} 只服务 sha1 hex 键 —— 防路径穿越与任意 URL 代理（SSRF）
_KEY_RE = re.compile(r"[0-9a-f]{40}")

#: iTunes 的 100x100 缩略图 URL 规律：尺寸段可替换（官方未文档化但是惯例）
_SIZE_RE = re.compile(r"100x100")


def covers_dir(config) -> Path:
    """封面缓存目录：db_path 同级的 covers/。

    取 db_path 推导而非新增配置键：covers 是缓存而非配置，跟随数据卷走
    （容器里 /config/hub.db → /config/covers），少一个需要解释的路径键。
    """
    return Path(config.db_path).parent / "covers"


def is_valid_key(key: str) -> bool:
    """代理端点的键校验：仅接受 40 位小写 hex（sha1）。"""
    return bool(_KEY_RE.fullmatch(key or ""))


def cache_key(artist: str, title: str, kind: str) -> str:
    """封面查询键：kind + 归一化的 歌手|标题 → sha1 hex。"""
    raw = f"{kind}|{normalize_key(artist)}|{normalize_key(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def read_meta(cache_dir: Path, key: str) -> dict | None:
    """读 <key>.json 元信息；不存在或损坏返回 None。"""
    try:
        return json.loads((Path(cache_dir) / f"{key}.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 元信息损坏视为未缓存
        return None


def _write_meta(cache_dir: Path, key: str, **fields) -> None:
    meta = {"fetched_at": iso_utc(), **fields}
    try:
        (Path(cache_dir) / f"{key}.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8",
        )
    except OSError as e:
        logger.debug(f"封面元信息写入失败 {key[:8]}: {e}")


def _negative_fresh(meta: dict) -> bool:
    """负缓存仍在 24h 有效期内。"""
    at = parse_iso(str(meta.get("fetched_at") or ""))
    if at is None:
        return False
    return (utcnow() - at).total_seconds() < NEGATIVE_TTL_SECONDS


async def cover_for(
    artist: str, title: str, kind: str, *,
    cache_dir: Path, provider: str = "itunes",
) -> str | None:
    """取一张封面，返回缓存键（40 位 hex，经 /covers/{key} 代理）；无封面返回 None。

    provider=none 直接 None（不发任何请求）。命中磁盘缓存不发请求；
    负缓存期内也直接 None。任何异常 → None（debug 日志）。
    """
    if provider != "itunes":
        return None
    cache_dir = Path(cache_dir)
    key = cache_key(artist, title, kind)
    img_path = cache_dir / f"{key}.img"
    try:
        if img_path.is_file():
            return key
        meta = read_meta(cache_dir, key)
        if meta and not meta.get("hit") and _negative_fresh(meta):
            return None
        return await _fetch_itunes(artist, title, kind, cache_dir, key)
    except Exception as e:  # noqa: BLE001 - 封面是装饰，任何失败都降级为占位卡
        logger.debug(f"封面获取失败 {kind} {artist}|{title}: {type(e).__name__}: {e}")
        return None


async def _fetch_itunes(artist: str, title: str, kind: str,
                        cache_dir: Path, key: str) -> str | None:
    """iTunes 搜索 → 抓 600x600 图 → 落盘。成功返回 key，无结果写负缓存返回 None。"""
    term = f"{artist} {title}".strip()
    if not term:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(ITUNES_SEARCH_URL, params={
            "term": term, "entity": _ENTITY.get(kind, "album"),
            "country": "CN", "limit": 3,
        })
        resp.raise_for_status()
        results = resp.json().get("results") or []
        artwork = next(
            (r.get("artworkUrl100") for r in results
             if isinstance(r, dict) and r.get("artworkUrl100")),
            None,
        )
        if not artwork:
            _write_meta(cache_dir, key, hit=False, term=term)
            return None
        source_url = _SIZE_RE.sub("600x600", artwork)
        img_resp = await client.get(source_url)
        img_resp.raise_for_status()
        content = img_resp.content
        if not content:
            _write_meta(cache_dir, key, hit=False, term=term)
            return None
        content_type = img_resp.headers.get("content-type", "image/jpeg").split(";")[0]
        (cache_dir / f"{key}.img").write_bytes(content)
        _write_meta(cache_dir, key, hit=True, source_url=source_url,
                    content_type=content_type, term=term)
        return key


async def fill_covers(
    cards: list, *, cache_dir: Path, provider: str = "itunes", max_cards: int = 60,
) -> None:
    """就地填充卡片的 cover 字段（/covers/{key}）；无封面保持空串（前端占位）。

    并发上限 6：整墙几十张卡同时外呼会触发 iTunes 限速；超出的卡片本次留白，
    下次开页（磁盘缓存已部分命中）再补。
    """
    if provider != "itunes":
        return
    sem = asyncio.Semaphore(6)

    async def _one(card) -> None:
        if card.cover:
            return
        async with sem:
            key = await cover_for(card.artist, card.title, card.kind,
                                  cache_dir=cache_dir, provider=provider)
        if key:
            card.cover = f"/covers/{key}"

    await asyncio.gather(*(_one(c) for c in cards[:max_cards]))
