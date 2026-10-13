# audio-hub · 音乐 + 有声书统一获取中枢

> 当前版本 **v0.5.0**（2026-10-01）· 575 项测试全绿 · ruff 0 error
> 面向家庭 NAS 的单用户工具：从 PT 站订阅音乐与有声书，经 qBittorrent 下载，
> 校验、归档到媒体库，触发刮削，并全程追踪做种义务（H&R）。

---

## 一、项目定位

audio-hub 解决的是「订阅 → 搜索 → 筛选 → 下载 → 归档 → 刮削 → 做种」这条链路里
**所有不该由人手重复做的事**：

- 按艺术家 / 书名订阅，自动在四个 PT 站（憨憨 hhanclub、天空 hdsky、观众 audiences、馒头 mteam）搜索；
- 区分音乐与有声书、识别音质（DSD / FLAC 24 / FLAC 16 / APE / MP3 / M4B …），
  低置信度结果进待审队列而不是误抓；
- 用 info_hash + 归一化标题双指纹去重，派发到 qBittorrent；
- 下载完成后**只复制不移动**（原种留在 `seeds/` 继续做种，保护 PT 哈希），
  复制件经扩展名 / 文件数 / FLAC 头伪无损 / 可选 auCDtect 校验，不合格进隔离区；
- 归档决策：库内无同名 → 新增；库内音质更差 → 升级并把旧版移入回收区（可回滚）；库内不差 → 跳过；
- 触发 MusicTag Web（音乐）或 Audiobookshelf（有声书）刮削，最终由 Navidrome / Audiobookshelf 播放；
- 有声书连载按分集号去重与追踪，完结时发事件；
- 做种时长 / 分享率达标前**绝不删除文件**，达标后可选择继续做种或暂停；
- 全部事件可推送到企业微信 / Telegram / 通用 webhook，并在 `/events` 页留痕。

它不是下载器、不是播放器、不是刮削器——这三件事分别交给 qBittorrent、Navidrome/Audiobookshelf、MTW/ABS。

---

## 二、架构与数据流

```
                 ┌─────────────────────────── audio-hub（FastAPI + APScheduler + SQLite）───────────────────────────┐
                 │                                                                                                   │
 PT 站 ×4 ──RSS/API──▶ adapters ──▶ 类型判别(music/audiobook) ──▶ matcher 打分 ──▶ dedup(info_hash+title_norm) ──┐  │
 (hhanclub/hdsky/      (nexusphp / mteam                 (VIDEO 硬排除)        (high → 直接派发)                   │  │
  audiences/mteam)      含 genDlToken)                                          (medium → /review 待审)             │  │
                 │                                                                                                   ▼  │
                 │                                                                     Dispatcher ──▶ qBittorrent WebAPI │
                 │                                                                     (dry_run 时只落库不推送)          │
                 │                                                                                                   │  │
                 │   CompletionMonitor 轮询 qb 完成 ◀─────────────────────────────────────────────────────────────────┘  │
                 │        │                                                                                            │
                 │        ▼  cp -a（原种不动，继续做种 → SeedingMonitor 跟踪 H&R）                                       │
                 │   scratch/inbox ──▶ validate_dir ──┬─ 不通过 ──▶ quarantine/ + quarantine_items（/quarantine 释放/丢弃）│
                 │                                    └─ 通过 ──▶ ArchiveDecision(NEW/UPGRADE/SKIP)                      │
                 │                                                   │ UPGRADE：旧版 → recycle/ + recycle_entries（可回滚） │
                 │                                                   ▼                                                  │
                 │                                       library_music / library_audiobook + library_items              │
                 │                                                   │                                                  │
                 │                                       trigger_for_kind ──▶ MTW webhook（音乐）/ ABS scan API（有声书）  │
                 │                                                                                                      │
                 │   events 表 ◀── MultiNotifier(Log/Db + 企业微信/Telegram/webhook) ◀── 全链路事件                       │
                 └──────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                                     │
                                                                     ▼
                                                   Navidrome（音乐）/ Audiobookshelf（有声书）播放
```

**源码布局**（`src/audio_hub/`）：

