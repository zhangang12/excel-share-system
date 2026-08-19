"""实体解析与纵深查询 —— 智能体 v2 阶段一（见 docs/agent-architecture-v2.md）。

为什么需要这一层
----------------
v1 的 13 个工具里 12 个是同一个形状：「列一类」。后果是 `for _ in range(4)`
的多轮工具循环**实际永远只跑 1 轮** —— 第一轮把那类列完就没有下一步可走。
**没有 `find → get → agg` 的递进，ReAct 就无事可做。**

这个模块补上缺的两头：
  find_entity  模糊词 → 实体（「南京那个」「迈克斯」「诺朋」都能落到 id）
  get_*        单实体全景（一次给全，而不是让模型分五次去列五个清单）

服务对象（拿生产 30 天审计日志定的，不是拍脑袋）
------------------------------------------------
  杨坛   188 次操作：销售台账+订单 62 次(32%)、收货人 12、派活 8、请款审批 4
         → 需要 get_customer / get_project
  赵仁辉 635 次操作：**仓库 230 次(36%)**、物料字典 170 次、派活 43、采购开票 18
         → 需要 get_material；助手原来对他的覆盖率只有 5%

口径纪律（手册第三章原则一：单一访问路径）
------------------------------------------
行级隔离一律复用 `sales_router._all_view`，**不在这里重写任何权限谓词**。
`Project.is_deleted == False` 每个查询都要带 —— 生产上有 28 行幽灵数据。
"""
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models
from ..routers.sales_router import _all_view

_FIND_MAX = 8          # 候选给太多，模型反而挑不定
_DETAIL_MAX = 30       # 单实体纵深里每一类明细的上限


def _age(d: datetime | None) -> int | None:
    if not d:
        return None
    ts = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return max(0, (date.today() - ts.astimezone(timezone.utc).date()).days)


def _over_days(expected: str | None) -> int | None:
    """预计到货日到今天差几天（正数=已超期）。日期坏就返回 None，不猜。"""
    e = (expected or "").strip()
    if not e:
        return None
    try:
        return (date.today() - date.fromisoformat(e)).days
    except ValueError:
        return None


def _live(q):
    """挂上「项目未被软删」。生产上漏这个会多算 28 行幽灵数据。"""
    return q.join(models.Project, models.Project.id == models.SalesLedger.project_id
                  ).where(models.Project.is_deleted == False)  # noqa: E712


# ══════════════════════════ 交期口径（只此一处）══════════════════════════
# 交货日期存在 `Project.extra["__o__交货日期"]` —— 那是「项目一览」表里的列，
# 前缀 `__o__` 见 projects_router._extract_overview_meta。
# ⚠️ 别去 sales_ledger 找交货日期：那里只有 ship_date（物流回传的**实际**发货日）
#    和 balance_date（尾款到期日），都不是「答应客户哪天交货」。
DELIVER_KEY = "__o__交货日期"


def deliver_date(p: models.Project) -> str:
    return str(((p.extra or {}).get(DELIVER_KEY) or "")).strip()


def _days_left(d: str | None) -> int | None:
    """离交货日还有几天（负数=已过期）。日期坏就返回 None，不猜。"""
    s = (d or "").strip()
    if not s:
        return None
    try:
        return (date.fromisoformat(s) - date.today()).days
    except ValueError:
        return None


async def _match_projects(db: AsyncSession, code: str,
                          limit: int = _FIND_MAX) -> list[models.Project]:
    """按编号/名称找项目。**精确编号优先，否则返回全部模糊命中**。

    ⚠️ 这里绝不能「模糊匹配完取第一条」。生产上项目编号大量带字母后缀
    （2026-071A / 071B、043B~043E、045A/045B…），用户口语说「071」时
    `%071%` 会同时命中 A 和 B —— 取一条就等于**悄悄把另一个项目从答案里删掉**，
    而用户看到的是一份看起来完整的分析。实测就是这么漏掉 2026-071A 的。
    """
    c = (code or "").strip()
    if not c:
        return []
    base = select(models.Project).where(models.Project.is_deleted == False)  # noqa: E712
    exact = (await db.execute(base.where(func.lower(models.Project.code) == c.lower())
                              )).scalars().all()
    if exact:
        return list(exact)
    return list((await db.execute(
        base.where(or_(models.Project.code.ilike(f"%{c}%"),
                       models.Project.name.ilike(f"%{c}%")))
        .order_by(models.Project.code).limit(limit))).scalars().all())


# ══════════════════════════════ find ══════════════════════════════

_KIND_CN = {"project": "项目", "customer": "客户", "supplier": "供应商", "material": "物料"}
# 每一类该看哪几列。第一列由渲染层把「编号+名称」并成一格，所以这里可以多写一个。
_KIND_COLS = {
    "project":  ["code", "name", "status", "customer"],
    "customer": ["customer", "ledger_rows"],
    "supplier": ["supplier"],
    "material": ["item_name", "spec"],
}


def _find_items(hits: dict) -> tuple[list[dict], list[str]]:
    """把候选摊成可渲染的表。

    ⚠️ **只铺命中最多的那一类**，其余留在 `matches` 里让模型在结论句里带一句。
       混着铺的话，项目要「状态/客户」、物料要「规格」、客户要「台账行数」，
       列对不上，只能退化成一个「名称」列——那还不如不出表。
       实际用法也支持这么做：问「200L的设备」时命中的几乎全是项目。
    """
    if not hits:
        return [], []
    # 并列时项目优先——问「XXX是哪个编号」时想要的一定是项目
    kind = max(hits, key=lambda k: (len(hits[k]), k == "project"))
    rows = list(hits[kind])
    if kind == "project":
        # 在建的排前面。「5L的设备有几台」这类问法，已完成的那些只是背景，
        # 手机上一屏就几行，让已完成的占着前排等于把要紧的挤下去。
        rows.sort(key=lambda r: r.get("status") == "已完成")
    return rows, _KIND_COLS.get(kind, [])


