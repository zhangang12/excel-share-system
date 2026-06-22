#!/bin/bash
# 清空仓库数据：删除全部「物料档案(wh_materials)」与「出入库记录(wh_txns)」，使仓库回到全空状态。
# 用法:
#   bash ops/clear-warehouse.sh --dry-run   # 仅预览当前条数，不删
#   bash ops/clear-warehouse.sh             # 交互确认后执行
#   bash ops/clear-warehouse.sh --yes       # 跳过确认直接执行（慎用）
#
# 说明：
# - 不可逆。删除前已自动 pg_dump 备份到 /backup（与 backup.sh 同口径，可回滚）。
# - 仅清仓库两张表；不动发货清单附件、不动项目/订单等其它数据。
# - wh_txns 含自引用(冲红单 reversal_of)与对 wh_materials 的外键，故先删 txns 再删 materials。

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
echo "  清空仓库数据（物料档案 + 出入库记录）"
[[ $DRY_RUN -eq 1 ]] && echo "  模式: DRY-RUN（仅预览，不删）" || echo "  模式: 正式执行"
echo "========================================"

MAT_CNT=$(run_sql "SELECT count(*) FROM wh_materials;")
TXN_CNT=$(run_sql "SELECT count(*) FROM wh_txns;")
echo "  当前物料档案 wh_materials : ${MAT_CNT:-?} 条"
echo "  当前出入库记录 wh_txns    : ${TXN_CNT:-?} 条"
echo ""

if [[ $DRY_RUN -eq 1 ]]; then
  echo "DRY-RUN 结束，未做任何修改。"
  exit 0
fi

if [[ "${MAT_CNT:-0}" == "0" && "${TXN_CNT:-0}" == "0" ]]; then
  echo "仓库已是空的，无需清理。"
  exit 0
fi

if [[ $ASSUME_YES -ne 1 ]]; then
  read -r -p "确认永久删除以上全部仓库数据？此操作不可逆。输入 yes 继续：" ans
  [[ "$ans" == "yes" ]] || { echo "已取消。"; exit 1; }
fi

# 删除前备份（优先复用 backup.sh；否则直接 pg_dump 到 /backup）
echo ""
echo "[1/2] 删除前备份数据库 ..."
if [[ -f ops/backup.sh ]]; then
  bash ops/backup.sh || echo "  ⚠ backup.sh 执行异常，继续前请确认已有备份"
else
  TS=$(date +%Y%m%d_%H%M%S)
  docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres \
    pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "/backup/pms-db-before-clear-warehouse-${TS}.sql.gz" \
    && echo "  → 备份到 /backup/pms-db-before-clear-warehouse-${TS}.sql.gz"
fi

echo ""
echo "[2/2] 清空 wh_txns 与 wh_materials（单事务）..."
run_sql "BEGIN; DELETE FROM wh_txns; DELETE FROM wh_materials; COMMIT;" >/dev/null

NEW_MAT=$(run_sql "SELECT count(*) FROM wh_materials;")
NEW_TXN=$(run_sql "SELECT count(*) FROM wh_txns;")
echo ""
echo "✅ 完成。现物料档案=${NEW_MAT:-?} 条，出入库记录=${NEW_TXN:-?} 条。"
echo "   仓库页面刷新后即为全空（库存汇总同步归零）。"
