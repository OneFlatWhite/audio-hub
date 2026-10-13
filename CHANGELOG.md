# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.6.0] - 2026-10-13

站点与下载器的配置权威源从 YAML + 覆盖层改为 DB 两张表，新增站点中心 / 下载器中心两个管理页，
搜索链路从「整轮 search_round」重写为订阅引擎。**测试 575 → 1004**，ruff 0 error。

### 新增

- **站点中心** `/sites`（`web/routes_sites.py`）
  - 列表六态：未配置 / 未认证 / 认证失效 / 已禁用 / 熔断中 / 正常；行内显示最后错误与熔断截止时间
  - 三步向导：凭据（RSS / Cookie / API Key，只写不读、留空 = 不修改）→ 认证测试（用户等级回显）→ 分类发现（远端分类拉取 + 本地映射）
  - 启用门禁是服务端的：`auth_status != valid` 的站点不构建适配器、toggle 启用被拒并回显原因
  - **认证巡检** `sites/auth_check.py`：定时重验 Cookie / API Key，失效即降级并发 `site_auth_expired` 事件，恢复不打扰；job 间隔 `search.auth_check_hours`（默认 24h）
- **下载器中心** `/downloaders`（`web/routes_downloaders.py`）
  - 多 qBittorrent 实例：列表五态（已禁用 / 未配置凭据 / 未测试 / 连接失败 / 正常）、新建 / 编辑、默认切换、启用禁用
  - 「测试连接」走 `downloaders/probe.py` 三层只读探测（TCP → HTTP → 登录），结果落 `last_test_json` 供列表展示 qB 版本 / 剩余空间；连接参数或凭据变更自动作废旧测试结果
  - 删除三重保护（服务端兜底）：在途任务引用 / 默认实例 / 最后一个启用实例均拒绝
  - 每次写操作后 `settings_store.rebuild_downloaders` 原地替换注册表，派发 / 完成监听 / 做种链路下一轮即用新配置，**不需重启**；任务 / 订阅可按 `downloader_id` 指定实例，空则回落默认
- **订阅引擎**（`subscription/engine.py`，取代旧 search_round）
  - RSS 捕获（`rss_capture` job，`search.rss_interval_minutes`）+ 到点主动搜索（`subscription_search` job，`search.subscription_scan_minutes`，每条订阅带独立回退间隔）
  - 订阅类型三分：歌手（artist）/ 专辑（album，一次性）/ 有声书（audiobook，可绑连载）；命中流水 `subscription_hits` 全量留痕，决策五态（dispatched / review / rejected / skipped_dup / upgrade）
  - 专辑级择优与洗版：`best_quality_for_album` 按音质档比对，更高档触发 `upgrade`；已拥有专辑跳过不重抓
  - 一次性订阅拿到即 `completed`；**派发失败自动回滚命中流水**，下一轮重新决策重试
  - 订阅详情页 `/watchlist/{id}`：命中流水按决策筛选、已拥有专辑、关联连载
- **DB v5**：`sites` / `site_categories` / `downloaders` / `subscription_hits` 四张新表；`watchlist` 补订阅引擎字段（sub_type / artist / album / status / 调度与统计列等），旧行按 kind 幂等回填；`tasks` 补 `downloader_id` / `watch_id` / `album_key` / `quality_tag`
- **共享 bootstrap**（`bootstrap.py`）：CLI 与诊断脚本和 Web 走同一套装配（DB → hydrate → 种子导入 → 注册表），`cli.py` / `check_qb.py` / `verify_adapters.py` 不再各自拼配置
- `registry_store`：YAML 的 sites / qbittorrent 段首启种子导入（幂等，表非空不覆盖），app_settings 旧站点 / qb 覆盖键收编进表后删除
- 设置页「通用」组新增三个调度间隔（RSS 捕获 / 订阅搜索 / 认证巡检），全部热生效

### 修复