async def find_entity(db: AsyncSession, current: models.User, q: str,
                      kind: str | None = None) -> dict:
    """模糊词 → 候选实体。这是「南京那个项目」能被理解的前提。

    kind 可限定 project/customer/supplier/material；不给就四类都找。
    **只返回候选，不猜**——同名多个时让模型/用户挑，猜错比问一句代价大得多。
    """
    text = (q or "").strip()
    if not text:
        return {"error": "要找什么？给个名字或编号"}
    like = f"%{text}%"
    out: dict[str, list] = {}

    if kind in (None, "project"):
        rows = (await db.execute(
            select(models.Project)
            .where(models.Project.is_deleted == False,  # noqa: E712
                   or_(models.Project.code.ilike(like), models.Project.name.ilike(like)))
            .order_by(models.Project.id.desc()).limit(_FIND_MAX))).scalars().all()
        # ⚠️ 带上 status 和客户。杨坛最高频的问法是「200L 的设备有哪几个编号」
        #    「5L 的设备有几台」——他真正要分的是**在建还是已完成**，
        #    以及是哪家的货。只给编号+名称，他还得再问一轮。
        cust: dict[int, str] = {}
        if rows:
            for pid, c in (await db.execute(
                    select(models.SalesLedger.project_id, models.SalesLedger.customer)
                    .where(models.SalesLedger.project_id.in_([p.id for p in rows]),
                           models.SalesLedger.customer != ""))).all():
                cust.setdefault(pid, c)
        out["project"] = [{"id": p.id, "code": p.code, "name": p.name,
                           "status": p.status, "customer": cust.get(p.id, "")}
                          for p in rows]

    if kind in (None, "customer"):
        # 客户在台账上是自由文本，没有独立主数据表 —— 只能去重取名
        rows = (await db.execute(
            _live(select(models.SalesLedger.customer, func.count().label("n"))
                  .where(models.SalesLedger.customer.ilike(like),
                         models.SalesLedger.customer != ""))
            .group_by(models.SalesLedger.customer)
            .order_by(func.count().desc()).limit(_FIND_MAX))).all()
        out["customer"] = [{"customer": r[0], "ledger_rows": r[1]} for r in rows]

    if kind in (None, "supplier"):
        rows = (await db.execute(
            select(models.Supplier).where(models.Supplier.name.ilike(like))
            .limit(_FIND_MAX))).scalars().all()
        out["supplier"] = [{"id": s.id, "supplier": s.name} for s in rows]

    if kind in (None, "material"):
        rows = (await db.execute(
            select(models.WhMaterial)
            .where(or_(models.WhMaterial.code.ilike(like),
                       models.WhMaterial.name.ilike(like),
                       models.WhMaterial.spec.ilike(like)))
            .limit(_FIND_MAX))).scalars().all()
        out["material"] = [{"id": m.id, "code": m.code, "item_name": m.name,
                            "spec": m.spec} for m in rows]

    hits = {k: v for k, v in out.items() if v}
    total = sum(len(v) for v in hits.values())
    items, cols = _find_items(hits)
    return {"query": text, "total": total, "matches": hits,
            # ⚠️ items/columns/count 是给渲染层的。没有它们的时候，
            #    「200L 的设备有哪几个编号」只能由模型一行行打字——实测 7 次问答
            #    里 6 次 rendered=false，全是文字墙。count 也必须给：
            #    以前不给，审计日志把答对了的问答统统记成「查到 0 条」，
            #    看日志的人会以为这个场景是坏的。
            "items": items, "columns": cols,
            "count": total, "shown": len(items),
            "hint": "一个都没找到，换个说法或直接给编号" if not total else None}


# ══════════════════════════════ get ══════════════════════════════

