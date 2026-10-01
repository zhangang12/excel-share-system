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

# 2. 备份（发版前置：只备份数据库；uploads 卷发版不动，全备由每日 cron 负责，避免每次发版都 tar 数百 MB 撑爆磁盘）
if [[ "$SKIP_BACKUP" == "0" ]]; then
    echo "[1/5] 备份数据库（跳过 uploads，日常 cron 每日全备）..."
    SKIP_UPLOADS=1 bash "$SCRIPT_DIR/backup.sh" || { echo "备份失败，升级中止"; exit 1; }
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

# 4.5 重载 nginx 配置
#   ⚠️ nginx/conf.d 是 **bind mount**，改了配置 `up -d` 不会重建 nginx 容器，
#      也就不会生效——git pull 拉下来了、发版报成功，线上还是旧配置，无声无息。
#      先 `nginx -t` 验证，语法错就不 reload（错配置 reload 下去会把整站打挂）。
if docker exec pms2_nginx nginx -t >/dev/null 2>&1; then
    docker exec pms2_nginx nginx -s reload >/dev/null 2>&1 && echo "  → nginx 配置已重载"
else
    echo "  ⚠ nginx 配置检查未通过，跳过 reload（线上继续跑旧配置）："
    docker exec pms2_nginx nginx -t 2>&1 | sed 's/^/    /'
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
    # 🆕 2026-10-01 发版成功后清理旧镜像。原来从不清：94 个被替换下来的 <none> 镜像、
    #   255 份旧代码层积到 31G，把磁盘顶到 85%（另一半原因是后端缺 .dockerignore，见 backend/.dockerignore）。
    #   · image prune 只删悬空镜像（<none>），正在用的不动；回滚是「切 commit + 重建」，不依赖旧镜像。
    #   · builder prune 只删 72 小时没用过的构建缓存；依赖层每次构建都会用到，不会被删，构建速度不受影响。
    #   ⚠️ 不要换成 `docker system prune --volumes`——会删数据卷（数据库、附件）。
    #   ⚠️ 不要在生产上跑 `docker system df`（明确禁止过）。
    #   清理失败不影响发版结果。
    docker image prune -f >/dev/null 2>&1 || true
    docker builder prune -f --filter until=72h >/dev/null 2>&1 || true
    echo "  → 已清理旧镜像和 72 小时没用过的构建缓存（磁盘占用 $(df -h / | awk 'NR==2{print $5}')）"
    exit 0
else
    echo "✗ 健康检查未通过，回滚"
    bash "$SCRIPT_DIR/rollback.sh" --to "$OLD_COMMIT" || exit 2
    exit 1
fi