| 问题 | 影响 | 修正 |
|---|---|---|
| `_downloader_cards` / `_subscription_overview` 缺失 | 总览页 500 | `routes_pages` 实现两张卡片 |
| 熔断事件在引擎路径丢失 | 站点连续失败不再发 `site_circuit_open` | 订阅引擎按 `record_site_failure` 返回值补发事件 |
| CLI / 诊断脚本被认证门禁废掉 | `cli run-once` 等永远空转 | 共享 bootstrap 与门禁对齐（`build_adapters` 显式参数） |
| `check_qb.py` 读旧配置源 | 诊断结果与运行时脱节 | 改读 downloaders 表（账密优先，API Key 兜底），只读不建库 |
| 设置页 qb 组静默失效 | 改了不生效且无提示 | 整组撤下，换只读指引卡指向下载器中心 |
| `/review` 采纳派发读 `config.qbittorrent` | 多实例时落错目录 / 分类 | 改从下载器注册表按 `task.downloader_id` 解析 |
| `NOTIFY_KINDS` 缺 v0.6 事件 | 新事件（订阅命中 / 洗版 / 认证失效 / 下载器不可达 / 轮次汇总等）无法外推 | 全集补齐并与代码产出对齐 |
| `SeedingMonitor` 快照过期 | 注册表重建后仍用旧配置轮询 | 每轮实时读注册表 |
| 一次性订阅过早 `completed` + 派发失败留 failed 态 | 永远完不成也不重试 | 成功派发才置 completed；失败回滚命中流水下轮重试 |
| `web.port` 是死字段 | 文档称「启动读取」，实际 serve_dev.sh / Dockerfile 各自硬编码端口 | 新增 `python -m audio_hub` 启动入口（`__main__.py`），统一读 `config.web.port`；Dockerfile CMD 与 serve_dev.sh 改走该入口 |
| compose 六个死环境变量（`QB_HOST` 等） | 改了不生效，误导部署 | 删除，注释指向 config.yaml / 下载器中心 |

### 移除

- 旧搜索轮次死代码：`pipeline/search.py`（`search_round` / `match_candidates`）、matcher 的 `_build_task` 等（由订阅引擎与 `Dispatcher.dispatch_tasks` 取代）
- 设置页 qBittorrent 可编辑组（连接参数迁移到下载器中心；旧 `qb_*` 覆盖键首启收编进 downloaders 表）

### 测试

- 575（0.5.0）→ **1004**：站点中心 / 下载器中心 / 订阅引擎 / bootstrap / check_qb 全链路集成与单测补齐；`tests/integration/test_pipeline_dryrun.py` 随死代码删除
- `scripts/smoke_check.py` 页面清单 11 → 12（补 `/downloaders`）

## [0.5.0] - 2026-10-01

六大功能模块落地：归档链路从「cp 到 scratch」变成完整的校验 → 决策 → 落位 → 刮削闭环，补齐连载追踪、做种 H&R 与外部推送。Web 界面从 7 页扩到 12 页。**测试 274 → 575**，ruff 0 error。

### 新增

- **模块 A · 馒头派发打通**
  - `MTeamAdapter.resolve_download_url`：搜索结果里的 `genDlToken` 占位 URL 在实跑派发前 POST `/api/torrent/genDlToken` 换取带临时凭据的直链；已是直链的原样返回不发请求
  - `Dispatcher.resolve_download_url` 公开入口：流水线实跑与 `/review` 采纳后的单任务派发都走它，换取失败标 `resolve_download_url_failed`，不碰 qb
- **模块 B · 归档决策与入库校验**
  - `archive.decision`：`ArchiveDecision(NEW / UPGRADE / SKIP)`，决策键 `(watch_id, title_norm)`，质量分复用 matcher 音质权重表 ×100，与搜索阶段口径一致
  - `validation.checker`：扩展名白名单、最少音频文件数、FLAC `STREAMINFO` 头解析识别伪无损（位深 <16 或采样率 <44100）、可选 auCDtect 频谱检测；纯 stdlib 无额外依赖
  - 隔离区：校验不通过且 `on_fail=quarantine` → move 到 `quarantine_dir/<task_id>/`，任务进 `quarantined`；`/quarantine` 页可放行（重走归档）或丢弃
  - 回收区：UPGRADE 时旧版本 move 到 `recycle_dir/<YYYYMMDD>/<item_id>/`，`/library` 页可回滚或立即清理；轮询尾部按 `recycle_retention_days` 自动清理，围栏检查确保只删 recycle_dir 之下的路径
  - 刮削触发 `scrape.trigger`：音乐 POST MTW webhook（刮削 scratch 副本，成品库不被改写）；有声书 POST ABS `/api/libraries/{id}/scan`。两条通道尽力而为，失败只发 `scrape_failed` 事件不阻塞轮询
  - 原种目录全链路只有一次 `cp`，没有任何 move / unlink（PRD §8.2 红线）