async def get_customer(db: AsyncSession, current: models.User, name: str) -> dict:
    """客户全景：这家客户的所有台账 + 回款到哪一步 + 有没有在拖。

    杨坛 32% 的操作在销售台账，这是他最需要「一次看全」的实体。
    ⚠️ 账龄只说「台账建了 N 天」——`ship_date` 生产上 0 条有值、
       `shipments.shipped_at` 90 条只填了 3 条，说「逾期 N 天」就是编。
    """
    nm = (name or "").strip()
    if not nm:
        return {"error": "要查哪个客户？"}
    q = _live(select(models.SalesLedger).where(models.SalesLedger.customer.ilike(f"%{nm}%")))
    if not _all_view(current):
        q = q.where(models.SalesLedger.sales_uid == current.id)
    leds = list((await db.execute(q)).scalars().all())
    if not leds:
        return {"customer": nm, "found": False,
                "hint": "没有这个客户的台账；可能是名字对不上，先用 find_entity 找"}

    pids = {l.project_id for l in leds}
    projs = {p.id: p for p in (await db.execute(select(models.Project).where(
        models.Project.id.in_(pids)))).scalars().all()}
    ships: dict[int, str] = {}
    for sh in (await db.execute(select(models.Shipment).where(
            models.Shipment.project_id.in_(pids))
            .order_by(models.Shipment.project_id, models.Shipment.id.desc()))).scalars().all():
        ships.setdefault(sh.project_id, sh.status or "")

    rows, unpaid, contract = [], 0.0, 0.0
    for l in leds:
        p = projs.get(l.project_id)
        due = float(l.ship_receivable or 0) + float(l.balance or 0)
        unpaid += due
        contract += float(l.amount or 0)
        rows.append({
            "project_code": p.code if p else f"#{l.project_id}",
            "project_name": (p.name if p else "") or "",
            "contract": float(l.amount or 0),
            "prepay": float(l.prepay or 0), "prepay_paid": bool(l.prepay_note),
            "before_ship": float(l.before_ship or 0), "before_ship_paid": bool(l.before_ship_note),
            "ship_receivable": float(l.ship_receivable or 0),
            "balance": float(l.balance or 0),
            "balance_date": l.balance_date or "",
            "ship_status": ships.get(l.project_id),
            "order_state": l.order_state,
            "ledger_age_days": _age(l.created_at),
        })
    rows.sort(key=lambda r: -(r["ship_receivable"] + r["balance"]))

    no_due = [r for r in rows if r["balance"] > 0 and not r["balance_date"].strip()]
    pending_ship = [r for r in rows if r["ship_receivable"] > 0 and r["ship_status"] == "pending"]
    return {
        "customer": nm, "found": True,
        "ledger_count": len(rows),
        "contract_total": round(contract, 2),
        "unpaid_total": round(unpaid, 2),
        "paid_ratio": round((contract - unpaid) / contract, 3) if contract else None,
        # 这两条是「该不该催」的判断依据，不是装饰
        "balance_without_due_date": len(no_due),
        "ship_receivable_but_not_shipped": len(pending_ship),
        "items": rows[:_DETAIL_MAX],
        "count": len(rows),
    }


# 部门/生产组的中文名。⚠️ 别把 "electric" 直接甩给用户看 ——
# 这些是数据库里的枚举值，不是人话。
_DEPT_CN = {"design": "设计", "electric": "电工", "produce": "生产"}
_GROUP_CN = {"sheetmetal": "钣金", "assembly": "装配", "sealing": "打胶"}
_ORDER_CN = {"pending_assign": "待分派", "assigned": "待接单",
             "in_progress": "进行中", "done": "已完成", "voided": "已作废",
             # 生产组任务用的是另一套：派下去了就是 dispatched
             "dispatched": "已派工"}


async def _project_snapshot(db: AsyncSession, p: models.Project) -> dict:
    """单个项目的全链路快照。get_project 与交期看板共用，**口径只有这一份**。"""
    led = (await db.execute(select(models.SalesLedger).where(
        models.SalesLedger.project_id == p.id)
        .order_by(models.SalesLedger.id).limit(1))).scalars().first()
    orders = list((await db.execute(select(models.DeptOrder).where(
        models.DeptOrder.project_id == p.id))).scalars().all())
    groups = list((await db.execute(select(models.ProduceGroupTask).where(
        models.ProduceGroupTask.project_id == p.id))).scalars().all())
    # ⚠️ PurchaseItem 上没有 project_id，关联字段是 **project_code**（字符串）。
    #    「未到货」的口径照抄 agent_router._po_arrival_overdue_rows：
    #    有预计到货日 且 arrival_date 为空 = 还没收货。别自创第二套口径。
    pos = list((await db.execute(select(models.PurchaseItem).where(
        models.PurchaseItem.project_code == p.code))).scalars().all())
    sh = (await db.execute(select(models.Shipment).where(
        models.Shipment.project_id == p.id)
        .order_by(models.Shipment.id.desc()).limit(1))).scalar_one_or_none()

    # ── 负责人姓名。⚠️ 生产实测：杨坛问过两次「2026-037 的电是谁做的」，
    #    而这里以前只给 dept/status/due_date，**根本答不了** —— 数据是有的
    #    （138 条部门单填了 worker_id），只是没取出来。
    uids = {o.worker_id for o in orders if o.worker_id} | \
           {g.worker_id for g in groups if g.worker_id}
    who: dict[int, str] = {}
    if uids:
        who = {u.id: (u.full_name or u.username)
               for u in (await db.execute(select(models.User).where(
                   models.User.id.in_(uids)))).scalars().all()}
    # 供应商名（采购明细里以前只给 supplier_id，一串数字对人没意义）
    sids = {i.supplier_id for i in pos if i.supplier_id}
    sup_name: dict[int, str] = {}
    if sids:
        sup_name = {s2.id: s2.name for s2 in (await db.execute(select(models.Supplier)
                    .where(models.Supplier.id.in_(sids)))).scalars().all()}

    today = date.today().isoformat()
    deliver = deliver_date(p)
    left = _days_left(deliver)
    # 作废的部门单不算在进度里 —— 2026-008 整个项目九张单全是 voided，
    # 不排掉的话它会以「全都没做完」的样子挂在最紧急的位置上
    live_orders = [o for o in orders if o.status != "voided"]
    po_pending = [i for i in pos if not (i.arrival_date or "").strip()]

    return {
        "project": p.code, "name": p.name, "found": True, "status": p.status,
        "deliver_date": deliver, "days_left": left,
        "ledger": None if not led else {
            "customer": led.customer, "contract": float(led.amount or 0),
            "prepay": float(led.prepay or 0), "before_ship": float(led.before_ship or 0),
            "ship_receivable": float(led.ship_receivable or 0),
            "balance": float(led.balance or 0), "balance_date": led.balance_date or "",
            "order_state": led.order_state,
        },
        "dept_orders": [{"dept": o.dept, "dept_name": _DEPT_CN.get(o.dept, o.dept),
                         "worker": who.get(o.worker_id or 0, ""),
                         "status": o.status, "due_date": o.due_date,
                         "overdue": bool(o.status == "in_progress" and o.due_date
                                         and o.due_date < today)}
                        for o in live_orders],
        "dept_overdue_count": sum(1 for o in live_orders if o.status == "in_progress"
                                  and o.due_date and o.due_date < today),
        "produce_groups": [{"group": g.group, "group_name": _GROUP_CN.get(g.group, g.group),
                            "worker": who.get(g.worker_id or 0, ""),
                            "status": g.status, "due_date": g.due_date} for g in groups],
        "produce_open_count": sum(1 for g in groups if g.status != "done"),
        "purchase_pending": [{"item_name": i.item_name, "spec": i.spec,
                              "supplier": sup_name.get(i.supplier_id or 0, ""),
                              "po_no": i.po_no,
                              "expected_arrival": i.expected_arrival,
                              "over_days": _over_days(i.expected_arrival)}
                             for i in po_pending][:_DETAIL_MAX],
        "purchase_pending_count": len(po_pending),
        "purchase_overdue_count": sum(1 for i in po_pending
                                      if (_over_days(i.expected_arrival) or -1) >= 0),
        "shipment_status": sh.status if sh else None,
        "shipment_receiver": (sh.receiver_name or "") if sh else None,
        **_diagnose(deliver, left, live_orders, orders, groups, po_pending, sh),
    }


