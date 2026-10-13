# audio-hub 开发交接文档

> 写给接手的开发工具。本文所有事实均已对照磁盘源码核实，标注了 `文件:行号`。
> 更新时间：2026-10-13（v0.7.5 静态资源缓存收尾；§二 / §五 / §八 已按现状复核，更早的历史描述保留供回溯）
> 上上一任助手因本机命令执行通道故障（`0xC0000142`，Bash/Grep/Glob/ComputerUse 全部不可用，
> 只能读文件与新建文件、无法修改已有文件、无法执行命令），改为「写补丁脚本 + 用户代跑」模式，
> 效率极低且已造成多次返工。本轮（0.4.0 / 0.5.0）已在正常命令执行能力下直接改文件、直接跑测试。

---

## 一、用户要的目标

**在本机（WSL Ubuntu）连通 NAS 上的 qBittorrent，跑通「订阅 → 搜索 → 筛选 → 派发下载」全链路。**

用户原话：「先从我本地跑我可以联接到 nas 的 qb 下载器，测通了后 可以订阅下载了再说！」

即分两步，**不要跳步**：

1. **先通 qb**：本机服务能连上 NAS 的 qb，凭据认证通过，`save_path` 语义正确
2. **再跑订阅下载**：真实站点搜索出结果 → 匹配打分 → 待审/采纳 → 派发 → 种子落到 NAS 正确目录

用户的最终归宿是部署到 NAS（`docker-compose.yml` 已按 host 网络 + `/volume1/musiclab` 挂载写好），
但**当前阶段明确只要求本地跑通**，部署是后续的事。

---

## 二、当前进度（已验证）

**版本：v0.7.5** · **测试：1074 passed / 0 failed** · **ruff：0 error**
（`cd audio-hub && .venv/bin/python -m pytest -q && .venv/bin/ruff check src scripts tests`。
当前开发机为 **macOS + Python 3.15.0 venv**（`.venv/`）；§五 描述的 WSL 流程是原始作者环境，见该节说明）

| 版本 | 内容 | 状态 |
|---|---|---|
| v0.1 | 核心链路：解析/判别/匹配/去重/派发/归档/崩溃恢复 + CLI | ✅ 已验证（175 测试） |
| v0.2 | Web UI 7 页面、Argon2id 认证、会话、CSRF、限流 | ✅ 已验证（24/24 冒烟） |
| v0.3 | 设置页可编辑（覆盖层、热生效/需重启分级、凭据加密入库、URL 校验） | ✅ 已验证（274 测试） |
| v0.4 | 三个阻塞缺陷修复、馒头 genDlToken、ruff 清零、config 子模型、DB v4、Notifier 抽象、新页面骨架 | ✅ 已验证 |
| v0.5 | 归档决策 / 校验隔离 / 回收回滚 / 刮削触发 / 连载追踪 / 做种 H&R / 通知推送 / 5 新页 / 总览升级 | ✅ 已验证（575 测试） |
| v0.6 | 站点中心 / 下载器中心（多 qb 实例、账密优先）/ 订阅引擎（RSS 捕获 + 主动搜索 + 认证巡检）/ DB v5 / 共享 bootstrap + registry_store / web.port 接线与死配置清理 | ✅ 已收尾（1004 测试） |
| v0.7 | 发现墙 `/discover`：各站 RSS 聚合卡片墙（跨站合并去重）+ 一键订阅 + iTunes 封面代理缓存 + 进程内 TTL/每站刷新下限（分支 feat/discover-wall） | ✅ 已收尾（1044 测试） |
| v0.7.1 | 发现墙空墙修复：`fetch_rss(kind=)` 分类预过滤（憨憨 cat410 复测生效、移除黑名单）、标题判别强视频信号优先（ALAC/16bit WEB-DL 音乐不再误判）、分类发现覆盖 sp_cat 分区 + 图标名回退 | ✅ 已收尾（1052 测试） |
| v0.7.2 | 四站实跑校准：馒头只发 x-api-key（Authorization 投毒被拒）+ 空关键词搜索 + 真实响应字段（字符串分类 ID/status 嵌套/imageList）；发现墙跨 tab 共享拉取避开 audiences RSS 2 分钟限速；限速文案识别；rsskey；繁体关键词 | ✅ 已收尾（1065 测试） |
| v0.7.3 | 馒头 `search(kind="all")` 改为音乐 + 有声书两路并发搜索合并去重——不带 mode 的全站最新几乎全是影视，有声书墙长期空墙 | ✅ 已收尾（1066 测试） |
| v0.7.4 | 发现墙跨站搜索框（`search_wall`：逐站 search 并发 + 失败隔离 + 跨站合卡）；限速站与失败站分列（audiences 限速不再误标「失败」）；徽章悬停说明 | ✅ 已收尾（1073 测试） |
| v0.7.5 | 静态资源带 `?v=` 版本号防升级后吃旧缓存；GET 表单不再注入 csrf_token（防泄进 URL/日志） | ✅ 已收尾（1074 测试） |
| **下一步** | **对接真实站点 + qb 实跑（§三 阶段 B〜E）、NAS Docker 部署验证（阶段 F）** | ❌ **未开始 ← 你在这里** |

