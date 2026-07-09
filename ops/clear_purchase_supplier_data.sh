#!/usr/bin/env bash
# ============================================================================
# 清空「采购明细 + 供应商资料」及其全部关联数据（生产 Postgres）。
#
# 会清空：供应商、采购明细、请款单/审批记录、请款单-明细关联、供应商期初余额、
#         收货凭证附件、采购收货自动入库流水(库存相应减少)、采购申请/申请行。
#         供应商账目是实时计算的，随之变空。
# 不影响：物料主数据、手工出入库流水、项目/清单、采购自定义字段配置、用户/权限。
#
# 用法（在服务器项目目录）：
#   bash ops/clear_purchase_supplier_data.sh          # 交互确认后执行
#   bash ops/clear_purchase_supplier_data.sh --yes    # 跳过确认(慎用)
#
# 执行前自动 pg_dump 全库备份到当前目录。**删除不可逆**——务必确认备份成功。
# 整个删除是单事务(all-or-nothing)；中途报错自动回滚，数据保持原样。
# ============================================================================
set -euo pipefail

CONTAINER="${PG_CONTAINER:-pms2_postgres}"
ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

# 说明：psql/pg_dump 均使用容器内环境变量($POSTGRES_USER/$POSTGRES_DB)，无需在外部提供密码

echo "==> 检查数据库容器：$CONTAINER"
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "❌ 找不到运行中的容器 '$CONTAINER'。若容器名不同，请用 PG_CONTAINER=xxx 覆盖。" >&2
  exit 1
fi

echo "==> 当前数据量（将被清空的表）："
docker exec -i "$CONTAINER" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT 'suppliers 供应商' AS 表, count(*) AS 行数 FROM suppliers
UNION ALL SELECT 'purchase_items 采购明细', count(*) FROM purchase_items
UNION ALL SELECT 'payment_requests 请款单', count(*) FROM payment_requests
UNION ALL SELECT 'payment_request_items 请款明细', count(*) FROM payment_request_items
UNION ALL SELECT 'supplier_opening_balances 期初余额', count(*) FROM supplier_opening_balances
UNION ALL SELECT 'attachments(收货凭证)', count(*) FROM attachments WHERE biz_type='receipt_doc'
UNION ALL SELECT 'wh_txns(采购入库流水)', count(*) FROM wh_txns WHERE purchase_item_id IS NOT NULL
UNION ALL SELECT 'purchase_requests 采购申请', count(*) FROM purchase_requests
UNION ALL SELECT 'purchase_request_lines 申请行', count(*) FROM purchase_request_lines;
SQL

# ---- 备份（不可逆前置保护） ----
BACKUP="pms_backup_$(date +%Y%m%d_%H%M%S).sql"
echo "==> 备份整库到 ./$BACKUP ..."
docker exec "$CONTAINER" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$BACKUP"
if [ ! -s "$BACKUP" ]; then
  echo "❌ 备份失败或为空，已中止（未做任何删除）。" >&2
  rm -f "$BACKUP"
  exit 1
fi
echo "✅ 备份完成：$(ls -lh "$BACKUP" | awk '{print $5, $9}')"

# ---- 二次确认 ----
if [ "$ASSUME_YES" -ne 1 ]; then
  echo
  echo "⚠️  即将永久清空上述数据（不可逆）。已备份到 $BACKUP。"
  printf '如确认清空，请输入大写 CLEAR 回车：'
  read -r ans
  if [ "$ans" != "CLEAR" ]; then
    echo "已取消，未做任何删除。"
    exit 0
  fi
fi

# ---- 单事务清空（按外键依赖顺序） ----
echo "==> 执行清空..."
docker exec -i "$CONTAINER" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
BEGIN;
DELETE FROM payment_request_items;                         -- 请款单-明细关联
DELETE FROM payment_requests;                              -- 请款单/审批记录
DELETE FROM supplier_opening_balances;                     -- 供应商期初余额
DELETE FROM attachments WHERE biz_type = 'receipt_doc';    -- 收货凭证附件
DELETE FROM wh_txns WHERE purchase_item_id IS NOT NULL;    -- 采购收货自动入库流水(库存相应减少)
DELETE FROM purchase_request_lines;                        -- 采购申请-行
DELETE FROM purchase_requests;                             -- 采购申请
DELETE FROM purchase_items;                                -- 采购明细
DELETE FROM suppliers;                                     -- 供应商资料
COMMIT;
SQL

# ---- 校验 ----
echo "==> 校验（应全为 0）："
docker exec -i "$CONTAINER" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT (SELECT count(*) FROM suppliers)          AS suppliers,
       (SELECT count(*) FROM purchase_items)     AS items,
       (SELECT count(*) FROM payment_requests)   AS pay_reqs,
       (SELECT count(*) FROM purchase_requests)  AS purch_reqs;
SQL

echo "✅ 清空完成。备份文件：$BACKUP（如需回滚：docker exec -i $CONTAINER sh -c 'psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\"' < $BACKUP）"
echo "   前端刷新即可看到已清空。"
