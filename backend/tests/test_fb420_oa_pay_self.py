"""OA 付款内控：申请人 ≠ 付款人（反馈#420 王芹，2026-09-02）。

原话：「OA审批里对公付款我的我这不应有标计已付款的按钮」——她说得对：
`mark_paid` 原来只查角色和状态，没有一行比较 requester_id。生产实测 88 张
待付款口径的单里 9 张(¥146,228)申请人自己就能付，全是兼任账号（王芹=采购+财务）。

口径（与 #237「不能审批自己提交的请款单」、#331「管理层不留后门」同源）：
  ① `can_mark_paid`：本人 → false，别的财务 → true；
  ② `mark_paid` 硬闸：本人 PUT → 400 带「职责分离」；
  ③ **admin 提的单 admin 自己也不能标**（不留后门）；
  ④ 待付款队列(scope=pending_pay)不含自己提的单（不然挂一条永远点不掉的，
     违背 #396「处理一个就减一个」），但别的财务看得到。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb420s")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

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

        # 王芹式兼任账号（buyer+finance）+ 另一位财务（杨倩式）
        wq_id = await mkuser("pay_self", ["buyer", "finance"])
        await mkuser("pay_other", ["finance"])
        Hs, Ho = await login("pay_self", "pass123"), await login("pay_other", "pass123")

        dept = (await c.get("/api/oa/departments", headers=H)).json()
        dept_id = dept[0]["id"] if dept else None

        async def mkreq(no, requester_id):
            async with SessionLocal() as db:
                req = models.OaRequest(
                    request_no=no, category="purchase", doc_type="payment_public",
                    title=no, department_id=dept_id, requester_id=requester_id, amount=1657,
                    status="pending_payment",
                    detail={"payee": "x", "payee_account": "1", "reason": "r"})
                db.add(req)
                await db.flush()
                oid = req.id
                await db.commit()
                return oid

        oid = await mkreq("OA-SELF-001", wq_id)

        # ① can_mark_paid：本人 false / 别的财务 true
        d = (await c.get(f"/api/oa/requests/{oid}", headers=Hs)).json()
        chk(d.get("can_mark_paid") is False, "本人看不到「标记已付款」(can_mark_paid=false)")
        d = (await c.get(f"/api/oa/requests/{oid}", headers=Ho)).json()
        chk(d.get("can_mark_paid") is True, "另一位财务能标记 (can_mark_paid=true)")

        # ② 硬闸：本人直接 PUT 也进不来（带凭证也不行——拦的是人不是凭证）
        r = await c.put(f"/api/oa/requests/{oid}/mark-paid", headers=Hs,
                        data={"pay_note": "我自己付"},
                        files={"file": ("r.png", b"\x89PNG fake", "image/png")})
        chk(r.status_code == 400, f"本人 PUT 被硬闸拦下 -> {r.status_code}")
        chk("职责分离" in r.json().get("detail", ""), f"拒绝语点明职责分离 -> {r.json().get('detail','')[:30]}")

        # ④ 待付款队列：本人的队列里没有自己这张；别的财务队列里有
        rows = (await c.get("/api/oa/requests", headers=Hs, params={"scope": "pending_pay"})).json()
        chk(all(x["id"] != oid for x in rows), "本人的待付款队列不含自己提的单（角标点得掉）")
        rows = (await c.get("/api/oa/requests", headers=Ho, params={"scope": "pending_pay"})).json()
        chk(any(x["id"] == oid for x in rows), "另一位财务的队列里有这张单（不会没人管）")

        # ②b 别的财务正常付（改动没有把正路堵死）
        r = await c.put(f"/api/oa/requests/{oid}/mark-paid", headers=Ho,
                        data={"pay_note": "网银已转"},
                        files={"file": ("r.png", b"\x89PNG fake", "image/png")})
        chk(r.status_code == 200 and r.json().get("status") == "paid",
            f"另一位财务正常标记付款 -> {r.status_code}")

        # ③ admin 不留后门：admin 提的单 admin 自己也不能标
        async with SessionLocal() as db:
            from sqlalchemy import select
            admin_id = (await db.execute(select(models.User.id).where(
                models.User.username == "admin"))).scalar()
        oid2 = await mkreq("OA-SELF-002", admin_id)
        d = (await c.get(f"/api/oa/requests/{oid2}", headers=H)).json()
        chk(d.get("can_mark_paid") is False, "admin 提的单 admin 也看不到按钮（不留后门）")
        r = await c.put(f"/api/oa/requests/{oid2}/mark-paid", headers=H,
                        files={"file": ("r.png", b"\x89PNG fake", "image/png")})
        chk(r.status_code == 400, f"admin 自付同样被拦 -> {r.status_code}")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
