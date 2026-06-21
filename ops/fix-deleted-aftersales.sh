#!/bin/bash
# 技改脚本：删除关联已删除项目的孤立售后记录
# 用法: bash ops/fix-deleted-aftersales.sh [--dry-run]
#
# 背景：售后记录 project_id 指向已软删除（is_deleted=true）或不存在的项目，
#       前端报"关联的数据不存在或被引用，无法操作"，且记录无法正常编辑/删除。
# 操作：
#   1. 查出所有关联已删除/不存在项目的 aftersales 行
#   2. 先将 mat_file_id 置 NULL（解除外键引用）
#   3. 删除 aftersales 行
#   --dry-run 仅预览，不写库

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do [[ "$arg" == "--dry-run" ]] && DRY_RUN=1; done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

echo "========================================"
echo "  孤立售后记录清理"
[[ $DRY_RUN -eq 1 ]] && echo "  模式: DRY-RUN（仅预览，不写库）" || echo "  模式: 正式执行"
echo "========================================"

DB_USER=$(grep -E '^POSTGRES_USER=' .env.prod 2>/dev/null | cut -d= -f2 || echo "pms_prod")
DB_NAME=$(grep -E '^POSTGRES_DB=' .env.prod 2>/dev/null | cut -d= -f2 || echo "pms")
DB_USER="${DB_USER:-pms_prod}"
DB_NAME="${DB_NAME:-pms}"

run_sql() {
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
    psql -U "$DB_USER" "$DB_NAME" -t -A -c "$1" 2>/dev/null \
    | grep -v '^$' | grep -v '^(' | grep -v '^ERROR'
}

# ---- STEP 1: 查孤立行 ----
echo ""
echo "[STEP 1] 查找孤立售后记录..."
ORPHAN_ROWS=$(run_sql "
  SELECT a.id, a.project_id, a.problem, a.cost::text, a.status,
         COALESCE(CAST(a.mat_file_id AS text), 'NULL') AS mat_file_id,
         COALESCE(p.code, '(项目不存在)') AS proj_code,
         COALESCE(p.name, '—') AS proj_name,
         COALESCE(CAST(p.is_deleted AS text), 'N/A') AS is_deleted
  FROM aftersales a
  LEFT JOIN projects p ON p.id = a.project_id
  WHERE p.id IS NULL OR p.is_deleted = true
  ORDER BY a.id;
" || true)

if [[ -z "$ORPHAN_ROWS" ]]; then
  echo "[OK] 未发现孤立售后记录，无需处理。"
  exit 0
fi

echo "发现以下孤立售后记录："
echo "-----------------------------------------------------------------------"
printf "%-6s %-16s %-20s %-22s %-10s %-10s\n" "ID" "项目编号" "项目名" "售后问题" "费用" "状态"
echo "-----------------------------------------------------------------------"
while IFS='|' read -r id proj_id problem cost status mat_file_id proj_code proj_name is_deleted; do
  printf "%-6s %-16s %-20s %-22s %-10s %-10s\n" \
    "$id" "$proj_code" "${proj_name:0:18}" "${problem:0:20}" "¥$cost" "$status"
done <<< "$ORPHAN_ROWS"
echo "-----------------------------------------------------------------------"

# 提取 ID 列表
ORPHAN_IDS=$(run_sql "
  SELECT a.id FROM aftersales a
  LEFT JOIN projects p ON p.id = a.project_id
  WHERE p.id IS NULL OR p.is_deleted = true
  ORDER BY a.id;
" || true)
ID_LIST=$(echo "$ORPHAN_IDS" | tr '\n' ',' | sed 's/,$//')
COUNT=$(echo "$ORPHAN_IDS" | wc -l | tr -d ' ')

echo ""
echo "待删除行 ID: ($ID_LIST)，共 $COUNT 条"

if [[ $DRY_RUN -eq 1 ]]; then
  echo ""
  echo "[DRY-RUN] 将执行以下 SQL（未实际写库）："
  echo "  -- Step A: 解除 mat_file_id 外键约束"
  echo "  UPDATE aftersales SET mat_file_id = NULL WHERE id IN ($ID_LIST);"
  echo "  -- Step B: 删除孤立行"
  echo "  DELETE FROM aftersales WHERE id IN ($ID_LIST);"
  echo ""
  echo "去掉 --dry-run 参数后重新运行即可正式执行。"
  exit 0
fi

echo ""
read -r -p "确认删除以上 $COUNT 条记录？[y/N] " CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
  echo "已取消。"
  exit 0
fi

# ---- STEP 2: 解除 mat_file_id 外键，再删行 ----
echo ""
echo "[STEP 2] 解除 mat_file_id 外键引用..."
run_sql "UPDATE aftersales SET mat_file_id = NULL WHERE id IN ($ID_LIST);"
echo "[OK] mat_file_id 已置 NULL"

echo "[STEP 3] 删除孤立售后记录..."
run_sql "DELETE FROM aftersales WHERE id IN ($ID_LIST);"
echo "[OK] 已删除 ID: ($ID_LIST)"

echo ""
echo "[VERIFY] 当前 aftersales 表剩余记录数："
run_sql "SELECT COUNT(*) FROM aftersales;"

echo ""
echo "========================================"
echo "  清理完成"
echo "========================================"
