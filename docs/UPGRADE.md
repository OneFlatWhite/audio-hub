# 升级指南（必读：怎么安全地升级、怎么回滚）

audio-hub 的设计原则：**程序绝不自动升级、绝不自动动你的容器和环境**。
页面上出现的「发现新版本」横幅只是提示；是否升级、何时升级，永远由你决定。

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

```bash
cd /volume1/docker/audio-hub
docker pull ghcr.io/oneflatwhite/audio-hub:0.14.1   # 1. 拉指定版本（推荐锁版本）
# 2. 改 compose 里的 image tag 到同一版本
docker compose up -d                                 # 3. 重建容器（数据都在卷里，无损）
docker logs --tail 50 audio-hub                      # 4. 看启动日志确认无迁移报错
```

- 数据库结构变化在启动时**自动迁移**（新增列自动补、旧数据自动回填），
  启动日志会打印 `schema 升级，新增列: [...]`——这是正常现象，不是报错；
- 配置新增键全部带默认值，旧 config.yaml 不改也能跑；新增键会写进
  `config.example.yaml` 并在 CHANGELOG 标注，按需补配；
- 网页打开 `/health` 确认 `version` 已是新版本。

## 三、回滚

镜像和数据卷是分离的——**回滚 = 把 image tag 换回旧版本重建容器**：

```bash
# compose 里 image 改回上一版本，例如 0.14.1
docker compose up -d
```

注意：新版本启动后数据库做过结构升级时，直接回滚旧镜像可能因缺列报错。
所以回滚前也请先停容器、把第一步的 `config.bak-*` 备份整目录换回去，
再起旧版本——这是唯一保证干净回滚的姿势。

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