def _short(text: str) -> str:
    """表格列用的短标签：去掉括号里的补充说明。

    手机上表格的第三列只有 ~34% 宽（375px 屏约 120px），一句
    「电工未完成（截止 2026-08-10）」会折成 4 行、把行高撑到 60px 以上。
    括号里的东西是补充，去掉不影响「卡在哪一环」这个判断。
    """
    t = (text or "").strip()
    for l, r in (("（", "）"), ("(", ")")):
        i = t.find(l)
        if i > 0:
            t = t[:i].strip()
    return t


def _diagnose(deliver: str, left: int | None, orders: list, all_orders: list,
              groups: list, po_pending: list, sh) -> dict:
    """卡在哪 + 交期风险。**这是这个功能的全部价值** —— 光列数据管理层自己也能看。

    ⚠️⚠️ **生产的截止日期就是项目一览里的交货日期**（业务口径，用户确认）。
       生产部门单的 `due_date` 只是「有人另外填了个更早的内部节点」，
       **没填不等于没有截止** —— 默认就按交货日倒推。
       早先把「produce 单没填 due_date」报成「盲区、算不出来」是**口径错的**：
       46 个在建项目里 produce 填了 due_date 的是 0 个，于是 39 个项目
       全被报成盲区，等于把一句正常的话说成了系统性缺陷。
    """
    blockers: list[str] = []
    risks: list[str] = []
    by_dept = {o.dept: o for o in orders}
    voided_depts = {o.dept for o in all_orders if o.status == "voided"}

    # 货已经发出去了：这个项目实际已经走完，剩下的只是状态没收尾。
    # ⚠️ 不先判这一条的话，2026-008 这种「九张部门单全作废 + 已发货」的项目
    #    会被报成「设计还没下单」，然后以 -181 天挂在最紧急的位置 —— 纯噪音，
    #    而且会把真正要盯的项目挤下去。
    if sh is not None and sh.status == "shipped":
        return {
            "blockers": ["已发货完毕，但项目状态还挂在「进行中」，该收尾了"],
            "blocked_at": "已发货，待收尾",
            "risks": ["项目已发货却仍是「进行中」，会一直占着在建列表和交期看板"],
            # 已发货的不是交期风险，是台账没收尾。看板要把它跟「真的赶不上」分开算，
            # 否则 2026-008 这种拖了 181 天的会一直占着「最急」第一位，
            # 把真正要盯的项目挤下去 —— 排第一的那条没人管，整张看板就没人看了。
            "shipped_not_closed": True,
        }

    for dept, label in (("design", "设计"), ("electric", "电工"), ("produce", "生产")):
        o = by_dept.get(dept)
        if o is None:
            blockers.append(f"{label}单已作废、没有重下" if dept in voided_depts
                            else f"{label}还没下单")
        elif o.status != "done":
            if o.due_date:
                due = f"（截止 {o.due_date}）"
            elif dept == "produce" and deliver:
                # 生产没单独填节点是常态，截止就是交货日 —— 别再写成「未设截止日期」，
                # 那会让人以为漏填了什么
                due = f"（按交货日 {deliver}）"
            else:
                due = ""
            blockers.append(f"{label}未完成{due}")

    open_groups = [g for g in groups if g.status != "done"]
    if open_groups:
        names = {"sheetmetal": "钣金", "assembly": "装配", "sealing": "打胶"}
        blockers.append("生产组未完工：" + "、".join(
            f"{names.get(g.group, g.group)}({g.status})" for g in open_groups))

    if po_pending:
        overdue = sum(1 for i in po_pending if (_over_days(i.expected_arrival) or -1) >= 0)
        blockers.append(f"采购未到货 {len(po_pending)} 项"
                        + (f"（其中 {overdue} 项已超期）" if overdue else ""))

    if sh is not None and sh.status == "pending":
        blockers.append("发货单还停在待发货")

    # ── 交期风险 ──
    if not deliver:
        risks.append("没填交货日期，算不出还剩多少天")
    elif left is None:
        risks.append(f"交货日期「{deliver}」不是合法日期，算不出剩余天数")
    elif left < 0:
        risks.append(f"交货日期已过 {-left} 天")
    elif left <= 7:
        risks.append(f"只剩 {left} 天")

    # ── 生产赶不赶得上：**基准是交货日期**，不是「有没有单独填 due_date」──
    prod = by_dept.get("produce")
    if prod is not None and prod.status != "done":
        prod_due = (prod.due_date or "").strip()
        group_dues = [g.due_date for g in open_groups if (g.due_date or "").strip()]
        inner = [d for d in ([prod_due] if prod_due else []) + group_dues if d]
        if left is not None and left < 0:
            risks.append(f"生产还没完成，而交货日已过 {-left} 天")
        elif left is not None and left <= 7:
            risks.append(f"生产还没完成，只剩 {left} 天到交货日")
        # 另外填了内部节点、而那个节点还晚于交货日 → 计划本身就交不了。
        # 这才是用户最初说的「生产截止日期没跟交货日期匹配上」的真实形态。
        # ⚠️ **只对还没到期的项目报**：已经过了交货日的，主风险就是「已经延期了」，
        #    再补一句「按计划也交不了」是废话，只会稀释真正的告警。
        #    这条信号的价值在**预警**，不在事后复述。
        if inner and deliver and left is not None and left >= 0:
            latest = max(inner)
            if latest > deliver:
                risks.append(f"生产内部节点 {latest} 晚于交货日 {deliver}，按计划就交不了")

    return {"blockers": blockers, "risks": risks,
            # ⚠️ **表格里的短标签**，不带括号补充。它会进一个 34% 宽的手机列，
            #    「电工未完成（截止 2026-08-10）」在 375px 下会折成 4 行、
            #    把行高撑到 60px 以上（实测）。完整描述留在 blockers 里，
            #    模型分析时照样看得到，不丢信息。
            "blocked_at": _short(blockers[0]) if blockers else None,
            "shipped_not_closed": False}


