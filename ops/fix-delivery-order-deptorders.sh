#!/bin/bash
# 技改脚本：清理调货订单残留的部门任务单（dept_orders）
# 用法: bash ops/fix-delivery-order-deptorders.sh [--dry-run]
#
# 背景：早期 backfill 给所有「进行中」项目无脑补建 design/electric/produce 任务单，
#       未排除调货订单（如 2026-008）。调货订单不流转生产，这些任务单是冗余的，
#       会让调货订单错误出现在设计/电工/生产工作台。
# 安全：只删 status='pending_assign' 且未派人(worker_id IS NULL) 且无关联附件的「裸」任务单；
#       已派人/有产物的会被列出但跳过，需人工核实。
#   --dry-run 仅预览，不写库。
#
# 注：展示问题已由代码修复（list_orders 排除调货订单）解决，本脚本为彻底清理冗余数据。

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do [[ "$arg" == "--dry-run" ]] && DRY_RUN=1; done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

echo "========================================"
echo "  调货订单残留部门任务单清理"
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

# ---- STEP 1: 列出所有调货订单的部门任务单 ----
echo ""
echo "[STEP 1] 调货订单的部门任务单（全部）："
ALL_ROWS=$(run_sql "
  SELECT o.id, p.code, o.dept, o.status,
         COALESCE(CAST(o.worker_id AS text), 'NULL') AS worker_id,
         (SELECT COUNT(*) FROM attachments a WHERE a.biz_id = o.id AND a.biz_type LIKE 'order%') AS att_cnt
  FROM dept_orders o
  JOIN projects p ON p.id = o.project_id
  JOIN sales_ledger s ON s.project_id = p.id
  WHERE s.order_type = '调货订单' AND o.dept IN ('design','electric','produce')
  ORDER BY p.code, o.dept;
" || true)

if [[ -z "$ALL_ROWS" ]]; then
  echo "[OK] 调货订单没有任何部门任务单，无需处理。"
  exit 0
fi

echo "-----------------------------------------------------------------"
printf "%-6s %-12s %-9s %-15s %-9s %-6s\n" "ID" "项目编号" "部门" "状态" "worker" "附件数"
echo "-----------------------------------------------------------------"
while IFS='|' read -r id code dept status worker att; do
  printf "%-6s %-12s %-9s %-15s %-9s %-6s\n" "$id" "$code" "$dept" "$status" "$worker" "$att"
done <<< "$ALL_ROWS"
echo "-----------------------------------------------------------------"

# ---- STEP 2: 可安全删除的（裸任务单） ----
SAFE_IDS=$(run_sql "
  SELECT o.id
  FROM dept_orders o
  JOIN projects p ON p.id = o.project_id
  JOIN sales_ledger s ON s.project_id = p.id
  WHERE s.order_type = '调货订单'
    AND o.dept IN ('design','electric','produce')
    AND o.status = 'pending_assign'
    AND o.worker_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM attachments a WHERE a.biz_id = o.id AND a.biz_type LIKE 'order%')
  ORDER BY o.id;
" || true)

if [[ -z "$SAFE_IDS" ]]; then
  echo ""
  echo "[!] 没有可安全删除的裸任务单（pending_assign + 未派人 + 无附件）。"
  echo "    上表中已派人/有产物的任务单需人工核实，本脚本不处理。"
  exit 0
fi

ID_LIST=$(echo "$SAFE_IDS" | tr '\n' ',' | sed 's/,$//')
COUNT=$(echo "$SAFE_IDS" | wc -l | tr -d ' ')
echo ""
echo "可安全删除的裸任务单 ID: ($ID_LIST)，共 $COUNT 条"

if [[ $DRY_RUN -eq 1 ]]; then
  echo ""
  echo "[DRY-RUN] 将执行：DELETE FROM dept_orders WHERE id IN ($ID_LIST);"
  echo "去掉 --dry-run 参数后重新运行即可正式执行。"
  exit 0
fi

echo ""
read -r -p "确认删除以上 $COUNT 条裸任务单？[y/N] " CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
  echo "已取消。"
  exit 0
fi

echo ""
echo "[STEP 3] 删除..."
run_sql "DELETE FROM dept_orders WHERE id IN ($ID_LIST);"
echo "[OK] 已删除 ID: ($ID_LIST)"

echo ""
echo "[VERIFY] 调货订单剩余部门任务单数："
run_sql "
  SELECT COUNT(*) FROM dept_orders o
  JOIN sales_ledger s ON s.project_id = o.project_id
  WHERE s.order_type = '调货订单' AND o.dept IN ('design','electric','produce');
"
echo ""
echo "========================================"
echo "  清理完成"
echo "========================================"
