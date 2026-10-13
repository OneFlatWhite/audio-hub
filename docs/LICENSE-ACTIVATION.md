# 授权码机制（star 换码）

audio-hub 源码全量开放（见 LICENSE）；运行时需要一份**免费授权码**，
获取条件是给仓库点一个 Star。授权码是 Ed25519 离线签名的字符串，
程序内置验证公钥即可离线校验，**运行时不联网、不回传任何数据**。

## 用户侧：三步拿到授权码

1. 给 `OneFlatWhite/audio-hub` 点 Star；
2. 打开发放页（作者部署，见下），GitHub 登录 —— 页面自动校验你 Star 过，签出授权码；
3. 打开 audio-hub 的 `/activate` 页粘贴授权码，点激活。

## 吊销（取消 Star 自动失效）

- 授权码带唯一 `jti`；发放 Worker 把 `{jti → 用户}` 记进 KV；
- Worker 每日 cron 拉取仓库 **Stargazers 公开列表**（无需用户配合），
  台账里不在列表的用户自动标记吊销；
- 程序侧环境变量 `AUDIO_HUB_LICENSE_VERIFY_URL=https://<worker>/api/status`
  开启在线复核：启动时 + 每 24 小时自动核对一次；
- **离线宽限**：距上次成功核对超过 `AUDIO_HUB_LICENSE_GRACE_DAYS`（默认 30 天）
  即视为过期——NAS 断网照样能用一段时间，不会突然锁死；
- 被吊销 / 超期的部署打开任意页面会进入 `/activate`，重新点 Star 领新码即可恢复。

## 作者侧：一次性准备

```bash
# 1. 生成密钥对（私钥离线保管，绝不进仓库）
python scripts/issue_license.py genkey --out-dir ./license-keys
# 输出 AUDIO_HUB_LICENSE_PUBLIC_KEY=xxx

# 2. 部署环境注入公钥（可选开关强制门禁）
AUDIO_HUB_LICENSE_PUBLIC_KEY=xxx
AUDIO_HUB_LICENSE_REQUIRED=1     # 1 = 无有效授权码则只放行 /activate /login /setup

# 3. 手动给人签码（自动化走下面的 Worker）
python scripts/issue_license.py issue --user <github用户名> [--days 365]
```

## 自动发放：Cloudflare Worker（模板见 license-issuer/worker.js）

`docs/license-issuer/worker.js` 是一个可直接部署的 Worker：

- GitHub OAuth 登录（建一个 GitHub OAuth App，回调指向 Worker）；
- 用用户 token 调 `GET /user/starred/OneFlatWhite/audio-hub` 校验 Star；
- 已 Star → 用 Ed25519 私钥签出授权码返回给用户（私钥经 Worker Secret 注入）。

需要配置的 Secret / 变量：`GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、
`LICENSE_PRIVATE_KEY`（PKCS8 PEM）、`REPO`（默认 OneFlatWhite/audio-hub）。

## 程序侧约定

| 环境变量 | 含义 |
| --- | --- |
| `AUDIO_HUB_LICENSE_PUBLIC_KEY` | Ed25519 公钥（32B raw 的 base64）；未配置 = 不校验 |
| `AUDIO_HUB_LICENSE_REQUIRED` | `1` 时无有效授权码只放行激活/登录/初始化/静态/健康页 |

授权码保存在 `app_settings.license_key`；`/activate` 页随时可换 / 清除。