- **模块 C · 有声书连载追踪** `series.tracker`
  - `SeriesEntry` / `PartEntry`：分集号解析（`第N集/部/卷` / `Part N` / `EP05` / `(03)`，支持自定义正则），**宁可解析失败不可解析错误**（括号数字限 1~3 位防年份误伤）
  - 匹配阶段按分集去重（已登记分集不再派发）；`total_parts` 达成时状态转 `completed` 并发 `series_completed` 事件
  - `/series` 页：建连载、绑订阅项、编辑规则与总部数、查看分卷进度
- **模块 D · 做种生命周期 H&R** `seeding.lifecycle`
  - `SeedingMonitor.poll_once`：从 qb 同步做种时长 / 分享率，`seed_seconds >= required 或 ratio >= required` 任一满足即达标；达标动作 `keep` / `pause`，**绝不调用任何删除接口**
  - 站点级阈值 `hr_min_seed_hours` / `hr_min_ratio` 覆盖全局默认；qb 中已消失的种子标 `removed`
  - `seeding_check` 调度任务（下限 1 分钟，独立于搜索轮次的防重入闸门）；`/seeding` 页支持暂停 / 恢复 / 手动标记达标 / 立即检查
- **模块 E · 通知推送**
  - `WecomNotifier` / `TelegramNotifier` / `GenericWebhookNotifier`，按 `notify.kinds` 过滤事件种类；设置页保存通知组后 `rebuild_push_sinks` 热重建，基础 sink（日志 / DB）不受影响
  - `POST /settings/notify/test` 测试发送
  - `/events` 事件日志页：按种类 / 级别筛选，可清空
- **总览升级**：新增隔离待处理、H&R 未达标、进行中连载 / 最近完结、最近 10 条事件四张卡片
- **站点熔断**（连续 N 次失败熔断 M 分钟，可配置）：`repo` 新增 `site_stats` 写入（`record_site_success` / `record_site_failure` / `is_circuit_open`），`search_round` 每站搜索前查熔断、成功 / 失败后计数；达阈值时发 `site_circuit_open` 事件（warning），熔断期内该站跳过并计入 `round_summary.skipped_circuit`，到期自动恢复。阈值 `search.circuit_fail_threshold`（默认 3）/ `search.circuit_open_minutes`（默认 30）可在设置页热改；总览 / 站点页显示「熔断至 HH:MM」
- **集成**：`PipelineRunner` 把自身 notifier 传给 `CompletionMonitor`，归档事件（`routed` / `quarantined` / `archive_skipped` / `scrape_failed`）进 events 表并外推；`register_task` 在派发成功后登记连载分卷

### 修复

| 问题 | 影响 | 修正 |
|---|---|---|
| 侧边栏 brand 写死 `v0.2` | 与 `/health` 返回的 `__version__` 不一致 | 模板改读 Jinja 全局变量 `version`，由 `templates.py` 注入 `__version__`，版本号回归单一真源 |
| `CompletionMonitor` 未接 notifier | 归档事件只写日志，事件页与推送渠道看不到归档结果 | `PipelineRunner.__init__` 显式传入，补装配断言测试 |
| 审查小修：`.upgrading` 残留清理扫的是 qb `save_path` | 扫错目录（原种只读，不可能有残留），成品库的残留反而清不到 | `_library_roots()` 改为 `paths.library_music` / `paths.library_audiobook`（去重，不存在跳过）；`SeedingMonitor._on_satisfied` 复用 `_qb_action` 统一封装；`search_round` 超时改为显式 `(asyncio.TimeoutError, TimeoutError)`；隔离丢弃 / 回收区清除 / 删连载 / 清理事件四个破坏性 POST 收紧为仅管理员（`deps.require_admin_csrf`） |

### 页面（12 个）

