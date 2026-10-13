# 部署与升级

完整细节见主仓 [docs/deploy-nas.md](https://github.com/OneFlatWhite/audio-hub/blob/main/docs/deploy-nas.md)
（群晖 compose 全文）与 [docs/UPGRADE.md](https://github.com/OneFlatWhite/audio-hub/blob/main/docs/UPGRADE.md)。
本页是速查版。

## 部署速查

```yaml
services:
  audio-hub:
    image: ghcr.io/oneflatwhite/audio-hub:latest   # 生产建议锁版本号
    container_name: audio-hub
    restart: unless-stopped
    ports: ["8090:8090"]
    environment:
      - TZ=Asia/Shanghai
      - AUDIO_HUB_SECRETS_KEY=/config/secrets.key
      # 授权（可选）：AUDIO_HUB_LICENSE_PUBLIC_KEY / AUDIO_HUB_LICENSE_REQUIRED
    volumes:
      - /volume1/docker/audio-hub/config:/config
      - /volume1/music/audio-hub:/musiclab
      - /volume1/books/audio-hub:/audiolab
```

初始化：`/setup` 建管理员 → 站点中心接站 → 下载器中心接 qb/tr →
设置页切实跑。

## 升级铁律（防「把本地环境推坏」）

1. **禁止 watchtower 类自动更新**——版本间可能有配置键 / 数据库迁移说明，
   必须人工过一遍 release notes；
2. 升级前先备份 `/config` 整目录（secrets.key 丢失 = 凭据全部作废）；
3. 拉指定版本号（别裸拉 latest 追新），compose 改 tag 后 `up -d`；
4. 数据库结构启动时自动迁移（日志会打印新增列，属正常）；
5. 回滚 = 换回旧 image tag；若新版已做过库结构升级，需连同 config 备份
   一起回退（详见 UPGRADE.md 第三节）。

程序自身只做「有新版本」提示（每日查一次 GitHub Release，页面上方横幅），
`AUDIO_HUB_NO_UPDATE_CHECK=1` 可关；**绝不自动升级、绝不动你的容器和环境**。

## 备份

- `/config` 整目录（hub.db + secrets.key + 授权码 + 日志 + 封面缓存）；
- 两份媒体库目录（音乐 / 有声书）；
- 建议每周一次，`cp -a` 即可（SQLite 单文件，热备也安全）。