| 目录 / 模块 | 职责 |
|---|---|
| `adapters/` | 站点适配：`nexusphp.py`（憨憨/天空/观众 RSS + 搜索）、`mteam.py`（馒头 API，含 `genDlToken` 二次请求） |
| `pipeline/` | `matcher`（类型判别 + 音质打分）、`dedup`、`dispatcher`（派发 qb、`resolve_download_url`） |
| `completion/` | `CompletionMonitor` 轮询 qb 完成任务，驱动归档链路 |
| `validation/` | `validate_dir`：扩展名 / 最少文件数 / FLAC 头伪无损 / 可选 auCDtect |
| `archive/` | `decision`（NEW/UPGRADE/SKIP）、`router`（cp 到 scratch → 决策 → library/recycle） |
| `scrape/` | `trigger_for_kind`：MTW webhook / Audiobookshelf scan |
| `series/` | 有声书连载：分集号解析、按分集去重、完结判定 |
| `seeding/` | `SeedingMonitor`：做种时长 / 分享率、H&R 达标判定、暂停/恢复 |
| `notify/` | `Event` / `Notifier` 协议、`MultiNotifier`、Log/Db/企业微信/Telegram/通用 webhook |
| `web/` | FastAPI 路由 + Jinja2 服务端渲染（原生 fetch，无 HTMX、无 CDN） |
| `db.py` / `repo.py` | SQLite（WAL）、幂等 migration v1→v4、数据访问 |
| `config.py` / `settings_store.py` | YAML 基线配置 + DB 覆盖层（设置页改动不回写 YAML） |
| `scheduler.py` | `PipelineRunner`、周期任务（搜索轮次 / 完成轮询 / 做种检查） |
| `crypto.py` | Fernet 凭据加密，密钥文件独立于配置 |

---

## 三、功能矩阵

| 能力 | 状态 | 说明 |
|---|---|---|
| 四站 RSS / 搜索适配 | ✅ 代码完成 · ⚠️ **真实站点未实测** | fixture 为按规范推断的合成样本，详见「已知限制」 |
| 馒头 `genDlToken` 二次请求 | ✅ | `downloadUrl` 为空时换临时令牌；待审采纳路径同样走它 |
| 音乐 / 有声书类型判别、音质打分 | ✅ | VIDEO 硬排除；medium 置信度进待审 |
| 去重（info_hash + 归一化标题） | ✅ | 跨站同专辑不重复下载 |
| 派发 qBittorrent（API Key 认证） | ✅ | `dry_run` 默认开，切实跑需重启 |
| 待审队列采纳 / 拒绝 | ✅ | 采纳后真实派发（`download_url` 已持久化） |
| 完成监听 + `cp -a` 到 scratch | ✅ | 原种保留只读做种，有测试断言源文件仍存在 |
| 入库校验 + 隔离区 | ✅ | auCDtect 可选（未配置则跳过频谱检测） |
| 归档决策 NEW / UPGRADE / SKIP | ✅ | 决策键 `(watch_id, title_norm)`，质量分复用 matcher 权重表 |
| 回收区 + 回滚 / 清除 | ✅ | `/library` 页面操作 |
| 刮削触发（MTW webhook / ABS scan） | ✅ 代码完成 · ⚠️ 未对接真实实例 | 失败只发 `scrape_failed` 事件，不阻断归档 |
| 有声书连载追踪 | ✅ | 分集号正则可按连载自定义；完结事件 |
| 做种生命周期 / H&R | ✅ | 时长或分享率任一满足即达标；**绝不删文件** |
| 通知推送（企业微信 / Telegram / webhook） | ✅ | 设置页热重建、测试发送 |
| 事件日志页 | ✅ | `/events`，可清空 |
| Web UI 12 页 + Argon2id 认证 + CSRF + 限流 | ✅ | 单管理员，首次启动走 `/setup` |
| 设置页可编辑（hot / restart 分级、覆盖层、凭据加密） | ✅ | 见「配置说明」 |
| CLI（init-db / add / list / remove / status / run-once / verify-adapters） | ✅ | `python -m audio_hub.cli` |
| Dockerfile / docker-compose | ✅ 构建修复 · ⚠️ **NAS 未部署验证** | host 网络，`/volume1/musiclab`、`/volume1/audiolab` |

---

## 四、快速开始

### 方式 A：本机 WSL 开发运行（推荐先走这条）

> Windows 侧开了 Smart App Control 时无法加载未签名 `.pyd`，pydantic / cryptography / argon2 导入即失败。
> 因此**所有 Python 都在 WSL 里跑**，D 盘是权威副本，`~/audio-hub` 是可运行副本。

