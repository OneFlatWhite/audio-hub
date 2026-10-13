#!/usr/bin/env bash
# 把 docs/wiki/*.md 推送到 GitHub Wiki（一键）。
# 前提：仓库 Settings → Features → Wikis 已勾选，并在网页上创建过一个页面
#      （GitHub 由此才会创建 <repo>.wiki.git 仓库）。
# 用法：bash scripts/push_wiki.sh
set -euo pipefail

REPO="OneFlatWhite/audio-hub"
WIKI_DIR="/tmp/${REPO##*/}.wiki"
SRC="$(cd "$(dirname "$0")/.." && pwd)/docs/wiki"

git clone "git@github.com:${REPO}.wiki.git" "$WIKI_DIR" 2>/dev/null \
  || (cd "$WIKI_DIR" && git pull --rebase)

cp "$SRC"/*.md "$WIKI_DIR"/
cd "$WIKI_DIR"
git add -A
if git diff --cached --quiet; then
  echo "wiki 无变更"
else
  git commit -m "docs: 同步使用手册（Home/Usage/部署升级/授权FAQ）"
  git push origin master
  echo "wiki 已推送：https://github.com/${REPO}/wiki"
fi
