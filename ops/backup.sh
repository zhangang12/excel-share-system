#!/bin/bash
# 数据库备份脚本 - 每日 cron 调用
#
# 用法:
#   bash backup.sh                        # 备份到 /backup/
#   BACKUP_DIR=/data/bak bash backup.sh   # 自定义目录
#   KEEP_DAYS=14 bash backup.sh           # 保留天数（默认 30）
#   bash backup.sh --upload-cos           # 备份并上传腾讯云 COS（需 coscli）
#   bash backup.sh --upload-oss           # 备份并上传阿里云 OSS（需 ossutil）
#
# 退出码: 0=成功 1=备份失败 2=上传失败

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# === 配置 ===
BACKUP_DIR="${BACKUP_DIR:-/backup}"
KEEP_DAYS="${KEEP_DAYS:-30}"
COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"
COS_BUCKET="${COS_BUCKET:-}"          # e.g. cos://pms-backup-xxx/db/
OSS_BUCKET="${OSS_BUCKET:-}"          # e.g. oss://pms-backup-xxx/db/
UPLOAD_COS=0
UPLOAD_OSS=0

case "$1" in
    --upload-cos) UPLOAD_COS=1 ;;
    --upload-oss) UPLOAD_OSS=1 ;;
esac

# === 准备 ===
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d_%H%M%S)
DB_USER=$(grep -E '^POSTGRES_USER=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
DB_NAME=$(grep -E '^POSTGRES_DB=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
DB_USER="${DB_USER:-pms_prod}"
DB_NAME="${DB_NAME:-pms}"

FILE_DB="$BACKUP_DIR/pms-db-${DATE}.sql.gz"
FILE_UPLOADS="$BACKUP_DIR/pms-uploads-${DATE}.tar.gz"

# === 1. 备份数据库 ===
echo "[$(date '+%F %T')] backup db -> $FILE_DB"
if ! docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T postgres \
        pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$FILE_DB"; then
    echo "ERROR: pg_dump 失败" >&2
    rm -f "$FILE_DB"
    exit 1
fi
SIZE=$(du -h "$FILE_DB" | cut -f1)
echo "  → ok ($SIZE)"

# === 2. 备份上传目录（uploads volume）===
echo "[$(date '+%F %T')] backup uploads -> $FILE_UPLOADS"
UPLOADS_VOL=$(docker volume ls -q | grep -E 'uploads_data$' | head -1)
if [[ -n "$UPLOADS_VOL" ]]; then
    docker run --rm -v "$UPLOADS_VOL":/src:ro -v "$BACKUP_DIR":/dst alpine \
        tar czf "/dst/pms-uploads-${DATE}.tar.gz" -C /src . || true
    SIZE=$(du -h "$FILE_UPLOADS" 2>/dev/null | cut -f1)
    [[ -n "$SIZE" ]] && echo "  → ok ($SIZE)"
fi

# === 3. 滚动清理 ===
echo "[$(date '+%F %T')] rotate (keep ${KEEP_DAYS} days)"
find "$BACKUP_DIR" -name 'pms-db-*.sql.gz' -mtime +${KEEP_DAYS} -delete
find "$BACKUP_DIR" -name 'pms-uploads-*.tar.gz' -mtime +${KEEP_DAYS} -delete
LEFT=$(find "$BACKUP_DIR" -name 'pms-db-*.sql.gz' | wc -l)
echo "  → ${LEFT} db backups left"

# === 4. (可选) 上传 COS ===
if [[ "$UPLOAD_COS" == "1" ]]; then
    if [[ -z "$COS_BUCKET" ]]; then
        echo "ERROR: --upload-cos 需要设 COS_BUCKET 环境变量" >&2
        exit 2
    fi
    if ! command -v coscli >/dev/null; then
        echo "ERROR: coscli 未安装" >&2
        exit 2
    fi
    echo "[$(date '+%F %T')] upload to $COS_BUCKET"
    coscli cp "$FILE_DB" "$COS_BUCKET" || { echo "ERROR: cos upload failed"; exit 2; }
    [[ -f "$FILE_UPLOADS" ]] && coscli cp "$FILE_UPLOADS" "$COS_BUCKET" || true
    echo "  → uploaded"
fi

# === 5. (可选) 上传阿里云 OSS ===
if [[ "$UPLOAD_OSS" == "1" ]]; then
    if [[ -z "$OSS_BUCKET" ]]; then
        echo "ERROR: --upload-oss 需要设 OSS_BUCKET 环境变量" >&2
        exit 2
    fi
    if ! command -v ossutil >/dev/null; then
        echo "ERROR: ossutil 未安装。装法: curl https://gosspublic.alicdn.com/ossutil/install.sh | sudo bash" >&2
        exit 2
    fi
    echo "[$(date '+%F %T')] upload to $OSS_BUCKET"
    ossutil cp -f "$FILE_DB" "$OSS_BUCKET" || { echo "ERROR: oss upload failed"; exit 2; }
    [[ -f "$FILE_UPLOADS" ]] && ossutil cp -f "$FILE_UPLOADS" "$OSS_BUCKET" || true
    echo "  → uploaded"
fi

echo "[$(date '+%F %T')] backup done."
