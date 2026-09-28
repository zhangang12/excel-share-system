"""🆕 2026-09-29 应付账期的口径 —— 资金面板与采购「应付到期」表共用这一份。

为什么单独一个文件：同一个「这笔钱哪天该付、还欠多少」，财务在资金面板上看一个数，
采购在应付到期表里看另一个数，两边对不上的时候谁也说不清哪个对。
所以口径只写在这里，两处都来调，别在各自的文件里再抄一遍。

口径（老板 2026-09-29 拍板：**从到货那天算**）：
  · 欠多少 = 收货金额 − 已付金额（明细级）。已付金额由付款登记回写，见 purchase_mgmt_router。
  · 哪天该付 = 到货日期 + 供应商「账期天数」（Supplier.credit_days）。
  · 供应商没填账期天数 → 算不出到期日，归「账期未填」，不猜。
    （不把「结算方式=现金」当成 0 天：那样上百条现金单会一下子全成「已过期」，
      而它们多半是当场结清、只是付款没回写 —— 猜错比不给更糟。）
为什么不按到票：生产上已到货的明细 1886 条，填了到票日期的只有 374 条，按票算大部分单子没有到期日。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


def is_before_opening(item, ob) -> bool:
    """这条明细是不是已经含在供应商「期初余额」里了（下单日期 ≤ 期初日期）。

    老板 2026-09-09 定的应付口径：期初之前的明细**不再累计**，否则重复 ——
    腾丰就因此多算了 ¥12,492。供应商账目 / 对账单 / 汇总报表 / 明细页欠款早就按这条走
    （purchase_mgmt_router._ap_bucket，现在也改成调这里），
    但**资金面板一直没用它**，那 ¥12,492 在资金面板里还在重复算（2026-09-29 查出，一并修掉）。
    """
    return bool(ob is not None and getattr(ob, "balance_date", None) and item.delivery_date
                and item.delivery_date <= ob.balance_date)


async def opening_balances(db) -> dict:
    """供应商 id → 期初余额行。算应付的地方都要先取这张表，用 is_before_opening 剔掉期初已含的。"""
    from sqlalchemy import select
    from . import models
    return {ob.supplier_id: ob for ob in
            (await db.execute(select(models.SupplierOpeningBalance))).scalars().all()}


def item_outstanding(item) -> float:
    """这条采购明细还欠供应商多少。"""
    return round((item.received_amount or 0) - (item.paid_amount or 0), 2)


def item_due_date(item, supplier) -> Optional[date]:
    """这条明细哪天该付。到货日期或账期天数缺一个就返回 None（不猜）。"""
    if not item.arrival_date or supplier is None or supplier.credit_days is None:
        return None
    try:
        return date.fromisoformat(item.arrival_date) + timedelta(days=supplier.credit_days)
    except ValueError:
        return None       # 到货日期格式坏了：算不出来，也不猜


def due_bucket(due: Optional[date], today: date) -> str:
    """分档：overdue 已过期 / week 7 天内 / month 30 天内 / later 更晚 / nocredit 账期未填。"""
    if due is None:
        return "nocredit"
    left = (due - today).days
    if left < 0:
        return "overdue"
    if left <= 7:
        return "week"
    if left <= 30:
        return "month"
    return "later"
