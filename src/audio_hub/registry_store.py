"""站点 / 下载器配置的权威源桥接（v0.6）。

v0.6 之前站点与 qBittorrent 的配置在 config.yaml，界面改动落 app_settings 覆盖层
（site_enabled:* / site_rss:* / site_api_key:* / site_hr_*:*）。v0.6 起 **sites 与
downloaders 两张表是唯一权威源**，YAML 只在首次启动时作为种子导入一次：

- 表空 → 全量导入 YAML 的 sites / qbittorrent 段；
- 表非空 → 只补入 YAML 里新出现的站点名（已有行不覆盖，否则界面改动会在每次
  重启时被 YAML 打回）；
- app_settings 里的旧站点覆盖项合并进 sites 后删除，避免两处真源互相打架。

本模块只做「DB ↔ config 对象」的转换与首启种子，SQL 全在 repo.py。
load_site_configs / load_downloader_configs 返回**含解密凭据**的配置对象，
只供适配器 / qb 客户端构建使用，不要往页面传；页面用 SiteRecord / DownloaderRecord。
"""

from __future__ import annotations

from loguru import logger

from . import repo
from .adapters.registry import _ADAPTER_CLASS_MAP, resolve_site_name
from .config import AppConfig, DownloaderConfig, SiteConfig
from .repo import downloader_record_from_row, site_record_from_row
from .settings_store import (
    SITE_APIKEY_PREFIX,
    SITE_ENABLED_PREFIX,
    SITE_HR_HOURS_PREFIX,
    SITE_HR_RATIO_PREFIX,
    SITE_KEY_PREFIXES,
    SITE_RSS_PREFIX,
)

__all__ = [
    "DEFAULT_DOWNLOADER_ID",
    "adapter_type_for",
    "downloader_record_from_row",
    "load_downloader_configs",
    "load_site_configs",
    "seed_from_yaml",
    "site_record_from_row",
]

#: YAML qbittorrent 段导入后的下载器 id；旧任务 downloader_id 为 NULL 时回填此值
DEFAULT_DOWNLOADER_ID = "default"

#: 适配器类名 → sites.adapter_type
_CLASS_TO_ADAPTER_TYPE = {
    "NexusPhpAdapter": "nexusphp",
    "MTeamAdapter": "mteam",
}


def adapter_type_for(site_name: str) -> str:
    """按注册表推断站点的适配器类型；未注册站点按 NexusPHP 处理（最常见的 PT 架构）。"""
    cls = _ADAPTER_CLASS_MAP.get(resolve_site_name(site_name))
    if cls is None:
        return "nexusphp"
    return _CLASS_TO_ADAPTER_TYPE.get(cls.__name__, "nexusphp")


# ────────────────────────────────────────────────────────────
# 旧覆盖项合并
# ────────────────────────────────────────────────────────────

def _parse_optional_int(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _parse_optional_float(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


async def _collect_legacy_overrides(db) -> dict[str, dict[str, str]]:
    """读 app_settings 里的站点级旧键，按站点名归组：{site_id: {prefix: stored}}。"""
    async with db.execute("SELECT key, value FROM app_settings") as cur:
        rows = await cur.fetchall()
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        key = r["key"]
        for prefix in SITE_KEY_PREFIXES:
            if key.startswith(prefix):
                site_id = resolve_site_name(key[len(prefix):])
                out.setdefault(site_id, {})[prefix] = r["value"] or ""
                break
    return out


async def _apply_legacy_overrides(db, legacy: dict[str, dict[str, str]]) -> int:
    """把旧覆盖项写进 sites（只对已存在的站点行），然后删掉这些键。返回合并的站点数。

    secret 键在 app_settings 里本来就是 Fernet token，直接搬进 *_enc 列，
    不解密再加密 —— 密钥文件换过的话解不开，搬运不该因此失败。
    """
    merged = 0
    for site_id, items in legacy.items():
        if not await repo.site_exists(db, site_id):
            logger.warning(f"app_settings 里有站点 {site_id} 的旧覆盖项，但 YAML 中已无此站，丢弃")
            continue
        sets: list[str] = []
        params: list = []
        if SITE_ENABLED_PREFIX in items:
            sets.append("enabled = ?")
            params.append(1 if items[SITE_ENABLED_PREFIX] == "1" else 0)
        if items.get(SITE_RSS_PREFIX):
            sets.append("rss_url_enc = ?")
            params.append(items[SITE_RSS_PREFIX])
        if items.get(SITE_APIKEY_PREFIX):
            sets.append("api_key_enc = ?")
            params.append(items[SITE_APIKEY_PREFIX])
        if SITE_HR_HOURS_PREFIX in items:
            sets.append("hr_min_seed_hours = ?")
            params.append(_parse_optional_int(items[SITE_HR_HOURS_PREFIX]))
        if SITE_HR_RATIO_PREFIX in items:
            sets.append("hr_min_ratio = ?")
            params.append(_parse_optional_float(items[SITE_HR_RATIO_PREFIX]))
        if sets:
            sets.append("updated_at = ?")
            params.append(repo._now())
            params.append(site_id)
            await db.execute(f"UPDATE sites SET {', '.join(sets)} WHERE id = ?", params)
            merged += 1

    for prefix in SITE_KEY_PREFIXES:
        await db.execute("DELETE FROM app_settings WHERE key LIKE ?", (f"{prefix}%",))
    await db.commit()
    return merged


# ────────────────────────────────────────────────────────────
# 首启种子与旧键收编
# ────────────────────────────────────────────────────────────

#: 设置页旧「qBittorrent」组的覆盖键 → downloaders 列（**仅供迁移**）。
#: v0.6 起下载器连接参数在 downloaders 表（/downloaders 页），这组键不再由
#: 设置页读写；qb_api_key 在 app_settings 里本就是 Fernet token，直接搬进
#: api_key_enc 列，不解密再加密 —— 密钥文件换过的话解不开，搬运不该因此失败。
LEGACY_QB_KEY_COLUMNS = {
    "qb_host": "host",
    "qb_port": "port",
    "qb_save_path": "save_path",
    "qb_category": "category",
    "qb_tag": "tag",
    "qb_api_key": "api_key_enc",
}


async def _merge_legacy_qb_overrides(db) -> int:
    """把存量 app_settings 的 qb_* 键收编进默认下载器行，然后删除。返回收编键数。

    首启种子里 config.qbittorrent 曾是「YAML + hydrate 合并 qb_* 覆盖」的结果，
    故老库升级时收编与种子最终一致；v0.6.1 起 hydrate 不再认识这组键（spec 已撤），
    收编改为直接读 app_settings 写 downloaders —— 否则首启后在旧设置页改过的值
    会静默丢失：使用者以为生效了，运行时却永远读下载器表。
    无论值是否应用成功旧键都删除：收编是一次性迁移，残留键没有意义。
    """
    placeholders = ",".join("?" * len(LEGACY_QB_KEY_COLUMNS))
    async with db.execute(
        f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})",
        tuple(LEGACY_QB_KEY_COLUMNS),
    ) as cur:
        rows = list(await cur.fetchall())
    if not rows:
        return 0

    default = await repo.get_default_downloader(db)
    if default is None:
        # 表空时本函数在种子导入之后调用，理论上必有默认行；防御：不丢键，留下次
        logger.warning("downloaders 表为空，qb_* 旧键本轮不收编")
        return 0

    sets: list[str] = []
    params: list = []
    for r in rows:
        col = LEGACY_QB_KEY_COLUMNS[r["key"]]
        value = r["value"]
        if col == "port":
            try:
                value = int(value)
            except (TypeError, ValueError):
                logger.warning(f"旧设置键 {r['key']} 的值 {value!r} 不是端口数字，丢弃")
                continue
        sets.append(f"{col} = ?")
        params.append(value)
    if sets:
        sets.append("updated_at = ?")
        params.append(repo._now())
        params.append(default.id)
        await db.execute(f"UPDATE downloaders SET {', '.join(sets)} WHERE id = ?", params)

    await db.execute(
        f"DELETE FROM app_settings WHERE key IN ({placeholders})",
        tuple(LEGACY_QB_KEY_COLUMNS),
    )
    await db.commit()
    logger.info(f"旧设置键 qb_* 已收编进默认下载器 {default.id}: {[r['key'] for r in rows]}")
    return len(rows)