def _project_items(snaps: list[dict], detail: str) -> tuple[list[dict], list[str]]:
    """把项目快照摊成**可渲染的明细行**，并给出该显示哪几列。

    ⚠️⚠️ 为什么非做不可：`get_project` 以前返回的是**嵌套对象**，而渲染层只认
       `items` / `suppliers` / `rows` 这几个**列表**字段。结果是——
       杨坛最近 5 次问答里有 3 次 `rendered=false`，**最高频的「查某个项目」
       反而享受不到表格**，又退回成一堆文字。
       口径归工具：这里同时给 items 和 columns，渲染层照着摆就行。
    """
    multi = len(snaps) > 1
    rows: list[dict] = []
    if detail == "purchase":
        for s in snaps:
            for it in s["purchase_pending"]:
                r = dict(it)
                if multi:
                    # ⚠️ 别单开一列 project：渲染层会把「编号+名称」并成第一格，
                    #    3 列的预算就被吃掉一列，供应商直接被挤没（实测）。
                    r["item_name"] = s["project"] + " " + str(r.get("item_name") or "")
                rows.append(r)
        return rows, ["item_name", "supplier", "over_days"]

    # 默认：卡点清单。用户问「这个项目怎么样／卡在哪」时要看的就是这张表。
    for s in snaps:
        pre = (s["project"] + " ") if multi else ""
        groups = [g for g in s["produce_groups"] if g["status"] != "done"]
        for o in s["dept_orders"]:
            if o["status"] == "done":
                continue
            # 生产已经拆到组了就只报组，别「生产 / 生产·钣金 / 生产·装配」报三行
            if o["dept"] == "produce" and groups:
                continue
            rows.append({"stage": pre + o["dept_name"], "worker": o["worker"],
                         "status": ("逾期未完成" if o["overdue"]
                                    else _ORDER_CN.get(o["status"], o["status"])),
                         "due_date": o["due_date"]})
        for g in groups:
            rows.append({"stage": pre + "生产·" + g["group_name"], "worker": g["worker"],
                         "status": _ORDER_CN.get(g["status"], g["status"]),
                         "due_date": g["due_date"]})
        if s["purchase_pending_count"]:
            extra = ""
            if s["purchase_overdue_count"]:
                extra = "（" + str(s["purchase_overdue_count"]) + " 项已超期）"
            rows.append({"stage": pre + "采购", "worker": "",
                         "status": str(s["purchase_pending_count"]) + " 项未到货" + extra})
        if s["shipment_status"] == "pending":
            rows.append({"stage": pre + "发货", "worker": "", "status": "待发货"})
    return rows, ["stage", "worker", "status"]


async def get_project(db: AsyncSession, current: models.User, code: str,
                      detail: str = "blockers") -> dict:
    """项目全景：交期 + 台账 + 各部门任务 + 生产组 + 采购在途 + 发货 + 卡点判定。

    detail="blockers"（默认）明细是**卡点清单**（环节/负责人/状态）；
    detail="purchase" 明细是**采购未到货**（物料/供应商/超期）。

    ⚠️ **多个项目命中时全部返回，绝不静默取一条**。项目编号大量带字母后缀
       （071A/071B、043B~043E…），说「071」时两个都该出现在答案里。
    """
    c = (code or "").strip()
    if not c:
        return {"error": "要查哪个项目？给编号"}
    ps = await _match_projects(db, c)
    if not ps:
        return {"project": c, "found": False, "hint": "查无此项目，先用 find_entity 找"}

    snaps = [await _project_snapshot(db, p) for p in ps]
    items, cols = _project_items(snaps, detail)
    if len(snaps) == 1:
        return {**snaps[0], "items": items, "columns": cols,
                "count": len(items), "shown": len(items)}
    return {
        "query": c, "found": True, "matched_count": len(snaps),
        "items": items, "columns": cols, "count": len(items), "shown": len(items),
        # 这句是给模型看的硬约束：多命中时**每一个都要讲到**，不许只挑一个说
        "note": f"「{c}」命中 {len(snaps)} 个项目（"
                + "、".join(s["project"] for s in snaps)
                + "），下面逐个给出，回答时每个都要讲到，不要只说其中一个。",
        "projects": snaps,
    }


