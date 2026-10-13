# NAS（群晖）部署指南

audio-hub 是单容器应用，下载器（qBittorrent / Transmission）在 NAS 上另跑。
本文给出一套可直接抄的 docker-compose，以及升级、备份要点。

## 一、镜像

发布后的官方镜像（推 v 标签后 GitHub Actions 自动构建）：

```
ghcr.io/oneflatwhite/audio-hub:latest        # 跟随最新 tag
ghcr.io/oneflatwhite/audio-hub:0.13.0        # 锁版本
```

也支持自行构建：仓库根目录 `docker build -t audio-hub .`。

## 二、目录规划（NAS 上）

```text
/volume1/docker/audio-hub/config/     # 配置 + hub.db + secrets.key + logs/ + covers/
/volume1/music/audio-hub/library/     # 音乐归档终点（容器内 /musiclab/library）
/volume1/books/audio-hub/library/     # 有声书归档终点（容器内 /audiolab/library）
/volume1/download/                    # 下载器下载目录（qb / tr 共用）
```

> 归档链路是「下载器 → 复制到容器内路径」。audio-hub 容器必须把
> 上述库目录挂载进容器，且与下载器看到的**同一份物理目录**对齐
> （NAS 本机场景两者都在本机，直接映射即可）。

## 三、docker-compose（群晖 Container Manager / SSH 均可）

```yaml
services:
  audio-hub:
    image: ghcr.io/oneflatwhite/audio-hub:latest
    container_name: audio-hub
    restart: unless-stopped
    ports:
      - "8090:8090"                       # Web UI：http://NAS_IP:8090
    environment:
      - TZ=Asia/Shanghai
      - AUDIO_HUB_CONFIG=/config/config.yaml
      # 密钥文件：首次启动前生成一次并放到 config 卷里（见下）
      - AUDIO_HUB_SECRETS_KEY=/config/secrets.key
      # 授权（可选，见 docs/LICENSE-ACTIVATION.md）：
      # - AUDIO_HUB_LICENSE_PUBLIC_KEY=<作者发布的公钥>
      # - AUDIO_HUB_LICENSE_REQUIRED=1
    volumes:
      - /volume1/docker/audio-hub/config:/config
      - /volume1/music/audio-hub:/musiclab
      - /volume1/books/audio-hub:/audiolab
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

  # 下载器二选一或都跑（audio-hub 的「下载器中心」支持多实例混用）
  # qbittorrent:  image: lscr.io/linuxserver/qbittorrent:latest   端口 8085
  # transmission: image: lscr.io/linuxserver/transmission:latest  端口 9091
  # 两者都要把 /volume1/download 挂为下载目录，PT 监听端口照常映射。
```

首次启动前生成密钥文件（任一有 docker 的机器执行一次）：

```bash
docker run --rm -v /volume1/docker/audio-hub/config:/config \
  ghcr.io/oneflatwhite/audio-hub:latest \
  python -c "from audio_hub.crypto import generate_key,pathlib; \
pathlib.Path('/config/secrets.key').write_bytes(generate_key())"
chmod 600 /volume1/docker/audio-hub/config/secrets.key   # 权限收紧
```

## 四、初始化

1. 打开 `http://NAS_IP:8090` → 首次进入 `/setup` 创建管理员；
2. 站点中心：逐站填凭据 → 测试认证 → RSS 测试 → 启用
   （自定义 NexusPHP 站点：标识 + 域名即可创建）；
3. 下载器中心：添加 NAS 上的 qB / TR（类型可选），测试连接，设默认；
4. 设置页：实跑模式（默认空跑）→ 保存；建议把「完成监听轮询间隔」
   调到 5–10 分钟，下载完成更快进归档；
5. 发现墙（可选）：`config.yaml` 加 `discover.auto_refresh_minutes: 15`
   开启后台定时刷新，页面永远秒开（需重启一次生效）。

## 五、升级与备份

- 升级：拉新镜像 → 重建容器即可（数据全在 /config 卷与媒体库卷，
  SQLite 单文件，容器无状态）；
- 备份：定期备份 `config/` 整个目录（含 hub.db / secrets.key / 授权码）
  + 两份媒体库目录；`secrets.key` 丢失 = 站点凭据全部作废需重填；
- 日志：`config/logs/hub.log`（10MB×5 轮转），界面「日志」页可实时查看。

## 六、常见问题

| 现象 | 处理 |
| --- | --- |
| 登录后立刻被踢回 | 纯内网 HTTP 下保持 Cookie Secure 关闭（设置页） |
| 归档不动 | 日志页看错误；多为容器内外路径没对齐同一物理目录 |
| 发现墙首访慢 | 配置 `discover.auto_refresh_minutes` 开后台刷新（v0.13+） |
| 想换下载器 | 下载器中心新建 → 设默认；删旧实例会自动继任默认 |
