#!/bin/bash
# 安全升级脚本：备份 → 拉代码 → 重建 → 健康检查 → 失败自动回滚
#
# 用法:
#   bash upgrade.sh                       # 拉最新代码升级
#   SKIP_BACKUP=1 bash upgrade.sh         # 跳过备份（不推荐）
#   bash upgrade.sh --no-rebuild          # 不 rebuild 镜像，只重启
#
# 退出码: 0=升级成功 1=失败但已回滚 2=失败且回滚也失败

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"
SKIP_BACKUP="${SKIP_BACKUP:-0}"
REBUILD=1
[[ "$1" == "--no-rebuild" ]] && REBUILD=0

echo "[$(date '+%F %T')] upgrade start"

# 1. 记录当前 git commit
OLD_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
echo "  current commit: $OLD_COMMIT"

# 2. 备份
if [[ "$SKIP_BACKUP" == "0" ]]; then
    echo "[1/5] 备份..."
    bash "$SCRIPT_DIR/backup.sh" || { echo "备份失败，升级中止"; exit 1; }
else
    echo "[1/5] (跳过备份)"
fi

# 3. 拉代码
echo "[2/5] git pull..."
git fetch
git pull --ff-only || { echo "git pull 失败"; exit 1; }
NEW_COMMIT=$(git rev-parse HEAD)
if [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then
    echo "  已经是最新 commit，无需升级"
    exit 0
fi
echo "  $OLD_COMMIT -> $NEW_COMMIT"

# 4. 重启服务
echo "[3/5] 重建并启动..."
if [[ "$REBUILD" == "1" ]]; then
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build
else
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d
fi

# 5. 等启动 + 健康检查
echo "[4/5] 等服务启动..."
for i in $(seq 1 30); do
    sleep 2
    if curl -fs http://localhost/api/health > /dev/null 2>&1; then
        echo "  → backend 起来了"
        break
    fi
    if [[ "$i" == "30" ]]; then
        echo "  ✗ backend 60s 内没起来，回滚"
        bash "$SCRIPT_DIR/rollback.sh" --to "$OLD_COMMIT" || exit 2
        exit 1
    fi
done

# 6. 跑完整健康检查
echo "[5/5] 健康检查..."
if bash "$SCRIPT_DIR/health-check.sh" --quiet; then
    echo "✓ 升级成功：$OLD_COMMIT → $NEW_COMMIT"
    exit 0
else
    echo "✗ 健康检查未通过，回滚"
    bash "$SCRIPT_DIR/rollback.sh" --to "$OLD_COMMIT" || exit 2
    exit 1
fi
