#!/bin/bash
# 从备份恢复数据库（**会清空当前数据库**）
#
# 用法:
#   bash restore.sh /backup/pms-db-20260514_030000.sql.gz
#   bash restore.sh /backup/pms-db-20260514_030000.sql.gz --uploads /backup/pms-uploads-20260514_030000.tar.gz
#   bash restore.sh --list                  # 列出可用备份
#
# 安全机制:
#   - 恢复前自动备份当前 db（为了万一）
#   - 需要输入 "yes I am sure" 才会真执行

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"
BACKUP_DIR="${BACKUP_DIR:-/backup}"

if [[ "$1" == "--list" || -z "$1" ]]; then
    echo "Available backups in $BACKUP_DIR:"
    ls -lh "$BACKUP_DIR"/pms-db-*.sql.gz 2>/dev/null | awk '{print $9, "  ("$5")"}'
    echo
    echo "用法: bash restore.sh <db-backup.sql.gz> [--uploads uploads.tar.gz]"
    exit 0
fi

DB_FILE="$1"
UPLOADS_FILE=""
shift
while [[ $# -gt 0 ]]; do
    case "$1" in
        --uploads) UPLOADS_FILE="$2"; shift 2;;
        *) echo "Unknown arg: $1"; exit 2;;
    esac
done

[[ ! -f "$DB_FILE" ]] && { echo "ERROR: $DB_FILE 不存在"; exit 1; }
[[ -n "$UPLOADS_FILE" && ! -f "$UPLOADS_FILE" ]] && { echo "ERROR: $UPLOADS_FILE 不存在"; exit 1; }

DB_USER=$(grep -E '^POSTGRES_USER=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
DB_NAME=$(grep -E '^POSTGRES_DB=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
DB_USER="${DB_USER:-pms_prod}"
DB_NAME="${DB_NAME:-pms}"

echo "================================================="
echo "  ⚠⚠⚠  即将用以下文件恢复 ⚠⚠⚠"
echo "    数据库:   $DB_FILE"
[[ -n "$UPLOADS_FILE" ]] && echo "    上传目录: $UPLOADS_FILE"
echo "    目标 db:  $DB_NAME (user $DB_USER)"
echo "    当前数据库会被完全清空替换"
echo "================================================="
read -p '请输入 "yes I am sure" 确认: ' CONFIRM
if [[ "$CONFIRM" != "yes I am sure" ]]; then
    echo "已取消"
    exit 0
fi

# 1. 先做个"恢复前快照"，万一恢复后想反悔
SNAPSHOT="$BACKUP_DIR/pre-restore-$(date +%Y%m%d_%H%M%S).sql.gz"
mkdir -p "$BACKUP_DIR"
echo "[1/5] 先备份当前 db 到 $SNAPSHOT"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T postgres \
    pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$SNAPSHOT" || true

# 2. 停 backend / nginx 防止持续写入
echo "[2/5] 停 backend / frontend / nginx"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" stop backend frontend nginx

# 3. drop & recreate db
echo "[3/5] 清空目标库 $DB_NAME"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T postgres \
    psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$DB_NAME\";"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T postgres \
    psql -U "$DB_USER" -d postgres -c "CREATE DATABASE \"$DB_NAME\";"

# 4. 恢复
echo "[4/5] 恢复 db..."
gunzip -c "$DB_FILE" | docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T postgres \
    psql -U "$DB_USER" "$DB_NAME" > /dev/null
echo "  → db restored"

if [[ -n "$UPLOADS_FILE" ]]; then
    UPLOADS_VOL=$(docker volume ls -q | grep -E 'uploads_data$' | head -1)
    if [[ -n "$UPLOADS_VOL" ]]; then
        echo "[4b] 恢复 uploads volume..."
        docker run --rm -v "$UPLOADS_VOL":/dst -v "$(dirname "$UPLOADS_FILE")":/src:ro alpine \
            sh -c "rm -rf /dst/* /dst/..?* /dst/.[!.]* 2>/dev/null; tar xzf /src/$(basename "$UPLOADS_FILE") -C /dst"
        echo "  → uploads restored"
    fi
fi

# 5. 重启所有服务
echo "[5/5] 重启服务"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d
sleep 5
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps

echo
echo "✓ 恢复完成。如果有问题，可用 $SNAPSHOT 反向恢复。"
