"""反馈 2026-08-07（杨坛）：出库登记要能搜采购单号，然后选里面的物料出库。

仓库出库时手头拿的是一张采购单，而登记表单只能从几百个物料里翻着找。

要锁死的：
  1. 按单号能带出该单的物料行（模糊匹配，仓库记不全单号）
  2. **只返回已到货的行**——没到货的东西出不了库，列出来只会让人误选
  3. 匹配不到物料主数据的行照样返回，但 material_id 为空并给出原因；
     直接不返回的话，仓库会以为"这单的料怎么少了"，比列出来更糟
  4. 带上现存数量——出库前要看得见有没有货
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="poitems")
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        await c.post("/api/admin/users", headers=H, json={
            "username": "w1", "password": "pass123", "full_name": "孙仓管", "role_id": rid["warehouse"]})
        r = await c.post("/api/auth/login", json={"username": "w1", "password": "pass123"})
        Hw = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # 物料主数据：只建「密封圈」，让另一行故意匹配不上
        r = await c.post("/api/wh/materials", headers=Hw, json={
            "name": "密封圈", "spec": "DN50", "unit": "个", "location": "A-03", "init_stock": 30})
        mid = r.json()["id"]

        # 直接造采购明细（走接口建采购单太绕，这里的被测对象是查询逻辑）
        async with SessionLocal() as db:
            sup = models.Supplier(name="顺鑫机电")
            db.add(sup)
            await db.flush()
            sid = sup.id
            db.add_all([
                models.PurchaseItem(supplier_id=sid, po_no="CG20260801-001", item_name="密封圈", spec="DN50",
                                    qty=10, arrival_date="2026-08-01", project_code="2026-071"),
                models.PurchaseItem(supplier_id=sid, po_no="CG20260801-001", item_name="法兰盘", spec="DN80",
                                    qty=4, arrival_date="2026-08-01", project_code="2026-071"),
                # 没到货的：不该出现在结果里
                models.PurchaseItem(supplier_id=sid, po_no="CG20260801-001", item_name="轴承", spec="6205",
                                    qty=2, arrival_date=None, project_code="2026-071"),
                # 别的单：按单号筛时不该混进来
                models.PurchaseItem(supplier_id=sid, po_no="CG20260715-009", item_name="密封圈", spec="DN50",
                                    qty=5, arrival_date="2026-07-15"),
            ])
            await db.commit()

        async def po(**kw):
            r = await c.get("/api/wh/po-items", headers=Hw, params=kw)
            assert r.status_code == 200, r.text
            return r.json()

        rows = await po(po_no="CG20260801-001")
        names = sorted(x["item_name"] for x in rows)
        chk(set(names) == {"法兰盘", "密封圈"}, f"1) 按单号带出该单的行: {names}")
        chk(all(x["po_no"] == "CG20260801-001" for x in rows), "1) 不会混进别的单")

        # 2) 未到货的不返回
        chk("轴承" not in names, "2) 没到货的行不返回（出不了库，列出来只会误选）")

        # 模糊匹配
        chk(len(await po(po_no="20260801")) == 2, "1) 单号模糊匹配（仓库记不全单号）")
        chk(len(await po(po_no="CG2026")) == 3, "1) 更短的前缀匹配到两张单共 3 行")
        chk(len(await po(po_no="不存在的单号")) == 0, "1) 搜不到就是 0")

        # 3) 匹配得上/匹配不上
        seal = [x for x in rows if x["item_name"] == "密封圈"][0]
        flan = [x for x in rows if x["item_name"] == "法兰盘"][0]
        chk(seal["material_id"] == mid, f"3) 匹配到物料主数据: {seal['material_id']}")
        chk(seal["unmatched_reason"] is None, "3) 匹配上的没有拦截原因")
        chk(flan["material_id"] is None, "3) 匹配不上的 material_id 为空")
        chk(bool(flan["unmatched_reason"]) and "建档" in flan["unmatched_reason"],
            f"3) 匹配不上的给出原因并指路: {flan['unmatched_reason']}")

        # 4) 带现存
        chk(seal["stock"] == 30, f"4) 带上现存数量: {seal['stock']}")
        chk(seal["project_code"] == "2026-071", "带上项目编号（出库要挂项目）")
        chk(seal["qty"] == 10, "带上采购数量")

        # 不传单号 = 列最近的已到货行（别一上来就空着让人不知道能干嘛）
        chk(len(await po()) == 3, "不传单号时列出最近的已到货行")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
