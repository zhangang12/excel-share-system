"""请款自助撤销（反馈#405 李新新，2026-08-22）。

原话：「请款这里我们能不能有个撤销功能，有的是已经请款了，但是又临时不买了，
需要审批人驳回，如果我们申请的有个撤销功能，就再方便不过了」

**最小口径（用户拍板：先最小化，后面有要求再加）**：
只有「待审 + 本人发起」能撤，撤 = 物理删除请款单和它的关联行。

三条设计决策，各自防着一件具体的事，改之前先看懂：

1) **为什么物理删除而不是加 cancelled 状态**
   她说"临时不买了"，意思是那条采购明细通常也要跟着删。而 delete_item 的校验是
   **数关联行、不看状态**——留软状态的话关联行还在，她撤完照样删不掉明细，
   报错还把她指回请款记录（她刚从那儿来），死循环。见断言 4。

2) **为什么只允许待审**
   已批 = 财务已经答应付这笔钱。撤它会动 13 周资金排程，还会捅穿 #177——
   系统里唯一那道防重复付款闸门只认 pending/approved，撤掉重提就可能同一批明细付两次。
   而且出纳可能已经在网银上操作了。见断言 5。

3) **为什么校验靠带 status 条件的 DELETE 而不是"先查再删"**
   生产跑 4 个 worker，全后端一处行锁都没有。财务点「通过」和她点「撤销」可以真并发，
   先查后删会把一张**已被批准**的单删掉。见断言 12——直接验那条 DELETE 的语义。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="selfcancel")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, delete, func
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def _pr(prid):
    async with SessionLocal() as db:
        return (await db.execute(select(models.PaymentRequest).where(
            models.PaymentRequest.id == prid))).scalars().unique().one_or_none()


async def _pri_count(prid):
    async with SessionLocal() as db:
        return (await db.execute(select(func.count(models.PaymentRequestItem.id)).where(
            models.PaymentRequestItem.request_id == prid))).scalar()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        buyer = await mkuser("sc_buyer", ["buyer"])       # 李新新
        other = await mkuser("sc_other", ["buyer"])       # 另一个采购员
        lead_id = await mkuser("sc_lead", ["finance", "finance_lead"])   # 审批人
        await mkuser("sc_cash", ["finance"])              # 出纳
        Hb, Ho, Hl, Hc = (await login("sc_buyer", "pass123"), await login("sc_other", "pass123"),
                          await login("sc_lead", "pass123"), await login("sc_cash", "pass123"))

        sid = (await c.post("/api/purchase-mgmt/suppliers", headers=Hb,
                            json={"name": "无锡市俊帆金属科技制造有限公司"})).json()["id"]

        async def new_item():
            return (await c.post("/api/purchase-mgmt/items", headers=Hb, json={
                "supplier_id": sid, "item_name": "不锈钢板", "qty": 2, "unit_price": 500,
                "received_amount": 1000, "arrival_date": "2026-08-20"})).json()["id"]

        async def new_pr(iid, hdr=None):
            r = await c.post("/api/purchase-mgmt/payment-requests", headers=hdr or Hb, json={
                "supplier_id": sid, "requested_amount": 1000, "notes": "月结",
                "items": [{"item_id": iid, "allocated_amount": 1000}]})
            assert r.status_code in (200, 201), r.text
            return r.json()

        # ============ 1) 本人撤自己的待审单 ============
        iid = await new_item()
        pr1 = await new_pr(iid)
        chk(pr1["can_cancel"] is True, f"1) 自己的待审单 can_cancel=true（前端按钮就靠它）: {pr1.get('can_cancel')}")
        r = await c.delete(f"/api/purchase-mgmt/payment-requests/{pr1['id']}/self-cancel", headers=Hb)
        chk(r.status_code == 200, f"1) 本人撤待审单: {r.status_code} {r.text[:90]}")
        chk(await _pr(pr1["id"]) is None, "1) 请款单已删除")
        chk(await _pri_count(pr1["id"]) == 0,
            "1) **关联行也删干净**（留着的话 delete_item 会把明细永久锁死，见断言 4）")

        # ============ 2) 撤完能重新请款（#177 放行）============
        r = await c.post("/api/purchase-mgmt/payment-requests", headers=Hb, json={
            "supplier_id": sid, "requested_amount": 1000,
            "items": [{"item_id": iid, "allocated_amount": 1000}]})
        chk(r.status_code == 200,
            f"2) 撤完同一条明细能重新请款（不再撞「已有未完成的请款单」）: {r.status_code} {r.text[:80]}")
        pr2 = r.json()

        # ============ 3) 明细付款状态回「未付款」============
        r2 = await c.delete(f"/api/purchase-mgmt/payment-requests/{pr2['id']}/self-cancel", headers=Hb)
        chk(r2.status_code == 200, f"3) 再撤一次: {r2.status_code}")
        items = (await c.get("/api/purchase-mgmt/items", headers=Hb)).json()
        row = next((x for x in (items if isinstance(items, list) else items.get("rows", [])) if x["id"] == iid), None)
        chk(row and row.get("pay_status") == "未付款",
            f"3) 撤销后明细回到「未付款」: {row and row.get('pay_status')}")

        # ============ 4) 撤销后**能删掉那条采购明细**（她真正想干的事）============
        r = await c.delete(f"/api/purchase-mgmt/items/{iid}", headers=Hb)
        chk(r.status_code == 200,
            f"4) **撤销后能删掉采购明细**——软状态方案在这里会死循环："
            f"删不掉，报错还把她指回请款记录（她刚从那儿撤完）: {r.status_code} {r.text[:90]}")

        # ============ 5) 已批 / 已付不许自助撤 ============
        iid2 = await new_item()
        pr3 = await new_pr(iid2)
        await c.put(f"/api/purchase-mgmt/payment-requests/{pr3['id']}/approve", headers=Hl)
        r = await c.delete(f"/api/purchase-mgmt/payment-requests/{pr3['id']}/self-cancel", headers=Hb)
        chk(r.status_code == 400 and "财务" in r.text,
            f"5) 已批的不许自助撤，且提示告诉她去找财务（钱已被承诺，撤它会动资金排程+捅穿防重复付款）: "
            f"{r.status_code} {r.text[:90]}")
        lst = (await c.get("/api/purchase-mgmt/payment-requests", headers=Hb)).json()
        cur = next(x for x in lst if x["id"] == pr3["id"])
        chk(cur["can_cancel"] is False, "5) 已批单 can_cancel=false（前端不该显示按钮）")

        # ⚠️ /pay 收的是 **Form**（要一起传付款凭证文件），不是 JSON——用 json= 会 422
        rp = await c.put(f"/api/purchase-mgmt/payment-requests/{pr3['id']}/pay", headers=Hc,
                         data={"paid_amount": "1000", "paid_date": "2026-08-22", "payment_method": "电汇"})
        # ⚠️ 必须先确认真的付成功了。只断言下一步返回 400 是不辨真伪的：
        #    付款没成功的话单子还是 approved，走的是「已批」那条分支，400 照样成立，
        #    「已付不许撤」这条其实一次都没被测到。
        chk(rp.status_code == 200, f"5) 前提：出纳把这单付掉: {rp.status_code} {rp.text[:110]}")
        chk((await _pr(pr3["id"])).status == "paid", "5) 前提：状态确实变成 paid")
        r = await c.delete(f"/api/purchase-mgmt/payment-requests/{pr3['id']}/self-cancel", headers=Hb)
        chk(r.status_code == 400 and "已经付款" in r.text,
            f"5) 已付的更不许撤，且文案说的是「已经付款」而不是「已经批准」: {r.status_code} {r.text[:80]}")

        # ============ 6) 越权 ============
        iid3 = await new_item()
        pr4 = await new_pr(iid3)
        r = await c.delete(f"/api/purchase-mgmt/payment-requests/{pr4['id']}/self-cancel", headers=Ho)
        chk(r.status_code == 403, f"6) 别的采购员撤不了我的单: {r.status_code} {r.text[:70]}")
        lst_o = (await c.get("/api/purchase-mgmt/payment-requests", headers=Ho)).json()
        seen = next((x for x in lst_o if x["id"] == pr4["id"]), None)
        if seen:
            chk(seen["can_cancel"] is False, "6) 就算他看得见这单，can_cancel 也是 false")
        r = await c.delete(f"/api/purchase-mgmt/payment-requests/{pr4['id']}/self-cancel", headers=Hl)
        chk(r.status_code == 403,
            f"6) 财务也走不了这个口子（撤销是采购侧动作，财务有 withdraw-approval 和删除）: {r.status_code}")

        # ============ 7) 通知 + 审计 ============
        r = await c.delete(f"/api/purchase-mgmt/payment-requests/{pr4['id']}/self-cancel", headers=Hb)
        chk(r.status_code == 200, f"7) 本人撤掉: {r.status_code}")
        async with SessionLocal() as db:
            msgs = (await db.execute(select(models.Message).where(
                models.Message.biz_type == "payment_request",
                models.Message.biz_id == pr4["id"]))).scalars().all()
            aud = (await db.execute(select(models.AuditLog).where(
                models.AuditLog.action == "payment_request_self_cancel",
                models.AuditLog.target_id == pr4["id"]))).scalars().all()
        got_lead = [m for m in msgs if m.to_user_id == lead_id and "撤销" in (m.text or "")]
        chk(got_lead,
            f"7) 财务收到「已撤销」通知——建单时推过「待审批」，系统没有消息撤回机制，"
            f"不补一条财务点进去只会看到一张不存在的单: {[(m.to_user_id, (m.text or '')[:22]) for m in msgs]}")
        chk(aud and "俊帆" in (aud[0].detail or "") and "1000" in (aud[0].detail or ""),
            f"7) 审计写全了——单子物理删了之后这是唯一还留着痕迹的地方: "
            f"{aud[0].detail if aud else '(没有审计)'}")

        # ============ 8) 并发闸门：带 status 条件的 DELETE 语义 ============
        iid4 = await new_item()
        pr5 = await new_pr(iid4)
        await c.put(f"/api/purchase-mgmt/payment-requests/{pr5['id']}/approve", headers=Hl)
        async with SessionLocal() as db:
            res = await db.execute(delete(models.PaymentRequest).where(
                models.PaymentRequest.id == pr5["id"],
                models.PaymentRequest.status == "pending"))
            await db.rollback()
        chk(res.rowcount == 0,
            f"8) **审批已落库时，那条带 status 条件的 DELETE 匹配 0 行** —— 这才是真正的闸门。"
            f"换成「先查状态再删」，并发下会把一张已批的单删掉: rowcount={res.rowcount}")
        chk(await _pr(pr5["id"]) is not None, "8) 已批的单还在，没被删掉")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
