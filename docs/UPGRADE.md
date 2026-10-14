# 升级指南（必读：怎么安全地升级、怎么回滚、怎么自动升级）

audio-hub 的设计原则：**程序本体绝不自我升级、绝不自动动你的容器和环境**。
页面上出现的「发现新版本」横幅只是提示。自动升级走**宿主机侧的
`scripts/auto-update.sh`**（备份 + 健康门 + 自动回滚，见第三节），
这是唯一被支持的自动化方式；用 watchtower 裸更新属于踩坑行为。

## 一、升级前：两条铁律

1. **不要用 watchtower 之类的自动更新工具盯着本镜像。** 版本之间可能伴随
   配置键变化与数据库列迁移，静默升级等于跳过迁移说明，是「把本地环境推坏」
   的第一大来源。
2. **先备份 `/config` 整个目录**（含 hub.db、secrets.key、授权码），再动手。
   secrets.key 丢失 = 站点凭据全部作废。

```bash
cp -a /volume1/docker/audio-hub/config /volume1/docker/audio-hub/config.bak-$(date +%F)
```

## 二、标准升级流程（NAS / docker compose）

推荐 compose 把镜像版本钉在 `.env` 里（自动升级脚本改的也是它）：

```dotenv
# /volume1/docker/audio-hub/.env
AHUB_VERSION=0.15.0
```

```yaml
# docker-compose.yml 里对应写法
image: ghcr.io/oneflatwhite/audio-hub:${AHUB_VERSION:-latest}
```

```bash
cd /volume1/docker/audio-hub
docker pull ghcr.io/oneflatwhite/audio-hub:0.14.1   # 1. 拉指定版本（推荐锁版本）
# 2. 改 .env 里 AHUB_VERSION 到同一版本
docker compose up -d                                 # 3. 重建容器（数据都在卷里，无损）
docker logs --tail 50 audio-hub                      # 4. 看启动日志确认无迁移报错
```

- 数据库结构变化在启动时**自动迁移**（新增列自动补、旧数据自动回填），
  启动日志会打印 `schema 升级，新增列: [...]`——这是正常现象，不是报错；
- 配置新增键全部带默认值，旧 config.yaml 不改也能跑；新增键会写进
  `config.example.yaml` 并在 CHANGELOG 标注，按需补配；
- 网页打开 `/health` 确认 `version` 已是新版本。

## 三、回滚

镜像和数据卷是分离的——**回滚 = 把 `.env` 的 `AHUB_VERSION` 改回旧版本，
`docker compose up -d` 重建容器**（旧镜像本地还在时秒级完成）：

```bash
# .env: AHUB_VERSION=0.15.0 → 改回 0.14.1
docker compose up -d
```

注意：新版本启动后数据库做过结构升级时，直接回滚旧镜像可能因缺列报错。
所以回滚前也请先停容器、把第一步的 `config.bak-*` 备份整目录换回去，
再起旧版本——这是唯一保证干净回滚的姿势。
`auto-update.sh` 的自动回滚走的就是同一套逻辑（改回 `.env` + 重建）。

## 三·五、自动升级（auto-update.sh）

```bash
AHUB_DIR=/volume1/docker/audio-hub bash scripts/auto-update.sh
```

流程与保障：
1. 比对 GitHub 最新 release 与 `.env` 里钉住的版本，相同即退出（幂等，可放 cron）；
2. 有新版 → 先把 `/config` 完整备份到 `backups/`（默认留 5 份）；
3. 改钉新版本 → `pull` + `up -d` → **健康门**：90 秒内 `/health` 必须
   恢复 `status: ok`；
4. 健康门不过 → 自动改回旧版本重建，服务恢复后再退出（exit 1 提醒查日志）；
5. 可选 `AHUB_NOTIFY_WEBHOOK` 推送成功 / 失败通知。

已在本机 docker 上做过真实端到端验证：正常升级与「镜像损坏起不来」的
自动回滚两条路径均通过。群晖接入方式见 docs/deploy-nas.md 第二节·五。

## 四、版本节奏与通知

- 每次 `git push` 一个 `v*` 标签，GitHub Actions 自动构建镜像并发布
  （`ghcr.io/oneflatwhite/audio-hub:<版本>` 与 `:latest`）；
- 程序每日检查一次 GitHub 最新 release，有新版本时页面上方出现提示条
  （点击可看 release notes）；不想联网检测：环境变量
  `AUDIO_HUB_NO_UPDATE_CHECK=1`；
- 升级内容永远写在 [CHANGELOG.md](CHANGELOG.md) 与 release notes 里；
  涉及「需要改环境变量 / 需要动 config.yaml」的版本会在 release notes
  顶部加粗标出。

## 五、什么情况必须看文档再升级

| 情况 | 动作 |
| --- | --- |
| release notes 标注「需改环境变量」 | 先改 compose 再重建容器 |
| 跨多个版本升级（如 0.9 → 0.14） | 建议逐版本过 CHANGELOG，重点看标注项 |
| 升级后某站点不能搜索 | 站点中心重新「测试认证」（Cookie 可能过期，与升级无关但常撞在一起） |
| 授权相关报错 | 见 docs/LICENSE-ACTIVATION.md（重新 Star 领码即可） |
