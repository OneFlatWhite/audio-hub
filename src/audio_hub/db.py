"""SQLite 数据库模块：WAL 模式 + 幂等 migration。

v0.1 三表：watchlist / tasks / torrents（去重指纹）。
v0.2 认证与审核：users / sessions / login_attempts / site_stats /
     review_decisions / alias_cache，并为 tasks 补派发所需列。
v0.3 设置覆盖层：app_settings（界面改动持久化，不回写 YAML）。
v0.4 归档链路：library_items / recycle_entries / quarantine_items /
     series / series_parts / seed_records / events，并为 tasks 与 watchlist 补列。
v0.6 站点 / 下载器中心与订阅引擎：sites / site_categories / downloaders /
     subscription_hits，并为 tasks（downloader_id / watch_id / album_key /
     quality_tag）与 watchlist（订阅类型、过滤规则、下载器归属）补列。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
from loguru import logger

#: 当前 schema 版本，写入 PRAGMA user_version 供诊断脚本读取
SCHEMA_VERSION = 5

# Migration v1 SQL
_MIGRATION_V1 = """
CREATE TABLE IF NOT EXISTS watchlist (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'music',
    query         TEXT NOT NULL,
    aliases       TEXT NOT NULL DEFAULT '[]',   -- JSON array
    regex         TEXT,
    sites         TEXT NOT NULL DEFAULT '[]',    -- JSON array
    min_confidence TEXT NOT NULL DEFAULT 'high',
    auto_download INTEGER NOT NULL DEFAULT 1,
    priority      INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    -- 可空：被硬排除的候选、人工手动添加的任务没有归属订阅项。
    -- 早期版本设为 NOT NULL，导致 watch_entry 为 None 时落库必然失败。
    watch_entry_id  TEXT,
    info_hash       TEXT NOT NULL,
    site            TEXT NOT NULL,
    title           TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'discovered',
    score           REAL NOT NULL DEFAULT 0.0,
    confidence      TEXT NOT NULL DEFAULT 'high',
    reject_reason   TEXT,
    qb_category     TEXT,
    qb_save_path    TEXT,
    archived_path   TEXT,
    error_message   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (watch_entry_id) REFERENCES watchlist(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_info_hash ON tasks(info_hash);

CREATE TABLE IF NOT EXISTS torrents (
    info_hash        TEXT PRIMARY KEY,
    site             TEXT NOT NULL,
    title_normalized TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL,
    task_id          TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
"""

# Migration v2：Web 界面与认证（PRD 第十二章）
_MIGRATION_V2 = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,           -- Argon2id，绝不用 passlib
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,        -- 随机 token
    user_id       TEXT NOT NULL,
    csrf_token    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- 登录限流：滑动窗口，单 IP 计数（对齐 Navidrome AuthRequestLimit 默认 5）
CREATE TABLE IF NOT EXISTS login_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ip         TEXT NOT NULL,
    username   TEXT,
    success    INTEGER NOT NULL DEFAULT 0,
    attempted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_ip_time ON login_attempts(ip, attempted_at);

-- 站点健康统计（/sites 页面）
CREATE TABLE IF NOT EXISTS site_stats (
    site             TEXT PRIMARY KEY,
    last_success_at  TEXT,
    last_error       TEXT,
    last_count       INTEGER NOT NULL DEFAULT 0,
    consecutive_fail INTEGER NOT NULL DEFAULT 0,
    circuit_open_until TEXT,
    total_requests   INTEGER NOT NULL DEFAULT 0,
    total_errors     INTEGER NOT NULL DEFAULT 0
);

-- 人工反馈闭环：拒绝原因回流（PRD §6.2 第 6 条）
CREATE TABLE IF NOT EXISTS review_decisions (
    id          TEXT PRIMARY KEY,
    task_id     TEXT,
    info_hash   TEXT NOT NULL,
    decision    TEXT NOT NULL,             -- accept | reject
    reason      TEXT,
    decided_at  TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_review_reason ON review_decisions(reason);

-- 别名表：人工反馈沉淀（/review 拒绝 → 规则迭代）
CREATE TABLE IF NOT EXISTS alias_cache (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,             -- alias | blacklist
    watch_id    TEXT,
    value       TEXT NOT NULL,
    reason      TEXT,
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alias_value ON alias_cache(value);
"""

# Migration v3：运行时设置覆盖层（v0.3 界面可编辑配置）
#
# 为什么用 DB 而不回写 config.yaml：
# YAML 是部署产物，人手维护且含注释（如「cat410 需实测确认」这类实测结论）。
# PyYAML round-trip 会丢注释，写入中断还会损坏配置导致下次起不来。
# 故 YAML 保持基线与唯一手写入口，界面改动存这里，启动时合并到 config 之上。
#
# key 命名：普通设置用与点分路径同名的键（dry_run / qb_port ...），
# 站点开关用 "site_enabled:<站点名>"，内部标记用 "__" 前缀（不参与合并）。
_MIGRATION_V3 = """
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Migration v4：归档链路（资料库 / 回收 / 隔离 / 连载 / 做种 / 事件）
#
# 七张表一次建齐，全部 IF NOT EXISTS。二波模块（归档决策 / 校验 / 连载解析 /
# 做种轮询 / 推送）只写不建，schema 的唯一真源在这里。
#
# watch_id 用 TEXT 而非 INT：watchlist.id 是 uuid 字符串，外键类型必须一致，
# 否则 join 时 SQLite 的类型亲和性转换会让比较结果依赖于存储类而非值。
_MIGRATION_V4 = """
-- 已入库的成品：归档决策（new / upgrade / skip）按 (watch_id, title_norm) 查旧版本
CREATE TABLE IF NOT EXISTS library_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id       TEXT,
    task_id        TEXT,
    title_norm     TEXT,
    kind           TEXT,
    quality        TEXT,
    quality_score  INTEGER,
    path           TEXT,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_library_watch_title ON library_items(watch_id, title_norm);

-- 升级替换时旧版本的去向，支持到期清理与回滚
CREATE TABLE IF NOT EXISTS recycle_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    library_item_id  INTEGER,
    original_path    TEXT,
    recycle_path     TEXT,
    reason           TEXT,
    created_at       TEXT,
    restored_at      TEXT
);

-- 校验不通过的内容：held → released / discarded
CREATE TABLE IF NOT EXISTS quarantine_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT,
    scratch_path  TEXT,
    reasons       TEXT,
    status        TEXT NOT NULL DEFAULT 'held',
    created_at    TEXT,
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_quarantine_status ON quarantine_items(status);

-- 连载（多部有声书 / 合辑分卷）
CREATE TABLE IF NOT EXISTS series (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id      TEXT,
    name          TEXT,
    total_parts   INTEGER,
    part_pattern  TEXT,
    last_part     INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS series_parts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id   INTEGER,
    part_no     INTEGER,
    task_id     TEXT,
    title       TEXT,
    created_at  TEXT,
    UNIQUE(series_id, part_no)
);

-- 做种 / H&R 跟踪：一个任务一条
CREATE TABLE IF NOT EXISTS seed_records (
    task_id           TEXT PRIMARY KEY,
    info_hash         TEXT,
    site              TEXT,
    started_at        TEXT,
    seed_seconds      INTEGER NOT NULL DEFAULT 0,
    ratio             REAL NOT NULL DEFAULT 0,
    required_seconds  INTEGER,
    required_ratio    REAL,
    status            TEXT NOT NULL DEFAULT 'seeding',
    hr_satisfied_at   TEXT,
    updated_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_seed_status ON seed_records(status);

-- 事件流：DbNotifier 落库，/events 页展示
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT,
    level       TEXT,
    site        TEXT,
    title       TEXT,
    detail      TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
"""

# Migration v5：站点 / 下载器中心与订阅引擎（v0.6）
#
# sites / downloaders 成为权威源，config.yaml 的对应段只在首启作种子导入
# （registry_store.seed_from_yaml）。凭据列统一 *_enc 后缀，存 Fernet token，
# 读写都经 crypto；任何返回给页面的模型只暴露 has_* 布尔。
#
# 站点 id 用 TEXT 而非自增：tasks.site / site_stats.site / seed_records.site
# 历史上都存站点名，让 sites.id 就是那个名字，免去一层 join。
_MIGRATION_V5 = """
CREATE TABLE IF NOT EXISTS sites (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    adapter_type        TEXT NOT NULL DEFAULT 'nexusphp',   -- nexusphp | mteam
    enabled             INTEGER NOT NULL DEFAULT 0,
    domain              TEXT,
    cookie_enc          TEXT,
    user_agent          TEXT,
    rss_url_enc         TEXT,
    search_url          TEXT,
    api_key_enc         TEXT,
    proxy               TEXT,
    priority            INTEGER NOT NULL DEFAULT 0,
    music_cat_ids       TEXT,                               -- 逗号分隔
    audiobook_cat_ids   TEXT,
    max_rows            INTEGER NOT NULL DEFAULT 50,
    title_bracket_style TEXT,
    hr_min_seed_hours   INTEGER,
    hr_min_ratio        REAL,
    auth_status         TEXT NOT NULL DEFAULT 'unconfigured', -- unconfigured|valid|expired|error
    auth_checked_at     TEXT,
    auth_error          TEXT,
    user_info_json      TEXT,
    last_rss_test_json  TEXT,
    created_at          TEXT,
    updated_at          TEXT
);

-- 站点远端分类发现结果与本地映射
CREATE TABLE IF NOT EXISTS site_categories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id       TEXT NOT NULL,
    remote_id     TEXT NOT NULL,
    remote_name   TEXT,
    local_kind    TEXT NOT NULL DEFAULT 'other',          -- music | audiobook | other
    discovered_at TEXT,
    UNIQUE(site_id, remote_id)
);

-- 下载器实例：仅 qBittorrent；username 非空用账密登录，否则 API Key
CREATE TABLE IF NOT EXISTS downloaders (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    type           TEXT NOT NULL DEFAULT 'qbittorrent',
    host           TEXT NOT NULL DEFAULT 'localhost',
    port           INTEGER NOT NULL DEFAULT 8085,
    username       TEXT,
    password_enc   TEXT,
    api_key_enc    TEXT,
    save_path      TEXT,
    category       TEXT,
    tag            TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    is_default     INTEGER NOT NULL DEFAULT 0,
    priority       INTEGER NOT NULL DEFAULT 0,
    last_test_json TEXT,
    last_test_at   TEXT,
    created_at     TEXT,
    updated_at     TEXT
);

-- 订阅命中记录：每次候选命中及其决策，专辑级去重 / 洗版按 (watch_id, album_key) 查
CREATE TABLE IF NOT EXISTS subscription_hits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id      TEXT NOT NULL,
    site          TEXT,
    torrent_title TEXT,
    info_hash     TEXT,
    album_key     TEXT,
    quality_tag   TEXT,
    discount      TEXT,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    seeders       INTEGER NOT NULL DEFAULT 0,
    score         REAL NOT NULL DEFAULT 0,
    found_via     TEXT NOT NULL DEFAULT 'rss',             -- rss | search
    decision      TEXT NOT NULL DEFAULT 'review',          -- dispatched|review|rejected|skipped_dup|upgrade
    reject_reason TEXT,
    task_id       TEXT,
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_hits_watch_album ON subscription_hits(watch_id, album_key);
CREATE INDEX IF NOT EXISTS idx_hits_info_hash ON subscription_hits(info_hash);
"""

# tasks 表的后续新增列（v2 派发 + v4 归档）。
#
# 为什么必须有 v2 这些列：/review 采纳待审项后要真实派发，而 TorrentMeta（含
# download_url）只存在于流水线轮次的内存中。若不持久化，采纳动作就只能改状态、
# 无法下载 —— 整个待审队列在实跑模式下失去意义。
#
# 为什么不用 ALTER TABLE ... executescript：SQLite 的 ALTER ADD COLUMN 没有
# IF NOT EXISTS，重复执行会抛 "duplicate column name"，破坏 init_db 的幂等性。
# 故改为读 PRAGMA table_info 后按需添加。
_TASK_COLUMNS = [
    # v2
    ("download_url", "TEXT"),
    ("size_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("quality", "TEXT"),
    ("format_tags", "TEXT"),
    ("site_category", "TEXT"),
    # v4：连载归属与归档链路结果
    ("series_id", "INTEGER"),
    ("part_no", "INTEGER"),
    ("validation_result", "TEXT"),
    ("archive_decision", "TEXT"),
    # v5：下载器归属（旧行由 seed_from_yaml 回填 default）与订阅引擎归属
    ("downloader_id", "TEXT"),
    ("watch_id", "TEXT"),
    ("album_key", "TEXT"),
    ("quality_tag", "TEXT"),
]
# 保留旧名：历史测试与文档引用这个常量名
_V2_TASK_COLUMNS = _TASK_COLUMNS

# watchlist 的后续新增列（v4 连载 + v5 订阅引擎）。
#
# v5 全部带默认值或可空：旧行加列后立即可读，再由 _backfill_watchlist_v5
# 按 kind 改写 sub_type（music → artist 且 artist=query；audiobook → audiobook）。
_WATCHLIST_COLUMNS = [
    ("series_id", "INTEGER"),
    # v5
    ("sub_type", "TEXT NOT NULL DEFAULT 'artist'"),
    ("artist", "TEXT"),
    ("album", "TEXT"),
    ("author", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("search_interval_hours", "INTEGER NOT NULL DEFAULT 6"),
    ("last_search_at", "TEXT"),
    ("next_search_at", "TEXT"),
    ("completed_at", "TEXT"),
    ("upgrade_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("min_seeders", "INTEGER NOT NULL DEFAULT 0"),
    ("hit_count", "INTEGER NOT NULL DEFAULT 0"),
    ("downloader_id", "TEXT"),
    ("save_path", "TEXT"),
    ("qb_category", "TEXT"),
    ("qb_tags", "TEXT"),                       # 逗号分隔
    ("preferred_quality", "TEXT NOT NULL DEFAULT 'any'"),
    ("min_size_mb", "INTEGER"),
    ("max_size_mb", "INTEGER"),
    ("free_only", "INTEGER NOT NULL DEFAULT 0"),
    ("exclude_keywords", "TEXT"),              # JSON 数组
    ("include_keywords", "TEXT"),              # JSON 数组
]


async def _migrate_columns(conn, table: str, columns: list[tuple[str, str]]) -> list[str]:
    """幂等地为任意表补列：PRAGMA table_info 检测后按需 ALTER。返回实际新增的列名。"""
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        existing = {row[1] for row in await cur.fetchall()}

    added = []
    for name, decl in columns:
        if name in existing:
            continue
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        added.append(name)
    if added:
        await conn.commit()
        logger.info(f"{table} 表 schema 升级，新增列: {added}")
    return added


async def _migrate_task_columns(conn) -> list[str]:
    """幂等地为 tasks 表补 v2/v4/v5 新增列。返回实际新增的列名。"""
    return await _migrate_columns(conn, "tasks", _TASK_COLUMNS)


async def _migrate_watchlist_columns(conn) -> list[str]:
    """幂等地为 watchlist 表补 v4/v5 新增列；首次加上 sub_type 时顺带回填旧行。"""
    added = await _migrate_columns(conn, "watchlist", _WATCHLIST_COLUMNS)
    if "sub_type" in added:
        await _backfill_watchlist_v5(conn)
    return added


async def _backfill_watchlist_v5(conn) -> None:
    """旧订阅行的订阅类型回填（只在 sub_type 列刚被加上时跑一次）。

    kind=music → sub_type=artist 且 artist=query（旧版的 query 就是歌手名）；
    kind=audiobook → sub_type=audiobook，author / album 留空，query 保留为书名。
    不在 ALTER 的 DEFAULT 里做：DEFAULT 是全表统一值，按 kind 分派只能 UPDATE。
    """
    await conn.execute(
        "UPDATE watchlist SET sub_type = 'artist', artist = query "
        "WHERE kind = 'music' AND (artist IS NULL OR artist = '')"
    )
    await conn.execute(
        "UPDATE watchlist SET sub_type = 'audiobook' WHERE kind = 'audiobook'"
    )
    await conn.commit()
    logger.info("watchlist 旧行已按 kind 回填 sub_type")


async def init_db(db_path: str | Path) -> aiosqlite.Connection:
    """初始化数据库连接，启用 WAL 模式并执行全部 migration。

    migration 幂等，可对既有库重复执行以升级 schema。

    Args:
        db_path: SQLite 数据库文件路径

    Returns:
        aiosqlite.Connection（已配置 WAL + busy_timeout）
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row

    # 启用 WAL 模式 + busy_timeout
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA synchronous = NORMAL")
    await conn.execute("PRAGMA busy_timeout = 5000")
    await conn.execute("PRAGMA foreign_keys = ON")

    # 执行 migration（v1 → v5 顺序）
    await conn.executescript(_MIGRATION_V1)
    await conn.executescript(_MIGRATION_V2)
    await conn.executescript(_MIGRATION_V3)
    await conn.executescript(_MIGRATION_V4)
    await conn.executescript(_MIGRATION_V5)
    await _migrate_task_columns(conn)
    await _migrate_watchlist_columns(conn)
    await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    await conn.commit()

    return conn
