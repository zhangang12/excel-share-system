"""金额审计(2026-09-09)·历史数据修复脚本。**默认只读(dry-run)**，加 --apply 才写库。

用法（在生产服务器上、容器内跑）：
  docker exec -i -e PYTHONPATH=/app pms2_backend python - < ops/audit_money_backfill.py            # 预演
  docker exec -i -e PYTHONPATH=/app pms2_backend python - --apply < ops/audit_money_backfill.py    # 执行

两件事（都是把代码修复后的口径补到存量数据上，不改任何业务数字的"应有值"）：
  A. 未定价收货的入库流水 amount=0 → NULL。
     代码修复见 purchase_mgmt_router._sync_txn_amount：0 元被加权均价当真实样本，34 个物料均价被拉低
     （液压站 1700→850），库存金额/领料腿/未归集跟着偏低；cost_audit「无价入库」只认 NULL 查不出来。
     条件：direction='in' 且 purchase_item_id 非空 且 unit_price IS NULL 且 amount=0，且对应采购明细收货金额也为 0
     （收货金额>0 的属于 B）。
  B. 采购明细已补价/直接填了收货金额，但收货流水金额还停在旧值（#329 修复之前的存量，2026-07-22~29 为主）。
     对这些明细重跑 _sync_txn_amount。⚠️ 跳过「流水数量 ≠ 明细数量」的（#1350 等 5 条），那是老板待定的业务问题，
     不能由脚本决定；数量一致的才同步金额。

不动：请款/已付款、OA、售后、销售——审计没有发现这些表的存量数据需要改。
"""
import asyncio
import sys

from sqlalchemy import select, text

from app.database import SessionLocal
from app import models

APPLY = "--apply" in sys.argv


async def main():
    async with SessionLocal() as db:
        # ── A ──
        rows_a = (await db.execute(text("""
            select w.id from wh_txns w join purchase_items pi on pi.id = w.purchase_item_id
            where w.direction='in' and w.unit_price is null and w.amount = 0
              and coalesce(pi.received_amount, 0) = 0"""))).scalars().all()
        print(f"A. amount=0 且无价的采购入库流水：{len(rows_a)} 条 → 置 NULL")

        # ── B ──
        rows_b = (await db.execute(text("""
            with t as (
              select purchase_item_id,
                     sum(coalesce(amount,0)) in_amt, sum(qty) in_qty, count(*) n
              from wh_txns where purchase_item_id is not null and not is_reversal and not reversed and direction='in'
              group by 1)
            select pi.id, pi.po_no, pi.item_name, pi.qty, pi.unit_price, pi.received_amount, t.in_amt, t.in_qty, t.n
            from t join purchase_items pi on pi.id = t.purchase_item_id
            where abs(t.in_amt - coalesce(pi.received_amount,0)) > 0.01
            order by pi.id"""))).all()
        fix_b, skip_b = [], []
        for r in rows_b:
            iid, po, name, qty, price, recv, in_amt, in_qty, n = r
            if n != 1 or qty is None or abs(float(in_qty) - float(qty)) > 1e-6:
                skip_b.append((iid, po, name, qty, in_qty, recv, in_amt))
            else:
                fix_b.append((iid, po, name, recv, in_amt))
        print(f"B. 收货流水金额 ≠ 明细收货金额：{len(rows_b)} 条；数量一致可同步 {len(fix_b)} 条，数量不一致跳过 {len(skip_b)} 条")
        for x in fix_b:
            print(f"   同步  #{x[0]} {x[1]} {x[2]}: 流水 {x[4]:.2f} → 明细收货金额 {x[3]:.2f}")
        for x in skip_b:
            print(f"   跳过  #{x[0]} {x[1]} {x[2]}: 明细数量 {x[3]} vs 流水数量 {x[4]}（收货 {x[5]} / 流水金额 {x[6]}）→ 待老板决定")

        if not APPLY:
            print("\n(dry-run，未写库；确认无误后加 --apply)")
            return

        if rows_a:
            await db.execute(text("update wh_txns set amount = NULL where id = any(:ids)"), {"ids": list(rows_a)})
        from app.routers.purchase_mgmt_router import _sync_txn_amount
        for x in fix_b:
            item = (await db.execute(select(models.PurchaseItem).where(models.PurchaseItem.id == x[0]))).scalar_one()
            await _sync_txn_amount(db, item)
        await db.commit()
        await db.execute(text("""insert into audit_logs(user_id, username, action, target_type, detail, created_at)
                                 values (NULL, 'ops', 'audit_money_backfill', 'wh_txn', :d, now())"""),
                         {"d": f"A: {len(rows_a)} 条 amount 0→NULL；B: 同步 {len(fix_b)} 条流水金额，跳过 {len(skip_b)} 条数量不一致"})
        await db.commit()
        print(f"\n✅ 已执行：A {len(rows_a)} 条，B {len(fix_b)} 条")


asyncio.run(main())