`/login` `/setup` · `/dashboard` `/watchlist` `/records` `/review` `/sites` `/settings` · **新增** `/library` `/quarantine` `/series` `/seeding` `/events`

### 已知限制

- 四个站点的解析器与分类 ID **仍未经真实响应验证**（见 README「已知限制」）
- NAS Docker 部署未实际验证（Dockerfile 已修，但未在 NAS 上 `docker compose up`）
- auCDtect 为可选外部工具，未配置时只做容器层检查
- 刮削联动的 MTW webhook 契约为本项目约定的 JSON `{path, kind, title}`，需 MTW 侧配合

## [0.4.0] - 2026-10-01

阻塞缺陷清零 + 为 0.5 的六大模块铺底：配置子模型、DB v4、通知抽象、页面骨架。

### 修复（HANDOFF §四 三个已知缺陷）

| 问题 | 影响 | 修正 |
|---|---|---|
| `serve_dev.sh` 以字符串调 `ensure_key_file`，`p.exists()` 抛 `AttributeError` 被 `set -uo pipefail`（无 `-e`）吞掉 | **密钥文件从未生成**，设置页保存任何凭据都失败 | `ensure_key_file` 改收 `str \| Path`；脚本侧显式 `Path(...)`，heredoc 加 `\|\| die`，并校验文件确实存在 |
| Dockerfile 先 `COPY pyproject.toml` 再 `pip install .`，此时 `src/` 不存在而 hatch 要从 `src/audio_hub/__init__.py` 读版本 | 镜像构建必然失败 | `COPY pyproject.toml README.md src/` 后只 install 一次 |
| ruff 积压告警 | 噪音掩盖真实问题 | `ruff check src tests` 清零；B008 按 ruff 文档对 `fastapi.Depends/Query` 配 `extend-immutable-calls`，其余可变默认值仍受检 |

### 新增

- **config 子模型**：`paths` / `validation` / `scrape` / `seeding` / `notify`，全部有默认值，旧 YAML 不写这些段仍能加载；顶层 `scratch_inbox` 自动迁到 `paths.scratch_inbox`；`SiteConfig` 增 `hr_min_seed_hours` / `hr_min_ratio`
- **设置页 5 个新分组**（路径 / 校验 / 刮削 / 做种 / 通知）+ 站点级 H&R 阈值；每项沿用 v0.3 的 `hot` / `restart` 声明，`abs_token` / `wecom_webhook` / `telegram_bot_token` 按凭据加密入库只写不读
- **DB v4 migration**：`library_items` / `recycle_entries` / `quarantine_items` / `series` / `series_parts` / `seed_records` / `events`，`tasks` 补 `validation_result` / `archive_decision` / `series_id` / `part_no` 等列，`watchlist` 补 `series_id`
- **Notifier 抽象** `notify.base`：`Event` 结构 + `Notifier` 协议（`async emit`）+ `BaseNotifier` 便捷方法 mixin + `MultiNotifier` 扇出（单 sink 异常不外溢）；`LogNotifier` / `DbNotifier` 两个基础 sink，`app.state.notifier` 供各模块复用
- **5 个新页面骨架**（只读）：`/library` `/quarantine` `/series` `/seeding` `/events`，侧边栏导航与隔离区徽标
- 馒头 `genDlToken` 二次请求（实现在适配器层，派发接入见 0.5.0 模块 A）

## [0.3.0] - 2026-09-30

设置页从只读变为可编辑，并诚实标注每一项的生效范围。

### 新增

- **覆盖层** `settings_store`：界面改动写 `app_settings` 表，启动时 `hydrate_config` 合并到 YAML 之上；**绝不回写 `config.yaml`**（PyYAML round-trip 会吃掉注释，写入中断会损坏配置）
- **`hot` / `restart` 分级**：每个 `Spec` 显式声明；热生效项保存后立即重排调度 / 重建适配器 / 派发时实时读取；需重启项只落库并置 `__restart_pending` 标记，页面顶部挂横幅，下次启动时清除
- **凭据加密入库**：`kind="secret"` 的键 Fernet 加密存 DB，界面永不回显，只显示「已配置（N 字符）」，留空表示不修改
- **站点凭据表单**：按 `site_credential_kinds` 动态生成（NexusPHP 收完整 RSS URL，馒头收 API Key），URL 字段保存时校验必须为 http(s)
- `db_path` 刻意排除在覆盖层外（改它会把覆盖层自己搬走）

