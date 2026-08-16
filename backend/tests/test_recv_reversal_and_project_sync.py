"""采购收货的两个**静默失败**（2026-08-09 排查「带项目编号的收货是否自动出入库」时查出）。

两个都不报错、界面还提示成功，所以不可能靠人工发现——只能靠测试锁住。

一、冲红后重新收货，库存一动不动
  `_auto_stock_in` 的幂等守卫只滤 `is_reversal==False`。而冲红的做法是
  **保留原单**、另插一条反向单、把原单标 `reversed=True`（原单的 is_reversal 仍是 False）。
  于是原单永远命中守卫：仓库把收错的入库冲红后再收一次，接口返回 200、
  「收货成功」照常弹，**库存却不动**。同文件删除明细那段两个条件都滤了，这里是漏写。

二、收货后改订单编号，流水还挂在旧项目
  入库流水上的 project_id 决定这批料出现在**哪个项目的「物料需求」**里，
  仓库才领得到、成本才落得上。`_auto_stock_in` 幂等，改编号不会重新过账，
  不同步的话：采购明细上编号改对了，流水还挂着旧项目（或压根没挂），
  这批料在正确的项目里永远不出现。生产上已经有 ¥11,637 挂在查不到的编号上。

要锁死的：
  1. 冲红后重新收货，库存必须真的加回来
  2. 冲红的净额必须是 0（原单和冲红单不能被改价改到对不上）
  3. 没冲红的重复收货仍然只入一次（幂等不能被这次修改破坏）
  4. 收货后改订单编号 → 入库流水的 project_id 跟着改
  5. 改成查不到的编号（打错/非项目）→ 置空，不能留着旧项目
  6. 采购在明细里改编号，同样要同步
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="recvfix")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, func
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


async def stock_of(name: str) -> float:
    """该物料的净库存 = Σ入 − Σ出（排除冲红单与被冲红的原单），与应用口径一致。"""
    async with SessionLocal() as db:
        m = (await db.execute(select(models.WhMaterial).where(
            models.WhMaterial.name == name))).scalars().first()
        if not m:
            return 0.0
        r = await db.execute(select(models.WhTxn).where(
            models.WhTxn.material_id == m.id,
            models.WhTxn.is_reversal == False,   # noqa: E712
            models.WhTxn.reversed == False))     # noqa: E712
        return sum((t.qty or 0) if t.direction == "in" else -(t.qty or 0) for t in r.scalars().all())


async def txn_project(item_id: int):
    """该采购明细当前有效入库流水挂的项目 id。"""
    async with SessionLocal() as db:
        r = await db.execute(select(models.WhTxn).where(
            models.WhTxn.purchase_item_id == item_id,
            models.WhTxn.is_reversal == False,   # noqa: E712
            models.WhTxn.reversed == False))     # noqa: E712
        rows = r.scalars().all()
        return [t.project_id for t in rows]


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
        await c.post("/api/admin/users", headers=H, json={
            "username": "wh9", "password": "pass123", "full_name": "孙仓管", "role_id": rid["warehouse"]})
        r = await c.post("/api/auth/login", json={"username": "wh9", "password": "pass123"})
        Hw = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # 两个项目，用来验证改编号
        pa = (await c.post("/api/projects", headers=H, json={
            "code": "2026-901", "name": "甲项目"})).json()
        pb = (await c.post("/api/projects", headers=H, json={
            "code": "2026-902", "name": "乙项目"})).json()
        sup = (await c.post("/api/purchase-mgmt/suppliers", headers=H, json={"name": "永强机加工"})).json()

        async def make_item(name, code):
            r = await c.post("/api/purchase-mgmt/orders", headers=H, json={
                "supplier_id": sup["id"], "delivery_date": "2026-08-09", "project_code": code,
                "lines": [{"item_name": name, "qty": 10, "unit_price": 5, "received_amount": 50}]})
            assert r.status_code == 200, r.text
            return r.json()[0]

        async def receive(iid, **kw):
            body = {"arrival_date": "2026-08-09", "stock_location": None}
            body.update(kw)
            return await c.put(f"/api/purchase-mgmt/items/{iid}/receive", headers=Hw, json=body)

        # ================= 一、冲红后重新收货 =================
        it = await make_item("轴承6205", "2026-901")
        r = await receive(it["id"])
        chk(r.status_code == 200, f"收货成功: {r.status_code} {r.text[:80]}")
        chk(await stock_of("轴承6205") == 10, f"1) 收货后库存 10: {await stock_of('轴承6205')}")

        # 幂等：没冲红时重复收货不重复入库
        await receive(it["id"])
        chk(await stock_of("轴承6205") == 10, f"3) 重复收货不重复入库（幂等仍在）: {await stock_of('轴承6205')}")

        # 冲红那笔入库
        async with SessionLocal() as db:
            t = (await db.execute(select(models.WhTxn).where(
                models.WhTxn.purchase_item_id == it["id"]))).scalars().first()
            txn_id = t.id
        r = await c.post(f"/api/wh/txns/{txn_id}/reverse", headers=Hw)
        chk(r.status_code == 200, f"冲红成功: {r.status_code} {r.text[:100]}")
        chk(await stock_of("轴承6205") == 0, f"冲红后库存归 0: {await stock_of('轴承6205')}")

        # ★ 重新收货——修复前这里静默失败：接口 200，库存仍是 0
        r = await receive(it["id"])
        st = await stock_of("轴承6205")
        chk(r.status_code == 200, f"重新收货接口 200: {r.status_code}")
        chk(st == 10, f"1) 冲红后重新收货，库存必须加回来（修复前是 0，静默失败）: {st}")

        # 2) 冲红的两条必须互相抵消：原单 + 冲红单净额 0
        async with SessionLocal() as db:
            m = (await db.execute(select(models.WhMaterial).where(
                models.WhMaterial.name == "轴承6205"))).scalars().first()
            rows = (await db.execute(select(models.WhTxn).where(
                models.WhTxn.material_id == m.id))).scalars().all()
            old = [t for t in rows if t.reversed]
            rev = [t for t in rows if t.is_reversal]
            net = sum((t.amount or 0) * (1 if t.direction == "in" else -1) for t in old + rev)
        chk(len(old) == 1 and len(rev) == 1, f"冲红是「保留原单+反向单」: 原单{len(old)} 冲红单{len(rev)}")
        chk(abs(net) < 0.005, f"2) 原单与冲红单净额为 0（改价没把它们改歪）: {net}")

        # ================= 二、收货后改订单编号 =================
        it2 = await make_item("联轴器", "2026-901")
        await receive(it2["id"])
        chk(await txn_project(it2["id"]) == [pa["id"]], f"4) 收货时流水挂甲项目: {await txn_project(it2['id'])}")

        # 仓库在收货弹窗里把编号改成乙项目（走 /receive）
        r = await receive(it2["id"], project_code="2026-902")
        chk(r.status_code == 200, f"改编号收货 200: {r.status_code}")
        chk(await txn_project(it2["id"]) == [pb["id"]],
            f"4) 改编号后流水跟着挂到乙项目（修复前还挂着甲）: {await txn_project(it2['id'])}")

        # 5) 改成查不到的编号（打错，或「备用」这类非项目）→ 置空，不能留着旧项目
        r = await receive(it2["id"], project_code="2025-087")
        chk(await txn_project(it2["id"]) == [None],
            f"5) 编号查不到项目 → 流水项目置空，不留旧值: {await txn_project(it2['id'])}")

        # 6) 采购在明细里改编号（走 PUT /items/{id}），同样要同步。
        #    ⚠️ 这里必须改成**乙**项目：前面几步流水一直是甲(或 None)，
        #    改回甲的话，坏代码「什么都不做」也能蒙对，这条断言就白写了。
        r = await c.put(f"/api/purchase-mgmt/items/{it2['id']}", headers=H,
                        json={"project_code": "2026-902"})
        chk(r.status_code == 200, f"明细改编号 200: {r.status_code} {r.text[:80]}")
        chk(await txn_project(it2["id"]) == [pb["id"]],
            f"6) 采购改编号也同步到流水: {await txn_project(it2['id'])}")

        # 改别的字段不该动项目
        await c.put(f"/api/purchase-mgmt/items/{it2['id']}", headers=H, json={"unit_price": 6})
        chk(await txn_project(it2["id"]) == [pb["id"]], "只改单价不会动流水上的项目")

        # 收货只入库、不出库——这次改动没有把老行为改掉
        async with SessionLocal() as db:
            dirs = (await db.execute(select(models.WhTxn.direction, func.count()).where(
                models.WhTxn.purchase_item_id.isnot(None)).group_by(models.WhTxn.direction))).all()
        chk(all(d == "in" for d, _ in dirs), f"收货仍然只产生入库、不自动出库: {dirs}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
