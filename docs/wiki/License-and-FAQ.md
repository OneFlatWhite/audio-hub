# 授权（star 换码）与 FAQ

## 授权怎么拿（免费，三步）

1. 给 [OneFlatWhite/audio-hub](https://github.com/OneFlatWhite/audio-hub) 点一个 **Star**；
2. 打开发放页（部署者配置的地址），GitHub 登录——自动校验 Star，秒发授权码；
3. 打开 audio-hub 的 `/activate`，粘贴 `AH1.` 开头的授权码，激活。

## 授权会过期 / 被吊销吗

- 取消 Star：授权码会被**自动吊销**（发放端每日比对仓库 Star 列表，
  程序端启动 + 每 24 小时在线复核一次）；
- 不会突然锁死：程序端有 **30 天离线宽限**，断网也能正常用；
  被吊销后打开任意页面会进入激活页，重新点 Star 领新码粘贴即恢复；
- 程序运行本身**不联网上传任何数据**，复核只是拿自己的授权 ID 去发放端
  问一句「是否被吊销」。

部署侧环境变量（可选）：

| 变量 | 含义 |
| --- | --- |
| `AUDIO_HUB_LICENSE_PUBLIC_KEY` | 验证公钥；不配 = 不校验 |
| `AUDIO_HUB_LICENSE_REQUIRED` | `1` = 无有效授权码只放行激活/登录页 |
| `AUDIO_HUB_LICENSE_VERIFY_URL` | 发放端复核地址（开启自动吊销的关键） |
| `AUDIO_HUB_LICENSE_GRACE_DAYS` | 离线宽限天数（默认 30） |

自建发放服务：[docs/license-issuer/worker.js](https://github.com/OneFlatWhite/audio-hub/blob/main/docs/license-issuer/worker.js)
（Cloudflare Worker，含每日吊销核对）。

## FAQ

**Q：下载器能用什么？能用 Motrix / aria2 吗？**
qBittorrent 与 Transmission 任选、可混用。Motrix/aria2 不在 PT 站客户端
白名单里，伪装接入有封号风险，不支持。

**Q：支持哪些站点？能加自己的站吗？**
内置憨憨 / 天空 / 观众 / 馒头。任意 NexusPHP 架构的站：新建站点时
直接输入自定义标识 + 域名，凭据与认证走同一套向导。

**Q：命中记录里「已派发」但进度条不动？**
在下载器队列里排队（qb 默认同时下载数 3）。去 qb 设置调大，或换
Transmission 接管音频下载。

**Q：发现墙上有拼音歌手名？**
把对应中文名加进订阅或别名即可自动还原（映射来自你自己的订阅库）。

**Q：为什么待审队列一直是空的？**
high 置信 + 自动下载开启的订阅直接派发，不经过待审。空 = 正常。

**Q：升级会弄坏我的配置吗？**
程序绝不自动升级。手动升级前备份 `/config`，按
[Deployment-and-Upgrade](Deployment-and-Upgrade.md) 的铁律走即可；
数据库结构启动时自动迁移，旧配置键全部有默认值。

**Q：文件下载完了怎么没进资料库？**
先看命中记录该行的错误信息，再到「日志」页看归档错误。最常见原因：
容器内外路径没有指向同一份物理目录（下载器看得见、audio-hub 看不见）。