```bash
# 1. 首次准备（WSL）：同步 D 盘代码到可运行副本，建 venv 并安装依赖
mkdir -p ~/audio-hub
cd /mnt/d/Documents/audio-hub && tar cf - --exclude=__pycache__ --exclude=.pytest_cache \
  src tests pyproject.toml scripts | (cd ~/audio-hub && tar xf -)
cd ~/audio-hub && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 2. 起服务（脚本会自动重新同步 D 盘代码、生成 dev 配置与密钥，监听 0.0.0.0:8000）
bash /mnt/d/Documents/audio-hub/scripts/serve_dev.sh
```

浏览器打开 <http://localhost:8000> → 首次自动跳 `/setup` 创建管理员 → 进入总览。

`serve_dev.sh` 生成的 dev 配置（`~/audio-hub-data/config.yaml`）**刻意保守**：
`dry_run: true`、四站全部 `enabled: false`、qb 凭据为空。设置页显示「✗ 未配置」是如实反映，不是坏了。

### 方式 B：Docker Compose（NAS 部署，尚未实机验证）

```bash
cp config/config.example.yaml config/config.yaml   # 填入实际值，见「配置说明」
python scripts/init_secrets.py                      # 生成 config/secrets.key 并加密凭据
docker compose up -d --build
```

- 服务监听 `0.0.0.0:8090`（host 网络，与 qb 同宿主机时直接 `localhost:8085` 访问 qb）；
- `./config` 挂到 `/config`（`config.yaml` + `secrets.key` + `hub.db`）；
- `/volume1/musiclab:/musiclab`、`/volume1/audiolab:/audiolab` **必须与 qb 容器内路径一致**，
  否则 `save_path` 与归档路径对不上。

---

## 五、配置说明

配置分两层：**`config.yaml` 是基线**（人手维护、含注释、随卷挂载），**设置页改动存 DB 覆盖层**（`app_settings` 表），
启动时合并到基线之上。设置页永不回写 YAML——PyYAML round-trip 会丢注释、写入中断会损坏配置。

唯一例外：`db_path` 不进覆盖层（覆盖层自身就存在这个库里）。

### `config.example.yaml` 各段

| 段 | 字段 | 含义 | 生效方式 |
|---|---|---|---|
| 顶层 | `dry_run` | 空跑：搜索 / 打分 / 落库但不推送 qb。**默认 true** | restart |
| | `db_path` | SQLite 路径 | 不可在界面改 |
| | `log_level` | 日志级别 | restart |
| | `poll_interval_minutes` | 搜索轮询间隔，下限 5 分钟（低于自动提升并告警） | hot |
| `search` | `circuit_fail_threshold` / `circuit_open_minutes` | 站点熔断：连续失败 N 次（默认 3）后该站跳过 M 分钟（默认 30） | hot |
| `web` | `port` / `cookie_secure` / `session_days` / `auth_request_limit` / `auth_window_minutes` | 端口、Secure cookie（纯内网 HTTP 必须 false）、会话天数、登录限流 | `cookie_secure` hot，其余启动读取 |
| `paths` | `scratch_inbox` / `library_music` / `library_audiobook` / `recycle_dir` / `quarantine_dir` / `recycle_retention_days` | 归档链路全部目录（**容器内视角**） | 全部 restart |
| `validation` | `enabled` / `allowed_exts` / `min_audio_files` / `fake_lossless_check` / `aucdtect_path` / `on_fail` | 入库校验；`on_fail` = `quarantine`（隔离）或 `pass_through`（照常归档只记警告） | 全部 hot |
| `scrape` | `mtw_webhook_url` / `abs_url` / `abs_library_id` / `abs_token_encrypted` / `timeout_seconds` | MTW 归档通知、Audiobookshelf 扫描 | 全部 hot |
| `seeding` | `enabled` / `check_interval_minutes` / `default_min_seed_hours` / `default_min_ratio` / `on_satisfied` | 做种轮询与 H&R 默认阈值；`on_satisfied` = `keep` / `pause` | `check_interval_minutes` restart，其余 hot |
| `notify` | `wecom_webhook_encrypted` / `telegram_bot_token_encrypted` / `telegram_chat_id` / `generic_webhook_url` / `kinds` | 推送渠道与订阅的事件种类 | 全部 hot（保存即热重建推送 sink） |
| `qbittorrent` | `host` / `port` / `api_key_encrypted` / `category` / `tag` / `save_path` | qb WebAPI；**`save_path` 是 qb 侧（NAS）路径，不是本机路径** | host/port/api_key restart；其余 hot |
| `sites[]` | `name` / `enabled` / `rss_url` / `search_url` / `api_key_encrypted` / `music_cat_id` / `audiobook_cat_id` / `max_rows` / `title_bracket_style` / `hr_min_seed_hours` / `hr_min_ratio` | 站点开关、凭据、分类 ID、站点级 H&R 阈值（留空沿用 `seeding.default_*`） | 全部 hot |