SIGN_KEY = "__o__签订日期"


async def sales_summary(db: AsyncSession, current: models.User,
                        months: int = 6, limit: int = 200) -> dict:
    """按月统计销售额（合同额），并给出本月/上月对比。

    ⚠️⚠️ **口径：按项目的「签订日期」归月，不是按台账录入时间。**
       `sales_ledger.created_at` 是**谁什么时候把它敲进系统**，跟生意什么时候做成
       没有关系 —— 生产上本月 created_at 是 0 行，照它算会得出「本月销售额 0」，
       而实际 7 月签了 16 个项目 113 万。这种错最糟：它给的是个看起来正常的数字。
       签订日期在 `Project.extra["__o__签订日期"]`（项目一览表的列）。

    ⚠️ 合同额为 0 的行会被算进「笔数」但不进金额 —— 生产上 128 行台账里有 21 行
       合同额是 0（毛利会被算成假亏损，见台账缺件）。这个数单独报出来，
       否则「这个月只有 3 万」可能只是有几行还没填。
    """
    rows = list((await db.execute(
        select(models.Project, models.SalesLedger)
        .join(models.SalesLedger, models.SalesLedger.project_id == models.Project.id)
        .where(models.Project.is_deleted == False)  # noqa: E712
    )).all())
    if not _all_view(current):
        rows = [(p, l) for p, l in rows if l.sales_uid == current.id]

    buckets: dict[str, dict] = {}
    no_sign = 0
    for p, l in rows:
        sign = str(((p.extra or {}).get(SIGN_KEY) or "")).strip()
        if len(sign) < 7:
            no_sign += 1
            continue
        m = sign[:7]                       # YYYY-MM
        b = buckets.setdefault(m, {"month": m, "count": 0, "amount": 0.0,
                                   "zero_amount": 0, "projects": []})
        b["count"] += 1
        amt = float(l.amount or 0)
        b["amount"] += amt
        if amt <= 0:
            b["zero_amount"] += 1
        b["projects"].append({"project": p.code, "name": p.name,
                              "customer": l.customer or "", "amount": amt,
                              "sign_date": sign})

    ms = sorted(buckets, reverse=True)[:max(1, months)]
    items = [buckets[m] for m in ms]
    for b in items:
        b["projects"].sort(key=lambda x: -x["amount"])
        b["projects"] = b["projects"][:_DETAIL_MAX]

    today = date.today()
    cur_m = today.strftime("%Y-%m")
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    cur_b, prev_b = buckets.get(cur_m), buckets.get(prev)
    return {
        "count": len(items), "shown": len(items), "today": today.isoformat(),
        "basis": "按项目签订日期归月（不是台账录入时间）",
        "this_month": cur_m,
        "this_month_amount": round((cur_b or {}).get("amount", 0.0), 2),
        "this_month_count": (cur_b or {}).get("count", 0),
        "last_month": prev,
        "last_month_amount": round((prev_b or {}).get("amount", 0.0), 2),
        "last_month_count": (prev_b or {}).get("count", 0),
        # 没签订日期的进不了任何月份 —— 必须说出来，否则合计对不上没人知道为什么
        "no_sign_date": no_sign,
        "items": [{"month": b["month"], "count": b["count"],
                   "amount": round(b["amount"], 2),
                   "zero_amount": b["zero_amount"]} for b in items],
        "months_detail": items,
    }


def _urgency(r: dict) -> str:
    """交期档位。顺序即紧急程度，渲染时按这个分段加小标题。"""
    if r["shipped_not_closed"]:
        return "已发货 · 只差收尾"
    left = r["days_left"]
    if left is None:
        return "没填交货日期"
    if left < 0:
        return "已过交货日"
    if left <= 7:
        return "7 天内交货"
    if left <= 30:
        return "30 天内交货"
    return "30 天以上"


