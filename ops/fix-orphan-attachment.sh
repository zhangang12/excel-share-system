#!/bin/bash
# 排查并清理孤立附件（DB 有记录但物理文件已丢失）
# 用法: bash ops/fix-orphan-attachment.sh [项目编号]
#   项目编号默认 2026-024B
#
# 流程：
#   1. 查该项目的开票申请表附件记录
#   2. 检查物理文件是否在 uploads 卷里
#   3. 文件不存在 → 提示确认后清除 DB 引用 + 删附件记录
#   4. 文件存在   → 提示文件正常，无需处理

set -euo pipefail

PROJECT_CODE="${1:-2026-024B}"

cd "$(dirname "$0")/.."

echo "========================================"
echo "  孤立附件排查 · 项目 $PROJECT_CODE"
echo "========================================"

# ── 自动探测 postgres 容器名和 DB 用户 ──────────
PG_CONTAINER=$(docker ps --format '{{.Names}}' | grep -E 'postgres|pms.*db|db.*pms' | head -1)
if [[ -z "$PG_CONTAINER" ]]; then
  echo "  ✗ 未找到运行中的 postgres 容器"
  echo "  当前所有容器："
  docker ps --format '  {{.Names}}  ({{.Image}})'
  exit 1
fi
echo "  [postgres 容器] $PG_CONTAINER"

# 从容器环境变量中读取 DB 用户和数据库名
DB_USER=$(docker exec "$PG_CONTAINER" env 2>/dev/null | grep '^POSTGRES_USER=' | cut -d= -f2)
DB_NAME=$(docker exec "$PG_CONTAINER" env 2>/dev/null | grep '^POSTGRES_DB='   | cut -d= -f2)
DB_USER="${DB_USER:-pms}"
DB_NAME="${DB_NAME:-pms}"
echo "  [DB 用户] $DB_USER  [DB 名] $DB_NAME"

# ── 1. 查 DB ──────────────────────────────
echo ""
echo "[1/3] 查数据库记录..."
RECORD=$(docker exec "$PG_CONTAINER" \
  psql -U "$DB_USER" -d "$DB_NAME" -t -A -F'|' -c "
SELECT sl.id,
       COALESCE(sl.invoice_state, ''),
       COALESCE(sl.invoice_apply_file_id::text, ''),
       COALESCE(a.name, ''),
       COALESCE(a.path, '')
FROM sales_ledger sl
JOIN projects p ON p.id = sl.project_id
LEFT JOIN attachments a ON a.id = sl.invoice_apply_file_id
WHERE p.code = '$PROJECT_CODE'
LIMIT 1;" 2>&1 || true)

# 过滤掉 psql 提示行和错误行
RECORD=$(echo "$RECORD" | grep -v '^$' | grep -v '^(' | grep -v '^ERROR' | grep -v '^psql' | head -1)

if echo "$RECORD" | grep -qi 'error\|does not exist'; then
  echo "  ✗ 查询出错：$RECORD"
  exit 1
fi

if [[ -z "$RECORD" ]]; then
  echo "  ✗ 未找到项目 $PROJECT_CODE 的销售台账，退出。"
  echo ""
  echo "  提示：可用以下命令查看数据库中实际的项目编号："
  echo "  docker exec $PG_CONTAINER psql -U $DB_USER -d $DB_NAME -c \"SELECT code FROM project ORDER BY code;\""
  exit 1
fi

LEDGER_ID=$(echo "$RECORD"  | cut -d'|' -f1)
INV_STATE=$(echo "$RECORD"  | cut -d'|' -f2)
ATT_ID=$(echo "$RECORD"     | cut -d'|' -f3)
FILE_NAME=$(echo "$RECORD"  | cut -d'|' -f4)
FILE_PATH=$(echo "$RECORD"  | cut -d'|' -f5)

echo "  台账ID       : $LEDGER_ID"
echo "  开票状态     : ${INV_STATE:-（空）}"
echo "  附件ID       : ${ATT_ID:-（无）}"
echo "  附件名       : ${FILE_NAME:-（无）}"
echo "  附件路径     : ${FILE_PATH:-（无）}"

if [[ -z "$ATT_ID" || "$ATT_ID" == "" ]]; then
  echo ""
  echo "  ✓ 该项目没有开票申请表附件引用，无需处理。"
  exit 0
fi

# ── 2. 检查物理文件 ────────────────────────
echo ""
echo "[2/3] 检查物理文件是否存在..."
UPLOADS_VOL=$(docker volume ls -q | grep -E 'uploads_data' | head -1)
if [[ -z "$UPLOADS_VOL" ]]; then
  echo "  ✗ 未找到 uploads_data 卷，无法检查物理文件。"
  echo "  所有卷："
  docker volume ls -q
  exit 1
fi

FILE_EXISTS=$(docker run --rm -v "$UPLOADS_VOL":/data alpine \
  sh -c "[ -f '/data/$FILE_PATH' ] && echo yes || echo no" 2>/dev/null)

echo "  卷名         : $UPLOADS_VOL"
echo "  文件是否存在 : $FILE_EXISTS"

if [[ "$FILE_EXISTS" == "yes" ]]; then
  echo ""
  echo "  ✓ 物理文件存在，无需处理。可能是权限或路径配置问题，请联系开发排查。"
  exit 0
fi

# ── 3. 清除孤立引用 ────────────────────────
echo ""
echo "  ✗ 物理文件丢失（附件ID=$ATT_ID，文件名=$FILE_NAME）"
echo ""
echo "[3/3] 准备清除孤立附件引用..."
echo "  操作："
echo "    · sales_ledger.invoice_apply_file_id → NULL"
echo "    · sales_ledger.invoice_state         → NULL（重置为未申请）"
echo "    · attachment 表删除 id=$ATT_ID 的记录"
echo ""
read -r -p "  确认执行？(y/N) " CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
  echo "  已取消。"
  exit 0
fi

docker exec "$PG_CONTAINER" \
  psql -U "$DB_USER" -d "$DB_NAME" -c "
BEGIN;
UPDATE sales_ledger
   SET invoice_apply_file_id = NULL,
       invoice_state = NULL
 WHERE id = $LEDGER_ID;
DELETE FROM attachments WHERE id = $ATT_ID;
COMMIT;"

echo ""
echo "  ✓ 清除完成。"
echo "    项目 $PROJECT_CODE 开票申请表已重置为未申请状态，销售可重新上传。"
