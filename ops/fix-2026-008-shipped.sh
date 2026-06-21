#!/bin/bash
# 一次性技改脚本：把存量调货订单 2026-008 标记为已发货，使其出现在物流发货部「已完成」
# 用法: bash ops/fix-2026-008-shipped.sh [--dry-run]
#
# 背景：2026-008 是历史存量调货订单，实际已调货发出，但因调货订单不建发货记录(Shipment)，
#       在物流发货部看不到。本脚本给它补一条 status='shipped' 的发货记录，让它走正常的
#       「已发货」路径出现在物流「已完成」里——代码不做任何调货订单特例。
# 幂等：无发货记录→插入 shipped；已有 pending→改为 shipped；已 shipped→跳过。
#   --dry-run 仅预览，不写库。
#
# 注：仅针对 2026-008。其他调货订单按系统正常逻辑走（不在此处理）。

set -euo pipefail

DRY_RUN=0
for arg in "$@"; do [[ "$arg" == "--dry-run" ]] && DRY_RUN=1; done

CODE="2026-008"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

echo "========================================"
echo "  $CODE 标记已发货（物流已完成）"
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

# ---- STEP 1: 校验项目存在且为调货订单 ----
echo ""
echo "[STEP 1] 核对 $CODE ..."
INFO=$(run_sql "
  SELECT p.id, p.name, p.status,
         COALESCE(s.order_type,'(无台账)') AS order_type,
         COALESCE(CAST(sh.id AS text),'NONE') AS ship_id,
         COALESCE(sh.status,'-') AS ship_status
  FROM projects p
  LEFT JOIN sales_ledger s ON s.project_id = p.id
  LEFT JOIN shipments sh ON sh.project_id = p.id
  WHERE p.code = '$CODE' AND p.is_deleted = false;
" || true)

if [[ -z "$INFO" ]]; then
  echo "[ERROR] 未找到项目 $CODE（或已删除）。"
  exit 1
fi
IFS='|' read -r PID PNAME PSTATUS OTYPE SHIP_ID SHIP_STATUS <<< "$INFO"
echo "  项目: $CODE  $PNAME"
echo "  项目状态: $PSTATUS   订单类别: $OTYPE"
echo "  现有发货记录: id=$SHIP_ID  status=$SHIP_STATUS"

if [[ "$OTYPE" != "调货订单" ]]; then
  echo "[!] 警告：$CODE 订单类别不是「调货订单」（实际：$OTYPE）。请确认后再运行。"
  exit 1
fi

# ---- 决定动作 ----
if [[ "$SHIP_STATUS" == "shipped" ]]; then
  echo ""
  echo "[OK] $CODE 已是 shipped，无需处理。"
  exit 0
fi

if [[ "$SHIP_ID" == "NONE" ]]; then
  ACTION="INSERT"
  SQL="INSERT INTO shipments (project_id, status, shipped_at, created_at)
       VALUES ($PID, 'shipped', now(), now());"
else
  ACTION="UPDATE"
  SQL="UPDATE shipments SET status='shipped', shipped_at=COALESCE(shipped_at, now())
       WHERE project_id=$PID;"
fi

echo ""
echo "[STEP 2] 计划动作: $ACTION"
echo "  $SQL"

if [[ $DRY_RUN -eq 1 ]]; then
  echo ""
  echo "[DRY-RUN] 未写库。去掉 --dry-run 参数后重新运行即可正式执行。"
  exit 0
fi

echo ""
read -r -p "确认对 $CODE 执行 $ACTION（标记为已发货）？[y/N] " CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
  echo "已取消。"
  exit 0
fi

run_sql "$SQL"
echo "[OK] 已执行。"

echo ""
echo "[VERIFY] $CODE 当前发货记录："
run_sql "SELECT id, status, shipped_at FROM shipments WHERE project_id=$PID;"
echo ""
echo "========================================"
echo "  完成：$CODE 现已出现在物流发货部「已完成」"
echo "========================================"