async def project_progress(db: AsyncSession, current: models.User,
                           within_days: int | None = None,
                           include_overdue: bool = True,
                           limit: int = 200) -> dict:
    """在建项目交期看板：还剩多少天 + 卡在哪一环 + 交期风险。

    管理层要的那句话是「交货日期快到的项目到底卡在哪里」，所以这里**不只列日期**，
    每一行都带 blockers（卡点）和 risks（风险判定）。

    within_days=None 给全部在建；给 7 就是「7 天内要交的」。
    include_overdue=True 时已过期的一律带上 —— 已经过期的比「还剩 7 天」的更急，
    按天数过滤时把它们漏掉是最容易犯的错。
    """
    ps = list((await db.execute(select(models.Project).where(
        models.Project.is_deleted == False,  # noqa: E712
        models.Project.status == "进行中"))).scalars().all())

    rows: list[dict] = []
    for p in ps:
        s = await _project_snapshot(db, p)
        left = s["days_left"]
        if within_days is not None:
            if left is None:
                continue                      # 没交货日期的不进「N 天内」这类过滤
            if left > within_days:
                continue
            if left < 0 and not include_overdue:
                continue
        rows.append(s)

    # 排序三档：真正在做的按剩余天数升序（已过期最前）→ 没填交货日期的 →
    # 已发货只差收尾的排最后。**都不丢掉**，只是不让收尾类占住最急的位置。
    rows.sort(key=lambda r: (r["shipped_not_closed"],
                             r["days_left"] is None,
                             r["days_left"] if r["days_left"] is not None else 0))

    live = [r for r in rows if not r["shipped_not_closed"]]
    total = len(rows)
    shown = rows[:max(1, min(limit, 200))]
    return {
        "count": total, "shown": len(shown),
        "truncated": total > len(shown),
        "today": date.today().isoformat(),
        # ⚠️ 明确声明表格该显示哪三列。不声明的话渲染层按全局优先级挑，
        #    会挑到「客户 / 合同额」——这个场景要看的是**还剩多久、卡在哪**。
        "columns": ["project", "days_left", "blocked_at"],
        # 🆕 配一张发散条形图：中线是今天，左边超期、右边还剩。
        #    「哪些项目快到期了」这个问题，一张图比十几行数字快得多——
        #    这也是杨坛/赵仁辉两人都会点的「交期看板」。
        "chart": {"kind": "bar", "label": "project", "value": "days_left",
                  "title": "距交货日"},
        "summary": {
            "in_progress_total": len(ps),
            # ⚠️ overdue 只统计**还在做**的。已发货待收尾的单独一档 ——
            #    混在一起会让「已过期 N 个」这个数字虚高，而虚高的告警没人信。
            "overdue": sum(1 for r in live if r["days_left"] is not None
                           and r["days_left"] < 0),
            "due_7d": sum(1 for r in live if r["days_left"] is not None
                          and 0 <= r["days_left"] <= 7),
            "no_deliver_date": sum(1 for r in live if not r["deliver_date"]),
            # ⚠️ 早先这里是 `no_produce_due`（生产没填截止日期），**口径是错的** ——
            #    生产的截止本来就是交货日期，没单独填不是缺陷。换成真正要盯的两个数：
            #    生产还没完成而交货日已过 / 内部节点排到了交货日之后。
            "produce_overdue": sum(1 for r in live if any(
                "生产还没完成，而交货日已过" in x for x in r["risks"])),
            "produce_plan_conflict": sum(1 for r in live if any(
                "晚于交货日" in x for x in r["risks"])),
            "blocked_by_purchase": sum(1 for r in live if r["purchase_pending_count"]),
            "shipped_not_closed": sum(1 for r in rows if r["shipped_not_closed"]),
        },
        "items": [{
            "project": r["project"], "name": r["name"],
            "customer": (r["ledger"] or {}).get("customer") or "",
            "contract": float((r["ledger"] or {}).get("contract") or 0),
            "deliver_date": r["deliver_date"], "days_left": r["days_left"],
            # 分组用的档位。46 个项目拉平成一串等长的行，人得一行行数才知道哪些真急；
            # 分了档才扫得动。档名由代码给死，模型不参与命名，各次回答才一致。
            "urgency": _urgency(r),
            "blocked_at": r["blocked_at"],
            "blockers": r["blockers"], "risks": r["risks"],
            "purchase_pending": r["purchase_pending_count"],
            "purchase_overdue": r["purchase_overdue_count"],
            "dept_overdue": r["dept_overdue_count"],
            "produce_open": r["produce_open_count"],
            "shipment_status": r["shipment_status"],
            "shipped_not_closed": r["shipped_not_closed"],
        } for r in shown],
    }


async def get_supplier(db: AsyncSession, current: models.User, name: str) -> dict:
    """供应商画像：准时率、平均超期、涉及项目、在途。

    「哪家供应商靠不住」现在只能靠一次性的清单感觉，这里给可比的数。
    """
    nm = (name or "").strip()
    if not nm:
        return {"error": "要查哪个供应商？"}
    sup = (await db.execute(select(models.Supplier).where(
        models.Supplier.name.ilike(f"%{nm}%")).limit(1))).scalar_one_or_none()
    if not sup:
        return {"supplier": nm, "found": False, "hint": "查无此供应商，先用 find_entity 找"}

    items = list((await db.execute(select(models.PurchaseItem).where(
        models.PurchaseItem.supplier_id == sup.id))).scalars().all())
    today = date.today()
    on_time = late = 0
    late_days: list[int] = []
    pending = []
    for i in items:
        exp = (i.expected_arrival or "").strip()
        if not exp:
            continue
        try:
            ed = date.fromisoformat(exp)
        except ValueError:
            continue
        arrived = (i.arrival_date or "").strip()
        if arrived:
            # 有实际到货日就能真比一次准时与否；比不了的（日期格式坏）算准时，不编数
            try:
                on_time += 1 if date.fromisoformat(arrived) <= ed else 0
                if date.fromisoformat(arrived) > ed:
                    late += 1
                    late_days.append((date.fromisoformat(arrived) - ed).days)
            except ValueError:
                on_time += 1
        elif ed <= today:
            late += 1
            late_days.append((today - ed).days)
            pending.append({"name": i.item_name, "spec": i.spec, "expected_arrival": exp,
                            "over_days": (today - ed).days, "po_no": i.po_no,
                            "project_code": i.project_code})
    pending.sort(key=lambda x: -x["over_days"])
    total = on_time + late
    return {
        "supplier": sup.name, "found": True,
        "purchase_items": len(items),
        "on_time": on_time, "overdue": late,
        "on_time_rate": round(on_time / total, 3) if total else None,
        "avg_overdue_days": round(sum(late_days) / len(late_days), 1) if late_days else 0,
        "max_overdue_days": max(late_days) if late_days else 0,
        "items": pending[:_DETAIL_MAX], "count": len(pending),
    }


