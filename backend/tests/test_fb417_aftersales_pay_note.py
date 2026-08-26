"""财务在「安装/售后费用」表上直接填/改备注（反馈#417 王芹，2026-08-26）。

原话：「表头增加个备注，方便安排完报销填写信息」——财务安排完报销要补记打款批次、
核对情况等，但备注(pay_note)以前只能在点「安排报销」那一刻由接口带入（前端还没传），
事后没有任何入口能写。

新端点 POST /aftersales/{aid}/pay-note（仅 finance）：
  ① 任意报销状态（含旧流程 NULL、已安排 reimbursed）都能填/改/清空；
  ② **唯独 invoice_fix（发票退回中）不许改**——那时 pay_note 存的是给登记人看的
     退回原因，盖掉之后登记人就不知道发票哪里要改了；
  ③ 非 finance 一律 403；未审批(status!=approved)的记录 400（复用 _get_for_pay 口径）。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb417")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
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

        await mkuser("pn_fin", ["finance"])         # 王芹
        await mkuser("pn_worker", ["as_worker"])    # 售后登记人（非财务）
        Hf, Hw = await login("pn_fin", "pass123"), await login("pn_worker", "pass123")

        # 直接造 4 条已审批的售后记录，覆盖 4 种报销状态
        async with SessionLocal() as db:
            rows = {}
            for key, pay_status, note in [
                ("old", None, None),                 # 旧流程
                ("chk", "checking", None),           # 待核对
                ("done", "reimbursed", "先垫付"),     # 已安排（已有备注）
                ("fix", "invoice_fix", "第2行发票抬头不对"),  # 退回中：pay_note=退回原因
            ]:
                a = models.AfterSales(kind="aftersales", problem=f"测试-{key}",
                                      cost=100, status="approved",
                                      pay_status=pay_status, pay_note=note)
                db.add(a)
                await db.flush()
                rows[key] = a.id
            pending = models.AfterSales(kind="aftersales", problem="未审批", cost=50,
                                        status="pending")
            db.add(pending)
            await db.flush()
            rows["pending"] = pending.id
            await db.commit()

        async def set_note(hdr, aid, note):
            return await c.post(f"/api/aftersales/{aid}/pay-note", headers=hdr,
                                data={"note": note})

        # ① 旧流程(NULL)可以填
        r = await set_note(Hf, rows["old"], "8/30 批次打款")
        chk(r.status_code == 200, f"旧流程记录可以填备注 -> {r.status_code}")

        # ② 已安排报销的可以改（这正是#417 的主场景：安排完再补信息）
        r = await set_note(Hf, rows["done"], "8/26 已转张会计，票已归档")
        chk(r.status_code == 200, f"已安排报销的可以改 -> {r.status_code}")

        # ③ 待核对的也能填
        r = await set_note(Hf, rows["chk"], "等催票")
        chk(r.status_code == 200, f"待核对的可以填 -> {r.status_code}")

        # ④ 退回中的不许改，且原退回原因必须原样保住
        r = await set_note(Hf, rows["fix"], "想盖掉退回原因")
        chk(r.status_code == 400, f"退回中(invoice_fix)拒绝 -> {r.status_code}")
        chk("退回" in r.json().get("detail", ""), "拒绝语说清了为什么不能改")

        # ⑤ 非财务 403
        r = await set_note(Hw, rows["old"], "售后想自己写")
        chk(r.status_code == 403, f"非 finance 被拒 -> {r.status_code}")

        # ⑥ 未审批的 400
        r = await set_note(Hf, rows["pending"], "还没批")
        chk(r.status_code == 400, f"未审批记录 400 -> {r.status_code}")

        # ⑦ 清空 = 删除备注
        r = await set_note(Hf, rows["done"], "   ")
        chk(r.status_code == 200, f"传空白可清空 -> {r.status_code}")

        # ⑧ 落库核对：改的真改了、该保住的真保住了
        async with SessionLocal() as db:
            m = {a.id: a for a in (await db.execute(select(models.AfterSales))).scalars().all()}
            chk(m[rows["old"]].pay_note == "8/30 批次打款", "旧流程备注已落库")
            chk(m[rows["done"]].pay_note is None, "清空后落库为 NULL")
            chk(m[rows["fix"]].pay_note == "第2行发票抬头不对", "退回原因原样保住")
            chk(m[rows["fix"]].pay_status == "invoice_fix", "退回状态没被动")

        # ⑨ 财务列表接口能看到备注（前端那列读的就是它）
        r = await c.get("/api/finance/aftersales", headers=Hf)
        got = {x["id"]: x.get("pay_note") for x in r.json()["rows"]}
        chk(got.get(rows["old"]) == "8/30 批次打款", "/finance/aftersales 返回 pay_note")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