冒烟检查（`scripts/smoke_check.py`，v3）：未登录守卫、首次初始化、**13 页面**渲染（6 旧 + 5 新 + /downloaders + /discover）、
静态资源可达（按页面实际引用提取，非写死）、CSRF 令牌下发、写操作三重防护、登出拦截、版本一致性（`/health` == `__version__` == 0.7.5，brand 显示 v0.7.5）。

**README.md 已在 0.5.0 全文重写、0.6.0 再次对齐**，与现状一致；§六「未验证项」部分仍由本文档保留。

**代码仓库**：2026-10-13 已推送 https://github.com/OneFlatWhite/audio-hub.git（分支 main）。
⚠️ 推送时的提交说明误写为「v0.3 / 274 测试」（推送者信息滞后），**实际代码为 v0.5.0**；
已补打附注标签 `v0.5.0` 指向同一提交，未改写已推送的 main 历史。
推送前已核实以下内容**未**入库：密钥文件、真实 `config.yaml`、会话 cookie（`cj.txt`，内含会话令牌）、
两个虚拟环境（`.venv/` 与 `.venv-pure/`，合计约 280 MB）、工具缓存、生成日志。
五张界面截图（`01_*.png`～`05_*.png`）留在磁盘但**未提交**——它们是旧版 UI，
若要入 README 应先重截新图，不要直接提交旧图。

---

## 三、需要达到的状态（验收标准）

按顺序勾选，**每一阶段绿了再进下一阶段**。

### 阶段 A：修掉阻塞缺陷（必须最先做）—— **✅ 0.4.0 已完成**

- [x] A1. `serve_dev.sh` 能真正生成 `config/secrets.key`（见 §四 缺陷 1，已修）
- [x] A2. 设置页填 qb API Key → 保存 → 不报错，刷新后仍显示「已配置」（密钥缺失路径已被测试覆盖）
- [x] A3. 全部测试仍绿（575 passed）

**A 不完成，后面全做不了**——凭据存不进去，qb 就连不上。

### 阶段 B：本机连通 NAS 的 qb

- [ ] B1. `scripts/check_qb.py` 七步全过（TCP 可达 / HTTP 响应 / API Key 认证 / 路径参照）
- [ ] B2. `save_path` 填的是 **qb 侧（NAS）路径**，不是 WSL 路径
      —— 脚本第 ⑥ 步会打印 qb 默认保存路径与现有种子路径作为参照
- [ ] B3. 浏览器打开服务首页（端口见配置 `web.port`），「下载器中心」（/downloaders）里
      该实例的 host/port/凭据状态与你填的一致（v0.6 起下载器配置以 downloaders 表为权威源）

### 阶段 C：真实站点解析验证（**高风险，别跳过**）

⚠️ **四个站点的解析器从未接触过真实响应**。`tests/fixtures/` 全是按规范推断的合成样本。
详见 §六。

- [ ] C1. `scripts/verify_adapters.py` 跑通，四个站各自返回条数 > 0
- [ ] C2. 专项验证 1：憨憨 `cat410=1` 是否生效（已知可能不生效）
- [ ] C3. 专项验证 2：NexusPHP 站 RSS `search` 参数是否被忽略
- [ ] C4. 专项验证 3：馒头双域名（`api.m-team.cc` / `api.m-team.io`）哪个可用
- [ ] C5. 用真实样本（`--save-samples`）替换合成 fixture，重跑测试校准解析器
- [x] C6. **馒头的 `genDlToken` 二次请求**（`downloadUrl` 为空时换临时令牌）——
      **0.4.0 已实现**：`MTeamAdapter.resolve_download_url` + `Dispatcher.resolve_download_url`，
      待审采纳路径同样走它。仍需真实站点验证响应字段名

### 阶段 D：dry-run 全链路

- [ ] D1. 添加 1～2 个订阅项（`cli add` 或界面「订阅清单」）
- [ ] D2. `run-once` 在 **dry_run=true** 下跑通：候选数 > 0、无误抓 VIDEO、去重生效
- [ ] D3. 日志 `round_summary` 各站错误为空
- [ ] D4. 待审队列有合理条目，界面可采纳/拒绝

### 阶段 E：实跑（用户最终要的）

- [ ] E1. 设置页把 `dry_run` 改「实跑」→ 保存 → **重启服务**（这是 restart 级覆盖项）
- [ ] E2. 采纳一个待审项 → qb 里出现该种子 → 下载启动
- [ ] E3. 种子落到 `seeds/`（NAS 路径），**原种只读做种**，PT 哈希未被改写
- [ ] E4. 归档链路：`seeds/` → `cp -a` → `scratch/` → 校验 → 归档决策 → `library/` → MTW webhook → Navidrome 可见（代码已全部就位，待实机验收）
- [ ] E5. 有声书路径落到 `/volume1/audiolab/library` → ABS scan API → Audiobookshelf 可见

