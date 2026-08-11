"""🆕 v3 物流发货部（M08）：发货看板 + D5 发货闸门 + 确认发货回传销售台账。

- 看板九列：项目/名称/设计资料/电工资料/生产状态(E3 无产物)/仓库发货清单/收货信息/状态/闸门
- D5 闸门：该项目「已下单(非作废)」任务全部完成且至少一单才可发；未下单部门不阻塞
- E1 一项目一次发货；E2 收货信息销售录入为权威、物流可修正（P-23 留痕）
- 确认发货：必传发货单 → status=shipped → 回写 sales_ledger.ship_date（销售台账只读列）
- 存量兜底：零任务单的存量项目闸门不通过，管理层可 force 强制发货
"""
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from ..database import get_db
from .. import models, schemas
from ..deps import get_current_user, require_roles
from ..dept_config import DEPTS
from ..notify import push_message
from ..utils import write_audit
from .attachments_router import save_upload
from .projects_router import OVERVIEW_KEY_PREFIX

router = APIRouter(prefix="/api/logistics", tags=["物流发货部"])


class DeptState(BaseModel):
    state: str   # none 未下单 / doing 进行中 / done 已完成
    label: str


class BoardRow(BaseModel):
    id: int
    project_id: int
    code: str
    name: str
    status: str                      # pending / shipped
    design_files: list[schemas.AttachmentOut] = []
    electric_files: list[schemas.AttachmentOut] = []
    produce_state: DeptState
    design_state: DeptState
    electric_state: DeptState
    ship_list_files: list[schemas.AttachmentOut] = []
    packlist_status: str = "none"    # 🆕 发货清单：none 未推送 / requested 待仓库备货 / ready 已备货完成
    receiver_name: Optional[str] = None
    receiver_company: Optional[str] = None
    receiver_phone: Optional[str] = None
    receiver_addr: Optional[str] = None
    ship_doc_name: Optional[str] = None
    ship_doc_id: Optional[int] = None
    shipped_at: Optional[datetime] = None
    # 🆕 #201 物料运输费
    freight_cost: Optional[float] = None
    freight_payer: Optional[str] = None
    freight_note: Optional[str] = None
    can_ship: bool = False
    gate_missing: list[str] = []     # 闸门缺口部门名


class ReceiverIn(BaseModel):
    name: str = ""
    company: str = ""
    phone: str = ""
    addr: str = ""


def _dept_state(orders: list[models.DeptOrder], dept: str) -> DeptState:
    os_ = [o for o in orders if o.dept == dept and o.status != "voided"]
    if not os_:
        return DeptState(state="none", label="未下单")
    if all(o.status == "done" for o in os_):
        return DeptState(state="done", label="已完成")
    return DeptState(state="doing", label="进行中")


def _gate(orders: list[models.DeptOrder]) -> tuple[bool, list[str]]:
    """D5：已下单(非作废)任务全 done 且至少一单。返回 (可发, 缺口部门名)。"""
    active = [o for o in orders if o.status != "voided"]
    if not active:
        return False, ["未下任何任务单"]
    missing = []
    for dept, cfg in DEPTS.items():
        ds = [o for o in active if o.dept == dept]
        if ds and not all(o.status == "done" for o in ds):
            missing.append(cfg["name"])
    return (not missing), missing