### 修复

| 问题 | 影响 | 修正 |
|---|---|---|
| `credential_status` 返回 `{"items": [...]}`，模板 `s.items` 被解析为 dict 方法 | 设置页渲染即崩，连带 35 个依赖 `/settings` 的测试 | 改名 `creds`；模板校验必须 render 不能只 parse |
| bool 字段用 checkbox | 取消勾选时浏览器不发送该字段，覆盖层语义「未提交＝不修改」→ 关不掉 | 改 select，永远提交一个值 |
| 渲染分组用 `SITE_PREFIX`、保存端点用全集前缀 | 凭据能保存但界面不渲染输入框 | 两处统一为同一前缀集 |

## [0.2.0] - 2026-09-30

Web 界面与认证。调试端点收敛到 `/api/` 前缀并要求登录。

### 新增

- **7 个页面**（Jinja2 服务端渲染 + 原生 fetch，不用 HTMX，无 CDN 离线可用）：登录 / 初始化、总览、订阅项、命中记录、待审队列、站点状态、设置（只读）
- **认证**：Argon2id 密码哈希（`argon2-cffi`，明确不用已停止维护的 passlib）；首次启动无管理员时 `/setup` 建号，不预设默认密码
- **会话**：服务端 DB 存储 + 随机不透明 token cookie，滑动续期，可全部吊销；不需要签名密钥
- **CSRF**：所有写操作必须带 token（表单隐藏字段或 `X-CSRF-Token` 头），meta 标签下发给前端自动注入
- **登录限流**：单 IP 窗口内尝试次数（对齐 Navidrome `AuthRequestLimit`），`login_attempts` 表
- **待审队列** `/review`：采纳即派发（实跑模式下真实投递 qb）、拒绝附原因；`review_decisions` 记录人工决策
- **站点健康表** `site_stats`：建表（含 `consecutive_fail` / `circuit_open_until` 列）与总览 / 站点页展示；**计数与熔断的写入逻辑尚未接入流水线**，页面目前只会显示默认值
- DB v2：`users` / `sessions` / `login_attempts` / `site_stats` / `review_decisions` / `alias_cache`，`tasks` 补派发所需列（`download_url` / `quality` / `site_category` 等）
- **24 项端到端冒烟** `scripts/smoke_check.py`：未登录守卫、首次初始化、页面渲染、静态资源按实际引用提取验证、CSRF 下发、写操作三重防护、登出拦截、版本一致性

### 修复

| 问题 | 影响 | 修正 |
|---|---|---|
| `_build_task` 在待审 / 失败路径未写 `download_url` 等 v2 字段 | `/review` 采纳后拿不到下载链接，无法真实派发 | 三条创建路径统一走 `_build_task` |
| 版本号散落（`__init__` / `FastAPI(version=)` / 启动日志） | 改一处漏两处 | 统一引用 `__version__`，pyproject 改 `dynamic = ["version"]` |

## [0.1.0] - 2026-09-29

首个可运行版本。核心链路（订阅 → 搜索 → 类型判别 → 匹配打分 → 去重 → 派发 → 完成监听）已实现并通过 dry-run 验证。

### 已实现

- **适配器层**
  - `NexusPhpAdapter`：憨憨 / 天空 / 观众三站共用，参数化 cat_id 与标题括号风格（【】vs []）
  - `MTeamAdapter`：馒头独立实现（mTorrent REST API + API Key 认证 + 双域名 failover）
  - 统一输出 `NormalizedTorrent`；registry 支持中文站点别名
- **流水线**
  - 搜索编排：asyncio 并发 + 单站失败隔离 + search 空结果自动降级全量 RSS
  - 类型判别：三级信号（站点分类名 / 标题关键词 / 扩展名），排除优先于音乐；分类与标题冲突时降级 UNKNOWN 进待审
  - 匹配打分：音质权重 + 优惠权重 + 别名命中 + 做种者加成；high/medium/low 分级
  - 去重准入：info_hash 全局唯一 + (site, title_normalized) 二级
  - 派发：qb API Key 认证；`Conflict409Error` 视为成功（幂等）；VIDEO 防御性拦截