首次实跑**只放 1～2 个订阅项、`max_rows` 调小**，观察一轮再放开。PT 站对自动化请求敏感，
触发风控的后果落在账号上。

### 阶段 F：NAS 部署（用户说「测通了后...再说」，非当前优先级）

- [x] F1. 修 Dockerfile 构建 bug（见 §四 缺陷 2，0.4.0 已修；但尚未实机 `docker build`）
- [ ] F2. `docker compose up -d` 在 NAS 起得来
- [ ] F3. host 网络下 `localhost:8085` 能访问 qb1
- [ ] F4. 路径与 qb1 完全一致（`/volume1/musiclab:/musiclab`）
- [ ] F5. **qb1 容器不得映射 CloudFS/CD2 挂载路径**（FUSE 不支持 BT 随机写，网盘做种属 PT 站禁止行为）

---

## 四、三个已知缺陷（**均已在 0.4.0 / 0.5.0 修复**，保留原文供回溯）

| 缺陷 | 状态 | 修复方式 |
|---|---|---|
| 1. `secrets.key` 静默生成失败 | ✅ 已修复（0.4.0） | `crypto.ensure_key_file` 签名改为 `str \| Path \| None`；`serve_dev.sh` 改为 `ensure_key_file(Path("$KEY")) \|\| die`；补「密钥缺失时保存凭据」测试 |
| 2. Dockerfile 构建必然失败 | ✅ 已修复（0.4.0） | `COPY pyproject.toml README.md ./` → `COPY src/ ./src/` → 一次 `pip install .`；`libffi-dev` 注释已更正（是否可删待实测构建） |
| 3. README.md 全面过期 | ✅ 已修复（0.5.0） | README 全文重写，§六未验证项保留并同步到 README「已知限制」 |

### 缺陷 1：`secrets.key` 静默生成失败 ← **（原阻塞点，已修复）**

**位置**：`scripts/serve_dev.sh:107-112`

```bash
  "$PY" - <<PYEOF
import sys; sys.path.insert(0, "$DST/src")
from audio_hub.crypto import ensure_key_file
ensure_key_file("$KEY")        # ← 传的是字符串
PYEOF
```

`ensure_key_file` 签名是 `(path: Path | None = None)`，内部执行 `p.exists()`
（`src/audio_hub/crypto.py:38-39`）。**字符串没有 `.exists()` 方法 → `AttributeError`**。

而这段 heredoc 既没有 `|| die`，`serve_dev.sh:15` 又是 `set -uo pipefail`（**没有 `-e`**），
所以异常被静默吞掉，脚本继续往下走、服务照常启动。

**后果链**：密钥文件不存在 → 设置页保存凭据时 `encrypt_value` → `load_fernet` →
`FileNotFoundError`（`crypto.py:54-58`）→ **保存失败**。用户无法配置任何凭据。

**为什么测试没抓到**：所有测试都用 fixture 现场生成密钥；dev 配置里 `_encrypted`
字段全是空串，而 `config.py` 的 `_decrypt_sensitive` 遇到空值会跳过解密。
所以「密钥缺失」这条路径**从未被覆盖**。

**修法**：`ensure_key_file("$KEY")` → `ensure_key_file(Path("$KEY"))`（需 import Path），
并给 heredoc 加 `|| die "密钥生成失败"`。**建议同时补一个测试**：密钥文件不存在时
调 `/settings/qbittorrent` 保存凭据，应返回明确错误而非 500。

> 注意：`scripts/init_secrets.py:59` 传的是 `Path` 对象，**那个是对的**。
> 两处写法不一致正是这个 bug 的来源。

### 缺陷 2：Dockerfile 构建必然失败（**已修复**）

**位置**：`Dockerfile:10-14`

```dockerfile
COPY pyproject.toml ./
RUN pip install --no-cache-dir .     # ← 此时 src/ 还不存在
COPY src/ ./src/
RUN pip install --no-cache-dir .     # ← 又装一遍
```

`pyproject.toml` 用 `[tool.hatch.build.targets.wheel] packages = ["src/audio_hub"]`，
第 11 行必然失败。**而且 v0.2 修复时把版本改成了 `dynamic = ["version"]` +
`path = "src/audio_hub/__init__.py"`，现在会在解析版本元数据阶段就失败**，报错更早更难懂。
这是上一任助手引入的回归。

**修法**：删掉第一次 `pip install`，改为先 `COPY pyproject.toml src/ ./` 再装一次；
或保留分层缓存但把 `src/audio_hub/__init__.py` 单独先 COPY 过去。