@router.get("/board", response_model=List[BoardRow])
async def board(
    year: Optional[str] = None,
    ship_status: Optional[str] = None,
    proj_status: Optional[str] = None,
    current: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """发货看板（物流/管理层；其它角色只读不限制——数据无敏感金额）。

    🆕 2026-08-11 筛选口径改为**发货状态**（已发货 / 未发货 / 全部）。
    这是发货看板，物流关心的是发没发货，不是项目立项状态。

    ⚠️ 旧口径会漏单：原「进行中」= 未发货 **且** 项目状态≠已完成。
       生产上有 27 张「未发货、但项目被手工标成了已完成」的单，
       「进行中」和「已完成」两个筛选都看不到它们，只有「全部」才露出来——
       而**运费就是在这张表上录的**，看不到就录不进成本（见反馈#364）。
       新口径只看 `Shipment.status`，这 27 张回到「未发货」里。

    `proj_status` 是旧客户端还在发的参数，映射到新口径后继续可用
    （进行中→未发货、已完成→已发货）；不映射的话没升级的客户端筛选会**静默失效**。
    """
    if not ship_status and proj_status:
        ship_status = {"进行中": "未发货", "已完成": "已发货"}.get(proj_status)

    # 统一从 Shipment 出发。原来「已完成」单独走一条以 Project 为主的路径，
    # 但它的 WHERE 要求项目必须有 shipped 的发货单，所以那条路径里
    # 「项目没有 Shipment」的兜底分支永远走不到，纯属死代码，一并去掉。
    ship_q = select(models.Shipment).join(models.Project).where(
        models.Project.is_deleted == False)  # noqa: E712
    if year:
        ship_q = ship_q.where(models.Project.code.like(f"{year}-%"))
    if ship_status == "已发货":
        ship_q = ship_q.where(models.Shipment.status == "shipped")
    elif ship_status == "未发货":
        ship_q = ship_q.where(models.Shipment.status != "shipped")
    res = await db.execute(ship_q.order_by(models.Project.code.desc()).limit(300))
    ships = [s for s in res.scalars().all()
             if not str((s.project.extra or {}).get("__o__销售") or "").startswith("备机")]
    pids = [s.project_id for s in ships]
    if not pids:
        return []
    ships_by_pid: dict[int, models.Shipment] = {s.project_id: s for s in ships}

    # 任务单批量
    res = await db.execute(select(models.DeptOrder).where(
        models.DeptOrder.project_id.in_(pids)))
    orders_by_pid: dict[int, list[models.DeptOrder]] = {}
    order_ids = []
    order_dept: dict[int, str] = {}
    for o in res.scalars().all():
        orders_by_pid.setdefault(o.project_id, []).append(o)
        order_ids.append(o.id)
        order_dept[o.id] = o.dept

    # 产物附件批量（设计/电工完成产物 → 物流资料列）
    # 🆕 #303/#294 上传/推送分离：只显示已推送的，待推送的电路图/图纸不进物流资料列
    files_by_pid_dept: dict[tuple[int, str], list] = {}
    if order_ids:
        res = await db.execute(select(models.Attachment).where(
            models.Attachment.biz_type == "order_output",
            models.Attachment.pushed == True,
            models.Attachment.biz_id.in_(order_ids)))
        for a in res.scalars().all():
            d = order_dept.get(a.biz_id)
            if d in ("design", "electric"):
                files_by_pid_dept.setdefault((a.project_id, d), []).append(
                    schemas.AttachmentOut.model_validate(a))

    # 仓库发货清单（M07 上传 biz_type=ship_list）
    res = await db.execute(select(models.Attachment).where(
        models.Attachment.biz_type == "ship_list",
        models.Attachment.project_id.in_(pids)))
    shiplist_by_pid: dict[int, list] = {}
    for a in res.scalars().all():
        shiplist_by_pid.setdefault(a.project_id, []).append(
            schemas.AttachmentOut.model_validate(a))

    # 发货单附件名
    doc_ids = [s.ship_doc_file_id for s in ships_by_pid.values() if s.ship_doc_file_id]
    doc_names: dict[int, str] = {}
    if doc_ids:
        res = await db.execute(select(models.Attachment).where(
            models.Attachment.id.in_(doc_ids)))
        doc_names = {a.id: a.name for a in res.scalars().all()}

    rows = []
    for s in ships_by_pid.values():
        orders = orders_by_pid.get(s.project_id, [])
        can, missing = _gate(orders)
        rows.append(BoardRow(
            id=s.id, project_id=s.project_id,
            code=s.project.code, name=s.project.name, status=s.status,
            design_files=files_by_pid_dept.get((s.project_id, "design"), []),
            electric_files=files_by_pid_dept.get((s.project_id, "electric"), []),
            design_state=_dept_state(orders, "design"),
            electric_state=_dept_state(orders, "electric"),
            produce_state=_dept_state(orders, "produce"),
            ship_list_files=shiplist_by_pid.get(s.project_id, []),
            packlist_status=s.packlist_status,
            receiver_name=s.receiver_name, receiver_company=s.receiver_company,
            receiver_phone=s.receiver_phone, receiver_addr=s.receiver_addr,
            ship_doc_name=doc_names.get(s.ship_doc_file_id),
            ship_doc_id=s.ship_doc_file_id,
            shipped_at=s.shipped_at,
            freight_cost=s.freight_cost, freight_payer=s.freight_payer, freight_note=s.freight_note,
            can_ship=(s.status == "pending" and can),
            gate_missing=missing if s.status == "pending" else [],
        ))
    return rows


@router.get("/pending-count")
async def pending_count(
    _: models.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # ⚠️ 只数「进行中」项目的待发货。2026-08-11 起存量已完成项目也会补出 pending 的
    #   Shipment 行（让它们在看板上可见、能录运费），但那些是历史补录、不是今天要干的活。
    #   角标是「你还有几件事要做」，混进 37 条历史项目就没人看了。
    #   看板要看全部：把筛选切到「全部」或「未发货」。
    res = await db.execute(
        select(func.count(models.Shipment.id)).join(models.Project).where(
            models.Shipment.status == "pending",
            models.Project.is_deleted == False,  # noqa: E712
            models.Project.status == "进行中",
        )
    )
    return {"count": res.scalar() or 0}


@router.put("/{sid}/receiver", response_model=schemas.Msg)
async def update_receiver(
    sid: int, data: ReceiverIn,
    current: models.User = Depends(require_roles("logistics", "sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """收货信息维护：销售录入为权威初值，物流可补充修正（P-23 审计留痕）。"""
    res = await db.execute(select(models.Shipment).where(models.Shipment.id == sid))
    s = res.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "发货单不存在")
    old = f"{s.receiver_name or ''}/{s.receiver_phone or ''}/{s.receiver_addr or ''}"
    s.receiver_name = data.name.strip() or None
    s.receiver_company = (data.company or "").strip() or None
    s.receiver_phone = data.phone.strip() or None
    s.receiver_addr = data.addr.strip() or None
    await db.commit()
    await write_audit(db, user=current, action="update_receiver", target_type="shipment",
                      target_id=sid, detail=f"{old} → {data.name}/{data.phone}/{data.addr}")
    return schemas.Msg(message="收货信息已保存")


class FreightIn(BaseModel):
    freight_cost: Optional[float] = None
    freight_payer: str = "我方"          # 我方 / 到付
    freight_note: Optional[str] = None


@router.put("/{sid}/freight", response_model=schemas.Msg)
async def update_freight(
    sid: int, data: FreightIn,
    current: models.User = Depends(require_roles("logistics")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #201 物料运输费录入（物流部，发货前后均可维护）。
    freight_payer=我方 计入公司成本（进财务支出总览、项目毛利运费腿）；到付=客户承担不计。"""
    s = (await db.execute(select(models.Shipment).where(
        models.Shipment.id == sid))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "发货单不存在")
    if data.freight_payer not in ("我方", "到付"):
        raise HTTPException(400, "运费承担方须为 我方/到付")
    s.freight_cost = data.freight_cost if (data.freight_cost or 0) > 0 else None
    s.freight_payer = data.freight_payer
    s.freight_note = (data.freight_note or "").strip() or None
    await db.commit()
    await write_audit(db, user=current, action="update_freight", target_type="shipment",
                      target_id=sid, detail=f"运费 {s.freight_cost} {s.freight_payer}")
    return schemas.Msg(message="物料运输费已保存")


@router.get("/receiver-by-code")
async def receiver_by_code(
    code: str,
    current: models.User = Depends(require_roles("logistics", "sales", "sales_lead")),
    db: AsyncSession = Depends(get_db),
):
    """🆕 #142：按「同数字编号」(忽略末尾 A/B/C 子项) 跨全部项目找已填收货信息的兄弟，
    供物流填 2026-062B 时自动带出 2026-062A 已填的收货信息（不限当前看板已加载的行）。"""
    import re
    base = re.sub(r"[A-Za-z]+$", "", (code or "").strip())
    if not base:
        return {"found": False}
    pr = await db.execute(select(models.Project).where(models.Project.is_deleted == False))  # noqa: E712
    pids = [p.id for p in pr.scalars().all() if re.sub(r"[A-Za-z]+$", "", p.code or "") == base]
    if not pids:
        return {"found": False}
    sr = await db.execute(select(models.Shipment).where(
        models.Shipment.project_id.in_(pids),
        models.Shipment.receiver_name.isnot(None)).order_by(models.Shipment.id.desc()))
    ship = sr.scalars().first()
    if not ship:
        return {"found": False}
    return {"found": True, "name": ship.receiver_name or "", "company": ship.receiver_company or "",
            "phone": ship.receiver_phone or "", "addr": ship.receiver_addr or ""}


@router.post("/{sid}/ship", response_model=schemas.Msg)
async def confirm_ship(
    sid: int,
    file: UploadFile = File(...),
    force: bool = Form(False),
    current: models.User = Depends(require_roles("logistics")),
    db: AsyncSession = Depends(get_db),
):
    """确认发货：必传发货单；服务端重算 D5 闸门（防前端绕过）；
    回写 sales_ledger.ship_date；E1 重复发货拒绝。
    存量零任务单项目：仅管理层可 force 强制发货。"""
    res = await db.execute(select(models.Shipment).where(models.Shipment.id == sid))
    s = res.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "发货单不存在")
    if s.status == "shipped":
        raise HTTPException(400, "该项目已发货（E1 一项目一次发货）")

    res = await db.execute(select(models.DeptOrder).where(
        models.DeptOrder.project_id == s.project_id))
    can, missing = _gate(list(res.scalars().all()))
    # 🆕 反馈#367（赵仁辉）：强制发货放给物流负责人。
    # ⚠️ 这一步的实际后果要清楚：本接口 require_roles("logistics")，能调它的本来就只有
    #    物流 + 管理层。把 force 也放给 logistics 之后，**D5 闸门就不再拦任何人了**，
    #    它从「硬闸」变成「提醒 + 留痕」。是有意为之，但补偿手段必须跟上：
    #      ① 审计写清楚绕过时到底缺了什么（原来只写一个 FORCE，事后看不出漏了哪道工序）
    #      ② 每次绕过都推给管理层——没人看得见的旁路等于没有旁路
    can_force = current.has_role("admin", "manager", "logistics")
    if not can and not (force and can_force):
        raise HTTPException(400, f"发货闸门未通过：{('、'.join(missing))} 未完成（D5：已下单任务须全部完成）")
    forced = bool(force and not can)

    a = await save_upload(db, file, biz_type="ship_doc", biz_id=s.id,
                          project_id=s.project_id, user=current)
    today_s = date.today().isoformat()
    s.status = "shipped"
    s.ship_doc_file_id = a.id
    s.shipped_at = datetime.now(timezone.utc)
    s.shipped_by = current.id

    # 🆕 已发货 → 项目自动置为「已完成」（冻结完成日期），项目目录同步显示已完成
    proj_auto_done = False
    prj = s.project
    if prj and prj.status != "已完成":
        prj.status = "已完成"
        cd_key = f"{OVERVIEW_KEY_PREFIX}完成日期"
        extra = dict(prj.extra or {})
        if not extra.get(cd_key):
            extra[cd_key] = today_s
            prj.extra = extra
        proj_auto_done = True

    # 回写销售台账发货日期（只读列）
    res = await db.execute(select(models.SalesLedger).where(
        models.SalesLedger.project_id == s.project_id))
    led = res.scalar_one_or_none()
    sales_uid = None
    if led:
        led.ship_date = today_s
        sales_uid = led.sales_uid
    await db.commit()

    code = s.project.code
    if sales_uid:
        await push_message(db, to_user_id=sales_uid, kind="wx",
                           text=f"【已发货】{code} 已发货，发货日期 {today_s} 已回传你的销售台账。",
                           biz_type="shipment", biz_id=s.id)
    await push_message(db, to_role="sales_lead", kind="info",
                       text=f"【已发货】{code} 已发货（{today_s}）。",
                       biz_type="shipment", biz_id=s.id)
    # 🆕 #367：绕过闸门必须让管理层看见——否则放开权限就等于把 D5 静悄悄关掉了
    if forced:
        who = current.full_name or current.username
        for role in ("manager", "admin"):
            await push_message(db, to_role=role, kind="warn",
                               text=f"【强制发货】{code} 在闸门未通过的情况下由 {who} 发出"
                                    f"（未完成：{'、'.join(missing)}）。",
                               biz_type="shipment", biz_id=s.id,
                               exclude_user_ids={current.id})
    await write_audit(db, user=current, action="ship", target_type="shipment", target_id=sid,
                      # 绕过时把缺了哪几道工序一并记下：原来只写 FORCE，事后看不出漏了什么
                      detail=f"{code} {today_s}" + (f" FORCE 未完成：{'、'.join(missing)}" if forced else ""))
    tail = "，项目已自动标记为已完成" if proj_auto_done else ""
    return schemas.Msg(message=f"{code} 已发货，发货日期已回传销售台账{tail}")