async def seed_from_yaml(db, config: AppConfig) -> dict:
    """把 YAML 的 sites / qbittorrent 段导入 DB（幂等），并收编旧覆盖项。

    返回统计 {"sites_added": n, "overrides_merged": n, "downloader_added": bool,
    "tasks_backfilled": n, "qb_overrides_merged": n}，供日志与测试断言。

    站点覆盖项由本函数自行合并（不经 hydrate）；app_settings 里的旧 qb_* 键
    在下载器种子导入后收编进默认下载器行并删除（v0.6.1：设置页该组已撤下，
    hydrate 不再认识这些键，不收编的话首启后改过的值会静默丢失）。
    """
    stats = {"sites_added": 0, "overrides_merged": 0,
             "downloader_added": False, "tasks_backfilled": 0, "qb_overrides_merged": 0}

    existing_ids = {r["id"] for r in await repo.list_site_rows(db)}
    for s in config.sites:
        sid = resolve_site_name(s.name)
        if sid in existing_ids:
            continue
        # YAML 里的 enabled 只是基线；进 DB 后是否参与搜索由认证门禁（2A）决定
        await repo.upsert_site(
            db, s.model_copy(update={"name": sid}), site_id=sid,
            adapter_type=adapter_type_for(sid),
        )
        existing_ids.add(sid)
        stats["sites_added"] += 1

    legacy = await _collect_legacy_overrides(db)
    if legacy:
        stats["overrides_merged"] = await _apply_legacy_overrides(db, legacy)

    if not await repo.list_downloader_rows(db):
        qb = config.qbittorrent.model_copy(update={
            "id": DEFAULT_DOWNLOADER_ID,
            "name": config.qbittorrent.name or "默认 qBittorrent",
            "enabled": True, "is_default": True,
        })
        await repo.upsert_downloader(db, qb)
        stats["downloader_added"] = True

    # 收编设置页旧「qBittorrent」组的 app_settings 键（必须在种子之后：
    # 表空时上面刚建好的默认行就是收编目标）
    stats["qb_overrides_merged"] = await _merge_legacy_qb_overrides(db)

    cur = await db.execute(
        "UPDATE tasks SET downloader_id = ? WHERE downloader_id IS NULL",
        (DEFAULT_DOWNLOADER_ID,),
    )
    stats["tasks_backfilled"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    await db.commit()

    if any(stats.values()):
        logger.info(f"配置种子导入: {stats}")
    return stats


# ────────────────────────────────────────────────────────────
# DB → 运行时配置对象
# ────────────────────────────────────────────────────────────

async def load_site_configs(db) -> list[SiteConfig]:
    """全部站点的 SiteConfig（含解密凭据），禁用站也在内（build_adapters 自己过滤）。"""
    return [repo.site_config_from_row(r) for r in await repo.list_site_rows(db)]


async def load_downloader_configs(db) -> list[DownloaderConfig]:
    """全部下载器的 DownloaderConfig（含解密凭据），默认实例排最前。"""
    return [repo.downloader_config_from_row(r) for r in await repo.list_downloader_rows(db)]