**附带**：`Dockerfile:7` 装 `libffi-dev` 注释写「opencc 需要」——**注释是错的**。
cryptography 有 manylinux wheel 不需编译；opencc（OpenCC Python 绑定）也是 wheel。
这行白增镜像体积，可删。删前建议实测一次构建确认。

### 缺陷 3：README.md 全面过期（**已修复**）

写着「v0.1 状态」「175 个测试全绿」「Web 界面 v0.2 未开始，仅有 /health /status /run-once」。
实际：v0.3、274 测试、7 个页面 + 认证 + 设置覆盖层全部完成。

**危害**：接手工具读 README 会误判进度，可能重复实现已有功能，或以为界面不存在。
README §二「未验证项」那部分**仍然准确且重要**（见 §六），别一起删掉。

---

## 五、环境与运行方式（重要，容易踩）

> **2026-10-13 现状注记**：本节描述的「WSL + D 盘双副本」流程是**原始作者的 Windows 环境约束**，
> 对新接手者**不是必需的**。本仓库（含 `.git`）现已是权威副本，当前开发机为 **macOS**，
> 直接在仓库内建 venv 即可：`python3 -m venv .venv && .venv/bin/pip install -e .`
> （当前 `.venv` 为 Python 3.15.0）。下方 WSL 两小节保留供回溯原始环境约束。

### 必须在 WSL 里跑，不能在 Windows Python 里跑（原始作者环境，见上方注记）

本机 Windows 开了 **Smart App Control（SAC）强制模式**，会拦截未签名的 `.pyd` 原生扩展。
`D:\soft\Python310` 存在，但 pydantic / cryptography / argon2 都依赖 `.pyd`，**导入即失败**。
不要尝试降级 SAC 或换 Python——用户已明确拒绝动安全设置。

Linux 的 `.so` 不受 SAC 约束，所以测试与服务都在 WSL Ubuntu 26.04.1 里跑，Python 3.14.4。

### 两份代码，需要同步

| 位置 | 角色 |
|---|---|
| `D:\Documents\audio-hub` | **权威副本**（上一任助手只能改这里） |
| `~/audio-hub`（WSL） | **可运行副本**（有 `.venv`，测试与服务都在这跑） |

同步命令（`serve_dev.sh:35` 与所有补丁脚本都用这个）：

```bash
cd /mnt/d/Documents/audio-hub && tar cf - --exclude=__pycache__ --exclude=.pytest_cache \
  src tests pyproject.toml scripts | (cd ~/audio-hub && tar xf -)
```

**改完 D 盘必须同步到 WSL 才生效**，否则你改的东西跑不到。

### 常用命令

当前开发机（macOS，仓库根目录即本目录）：

```bash
# 跑测试 + lint
.venv/bin/python -m pytest -q && .venv/bin/ruff check src scripts tests

# 起服务（前台，Ctrl+C 停）：先准备一份 config.yaml（可复制 config/config.example.yaml），然后
AUDIO_HUB_CONFIG=/path/to/config.yaml AUDIO_HUB_SECRETS_KEY=/path/to/secrets.key \
  .venv/bin/python -m audio_hub
# 监听端口由配置 web.port 决定（example 为 8090），浏览器打开 http://localhost:<port>，首次跳 /setup 建管理员

# qb 连通性诊断（只读，不改配置不动种子）
AUDIO_HUB_CONFIG=/path/to/config.yaml .venv/bin/python scripts/check_qb.py

# 冒烟检查（自拉起临时实例，不依赖外部配置）
.venv/bin/python scripts/smoke_check.py

# 真实站点验证（阶段 C，会发真实请求，注意配额）
.venv/bin/python scripts/verify_adapters.py --save-samples
```

原始作者环境（WSL）的命令供回溯：

```bash
# 跑测试 + lint
cd ~/audio-hub && source .venv/bin/activate && pytest -q && ruff check src tests

# 起服务（前台，Ctrl+C 停；脚本自身会先同步 D 盘代码）
bash /mnt/d/Documents/audio-hub/scripts/serve_dev.sh
# → Windows 浏览器打开 http://localhost:8000，首次跳 /setup 建管理员

# qb 连通性诊断（只读，不改配置不动种子）
cd ~/audio-hub && .venv/bin/python /mnt/d/Documents/audio-hub/scripts/check_qb.py

# 冒烟检查（结果落盘 smoke_report.txt）
cd ~/audio-hub && .venv/bin/python /mnt/d/Documents/audio-hub/scripts/smoke_check.py

# 真实站点验证（阶段 C，会发真实请求，注意配额）
cd ~/audio-hub && .venv/bin/python scripts/verify_adapters.py --save-samples
```

### dev 配置的刻意保守默认值

`serve_dev.sh` 生成的 `~/audio-hub-data/config.yaml`：`dry_run: true`、四个站全部 `enabled: false`、
`qb host: localhost`、`api_key_encrypted: ""`，并带完整的 paths / validation / scrape / seeding / notify 段。
**这是故意的**——避免未验证就消耗 PT 站配额。所以设置页显示「✗ 未配置」是如实反映，不是坏了。

