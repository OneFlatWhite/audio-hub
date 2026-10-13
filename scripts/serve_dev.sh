#!/usr/bin/env bash
# audio-hub 开发服务启动脚本（WSL 内执行）
#
# 作用：生成一份可启动的 dev 配置（若已存在则不覆盖），然后前台启动 Web 服务。
# 前台运行是刻意的——你能实时看到请求日志，出问题直接把终端内容给助手。
# 停止服务：Ctrl+C
#
# 用法（WSL 内）：
#     bash /mnt/d/Documents/audio-hub/scripts/serve_dev.sh
#
# 启动后在 Windows 浏览器打开：
#     http://localhost:8000
# 首次访问会跳到 /setup 让你创建管理员账号（不预设默认密码）。

set -uo pipefail

SRC=/mnt/d/Documents/audio-hub
DST="$HOME/audio-hub"
DATA="$HOME/audio-hub-data"
PY="$DST/.venv/bin/python"
UVICORN="$DST/.venv/bin/uvicorn"
CFG_DIR="$DST/config"
CFG="$CFG_DIR/config.yaml"
KEY="$CFG_DIR/secrets.key"

log() { echo "[serve] $*"; }
die() { echo "[serve] !! $*" >&2; exit 1; }

# ── 前置检查 ──────────────────────────────────────────────
[ -x "$PY" ] || die "未找到 $PY，虚拟环境不存在"
log "虚拟环境: $PY ($($PY -V 2>&1))"

# 同步 D 盘源码（D 盘是权威副本，助手改的是那边）
log "同步 D 盘源码 -> $DST"
cd "$SRC" && tar cf - --exclude=__pycache__ --exclude=.pytest_cache src tests pyproject.toml scripts \
  | (cd "$DST" && tar xf -) || die "源码同步失败"

# 依赖自检
log "校验依赖"
"$PY" - <<'PYEOF' || die "依赖缺失，请先执行: .venv/bin/pip install -e ."
import importlib, sys
missing = []
for m in ('fastapi', 'uvicorn', 'jinja2', 'multipart', 'apscheduler',
          'pydantic', 'httpx', 'loguru', 'argon2', 'cryptography', 'audio_hub'):
    try:
        importlib.import_module(m)
    except Exception as e:
        missing.append(f"{m}: {e}")
if missing:
    print("  缺失模块:")
    for x in missing:
        print(f"    - {x}")
    sys.exit(1)
print("  依赖齐全")
PYEOF

# ── 数据目录与配置 ────────────────────────────────────────
mkdir -p "$DATA/seeds" "$DATA/scratch/inbox" "$DATA/library" "$DATA/audiobooks" \
         "$DATA/recycle" "$DATA/quarantine" "$CFG_DIR"

if [ -f "$CFG" ]; then
  log "已存在配置，保留不动: $CFG"
else
  log "生成 dev 配置: $CFG"
  cat > "$CFG" <<CFGEOF
# audio-hub 开发环境配置（由 serve_dev.sh 生成）
# dry_run=true：搜索/匹配照常执行，但派发只记日志不真正下载
dry_run: true
db_path: "$DATA/hub.db"
log_level: "INFO"
poll_interval_minutes: 30

web:
  cookie_secure: false      # 本机走 http，若设 true 浏览器不回传 cookie 会一直踢回登录页
  session_days: 7
  auth_request_limit: 10    # 15 分钟内最多 10 次登录尝试
  auth_window_minutes: 15

qbittorrent:
  host: "localhost"
  port: 8085
  api_key_encrypted: ""     # dev 阶段留空；实跑前用 scripts/init_secrets.py 生成
  category: "audio-hub"
  tag: "hub"
  save_path: "$DATA/seeds"

# 归档链路路径（dev 全部落在 $DATA 下，不碰真实 NAS 目录）
paths:
  scratch_inbox: "$DATA/scratch/inbox"
  library_music: "$DATA/library"
  library_audiobook: "$DATA/audiobooks"
  recycle_dir: "$DATA/recycle"
  quarantine_dir: "$DATA/quarantine"
  recycle_retention_days: 30

validation:
  enabled: true
  allowed_exts: [flac, ape, wav, dsf, dff, mp3, m4a, m4b, aac, ogg, opus, wma]
  min_audio_files: 1
  fake_lossless_check: true
  aucdtect_path: ""         # 空则跳过频谱检测
  on_fail: "quarantine"

scrape:
  mtw_webhook_url: ""
  abs_url: ""
  abs_library_id: ""
  abs_token_encrypted: ""
  timeout_seconds: 15

seeding:
  enabled: true
  check_interval_minutes: 30
  default_min_seed_hours: 72
  default_min_ratio: 1.0
  on_satisfied: "keep"

notify:
  wecom_webhook_encrypted: ""
  telegram_bot_token_encrypted: ""
  telegram_chat_id: ""
  generic_webhook_url: ""
  kinds: [dispatched, completed, routed, quarantined, hr_satisfied, series_completed, failed]

# dev 阶段全部禁用：不向任何 PT 站发真实请求，避免消耗配额或触发风控
sites:
  - name: "hhanclub"
    enabled: false
    rss_url: ""
  - name: "hdsky"
    enabled: false
    rss_url: ""
  - name: "audiences"
    enabled: false
    rss_url: ""
  - name: "mteam"
    enabled: false
    rss_url: ""
CFGEOF
fi

if [ -f "$KEY" ]; then
  log "已存在密钥文件，保留不动: $KEY"
else
  log "生成凭据密钥: $KEY"
  # 以 Path 传入：早期版本传字符串，而 ensure_key_file 当时只收 Path，
  # `.exists()` 抛 AttributeError 被脚本吞掉，密钥文件从未生成。
  # 现在函数两种都收，但脚本侧也显式用 Path，并不再吞错。
  "$PY" - <<PYEOF || die "密钥生成失败"
import sys; sys.path.insert(0, "$DST/src")
from pathlib import Path
from audio_hub.crypto import ensure_key_file
ensure_key_file(Path("$KEY"))
PYEOF
  [ -f "$KEY" ] || die "密钥文件未生成: $KEY"
fi

# ── 启动 ──────────────────────────────────────────────────
export AUDIO_HUB_CONFIG="$CFG"
export AUDIO_HUB_SECRETS_KEY="$KEY"
export PYTHONPATH="$DST/src"

log "配置: $CFG"
log "数据库: $DATA/hub.db"
log "模式: dry_run=true（不会真实下载）"
log "站点: 全部禁用（不会向 PT 站发请求）"
echo
log "启动 Web 服务 —— Windows 浏览器打开 http://localhost:8000"
log "按 Ctrl+C 停止"
echo

# 监听 0.0.0.0：WSL2 默认 NAT 网络，不会暴露到物理局域网，
# 但能让 Windows 侧通过 localhost 端口转发访问
exec "$UVICORN" audio_hub.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --log-level info