### hot / restart 语义

每个设置项显式声明生效方式，设置页按分组展示：

- **hot**：保存后立即应用到运行时（如 `poll_interval_minutes` 重排调度、`notify.*` 重建推送 sink、校验与刮削参数下次归档即用）。
- **restart**：保存后写入 `__restart_pending` 标记，页面顶部挂橙色横幅明说「运行时仍在用旧值」；
  下次启动自动清除标记。`dry_run`、qb 连接参数、全部 `paths.*` 属于此类——这些值在 `Dispatcher` / `CompletionMonitor` / qb 客户端构造时固化，
  不能谎报「已生效」，否则会出现「以为空跑保护还开着，实际已在真实下载」。

### 凭据加密

- `*_encrypted` 字段（qb API Key、馒头令牌、ABS token、企业微信 webhook、Telegram token）用 Fernet 加密，密钥在 `secrets.key`（`AUDIO_HUB_SECRETS_KEY`）。
- 设置页**只写不读**：保存后只显示「已配置（N 字符）」，永不回显明文。
- `load_fernet` 每次调用时读密钥文件，新建密钥不需要重启。
- **密钥丢失 = 所有加密凭据永久不可恢复**（启动时记错误日志并回退到 YAML 值）。请单独备份密钥，且不要和 `config.yaml` 放同一个备份包。
- 站点凭据是**完整 RSS 地址**（passkey 内联其中），不是裸 passkey；`SiteConfig` 没有 `passkey` 字段，填裸 passkey 会被静默丢弃。
  馒头例外：用存取令牌（`api_key_encrypted`）走 `Authorization` header。

---

## 六、页面导览（12 页）

| 路由 | 一句话 |
|---|---|
| `/setup` | 首次启动创建唯一管理员（已有用户后拒绝访问） |
| `/login` | Argon2id 口令登录，单 IP 限流（默认 15 分钟 5 次） |
| `/dashboard` | 总览：订阅 / 待审 / 已派发 / 已完成 / 失败 / 磁盘剩余，v0.5 新增隔离待处理、H&R 未达标、活跃连载三卡，站点健康表与最近事件 |
| `/watchlist` | 订阅清单：添加 / 启停 / 删除订阅项（艺术家名或书名 + 别名 + 类型） |
| `/records` | 任务记录：按状态筛选全部任务（discovered → dispatched → completed → archived / failed） |
| `/review` | 待审队列：medium 置信度候选的采纳（真实派发）/ 拒绝 |
| `/sites` | 站点状态：开关、凭据配置情况、健康表（`site_stats`，当前自动计数未接入，见已知限制） |
| `/settings` | 设置：通用 / qBittorrent / 站点 / 路径 / 校验 / 刮削 / 做种 / 通知八组，含推送测试发送 |
| `/library` | 资料库与回收区：已入库条目一览；回收区条目可**还原**（回滚升级）或**清除** |
| `/quarantine` | 隔离区：校验未通过的内容及短码原因，可**释放**（继续归档）或**丢弃** |
| `/series` | 连载追踪：按订阅项建连载、设总集数与分集号正则、查看已收集分集与完结状态 |
| `/seeding` | 做种 / H&R：每个任务的做种时长、分享率、达标状态；可暂停 / 恢复 / 手动标记达标 / 立即检查 |
| `/events` | 事件日志：DbNotifier 落库的全部事件（派发 / 完成 / 归档 / 隔离 / H&R 达标 / 连载完结 / 失败），可清空 |

另有无认证的 `/health`（返回 `{status, version, dry_run, adapters}`）与需 CSRF 的 `/api/status`、`/api/run-once`、`/run-once`。
侧边栏版本号与 `/health` 同读 `audio_hub.__version__`，不会再漂移。

**权限**：所有页面与写操作需登录 + CSRF；不可逆的破坏性动作——修改设置、隔离区**丢弃**、回收区**清除**、**删除连载**、**清理事件**——仅管理员可操作（普通用户返回 403）；放行 / 恢复 / 暂停 / 恢复做种 / 编辑连载等可逆动作任何已登录用户都可。

---

## 七、真实站点与 qBittorrent 接入步骤

> 按顺序做，**每一步绿了再进下一步**。PT 站对自动化请求敏感，触发风控的后果落在账号上。