---

## 六、尚未验证的高风险项（继承自旧 README §二，已按 0.5.0 现状标注）

**1〜5 仍然成立，是下一阶段的真正难点；6〜10 已在 0.5.0 实现，但未对接真实外部实例。**

1. **解析器从未接触真实站点响应。** `tests/fixtures/` 是按 NexusPHP 规范与馒头 API 文档
   推断构造的合成样本，不是真实抓包。字段名、嵌套结构、编码都可能有出入。
   → 必须先跑 `scripts/verify_adapters.py`，用真实响应替换 fixture，再修解析器。

2. **四个站点的分类 ID 全是猜测值。** `config.example.yaml` 里的 `music_cat_id`
   （憨憨 410、天空 401）需实测。**已知憨憨 `cat410=1` 不生效**，验证脚本会专项检测。

3. **馒头 API 字段名未实测。** `_parse_torrent` 对 `name`/`size`/`infoHash`/`discount`/`labels`
   做了多重兜底，但真实字段名与嵌套层级需校准。

4. **馒头 `genDlToken` 二次请求** —— ✅ 0.4.0 已实现（`adapters/mteam.py` `resolve_download_url`，
   `pipeline/dispatcher.py` 派发前调用）。但 `genDlToken` 响应的字段名仍是按文档推断，需真实验证。

5. **观众站（Audiences）资料最少。** Prowlarr/Jackett 索引器仓库里没有它的独立定义，
   几乎所有细节需实测。

6. **入库前校验 + 隔离区** —— ✅ 0.5.0 已实现（`validation/checker.py`：扩展名 / 最少文件数 / FLAC 头伪无损 / 可选 auCDtect；`/quarantine` 页释放或丢弃）。auCDtect 未配置时跳过频谱检测
7. **归档决策 ArchiveDecision（upgrade/回收区/回滚）** —— ✅ 0.5.0 已实现（`archive/decision.py` NEW/UPGRADE/SKIP，决策键 `(watch_id, title_norm)`；`/library` 页回收区还原 / 清除）
8. **刮削触发（MTW webhook / ABS scan API）** —— ✅ 0.5.0 已实现（`scrape/trigger.py`），**但未对接真实 MTW / ABS 实例**，阶段 E4/E5 需实机验收
9. **有声书连载追踪 SeriesEntry** —— ✅ 0.5.0 已实现（`series/tracker.py`：`DEFAULT_PART_PATTERN` 解析分集号、按分集去重、`series_completed` 事件；`/series` 页）
10. **做种生命周期 H&R** —— ✅ 0.5.0 已实现（`seeding/lifecycle.py` `SeedingMonitor`：时长或分享率任一满足即达标；`keep`/`pause`；**绝不删文件**；`/seeding` 页）
11. **站点健康统计与熔断** —— ✅ 0.5.0 收尾已实现（`repo.py` `# ---- site_stats ----` 区块：
    `record_site_success` / `record_site_failure` / `is_circuit_open`；`pipeline/search.py` 每站搜索前查熔断、成功 / 失败后写入，
    达阈值发 `site_circuit_open` 事件，熔断期跳过并计入 `round_summary.skipped_circuit`；阈值 `search.circuit_fail_threshold`=3 /
    `search.circuit_open_minutes`=30 可在设置页热改；总览 / 站点页显示「熔断至 HH:MM」；单测 `tests/unit/test_site_stats.py`，
    集成 `TestSiteCircuitBreaker`）。阈值默认值仍是设计约定，实跑后按站点表现调。

---

## 七、设计约定（**违反这些会引入难查的 bug**）

上一任助手在这几条上反复栽过，写下来避免重蹈。

### 1. 配置改动存 DB 覆盖层，**绝不回写 `config.yaml`**

`config.yaml` 是部署产物：人手维护、含注释、随容器卷挂载。回写它有三个代价：
① PyYAML round-trip 吃掉注释（那些注释里存着「cat410 需实测确认」这类实测结论，丢了就是知识流失）
② 写入中断会损坏配置，下次直接起不来
③ 与人工编辑冲突。

YAML 是**基线**，覆盖层（`app_settings` 表）优先级更高，启动时 `hydrate_config` 合并。
见 `src/audio_hub/settings_store.py` 模块 docstring。

**例外**：`db_path` 刻意排除在覆盖层外——否则「改数据库路径」会把覆盖层自己搬走。

### 2. 每个设置项显式声明 `hot`（热生效）或 `restart`（需重启）

**不能对所有项谎报「保存成功」**。`dry_run` 在 `Dispatcher` / `CompletionMonitor` 构造时固化
（`scheduler.py`），qb 客户端启动时创建，适配器在 `PipelineRunner.__init__` 构建。
若界面显示已保存、运行时仍用旧值 → **用户以为空跑保护还开着，实际已在真实下载**。

