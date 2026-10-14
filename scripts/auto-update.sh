#!/usr/bin/env bash
# audio-hub 安全自动升级（NAS 计划任务 / cron 用）。
#
# 设计（与 docs/UPGRADE.md 一致）：
#   版本钉在 .env 的 AHUB_VERSION → 每次运行与 GitHub 最新 release 比对 →
#   有新版：备份 /config → 改钉新版本 → compose up -d → 健康门检查（HTTP /health）→
#   失败自动回滚旧版本与 .env → 可选 webhook 通知结果。
# 程序本体绝不自我升级、不需要 docker.sock；升级永远发生在宿主机这一层。
#
# 用法（全部可用环境变量覆盖，也可直接写在 .env 里兜底——cron/非交互 SSH 环境变量常为空）：
#   AHUB_DIR=/volume1/docker/audio-hub       # 放 docker-compose.yml 与 .env 的目录
#   AHUB_SERVICE=audio-hub                   # compose 服务名
#   AHUB_HEALTH_URL=http://127.0.0.1:8090/health   # 注意端口以 compose 映射的宿主机端口为准
#   AHUB_CONFIG_DIR=                         # 默认 $AHUB_DIR/config
#   AHUB_KEEP_BACKUPS=5                      # config 备份保留份数
#   AHUB_NOTIFY_WEBHOOK=                     # 可选：POST {"content": "..."} 通知
# 计划任务示例（每天 04:30）：
#   30 4 * * * AHUB_DIR=/volume1/docker/audio-hub bash /volume1/docker/audio-hub/auto-update.sh >> /volume1/docker/audio-hub/auto-update.log 2>&1
set -uo pipefail

# 群晖等非交互 SSH / cron 环境的 PATH 常缺 /usr/local/bin（docker 所在处），
# 缺了它所有 docker 调用都会静默 "command not found"，表现为假升级/假回滚。
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

AHUB_DIR="${AHUB_DIR:-/volume1/docker/audio-hub}"
AHUB_KEEP_BACKUPS="${AHUB_KEEP_BACKUPS:-5}"
AHUB_NOTIFY_WEBHOOK="${AHUB_NOTIFY_WEBHOOK:-}"
GH_LATEST_API="${GH_LATEST_API:-https://api.github.com/repos/OneFlatWhite/audio-hub/releases/latest}"

log() { echo "[$(date '+%F %T')] $*"; }
notify() { [ -n "$AHUB_NOTIFY_WEBHOOK" ] && curl -fsS -m 10 -H 'content-type: application/json' \
  -d "{\"content\": \"$1\"}" "$AHUB_NOTIFY_WEBHOOK" >/dev/null 2>&1 || true; }
die() { log "❌ $*"; notify "audio-hub 自动升级失败：$*"; exit 1; }

cd "$AHUB_DIR" || die "目录不存在: $AHUB_DIR"
[ -f docker-compose.yml ] || [ -f compose.yaml ] || die "找不到 docker-compose.yml"
[ -f .env ] || die "缺少 .env（内含 AHUB_VERSION=版本号，参考部署文档）"

# .env 里的同名键作兜底（cron/非交互 SSH 常没有环境变量）；显式传入的环境变量优先
_env_val() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d ' '; }
AHUB_SERVICE="${AHUB_SERVICE:-$(_env_val AHUB_SERVICE)}"
AHUB_HEALTH_URL="${AHUB_HEALTH_URL:-$(_env_val AHUB_HEALTH_URL)}"
AHUB_CONFIG_DIR="${AHUB_CONFIG_DIR:-$(_env_val AHUB_CONFIG_DIR)}"
AHUB_SERVICE="${AHUB_SERVICE:-audio-hub}"
AHUB_HEALTH_URL="${AHUB_HEALTH_URL:-http://127.0.0.1:8090/health}"
AHUB_CONFIG_DIR="${AHUB_CONFIG_DIR:-$AHUB_DIR/config}"

cur=$(grep -E '^AHUB_VERSION=' .env | head -1 | cut -d= -f2 | tr -d '"' | tr -d ' ')
[ -n "$cur" ] || die ".env 里没有 AHUB_VERSION"

# 1. 最新版本（GitHub 公开 API；拿不到就安静退出，绝不动运行中的服务）
latest=$(curl -fsS -m 15 "$GH_LATEST_API" 2>/dev/null |
  sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1 | sed 's/^v//')
[ -n "$latest" ] || { log "未获取到最新版本号（网络/限流？），本轮跳过"; exit 0; }

if [ "$latest" = "$cur" ]; then
  log "已是最新版本 v${cur}，无需升级"
  exit 0
fi
log "发现新版本：v${cur} → v${latest}"

# 2. 备份 /config（保留最近 N 份）
if [ -d "$AHUB_CONFIG_DIR" ]; then
  bk="backups/config-v${cur}-$(date '+%Y%m%d-%H%M%S')"
  mkdir -p "$AHUB_DIR/backups"
  cp -a "$AHUB_CONFIG_DIR" "$AHUB_DIR/$bk" || die "配置备份失败，终止升级"
  log "配置已备份: $bk"
  ls -1dt "$AHUB_DIR"/backups/config-* 2>/dev/null | tail -n +"$((AHUB_KEEP_BACKUPS + 1))" | xargs -r rm -rf
fi

# 3. 记录回滚点 → 改钉新版本 → 重建容器
rollback() {
  log "↩️ 回滚到 v${cur}"
  sed -i.bak "s/^AHUB_VERSION=.*/AHUB_VERSION=$cur/" .env && rm -f .env.bak
  docker compose up -d "$AHUB_SERVICE" >/dev/null 2>&1 || \
    docker compose up -d >/dev/null 2>&1 || true
}
healthy() {
  local i
  for i in $(seq 1 30); do
    body=$(curl -fsS -m 5 "$AHUB_HEALTH_URL" 2>/dev/null) && \
      echo "$body" | grep -q '"status":"ok"' && return 0
    sleep 3
  done
  return 1
}

sed -i.bak "s/^AHUB_VERSION=.*/AHUB_VERSION=$latest/" .env && rm -f .env.bak
docker compose pull "$AHUB_SERVICE" >/dev/null 2>&1 || log "pull 失败（可能镜像未就绪），继续尝试本地镜像"
docker compose up -d "$AHUB_SERVICE" >/dev/null 2>&1 || docker compose up -d >/dev/null 2>&1

# 4. 健康门：30×3=90 秒内 /health 必须恢复 ok
if healthy; then
  ver=$(curl -fsS -m 5 "$AHUB_HEALTH_URL" 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
  log "✅ 升级成功：v${cur} → v${latest}（/health version=${ver}）；如需回滚改 .env 后 up -d，备份在 backups/"
  notify "audio-hub 已自动升级：v${cur} → v${latest}"
  exit 0
fi

log "升级后健康检查未通过，自动回滚"
rollback
if healthy; then
  log "✅ 已回滚到 v${cur}，服务恢复；新版本 v${latest} 暂缓（请查看 docs/UPGRADE.md 与 release notes）"
  notify "audio-hub 升级 v${latest} 失败已自动回滚到 v${cur}，服务已恢复"
  exit 1
fi
die "回滚后服务仍未恢复，请人工介入（配置备份在 backups/）"