1. **起服务并建号**：`bash scripts/serve_dev.sh` → `/setup`。
2. **设置页 → qBittorrent**：填 host / port / API Key（qb WebUI 的 API Key 认证）/ `save_path`。
   `save_path` 必须是 **qb 进程视角的路径**（NAS 上通常是 `/musiclab/seeds` 之类容器内路径），不是 WSL 路径。
   保存后页面提示需重启——重启 `serve_dev.sh`。
3. **qb 连通性诊断**（只读，不改配置不动种子）：
   ```bash
   cd ~/audio-hub && .venv/bin/python /mnt/d/Documents/audio-hub/scripts/check_qb.py
   ```
   七步：① 生效配置（YAML + 界面覆盖层）→ ② `save_path` 语义检查 → ③ TCP 可达 → ④ HTTP 响应 → ⑤ API Key 认证 → ⑥ qb 侧真实路径参照 → ⑦ 结论。
   第 ⑥ 步会打印 qb 默认保存路径与现有种子路径，用来确认你填的 `save_path` 语义正确。
4. **设置页 → 站点**：逐站填完整 RSS URL（含 passkey）；馒头填存取令牌；打开 `enabled`。
5. **真实站点解析验证**（会发真实请求，注意配额）：
   ```bash
   cd ~/audio-hub && .venv/bin/python scripts/verify_adapters.py --save-samples        # 全部站点
   cd ~/audio-hub && .venv/bin/python scripts/verify_adapters.py --site mteam           # 单站
   ```
   专项检测：憨憨 `cat410=1` 是否生效、NexusPHP RSS `search` 参数是否被忽略、馒头双域名哪个可用。
   `--save-samples` 把真实响应存为样本，用于替换 `tests/fixtures/` 的合成 fixture、校准解析器。
6. **dry-run 全链路**：`/watchlist` 加 1～2 个订阅项 → `/run-once` → 看 `/records` 候选数、`/review` 待审、日志 `round_summary` 各站错误为空。
7. **切实跑**：设置页 `dry_run` 改为「实跑」→ 保存 → **重启服务** → 采纳一个待审项 → qb 出现该种子。
   首次只放 1～2 个订阅、`max_rows` 调小，观察一轮再放开。
8. **归档验收**：下载完成后 `seeds/` 原种仍在、`scratch/inbox` 有复制件、`/library` 出现条目、MTW/ABS 收到触发、Navidrome/ABS 可见。

---

## 八、通知 / 刮削 / H&R 配置示例

### 通知推送

```yaml
notify:
  wecom_webhook_encrypted: "<init_secrets.py 加密后的企业微信群机器人 webhook>"
  telegram_bot_token_encrypted: "<加密后的 Bot token>"
  telegram_chat_id: "123456789"
  generic_webhook_url: "http://your-host:9000/hook"     # POST JSON {kind, site, title, detail, level, at}（即 Event.to_json）
  kinds: [dispatched, completed, routed, quarantined, hr_satisfied, series_completed, failed]
```

- `kinds` 可选值即 7 种事件；留空则所有推送渠道静默，但 Log / Db 两个基础 sink 始终记录。
- 设置页「通知」组保存即热重建推送 sink，并提供「测试发送」按钮。
- 也可全部在设置页填写（凭据只写不读）。

### 刮削联动

```yaml
scrape:
  mtw_webhook_url: "http://localhost:8002/api/webhook/archive"   # 音乐归档完成后 POST {path, kind: "music", title}
  abs_url: "http://localhost:13378"                               # 有声书归档完成后 POST /api/libraries/{id}/scan
  abs_library_id: "lib_xxxxxxxx"
  abs_token_encrypted: "<加密后的 ABS API token>"
  timeout_seconds: 15
```

刮削触发失败只产生 `scrape_failed` 事件，不回滚归档——文件已在库里，可以手动在 MTW / ABS 里重扫。

### 做种 / H&R

```yaml
seeding:
  enabled: true
  check_interval_minutes: 30
  default_min_seed_hours: 72
  default_min_ratio: 1.0
  on_satisfied: keep            # 或 pause：达标后暂停 qb 任务

sites:
  - name: hhanclub
    hr_min_seed_hours: 48       # 站点级覆盖
    hr_min_ratio: 1.0
```

- 判定规则：**做种时长 ≥ 阈值 或 分享率 ≥ 阈值，任一满足即达标**，产生 `hr_satisfied` 事件。
- 任何路径都**不会删除文件或移除种子**；`pause` 只是暂停。`/seeding` 页可手动暂停 / 恢复 / 标记达标。