需重启的项保存后要写 `__restart_pending` 标记，页面顶部挂橙色横幅明说「运行时仍在用旧值」；
**该标记必须在下次启动时清除**，否则横幅永久挂着、提示变噪音被无视。

### 3. bool 字段用 select，**不能用 checkbox**

取消勾选时浏览器根本不发送该字段，而覆盖层语义是「未提交＝不修改」→
取消勾选会被静默忽略（用户点了取消、页面提示保存成功、开关实际没动）。
select 永远提交一个值。

**推论**：表单漏带 `dry_run` 字段绝不能当成 `False`，否则任何缺字段的请求都会静默关掉空跑保护。
测试数据也必须用单值 `"0"` / `"1"`，**不能写成 `["0","1"]`**（列表尾元素语义会咬人，
上一任助手在这里栽过一次）。

### 4. dict 键名避开内置方法名

`credential_status` 曾返回 `{"items": [...]}`，模板里 `s.items` 被 Jinja2 解析成
**dict 的 `.items()` 方法**而非列表 → 运行时 `TypeError` → 一个 bug 打挂 35 个测试
（因为 `_csrf()` 辅助函数要 GET `/settings`，页面一渲染就崩）。已改名为 `creds`。

**同类风险**：`keys`、`values`、`get`、`update`、`pop`、`copy` 都不要当模板变量的键名。

### 5. 模板校验必须 **render**，不能只 `parse`

`.parse()` 只查语法。`s.items` 撞名、for-else 逻辑写反（`{% else %}` 挂在 for 上，
列表非空时永不执行「凭据齐全」分支）——**这两类都是语法合法、渲染才炸**。

已删除的 `scripts/fix_all_v7.py` 里的 render 校验是正确做法：用假数据真渲染，
并断言「缺凭据」「凭据齐全」「熔断」三个分支都出现在输出里。
**任何改模板的改动都该带这一步**（现在由 `tests/integration/` 的路由测试 + `smoke_check.py` 12 页渲染承担）。

### 6. 多行字符串字面量做锚点替换时，续行会继承脚本自身缩进（历史教训，补丁脚本已删）

当时的 `fix_all_v6.py` D 补丁因此把 8 空格缩进的测试代码替换成 12 空格 → `unexpected indent`。
**修法**：锚点替换优先用单行进单行出；必须多行时，把 OLD/NEW 放模块级常量、
按目标文件的绝对缩进书写，不随脚本嵌套层级缩进。

### 7. 渲染分组与保存端点必须用同一个字段前缀集

`build_settings_groups` 曾用 `SITE_PREFIX`（只匹配 `site_enabled:`）过滤，
而保存端点 `_specs_for` 用 `SITE_ALL_PREFIXES`（含 `site_rss:` / `site_api_key:`）
→ **凭据能保存但界面不渲染输入框**，用户无从填写。已统一为 `SITE_ALL_PREFIXES`。

改任何一处都要检查另一处。

### 8. 凭据只写不读

Fernet 加密入库，界面**永不回显**明文，只显示「已配置（N 字符）」。
`load_fernet` 每次调用时读文件，所以**新建密钥文件不用重启服务**。
密钥文件丢失 = 所有加密凭据永久不可恢复（`hydrate_config` 会记错误日志并回退到 YAML 值，
表现为「我明明填过 API Key 怎么又没了」）——提醒用户单独备份，但不要和 `config.yaml` 放同一个备份包。

### 9. 站点凭据 = **完整 RSS 地址**，不是裸 passkey

NexusPHP 系的 passkey **内联在 RSS URL 里**（`adapters/nexusphp.py` 的 `build_rss_url`
直接以 `rss_url` 为基址）。而 `SiteConfig`（`config.py`）**没有 `passkey` 字段**——
只有 `name/enabled/rss_url/search_url/api_key/music_cat_id/audiobook_cat_id/max_rows/title_bracket_style`。
Pydantic 默认忽略多余字段，所以**填裸 passkey 会被静默丢弃**。

馒头（mteam）不同：认证用存取令牌，走 `api_key` + `Authorization` header。

### 10. 前端是原生 fetch + 服务端渲染，**不是 HTMX**

`routes_pages.py:3-4` 明确注释「不使用 HTMX」。
⚠️ 但 PRD（见 §七.11 路径）v0.3 写明前端用 HTMX。这是实现与 PRD 的真实偏差，
是否对齐属待确认项，不要擅自改。

### 11. PRD 不在仓库内，但在磁盘上（仓库外）

路径：`D:\Documents\qwen-agent\lqv6wiolgv\default\音频内容获取中枢-需求文档.md`（v0.4 草案，约 1276 行）。
它在项目目录之外，所以没进 git；接手工具可直接读该路径。
此前「PRD 磁盘上不存在」的断言是错的——它只是不在仓库里。

---

## 八、脚本清单

### 可长期使用

