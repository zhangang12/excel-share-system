"""🆕 2026-09-24 两个线上 bug，都由「后端一重启就跑的回填」把事情做过了头。

【一】反馈#435「设计师下的单子跑到销售部来了」
  备机项目 2026-080.. 出现在销售台账里，「销售」一栏写着**设计师陈立新**。
  链条：
    09-17 陈立新走「备机下单」建项目 → create_spare 把一览「销售」写成「备机·陈立新」；
    09-19 有人在项目目录里把这一格改成「陈立新」（那一格是可编辑的）；
    09-19 下一次后端启动 → backfill_sales_ledger 的判据是
          `sales_name.startswith("备机")`，现在认不出来了 → 给备机补了一行销售台账，
          还按姓名唯一匹配把 sales_uid 设成了那个设计师。
  **根因是把身份存在一个用户能改的显示字符串里。** 现在另存 `extra["__spare__"]`，
  判据以它为准（_is_spare），字符串只作为老数据的兜底。

【二】截图「数据已存在，不能重复」——销售下单审批点「通过」点不动
  `_materialize_order_downstream` 无条件 insert 发货行，撞 uq_shipment_project → 409，
  而这个 409 把**整个审批卡死**，主管点多少次都过不去。
  已经有发货行是常态不是异常：`backfill_shipments` 每次启动都跑，
  只要「下单」和「审批」之间隔了一次发版，那行就先被补出来了
  （2026-090 实测：发货行建于 09-23 17:22，正是一次发版重启）。
  两侧都改：审批侧「有就用、没有才建」；回填侧不给待审批/草稿单建发货行。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb435")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import (run_all, ensure_schema_columns,
                                backfill_sales_ledger, backfill_shipments,
                                backfill_spare_marker, _is_spare)
from app import models
from app.config import settings

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

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
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

        await mkuser("陈师傅", ["design_lead", "designer"])
        await mkuser("小销售", ["sales"])
        await mkuser("销售头", ["sales", "sales_lead"])
        Hdes = await login("陈师傅", "pass123")
        Hs = await login("小销售", "pass123")
        Hsl = await login("销售头", "pass123")

        print("\n=== 一、备机不该进销售台账 ===")
        r = await c.post("/api/orders/spare", headers=Hdes, json={
            "code": "2026-备99", "name": "50L 备机", "depts": ["produce"],
            "qty": 1, "unit": "台", "req_text": "备机"})
        chk(r.status_code == 200, f"设计部负责人建备机: {r.status_code} {r.text[:90]}")
        pid = r.json()["project_id"]

        async with SessionLocal() as db:
            p = (await db.execute(select(models.Project).where(
                models.Project.id == pid))).scalar_one()
            chk(bool((p.extra or {}).get("__spare__")),
                "建的时候就打上了 __spare__ 标记（不靠那个能被改的显示字符串）")

        # 复现事故：把一览「销售」从「备机·陈师傅」改成纯姓名
        r = await c.put(f"/api/projects/{pid}/header-cell", headers=H,
                        json={"key": "销售", "value": "陈师傅", "is_overview": True})
        chk(r.status_code == 200, f"有人把「销售」格改成纯姓名（线上就是这么发生的）: {r.status_code}")

        async with SessionLocal() as db:
            p = (await db.execute(select(models.Project).where(
                models.Project.id == pid))).scalar_one()
            chk((p.extra or {}).get("__o__销售") == "陈师傅", "那一格确实被改了")
            chk(_is_spare(p), "**但它仍然被认作备机** —— 判据看标记，不看那一格")

        # 再跑一次回填（= 后端重启），这正是线上补出那行台账的时刻
        async with SessionLocal() as db:
            await backfill_spare_marker(db)
            await backfill_sales_ledger(db)
            await backfill_shipments(db)
        async with SessionLocal() as db:
            n = (await db.execute(select(models.SalesLedger).where(
                models.SalesLedger.project_id == pid))).scalars().all()
            ns = (await db.execute(select(models.Shipment).where(
                models.Shipment.project_id == pid))).scalars().all()
        chk(len(n) == 0,
            f"重启回填后备机**仍然没有销售台账行** —— 改之前这里会补出 1 行，"
            f"sales_uid 还按姓名匹配成了那个设计师 → 实得 {len(n)} 行")
        chk(len(ns) == 0, f"也不进物流发货看板 → {len(ns)} 行")

        print("\n=== 一之二、老数据（只有「备机·」字符串、没有标记）也要认出来 ===")
        async with SessionLocal() as db:
            old = models.Project(code="2026-备98", name="老备机", status="进行中",
                                 extra={"__o__销售": "备机·某人"})
            db.add(old)
            await db.commit()
            oid = old.id
        async with SessionLocal() as db:
            await backfill_spare_marker(db)
        async with SessionLocal() as db:
            p2 = (await db.execute(select(models.Project).where(
                models.Project.id == oid))).scalar_one()
            chk(bool((p2.extra or {}).get("__spare__")),
                "存量备机被补上了标记（以后再改那一格也不怕了）")

        print("\n=== 二、下单审批不能被「发货行已存在」卡死 ===")
        settings.sales_order_approval = True     # 开审批流，让销售员下单进 pending
        r = await c.post("/api/sales/orders", headers=Hs, json={
            "code": "2026-991", "name": "100ml搅拌罐", "customer": "东莞市科路得新能源",
            "cust_type": "终端客户", "depts": ["design", "produce"],
            "qty": 1, "unit": "台", "amount": 0, "req_text": "下单"})
        chk(r.status_code == 200, f"销售员下单（进待审批）: {r.status_code} {r.text[:90]}")
        pid2 = r.json()["project_id"]

        async with SessionLocal() as db:
            led = (await db.execute(select(models.SalesLedger).where(
                models.SalesLedger.project_id == pid2))).scalar_one()
            lid = led.id
            chk(led.order_state == "pending", f"确实是待审批 → {led.order_state}")

        # 模拟「下单和审批之间发了一次版」：重启回填跑一遍
        async with SessionLocal() as db:
            await backfill_shipments(db)
        async with SessionLocal() as db:
            ns = (await db.execute(select(models.Shipment).where(
                models.Shipment.project_id == pid2))).scalars().all()
        chk(len(ns) == 0,
            f"待审批的单**不该被补出发货行**（还没批呢，也不该进物流看板）→ {len(ns)} 行")

        # 再狠一点：手工塞一行进去（老数据/别的路径都可能造成），审批仍然要过
        async with SessionLocal() as db:
            db.add(models.Shipment(project_id=pid2, receiver_name="物流先填的收货人"))
            await db.commit()

        r = await c.post(f"/api/sales/ledger/{lid}/order-approve", headers=Hsl)
        chk(r.status_code == 200,
            f"**已经有发货行时，审批照样要通过** —— 改之前这里是 409"
            f"「数据已存在，不能重复」，主管点多少次都过不去 → {r.status_code} {r.text[:110]}")

        async with SessionLocal() as db:
            ns = (await db.execute(select(models.Shipment).where(
                models.Shipment.project_id == pid2))).scalars().all()
            led2 = (await db.execute(select(models.SalesLedger).where(
                models.SalesLedger.id == lid))).scalar_one()
            orders = (await db.execute(select(models.DeptOrder).where(
                models.DeptOrder.project_id == pid2))).scalars().all()
        chk(len(ns) == 1, f"发货行仍然只有一行，没有变成两行 → {len(ns)}")
        chk(ns[0].receiver_name == "物流先填的收货人",
            f"**物流先填的收货人没被空值盖掉** → {ns[0].receiver_name}")
        chk(led2.order_state is None, f"订单状态已生效 → {led2.order_state}")
        chk(len(orders) == 2, f"两个部门任务都派出去了 → {len(orders)}")


asyncio.run(main())
print("\n" + ("全部通过 ✅" if not FAIL else f"{len(FAIL)} 条失败 ❌"))
for m in FAIL:
    print(" -", m)
sys.exit(1 if FAIL else 0)