---

## 九、开发与测试

```bash
# 同步 D 盘 → WSL 可运行副本（改完 D 盘必须同步，否则跑的是旧代码）
cd /mnt/d/Documents/audio-hub && tar cf - --exclude=__pycache__ --exclude=.pytest_cache \
  src tests pyproject.toml scripts | (cd ~/audio-hub && tar xf -)

# 全量测试
cd ~/audio-hub && source .venv/bin/activate && pytest -q

# Lint
ruff check src tests

# 端到端冒烟（TestClient 跑真实用户流程：未登录守卫 → /setup → 11 页渲染 → 静态资源 → CSRF → 登出 → 版本一致性；
# 结果落盘 smoke_report.txt）
.venv/bin/python /mnt/d/Documents/audio-hub/scripts/smoke_check.py

# CLI
python -m audio_hub.cli init-db
python -m audio_hub.cli add --query "周杰伦" --kind music --aliases "Jay Chou"
python -m audio_hub.cli list
python -m audio_hub.cli run-once            # 加 --no-dry-run 真实派发
python -m audio_hub.cli verify-adapters
```

测试分布：`tests/unit/`（解析、匹配、决策、校验、连载、做种、通知等纯逻辑）、
`tests/integration/`（lifespan 装配、全链路 dry-run、Web 路由、设置覆盖层）。
`live` 标记的用例会访问真实站点，默认不跑。

版本号单一真源：`src/audio_hub/__init__.py` 的 `__version__`；`pyproject.toml` 用 `dynamic = ["version"]` 读取，
模板侧边栏与 `/health` 也读它，`smoke_check.py` 会校验三者一致。

---

## 十、已知限制

1. **四站解析器未接触真实响应**。`tests/fixtures/` 是按 NexusPHP 规范和馒头 API 文档推断的合成样本。
   字段名、嵌套结构、分类 ID（憨憨 410、天空 401，观众未知）都需要用 `verify_adapters.py --save-samples` 实测校准。
2. **NAS Docker 部署未验证**。Dockerfile 构建顺序已修复，但 `docker compose up` 尚未在实机跑过；
   `libffi-dev` 是否必要待实测构建后决定。
3. **auCDtect 可选**。未配置 `validation.aucdtect_path` 时只做 FLAC 头伪无损启发式检测，不做频谱分析。
4. **MTW / Audiobookshelf 刮削触发未对接真实实例**，webhook 路径与负载按两者公开接口约定实现。
5. 单用户、单进程设计：不支持多管理员，不做分布式调度。
6. 前端为服务端渲染 + 原生 fetch，无 HTMX、无 CDN（NAS 内网离线可用）；移动端只做基本自适应。
7. **站点熔断已实现，但阈值未经真实站点校准**。搜索轮次每站成功 / 失败都写入 `site_stats`，连续
   `search.circuit_fail_threshold`（默认 3）次失败后该站熔断 `search.circuit_open_minutes`（默认 30）分钟，期间跳过不请求，到期自动恢复；
   两个参数可在设置页热改。默认值是设计约定，是否足够保守要等实跑观察。

---

## 十一、安全红线

- RSS 链接内嵌 passkey，等同账号密码：日志已脱敏（`_mask_passkey`），**不要贴进 issue / 聊天 / 版本库**。
- 每站搜索频率由轮询间隔控制，下限 5 分钟（代码强制）；站点返回 429 时抛 `AdapterRateLimited`，该站本轮计入 `round_summary.site_errors`。
  连续 3 次失败（含 429 / 超时 / 解析失败）该站熔断 30 分钟，期间不再请求并发 `site_circuit_open` 事件（阈值见已知限制 7）；首次实跑前仍请用少量订阅观察。
- 默认 `dry_run: true`，切实跑必须用户明确操作并重启。
- host 网络下安全边界在路由器端口映射表：确认 8090 / 8085 / 8002 / 4533 / 13378 均未映射到公网，外网访问走 Tailscale。
- qb 容器不得映射 CloudFS / CD2 等 FUSE 挂载（不支持 BT 随机写，且网盘做种属 PT 站禁止行为）。
- 归档用 `cp -a` 不用 `mv`：原种只读做种，保护 PT 哈希。

---

更多历史与交接细节见 [HANDOFF.md](HANDOFF.md)，版本变更见 [CHANGELOG.md](CHANGELOG.md)。
