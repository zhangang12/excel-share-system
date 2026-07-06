#!/bin/bash
# 清空「采购明细 + 仓库」数据，回到全空状态（供试运行数据清理用）。
# 用法:
#   bash ops/clear-purchase-warehouse.sh --dry-run   # 仅预览当前条数，不删
#   bash ops/clear-purchase-warehouse.sh             # 交互确认后执行
#   bash ops/clear-purchase-warehouse.sh --yes       # 跳过确认直接执行（慎用）
#
# 会清空的表（按外键依赖顺序，单事务）：
#   payment_request_items  请款单-明细关联
#   payment_requests       请款单
#   wh_txns                出入库记录
#   wh_materials           物料档案
#   purchase_items         采购明细
#
# 不动：供应商(suppliers)、供应商期初(supplier_opening_balances)、项目/订单、自定义字段定义、物料字典等。
# 不可逆；删除前自动 pg_dump 备份到 /backup（可回滚）。

set -euo pipefail

DRY_RUN=0
ASSUME_YES=0
for arg in "$@"; do
  [[ "$arg" == "--dry-run" ]] && DRY_RUN=1
  [[ "$arg" == "--yes" || "$arg" == "-y" ]] && ASSUME_YES=1
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

DB_USER=$(grep -E '^POSTGRES_USER=' .env.prod 2>/dev/null | cut -d= -f2 || echo "pms_prod")
DB_NAME=$(grep -E '^POSTGRES_DB=' .env.prod 2>/dev/null | cut -d= -f2 || echo "pms")
DB_USER="${DB_USER:-pms_prod}"
DB_NAME="${DB_NAME:-pms}"

run_sql() {
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
    psql -U "$DB_USER" "$DB_NAME" -t -A -c "$1" 2>/dev/null | grep -v '^$' || true
}

echo "========================================"
echo "  清空 采购明细 + 仓库 数据"
[[ $DRY_RUN -eq 1 ]] && echo "  模式: DRY-RUN（仅预览，不删）" || echo "  模式: 正式执行"
echo "========================================"

PI_CNT=$(run_sql "SELECT count(*) FROM purchase_items;")
PR_CNT=$(run_sql "SELECT count(*) FROM payment_requests;")
PRI_CNT=$(run_sql "SELECT count(*) FROM payment_request_items;")
MAT_CNT=$(run_sql "SELECT count(*) FROM wh_materials;")
TXN_CNT=$(run_sql "SELECT count(*) FROM wh_txns;")
echo "  采购明细 purchase_items        : ${PI_CNT:-?} 条"
echo "  请款单 payment_requests        : ${PR_CNT:-?} 条"
echo "  请款单明细 payment_request_items: ${PRI_CNT:-?} 条"
echo "  物料档案 wh_materials          : ${MAT_CNT:-?} 条"
echo "  出入库记录 wh_txns             : ${TXN_CNT:-?} 条"
echo ""

if [[ $DRY_RUN -eq 1 ]]; then
  echo "DRY-RUN 结束，未做任何修改。"
  exit 0
fi

TOTAL=$(( ${PI_CNT:-0} + ${PR_CNT:-0} + ${PRI_CNT:-0} + ${MAT_CNT:-0} + ${TXN_CNT:-0} ))
if [[ "$TOTAL" == "0" ]]; then
  echo "以上数据已是空的，无需清理。"
  exit 0
fi

if [[ $ASSUME_YES -ne 1 ]]; then
  read -r -p "确认永久删除以上全部「采购明细 + 仓库」数据？此操作不可逆。输入 yes 继续：" ans
  [[ "$ans" == "yes" ]] || { echo "已取消。"; exit 1; }
fi

echo ""
echo "[1/2] 删除前备份数据库 ..."
if [[ -f ops/backup.sh ]]; then
  bash ops/backup.sh || echo "  ⚠ backup.sh 执行异常，继续前请确认已有备份"
else
  TS=$(date +%Y%m%d_%H%M%S)
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
    pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "/backup/pms-db-before-clear-purchase-warehouse-${TS}.sql.gz" \
    && echo "  → 备份到 /backup/pms-db-before-clear-purchase-warehouse-${TS}.sql.gz"
fi

echo ""
echo "[2/2] 按外键依赖顺序清空（单事务）..."
run_sql "BEGIN;
  DELETE FROM payment_request_items;
  DELETE FROM payment_requests;
  DELETE FROM wh_txns;
  DELETE FROM wh_materials;
  DELETE FROM purchase_items;
COMMIT;" >/dev/null

echo ""
echo "✅ 完成。现各表条数："
echo "  purchase_items         = $(run_sql 'SELECT count(*) FROM purchase_items;') 条"
echo "  payment_requests       = $(run_sql 'SELECT count(*) FROM payment_requests;') 条"
echo "  wh_materials           = $(run_sql 'SELECT count(*) FROM wh_materials;') 条"
echo "  wh_txns                = $(run_sql 'SELECT count(*) FROM wh_txns;') 条"
echo "   刷新采购明细/仓库页面即为全空（库存汇总、供应商账目同步归零）。"
