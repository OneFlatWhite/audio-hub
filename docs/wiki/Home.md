# audio-hub 使用手册（Home）

> 音乐 + 有声书统一获取中枢：**订阅 → 搜索 → 筛选 → 下载 → 校验 → 归档 → 刮削 → 做种**，
> 全链路自动化，面向家庭 NAS 的单用户工具。源码全量开放（[LICENSE](https://github.com/OneFlatWhite/audio-hub/blob/main/LICENSE)）。

## 五分钟上手

1. **部署**：NAS 上一份 docker-compose 跑起来 → [部署指南](Deployment-and-Upgrade.md)
2. **初始化**：打开 `http://NAS_IP:8090` → `/setup` 创建管理员
3. **接站点**：站点中心逐站「填凭据 → 测试认证 → 启用」（支持任意 NexusPHP 站）
4. **接下载器**：下载器中心添加 NAS 上的 qBittorrent / Transmission（可混用，测试连接后设默认）
5. **去订阅**：订阅项页新建（歌手 / 专辑 / 有声书 / 关键词），保存即自动搜索
6. 之后交给程序：命中 → 派发下载 → 校验 → 归档入库 → 通知刮削 → 追做种

## 页面导览

| 页面 | 干什么 | 什么时候去看 |
| --- | --- | --- |
| 总览 | 订阅 / 下载 / 系统三区健康度一眼读完 | 日常入口 |
| 发现 | 各 PT 站最新上架墙，点卡片即订阅 | 想找新东西 |
| 订阅项 | 你追的歌手 / 专辑 / 书；命中统计与操作 | 管理追更 |
| 命中记录 | 每一条候选的完整流转（含实时下载进度条） | 排查「为什么没下」 |
| 待审队列 | 低置信 / 关闭自动下载的候选等你裁决 | 有徽标数字时 |
| 资料库 | 校验通过、归档入库的成果 | 看收获了什么 |
| 隔离区 | 校验不通过的内容等人工「放行 / 丢弃」 | 有徽标数字时 |
| 连载 | 有声书按分集追踪，完结提醒 | 追长篇有声书 |
| 做种 | H&R 时长 / 分享率进度，达标前绝不删种 | 保号 |
| 事件 | 全部流水线事件留痕（可推企微 / TG） | 复盘 |
| 站点 / 下载器 | 连接、凭据、状态、启停 | 出问题时 |
| 日志 | 实时日志 tail（INFO/WARNING/ERROR 筛选） | 排障 |
| 设置 | 运行参数（绝大多数热生效） | 调优 |

## 目录

- [部署与升级](Deployment-and-Upgrade.md)（含安全升级 / 回滚铁律）
- [核心概念与日常使用](Usage.md)（订阅怎么写、流转状态含义、自动化的边界）
- [授权（star 换码）与 FAQ](License-and-FAQ.md)
- 仓库内深度文档：[部署指南](https://github.com/OneFlatWhite/audio-hub/blob/main/docs/deploy-nas.md) ·
  [升级指南](https://github.com/OneFlatWhite/audio-hub/blob/main/docs/UPGRADE.md) ·
  [授权机制](https://github.com/OneFlatWhite/audio-hub/blob/main/docs/LICENSE-ACTIVATION.md)