| 脚本 | 用途 |
|---|---|
| `scripts/serve_dev.sh` | 同步 D 盘 → 生成 dev 配置与密钥 → 起服务（缺陷 1 已修） |
| `scripts/check_qb.py` | qb 连通性七步诊断，只读 |
| `scripts/smoke_check.py` | 端到端冒烟 v3（12 页 + 版本号校验），结果落盘 `smoke_report.txt` |
| `scripts/verify_adapters.py` | **阶段 C 关键**：真实站点验证 + 三项专项检测（`--site` / `--save-samples`） |
| `scripts/init_secrets.py` | 交互式加密凭据（收集完整 RSS URL，输出正确嵌套 YAML） |

### 一次性补丁脚本（**已于 0.5.0 全部删除**）

这些是上上一任助手因无法直接改文件而写的锚点补丁，改动已全部落盘，脚本本身已删：
`apply_settings_editable.py` / `apply_settings_v2.py` / `apply_settings_v3.py` / `apply_site_credentials.py` /
`apply_v02_fixes.py` / `fix_all_v4.py`〜`fix_all_v7.py` / `fix_bool_encoding.py` / `fix_check_qb.py` /
`fix_render_sites.py` / `fix_smoke_check.py` / `fix_and_test.sh`。
它们在 §七.5 / §七.6 里留下的教训仍然有效。

### 其他残留

- `.probe_write_test.txt`、`last_test_output.txt`（项目根）—— ✅ 已删
- `D:\Documents\qwen-agent\lqv6wiolgv\default\write_probe.txt`（仓库外，本轮未动）

---

## 九、建议的执行顺序（已按 0.6.0 现状更新）

```
✓ 1〜2. 缺陷 1 + 密钥缺失测试                   ← 0.4.0 已完成
✓ 5.    缺陷 2（Dockerfile）                      ← 0.4.0 已完成（未实机构建）
✓ 6.    README 重写                              ← 0.5.0 已完成
✓ 8.    馒头 genDlToken                          ← 0.4.0 已实现（字段名待实测）

→ 3.  跑 serve_dev.sh，界面填 qb host/port/API Key，验证阶段 B（A2 已有测试覆盖，这里是真机验证）
→ 4.  跑 check_qb.py，过阶段 B
→ 7.  跑 verify_adapters.py --save-samples，进入阶段 C   ← 真正的工作量在这里
→ 9.  用真实样本替换 fixture，重跑测试校准解析器；校准 music_cat_id（憨憨 410 / 天空 401 / 观众未知）
→ 9b. ✅ 已完成：site_stats 写入与熔断（§六 第 11 条）
→ 10. 阶段 D（dry-run 全链路）→ 阶段 E（实跑 + 归档 / 刮削 / H&R 实机验收）
→ 11. 阶段 F：NAS 上 docker build + compose up，确认 libffi-dev 是否可删
```

**阶段 C 是整个项目最大的未知**。前面 A/B 都是修 bug，C 是「代码第一次接触现实」——
四个站的真实响应结构、分类 ID、馒头的令牌流程，全都没验证过。
建议给 C 留足时间，别急着承诺 E 的完成时间。

---

## 十、安全红线（不要越）

- **PT 站 RSS 链接内嵌 passkey，等同账号密码**：不要贴进日志/提交版本库/发聊天记录。
  代码里已有脱敏（`_mask_passkey`、日志输出前对 `passkey=` / `api_key` 脱敏），保持住。
- **每站搜索频率由轮询间隔控制**，下限 5 分钟（`scheduler.py` 强制，低于自动提升并告警）；
  连续 3 次失败熔断该站 30 分钟（`search.circuit_fail_threshold` / `search.circuit_open_minutes`，已实现，见 §六 第 11 条）。
  **PT 站对高频自动化请求敏感，触发风控的后果落在账号上，不是程序上。**
- **默认 `dry_run: true`**，切实跑必须用户明确要求。
- **host 网络下无法只绑内网 IP，安全边界在路由器端口映射表**：确认 8090（本服务）、
  8085（qb）、19798（CD2）、8002（MTW）、4533（Navidrome）、13378（ABS）均未映射到公网。
  外网访问走 Tailscale。注意 qb 开了 UPnP 可能自行添加映射，配置后回路由器核实。
- **qb1 容器不得映射 CloudFS/CD2 挂载路径**：FUSE 不支持 BT 随机写，且网盘做种属 PT 站禁止行为。
- **归档用 `cp -a` 不用 `mv`**：原种必须保留只读做种，保护 PT 哈希。已有测试断言源文件仍存在。
- **做种 / H&R 链路绝不删文件、不移除种子**：`on_satisfied: pause` 只是暂停，没有任何代码路径调 qb delete。

---

## 十一、0.4.0 / 0.5.0 新增模块与接口要点（接手必读）

### 配置与设置

- `config.py` 新增子模型 `PathsConfig` / `ValidationConfig` / `ScrapeConfig` / `SeedingConfig` / `NotifyConfig`，
  `SiteConfig` 新增 `hr_min_seed_hours` / `hr_min_ratio`（可空，沿用 `seeding.default_*`）。
