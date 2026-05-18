#!/bin/bash
# 一键收集诊断信息（出问题给开发看）
#
# 用法:
#   bash diagnose.sh                # 默认输出到 /tmp/pms-diag-YYYYMMDD_HHMMSS.tar.gz

set +e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

DATE=$(date +%Y%m%d_%H%M%S)
TMP="/tmp/pms-diag-${DATE}"
OUT="/tmp/pms-diag-${DATE}.tar.gz"
mkdir -p "$TMP"

echo "收集诊断信息到 $TMP"

# 1. 系统信息
{
    echo "===== uname ====="; uname -a
    echo
    echo "===== uptime ====="; uptime
    echo
    echo "===== /etc/os-release ====="; cat /etc/os-release 2>/dev/null
    echo
    echo "===== free -h ====="; free -h
    echo
    echo "===== df -h ====="; df -h
    echo
    echo "===== top -bn1 (top 20) ====="; top -bn1 | head -25
} > "$TMP/system.txt" 2>&1

# 2. Docker 信息
{
    echo "===== docker version ====="; docker version
    echo
    echo "===== docker info ====="; docker info
    echo
    echo "===== docker ps -a ====="; docker ps -a
    echo
    echo "===== docker volume ls ====="; docker volume ls
    echo
    echo "===== docker compose ps ====="
    docker compose -f docker-compose.prod.yml ps 2>&1 || docker compose ps 2>&1
} > "$TMP/docker.txt" 2>&1

# 3. 各容器日志（最近 500 行）
for c in pms2_postgres pms2_redis pms2_backend pms2_frontend pms2_nginx; do
    docker logs --tail 500 "$c" > "$TMP/${c}.log" 2>&1 || true
done

# 4. 配置（脱敏）
if [[ -f .env.prod ]]; then
    sed -E 's/(PASSWORD|SECRET_KEY|KEY)=.*/\1=***REDACTED***/' .env.prod > "$TMP/env.prod.redacted"
fi

cp docker-compose.prod.yml "$TMP/" 2>/dev/null
cp -r nginx/conf.d "$TMP/nginx-conf" 2>/dev/null

# 5. 数据库状态
docker exec pms2_postgres psql -U pms_prod -d pms -c "
SELECT relname AS table, n_live_tup AS rows, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 30;
" > "$TMP/db-tables.txt" 2>&1 || true

docker exec pms2_postgres psql -U pms_prod -d pms -c "
SELECT pid, state, query_start, LEFT(query, 100) AS query
FROM pg_stat_activity WHERE datname='pms' ORDER BY query_start;
" > "$TMP/db-activity.txt" 2>&1 || true

# 6. 健康检查
bash "$SCRIPT_DIR/health-check.sh" > "$TMP/health.txt" 2>&1 || true

# 7. Nginx 错误日志（如果落盘了）
docker exec pms2_nginx tail -200 /var/log/nginx/error.log > "$TMP/nginx-error.log" 2>&1 || true
docker exec pms2_nginx tail -200 /var/log/nginx/access.log > "$TMP/nginx-access.log" 2>&1 || true

# 8. 打包
tar czf "$OUT" -C /tmp "pms-diag-${DATE}"
rm -rf "$TMP"

SIZE=$(du -h "$OUT" | cut -f1)
echo
echo "✓ 诊断包: $OUT ($SIZE)"
echo "  发给开发: scp $OUT user@your-pc:/tmp/"