async def get_material(db: AsyncSession, current: models.User, q: str) -> dict:
    """物料全景：库存、库位、安全库存、近期出入库、在途采购。

    赵仁辉 36% 的操作在仓库，而 v1 一个仓库工具都没有（对他覆盖率 5%）。
    """
    text = (q or "").strip()
    if not text:
        return {"error": "要查哪个物料？给编码或名称"}
    m = (await db.execute(select(models.WhMaterial).where(
        or_(models.WhMaterial.code.ilike(f"%{text}%"),
            models.WhMaterial.name.ilike(f"%{text}%"),
            models.WhMaterial.spec.ilike(f"%{text}%"))).limit(1))).scalar_one_or_none()
    if not m:
        return {"material": text, "found": False, "hint": "查无此物料，先用 find_entity 找"}

    txns = list((await db.execute(select(models.WhTxn).where(
        models.WhTxn.material_id == m.id, models.WhTxn.is_reversal == False)  # noqa: E712
        .order_by(models.WhTxn.id.desc()))).scalars().all())
    stock = float(m.init_stock or 0)
    for t in txns:
        stock += float(t.qty or 0) if t.direction == "in" else -float(t.qty or 0)

    safety = float(m.safety_stock or 0)
    return {
        "material": m.name, "code": m.code, "spec": m.spec, "unit": m.unit, "found": True,
        "stock": round(stock, 3),
        "safety_stock": safety,
        # 低于安全库存是「要不要马上补货」的直接依据
        "below_safety": bool(safety and stock < safety),
        "shortfall": round(safety - stock, 3) if safety and stock < safety else 0,
        "location": m.location,
        "txn_count": len(txns),
        "recent_txns": [{"date": t.biz_date, "direction": t.direction, "qty": float(t.qty or 0),
                         "party": t.party, "project_id": t.project_id, "ref_no": t.ref_no}
                        for t in txns[:_DETAIL_MAX]],
    }


# ══════════════════════ 管理层下发的待办 ══════════════════════

_MTODO_CN = {"pending": "还没回复", "committed": "承诺了没做完", "done": "已完成"}


async def mgmt_todo_watch(db: AsyncSession, current: models.User,
                          limit: int = 30) -> dict:
    """管理层：我发出去的待办，谁没回、谁超期、谁申请顺延。

    **为什么值得单独做一个工具**：生产实测这是两位管理层唯一在持续用的管理动作
    （赵仁辉 30 条 / 杨坛 5 条，29 条填了截止日、9 条标了紧急）。
    但闭环断在三处：**7 条顺延申请挂着没批**、5 条承诺日已过、3 条根本没人回。
    这些散在电脑端的列表里，手机上一句话问不到。

    ⚠️ **只看自己发的**。不是权限问题（管理层本来互相可见），是相关性问题：
       赵仁辉的 30 条混进杨坛的 5 条里，两个人打开都得先筛一遍。
    """
    me = current.id
    rows = list((await db.execute(
        select(models.ManagementTodoTarget, models.ManagementTodo, models.User)
        .join(models.ManagementTodo,
              models.ManagementTodo.id == models.ManagementTodoTarget.todo_id)
        .join(models.User, models.User.id == models.ManagementTodoTarget.user_id)
        .where(models.ManagementTodo.created_by == me)
        .order_by(models.ManagementTodo.created_at.desc()))).all())

    today = date.today()
    items: list[dict] = []
    n_extend = n_overdue = n_silent = 0
    for tgt, todo, who in rows:
        if tgt.status == "done":
            continue                      # 做完的不用管，别占屏
        name = who.full_name or who.username
        # ⚠️ 逾期以**承诺日**为准；没承诺过才退回管理层设的截止日。
        #    反过来用会把「已经承诺了更晚日期」的人误报成超期。
        ref_date = tgt.committed_at or todo.due_date
        over = None
        if ref_date:
            try:
                over = (today - date.fromisoformat(ref_date)).days
            except (ValueError, TypeError):
                over = None
        pending_extend = tgt.extend_status == "pending" and bool(tgt.extend_to)
        if pending_extend:
            n_extend += 1
            state = f"申请顺延到 {tgt.extend_to}"
        elif tgt.status == "pending":
            n_silent += 1
            state = "还没回复"
        elif over is not None and over > 0:
            n_overdue += 1
            state = f"超 {over} 天"
        else:
            state = _MTODO_CN.get(tgt.status, tgt.status)
        items.append({
            "worker": name,
            # ⚠️ 键叫 todo 不叫 name：`name` 在渲染层的 _NAME_KEYS 里，
            #    会被当成「主体」并进第一列 —— 表头成了「名称」、人被挤到第二列。
            #    催人这个场景第一眼要看的是**谁**。
            "todo": _short(todo.title or "")[:20],
            "status": state,
            # target_id：批顺延、催办都按它走（**不是 todo_id**）
            "target_id": tgt.id,
            "over_days": over if (over or 0) > 0 else None,
            "due_date": ref_date,
            "urgent": todo.priority == "urgent",
        })

    # 要管的排前面：申请顺延 > 超期越久 > 没回复 > 其余
    def _rank(it: dict) -> tuple:
        return (0 if "顺延" in it["status"] else 1,
                -(it["over_days"] or 0),
                0 if it["status"] == "还没回复" else 1)

    items.sort(key=_rank)
    shown = items[:max(1, min(limit, 200))]
    return {
        "count": len(items), "shown": len(shown), "truncated": len(items) > len(shown),
        "summary": {"pending_extend": n_extend, "overdue": n_overdue,
                    "no_reply": n_silent, "open_total": len(items)},
        # 谁 · 什么事 · 什么状态 —— 催人时要的就这三样
        "columns": ["worker", "todo", "status"],
        "items": shown,
        "hint": (f"有 {n_extend} 条顺延申请等你批" if n_extend else None),
    }
