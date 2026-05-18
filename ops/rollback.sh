#!/bin/bash
# 回滚到指定 git commit 或上一版
#
# 用法:
#   bash rollback.sh                      # 回到上一次 commit (HEAD~1)
#   bash rollback.sh --to abc123          # 回到指定 commit
#   bash rollback.sh --list               # 列出最近 10 次 commit

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"
TARGET=""

if [[ "$1" == "--list" ]]; then
    git log --oneline -10
    exit 0
fi

if [[ "$1" == "--to" ]]; then
    TARGET="$2"
elif [[ -z "$1" ]]; then
    TARGET=$(git rev-parse HEAD~1)
else
    TARGET="$1"
fi

echo "回滚到 commit: $TARGET"
git log -1 --format='  %h  %s  (%ar)' "$TARGET" || { echo "commit 不存在"; exit 1; }
read -p '继续？(y/N): ' yn
[[ "$yn" != "y" ]] && { echo "取消"; exit 0; }

# 1. 切换代码
echo "[1/3] git checkout"
git checkout "$TARGET" || exit 1

# 2. 重建
echo "[2/3] 重建并启动"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build

# 3. 等就绪
echo "[3/3] 等启动..."
for i in $(seq 1 30); do
    sleep 2
    if curl -fs http://localhost/api/health > /dev/null; then
        echo "✓ 回滚完成"
        exit 0
    fi
done
echo "✗ 回滚后服务未起来，请手工排查"
exit 1