- `settings_store.py` 新增五个分组 paths / validation / scrape / seeding / notify，保存端点 `/settings/{组名}`。
  scope：`paths.*` 全部 restart；`seeding.check_interval_minutes` restart；其余新增项全部 hot。
  `notify.*` 保存后调 `rebuild_push_sinks` 热重建；`/settings/notify/test` 测试发送。
- `config.example.yaml` 与 `config.py` 字段已在 0.5.0 逐条对齐，每段带生效方式注释。

### 数据库（`db.py` v4）

七张新表 `library_items` / `recycle_entries` / `quarantine_items` / `series` / `series_parts` / `seed_records` / `events`；
`tasks` 补 `series_id` / `part_no` / `validation_result` / `archive_decision`，`watchlist` 补 `series_id`。
全部 `IF NOT EXISTS` + `PRAGMA table_info` 补列，`init_db` 幂等。**schema 唯一真源在 `db.py`，二波模块只写不建。**

### 通知（`notify/`）

- `Event(kind, site, title, detail, level, at)`；`Notifier` 协议 `async emit(event)`。
- `MultiNotifier` = 基础 sink（`LogNotifier` + `DbNotifier` 落 `events` 表）+ 推送 sink（`WecomNotifier` / `TelegramNotifier` / `GenericWebhookNotifier`，按 `notify.kinds` 过滤）。
- 7 种事件：dispatched / completed / routed / quarantined / hr_satisfied / series_completed / failed（另有 `archive_skipped` / `scrape_failed` 等内部事件走基础 sink）。
- **装配要点**：`PipelineRunner.__init__` 必须把 `notifier=self.notifier` 传给 `CompletionMonitor`，否则归档链路事件不进 `events` 表。
  0.5.0 收尾时补上了这一点，并有 `test_monitor_shares_runner_notifier` 断言。

### 归档链路（`completion/monitor.py` → `archive/router.py`）

`poll_once` → 发现 qb 完成 → `process_completed_task(db, cfg, task, content_path, notifier)`：
`copy_to_scratch`（原种不动）→ `validate_dir` → 不通过则 `quarantine_items(held)` + 移入 `quarantine_dir`；
通过则 `decide()` → `NEW` 直接 `copy_tree` 到 library / `UPGRADE` 先 `_recycle_old_version` 再复制 / `SKIP` 丢弃 scratch 副本；
最后 `trigger_for_kind`（音乐 POST MTW webhook `{path, kind, title}`；有声书 POST `{abs_url}/api/libraries/{id}/scan` Bearer token）。
校验短码：`no_audio_files` / `too_few_audio_files:<n><<min>` / `suspect_lossless:<file>` / `aucdtect_mpeg:<file>`。
质量分复用 `pipeline/matcher` 权重表（dsd 1.00 / flac_24 0.95 / flac_16 0.85 / ape、wav 0.75 / alac 0.70 / mp3 0.40 / m4b 0.95 / audiobook 0.85）× 100。

### 连载（`series/tracker.py`）

`DEFAULT_PART_PATTERN = r"第\s*(\d+)\s*[集部卷章回期]|\bPart\s*(\d+)|\bEP?\s*(\d{1,3})\b|\((\d{1,3})\)"`，每个连载可自定义。
`register_task` 按 `(series_id, part_no)` 唯一去重；`refresh_status` 在 `last_part >= total_parts` 时置 `completed` 并发 `series_completed`。

### 做种（`seeding/lifecycle.py`）

`SeedingMonitor.poll_once` 从 qb 读做种时长 / 分享率写 `seed_records`；`is_hr_satisfied` = 时长或分享率任一达标；
达标后按 `on_satisfied` keep/pause，发 `hr_satisfied`。调度任务 `seeding_check` 间隔下限 1 分钟。
`/seeding` 提供 pause / resume / mark-satisfied / check-now。

### 馒头（`adapters/mteam.py`）

`resolve_download_url(meta)`：`download_url` 为空时 POST `genDlToken` 换临时链接；`Dispatcher.resolve_download_url` 在派发前统一调用，
待审采纳路径也走它（早期采纳只改状态不换令牌，馒头必失败）。

### 页面与版本

新增 `/library`（`/library/recycle/{id}/restore|purge`）、`/quarantine`（`/{id}/release|discard`）、`/series`（`/{id}` / `edit` / `delete`）、
`/seeding`（`/{task_id}/pause|resume|mark-satisfied`、`/check-now`）、`/events`（`/events/clear`）。
总览新增隔离待处理 / H&R 未达标 / 活跃连载三卡 + 最近事件。
版本号唯一真源 `src/audio_hub/__init__.py`：`pyproject` dynamic 读它，`/health` 读它，`base.html` brand 通过 Jinja 全局 `version` 读它（0.2 时写死过一次导致漂移）。
