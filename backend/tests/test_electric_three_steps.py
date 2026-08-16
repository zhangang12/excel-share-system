"""电工部三步流：主板完成 → 电路完成 → 上传电路图（2026-08-12 业务确认）。

业务定的三条：
  1. **考核算在第一步（主板完成）**，预计完成时间也是对着主板完成定的
  2. 第二步（电路完成）才算部门完成 → 物流 D5 发货闸门放行
  3. 电路图必传，但**不卡发货**

由此产生本系统里唯一一处「考核完成日 ≠ 部门完成日」：
    done_date      = 主板完成日（效率/按时/逾期只认它）
    wire_done_date = 电路完成日（这一刻 status=done）
拆开之后有三处连带逻辑必须跟着改，漏一处线上就会出怪事，本文件逐条锁住：
  A. `overdue.scan_overdue` 要跳过已结考核的单 —— 否则考核结了还天天报逾期
  B. `reports_router._kpi_done` 要认主板标记 —— 否则考核结了报表却不算完成，按时率算错
  C. `reopen` 要把三步标记全清 —— 漏清主板标记，返工后「主板完成」点不动，单子卡死
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="elec3")
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
from app import models, overdue
from app.routers.reports_router import _kpi_done

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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        uid = {}
        for uname, name, rc in [("e1", "电工甲", "electrician"), ("lg", "物流员", "logistics")]:
            rr = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": name, "role_id": rid[rc]})
            chk(rr.status_code == 200, f"建用户 {name}: {rr.status_code} {rr.text[:70]}")
            uid[name] = rr.json()["id"]
        H1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 'e1', 'password': 'pass123'})).json()['access_token']}"}

        pj = (await c.post("/api/projects", headers=H, json={"code": "E-001", "name": "三步流测试"})).json()
        yesterday = (date.today() - timedelta(days=3)).isoformat()   # 故意让它逾期
        # 建一张电工任务单并派给电工甲
        # ⚠️ 还要建 Shipment：物流看板读的是 shipments 表，而这行是**销售下单**时才建的
        #    （sales_router:425），直接 POST /projects 不会建。少了它项目根本不在看板上，
        #    闸门断言就会变成"无此项目"——绿不了也红不对。
        async with SessionLocal() as db:
            o = models.DeptOrder(project_id=pj["id"], dept="electric", status="in_progress",
                                 worker_id=uid["电工甲"], start_date=yesterday,
                                 due_date=yesterday)   # 预计完成=3天前 → 逾期
            db.add(o)
            db.add(models.Shipment(project_id=pj["id"], status="pending"))
            await db.commit()
            oid = o.id

        # ---------- 顺序：不能跳过第一步 ----------
        r = await c.post(f"/api/orders/{oid}/electric_done", headers=H1)
        chk(r.status_code == 400 and "主板完成" in r.text,
            f"不能跳过主板直接点电路完成: {r.status_code} {r.text[:90]}")

        # ---------- 第一步：主板完成 = 结考核，但**状态不变** ----------
        r = await c.post(f"/api/orders/{oid}/mainboard_done", headers=H1)
        chk(r.status_code == 200, f"主板完成: {r.status_code} {r.text[:110]}")
        chk("逾期" in r.json()["message"], f"逾期在第一步就提醒: {r.json()['message']}")
        async with SessionLocal() as db:
            o = (await db.execute(select(models.DeptOrder).where(
                models.DeptOrder.id == oid))).scalar_one()
            chk(o.mainboard_done_flag is True, "1) 主板标记已置")
            chk(o.done_date == date.today().isoformat(),
                f"1) **考核日 = 主板完成那天**: {o.done_date}")
            chk(o.status == "in_progress",
                f"1) 状态仍是进行中（电路没接完，部门不算完成）: {o.status}")
            chk(o.wire_done_date is None, "1) 电路完成日还是空的")
            chk(_kpi_done(o) is True,
                "B) 报表口径上已经算完成了（否则按时率的分子分母一起少，数字看着还挺正常）")

        # ---------- A. 已结考核的单，逾期扫描不能再报 ----------
        async with SessionLocal() as db:
            res = await overdue.scan_overdue(db)
            await db.commit()
        async with SessionLocal() as db:
            msgs = list((await db.execute(select(models.Message).where(
                models.Message.biz_type == "order_overdue",
                models.Message.biz_id == oid))).scalars().all())
        chk(len(msgs) == 0,
            f"A) 主板完成后不再天天报逾期（考核已结，再骂就是废提醒）: 推了 {len(msgs)} 条")

        # ---------- 发货闸门：这时候还不能发 ----------
        board = (await c.get("/api/logistics/board", headers=H,
                             params={"ship_status": "未发货"})).json()
        row = next((x for x in board if x["project_id"] == pj["id"]), None)
        chk(row is not None and row["can_ship"] is False and "电工部" in row["gate_missing"],
            f"2) 电路没完成 → 发货闸门仍拦着: {row['gate_missing'] if row else '无此项目'}")

        # 重复点第一步要拦
        r = await c.post(f"/api/orders/{oid}/mainboard_done", headers=H1)
        chk(r.status_code == 400, f"1) 主板不能重复点: {r.status_code}")

        # ---------- 第二步：电路完成 = 部门完成、闸门放行，且**不改考核日** ----------
        r = await c.post(f"/api/orders/{oid}/electric_done", headers=H1)
        chk(r.status_code == 200, f"电路完成: {r.status_code} {r.text[:110]}")
        chk("电路图还没传" in r.json()["message"],
            f"3) 没传图要催（但不拦）: {r.json()['message']}")
        async with SessionLocal() as db:
            o = (await db.execute(select(models.DeptOrder).where(
                models.DeptOrder.id == oid))).scalar_one()
            chk(o.status == "done", f"2) 部门已完成: {o.status}")
            chk(o.wire_done_date == date.today().isoformat(),
                f"2) 电路完成日单独记: {o.wire_done_date}")
            chk(o.done_date == date.today().isoformat(),
                "2) **考核日没被第二步覆盖**（覆盖了就等于用电路完成日重算效率，第一步的考核作废）")
            chk(o.electric_done_flag is True, "2) 电路标记已置")
        # 物流收到通知
        async with SessionLocal() as db:
            lm = list((await db.execute(select(models.Message).where(
                models.Message.to_user_id == uid["物流员"],
                models.Message.biz_id == oid))).scalars().all())
            # 第二步的展示名 2026-08-14 按周瑞的反馈#400 从「电路」改成「安装调试」
            chk(len(lm) >= 1 and "安装调试" in lm[0].text,
                f"2) 物流收到可发货通知: {[m.text for m in lm]}")

        # ---------- 3. 电路图没传也能发货 ----------
        board = (await c.get("/api/logistics/board", headers=H,
                             params={"ship_status": "未发货"})).json()
        row = next((x for x in board if x["project_id"] == pj["id"]), None)
        chk(row is not None and row["can_ship"] is True,
            f"3) **电路图没传照样能发货**（忘传图不该顶住整个项目）: "
            f"can_ship={row['can_ship'] if row else '?'} missing={row['gate_missing'] if row else '?'}")

        # has_circuit 要如实反映
        orders = (await c.get("/api/orders", headers=H, params={"dept": "electric"})).json()
        row_o = next((x for x in (orders if isinstance(orders, list) else orders.get("rows", []))
                      if x["id"] == oid), None)
        chk(row_o is not None and row_o["has_circuit"] is False and row_o["mainboard_done_flag"] is True,
            f"接口带出三步状态给界面: {row_o and {k: row_o[k] for k in ('mainboard_done_flag','electric_done_flag','has_circuit')}}")

        # ---------- C. 返工要把三步全清 ----------
        r = await c.post(f"/api/orders/{oid}/reopen", headers=H)
        chk(r.status_code == 200, f"改回进行中: {r.status_code} {r.text[:90]}")
        async with SessionLocal() as db:
            o = (await db.execute(select(models.DeptOrder).where(
                models.DeptOrder.id == oid))).scalar_one()
            chk(o.mainboard_done_flag is False and o.electric_done_flag is False
                and o.done_date is None and o.wire_done_date is None,
                f"C) 返工清掉全部三步标记: 主板={o.mainboard_done_flag} 电路={o.electric_done_flag} "
                f"考核日={o.done_date} 电路日={o.wire_done_date}")
        # 清干净了才能重新走第一步（漏清主板标记的话这里会 400，单子卡死）
        r = await c.post(f"/api/orders/{oid}/mainboard_done", headers=H1)
        chk(r.status_code == 200, f"C) 返工后能重新点主板完成: {r.status_code} {r.text[:90]}")

        # ---------- 存量单回填 ----------
        async with SessionLocal() as db:
            old = models.DeptOrder(project_id=pj["id"], dept="electric", status="done",
                                   worker_id=uid["电工甲"], start_date="2026-07-01",
                                   due_date="2026-07-10", done_date="2026-07-09")
            db.add(old)
            await db.commit()
            old_id = old.id
        from app.data_migration import backfill_electric_mainboard
        async with SessionLocal() as db:
            res = await backfill_electric_mainboard(db)
        async with SessionLocal() as db:
            o = (await db.execute(select(models.DeptOrder).where(
                models.DeptOrder.id == old_id))).scalar_one()
            chk(o.mainboard_done_flag is True and o.electric_done_flag is True
                and o.wire_done_date == "2026-07-09",
                f"存量已完成的老单补上三步标记（否则界面显示成卡在第一步）: "
                f"主板={o.mainboard_done_flag} 电路日={o.wire_done_date}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