- **Dry-run**：dispatcher 参数化，测试与生产走同一匹配逻辑，仅替换副作用层
- **持久化**：SQLite + WAL；watchlist / tasks / torrents 三表；repo 层收敛 SQL
- **崩溃恢复**：启动对账（与 qb 比对 in-flight 任务）、孤儿任务标记、`.upgrading` 残留清理
- **调度**：APScheduler 内嵌；轮询间隔下限 5 分钟；防重入（runner + max_instances 双重）
- **CLI**：init-db / add / list / remove / status / run-once / verify-adapters
- **Web**：FastAPI + lifespan；`/health` `/status` `/run-once`
- **安全**：Fernet 凭据加密；passkey 日志脱敏；keyfile 与 config 分离
- **验证工具**：`scripts/verify_adapters.py`，含三项专项风险检测

### 修复（开发过程中发现并修正）

| 问题 | 影响 | 修正 |
|---|---|---|
| `score_candidate` 的 high 置信度条件写死 `category == MUSIC` | **有声书永远达不到 high，整条有声书链路静默卡在待审队列，无任何报错** | 改为 `category in (MUSIC, AUDIOBOOK)`，并加回归测试锁死 |
| 有声书 MP3 被按音乐音质阶梯扣分（0.40 < 0.7 门槛） | 同上，叠加放大 | 拆出 `detect_audiobook_format`（MP3 权重 0.80），与音乐阶梯分离 |
| high 置信度只认 query 本身，别名命中被降级 | 用英文名/别名订阅时全部落入待审 | 改为 `bool(hits)` 判定 |
| `search` 返回空时不 fallback | **静默漏抓**：被当作"确实没有资源"，而实际是站点忽略了 search 参数 | 空结果也走全量 RSS 降级 |
| `tasks.watch_entry_id` 设 NOT NULL + 外键 | 硬排除候选、人工手动任务无法落库（IntegrityError） | 改为可空 |
| `total_raw` 统计去重后的数量 | 转化率分析失真，看不出站点真实返回量 | 改为去重前计数 |
| RSS `<category>` 解析了却未参与判别 | PRD §6.1 规定站点分类是权重最高的一级信号，实际只用了标题关键词 | 新增 `_resolve_category` 融合站点分类名与标题判别，VIDEO 优先、冲突降级 UNKNOWN；`NormalizedTorrent` 增 `site_category` 字段供溯源；补 9 个融合逻辑测试 |
| 全站时间戳为 naive datetime | **馒头 `createdDate` 是北京时间无时区标记，按 UTC 解析会偏 8 小时**，影响新种优先与做种时长计算；且跨时区比较只在边界暴露，极难排查 | 新增 `timeutil`（UTC-aware 统一约定 + `parse_site_datetime` 按 Asia/Shanghai 解析站点本地时间）；全模块时间戳改用 UTC；补 13 个时区测试含 8 小时偏移回归锁 |
| opencc 繁简转换静默失败（`except: pass`） | 繁体标题归一化漏转 → **静默漏抓**，用户无从得知原因；且每候选重建 OpenCC 实例 | 改为惰性缓存单例 + 失败仅警告一次（可观测） |

### 已知限制

- **解析器未经真实站点响应验证**，fixture 为合成样本（详见 README「未验证项」）
- 各站 `music_cat_id` 为猜测值，需实测
- 馒头 `genDlToken` 二次请求未实现（downloadUrl 为空时无法派发）
- 优惠状态在三个 NexusPHP 站上依赖标题文本，RSS 不提供该字段时为 `UNKNOWN`
- 通知仅结构化日志，无外部推送
- 无 Web 界面（`/review` 待审队列在 v0.2）
- 未实现：入库前校验、归档决策（upgrade/回收区）、刮削触发、有声书连载追踪、做种生命周期

### 依赖说明

- Python ≥ 3.11（3.10 会因 `qbittorrent-api` / `pydantic-core` 版本失败）
- 明确**不使用 passlib**（已停止维护，与 bcrypt 5.0.0 不兼容），改用 argon2-cffi
- qbittorrent-api ≥ 2026.8（标注支持 qb v5.2.3 / Web API v2.15.1）
