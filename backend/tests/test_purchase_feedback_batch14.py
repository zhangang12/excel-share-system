"""🆕 用户反馈第14批（采购域）测试：
#274 供应商编码唯一校验：
  - 新建重复编码 → 400「供应商编码已存在」；大小写不敏感（gys-003 也撞 GYS-003）；前后空格视同重复
  - 编辑：把另一家的编码改成重复 → 400；自身保持原编码 → 放行；改成新码 → 放行且新码同样受保护
  - 编码为空（不传/空串）→ 不校验，放行
#283 采购申请推送目标回归（锁定 #242 口径，防回退）：
  - 指定采购员 → 只推给他 1 人（其他 buyer 角色用户不收）
  - 未指定 → 推全体采购角色（buyer/buyer_standard 并集），非采购用户不收
#276/#277 付款凭证链路回归：
  - 请款单付款（带凭证附件）后，请款单返回 pay_voucher_file_id/name；
    采购明细 /items 行也带出同一凭证（申请人/采购在明细行可下载回执）
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="purchfb")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        async def login(u, p):
            r = await c.post('/api/auth/login', json={'username': u, 'password': p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login('admin', 'admin123')
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(uname, role_code):
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": uname, "password": "pass123",
                                   "full_name": uname, "role_id": rid[role_code]})
            assert r.status_code == 200, r.text
            return r.json()["id"] if isinstance(r.json(), dict) and "id" in r.json() else None

        b1_id = await mkuser("b1", "buyer")
        b2_id = await mkuser("b2", "buyer_standard")
        w1_id = await mkuser("w1", "warehouse")
        Hb1 = await login('b1', 'pass123')
        Hw1 = await login('w1', 'pass123')
        if not (b1_id and b2_id and w1_id):
            async with SessionLocal() as db:
                ids = {u.username: u.id for u in (await db.execute(select(models.User))).scalars().all()}
            b1_id, b2_id, w1_id = ids["b1"], ids["b2"], ids["w1"]

        # ==================== #274 供应商编码唯一 ====================
        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1,
                         json={"name": "供应商甲", "code": "GYS-003"})
        chk(r.status_code == 200, f"新建供应商甲: {r.status_code} {r.text[:120]}")
        s1 = r.json()["id"] if r.status_code == 200 else None

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1,
                         json={"name": "供应商乙", "code": "GYS-003"})
        chk(r.status_code == 400 and r.json().get("detail") == "供应商编码已存在",
            f"重复编码 400+中文提示: {r.status_code} {r.text[:120]}")

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1,
                         json={"name": "供应商乙", "code": "gys-003"})
        chk(r.status_code == 400, f"大小写不敏感重复 400: {r.status_code}")

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1,
                         json={"name": "供应商乙", "code": " GYS-003 "})
        chk(r.status_code == 400, f"前后空格视同重复 400: {r.status_code}")

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1,
                         json={"name": "供应商乙"})
        chk(r.status_code == 200, f"无编码放行: {r.status_code} {r.text[:120]}")
        s2 = r.json()["id"] if r.status_code == 200 else None

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1,
                         json={"name": "供应商丙", "code": ""})
        chk(r.status_code == 200, f"空串编码放行: {r.status_code}")
        s3 = r.json()["id"] if r.status_code == 200 else None

        # 编辑：把乙的编码改成甲的 → 400
        r = await c.put(f"/api/purchase-mgmt/suppliers/{s2}", headers=Hb1,
                        json={"code": "GYS-003"})
        chk(r.status_code == 400 and r.json().get("detail") == "供应商编码已存在",
            f"编辑成重复编码 400: {r.status_code} {r.text[:120]}")

        # 编辑：甲保持自己的编码（排除自身）→ 放行
        r = await c.put(f"/api/purchase-mgmt/suppliers/{s1}", headers=Hb1,
                        json={"code": "GYS-003", "contact": "张三"})
        chk(r.status_code == 200, f"自身编码不变放行: {r.status_code} {r.text[:120]}")

        # 编辑：丙改成新码 GYS-009 → 放行；此后 GYS-009 也被占用
        r = await c.put(f"/api/purchase-mgmt/suppliers/{s3}", headers=Hb1,
                        json={"code": "GYS-009"})
        chk(r.status_code == 200, f"改成新码放行: {r.status_code} {r.text[:120]}")
        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1,
                         json={"name": "供应商丁", "code": "GYS-009"})
        chk(r.status_code == 400, f"更新后的新码同样查重 400: {r.status_code}")

        # 编辑：编码不变大小写（gys-003→GYS-003 同一家）→ 放行
        r = await c.put(f"/api/purchase-mgmt/suppliers/{s1}", headers=Hb1,
                        json={"code": "gys-003"})
        chk(r.status_code == 200, f"自身仅改大小写放行: {r.status_code} {r.text[:120]}")

        # ==================== #283 采购申请推送目标 ====================
        async def preq_msgs(prid):
            async with SessionLocal() as db:
                return list((await db.execute(select(models.Message).where(
                    models.Message.biz_type == "purchase_request",
                    models.Message.biz_id == prid))).scalars().all())

        # 指定采购员 b1 → 只推给 b1 一人
        r = await c.post("/api/purchase-mgmt/purchase-requests", headers=Hw1,
                         json={"buyer_id": b1_id, "lines": [
                             {"item_name": "螺丝", "qty": 100, "project_code": "P-001"}]})
        chk(r.status_code == 200, f"w1 提请购单(指定b1): {r.status_code} {r.text[:150]}")
        pr1 = r.json()["id"] if r.status_code == 200 else None
        ms = await preq_msgs(pr1)
        chk(len(ms) == 1 and ms[0].to_user_id == b1_id,
            f"指定采购员只推 1 人: {[(m.to_user_id, m.text[:30]) for m in ms]}")

        # 未指定 → 推全体采购角色（b1+b2；w1/admin/manager 非采购角色不收）
        r = await c.post("/api/purchase-mgmt/purchase-requests", headers=Hw1,
                         json={"lines": [{"item_name": "钢板", "qty": 2}]})
        chk(r.status_code == 200, f"w1 提请购单(未指定): {r.status_code} {r.text[:150]}")
        pr2 = r.json()["id"] if r.status_code == 200 else None
        ms = await preq_msgs(pr2)
        got = {m.to_user_id for m in ms}
        chk(got == {b1_id, b2_id},
            f"未指定推全体采购角色(b1+b2): {sorted(got)} vs {sorted({b1_id, b2_id})}")
        chk(w1_id not in got, "未指定时申请人(非采购角色)不收推送")

        # ==================== #276/#277 付款凭证链路 ====================
        # 建一张已收货的采购明细（b1 下单，甲供应商）
        r = await c.post("/api/purchase-mgmt/items", headers=Hb1,
                         json={"supplier_id": s1, "item_name": "电机", "qty": 1,
                               "unit_price": 500, "received_amount": 500,
                               "project_code": "P-001", "arrival_date": "2026-07-20"})
        chk(r.status_code == 200, f"建采购明细: {r.status_code} {r.text[:150]}")
        item_id = r.json()["id"] if r.status_code == 200 else None

        # b1 发起请款
        r = await c.post("/api/purchase-mgmt/payment-requests", headers=Hb1,
                         json={"supplier_id": s1, "requested_amount": 500,
                               "items": [{"item_id": item_id, "allocated_amount": 500}]})
        chk(r.status_code == 200, f"发起请款: {r.status_code} {r.text[:150]}")
        payreq_id = r.json()["id"] if r.status_code == 200 else None

        # 财务审批 + 另一个财务付款（职责分离：审批≠付款）
        f1_id = await mkuser("f1", "finance")
        f2_id = await mkuser("f2", "finance")
        Hf1 = await login('f1', 'pass123')
        Hf2 = await login('f2', 'pass123')
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{payreq_id}/approve", headers=Hf1)
        chk(r.status_code == 200, f"财务审批请款: {r.status_code} {r.text[:150]}")
        # 付款并上传凭证（multipart）
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{payreq_id}/pay", headers=Hf2,
                        data={"paid_amount": "500", "paid_date": "2026-07-22", "payment_method": "对公全款"},
                        files={"file": ("回执单.pdf", b"%PDF-1.4 fake", "application/pdf")})
        chk(r.status_code == 200, f"财务付款(带凭证): {r.status_code} {r.text[:150]}")

        # #276：请款记录（申请人视角）带付款凭证
        r = await c.get("/api/purchase-mgmt/payment-requests", headers=Hb1)
        rows = [x for x in r.json() if x["id"] == payreq_id]
        chk(rows and rows[0].get("pay_voucher_file_id") and rows[0].get("pay_voucher_name") == "回执单.pdf",
            f"请款记录带凭证 id+name: {rows[0].get('pay_voucher_file_id') if rows else None} "
            f"{rows[0].get('pay_voucher_name') if rows else None}")

        # #277：采购明细行也带同一凭证（已付款 pill 旁的回执链接数据）
        r = await c.get("/api/purchase-mgmt/items", headers=Hb1)
        irows = [x for x in r.json() if x["id"] == item_id]
        chk(irows and irows[0].get("pay_status") == "已付款",
            f"明细行付款状态=已付款: {irows[0].get('pay_status') if irows else None}")
        chk(irows and irows[0].get("pay_voucher_file_id") and irows[0].get("pay_voucher_name") == "回执单.pdf",
            f"明细行带付款回执: {irows[0].get('pay_voucher_file_id') if irows else None} "
            f"{irows[0].get('pay_voucher_name') if irows else None}")

        # 凭证可下载
        if rows and rows[0].get("pay_voucher_file_id"):
            r = await c.get(f"/api/attachments/{rows[0]['pay_voucher_file_id']}/download", headers=Hb1)
            chk(r.status_code == 200 and r.content == b"%PDF-1.4 fake",
                f"凭证可下载且内容一致: {r.status_code}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
