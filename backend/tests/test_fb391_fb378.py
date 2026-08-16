"""反馈#391（李新新）+ #378（超级管理员）。

#391「这个现有库存是库里有的（没有订单编号）还是我有订单已采购的？采购从哪里能确认库存？」
    ——问到点子上了。采购申请/从清单下单那一列给的是**全部库存**，其中大半是别的项目
    已经买好、动不得的料。生产实测：517 种有货物料里 394 种挂着项目编号（数量 1111/7709）。
    采购看到「现有库存 5」就不下单，可那 5 个是别人项目的 → **系统性少采**。
    改成：可用库存(通用物料) 单独一列，建议采购只减可用；项目占用另外提示。

#378「预计到货时间不给采购修改了，只支持一次维护，后面不给改了」
    这个日期是逾期提醒和催货的唯一基准，一路往后改逾期就永远不会发生。
    填过之后普通采购改不了；采购主管/管理层可以改（供应商确实改期），改动照旧留痕。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb391")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

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
        # 普通采购员（非主管）——#378 的锁就是冲他来的
        r = await c.post("/api/admin/users", headers=H, json={
            "username": "buy1", "password": "pass123", "full_name": "采购小张",
            "role_id": rid["buyer"]})
        chk(r.status_code == 200, f"建普通采购员: {r.status_code} {r.text[:80]}")
        r = await c.post("/api/auth/login", json={"username": "buy1", "password": "pass123"})
        HB = {"Authorization": f"Bearer {r.json()['access_token']}"}

        rr = await c.post("/api/projects", headers=H, json={"code": "P-001", "name": "测试项目"})
        chk(rr.status_code == 200, f"建项目: {rr.status_code}")
        sup = (await c.post("/api/purchase-mgmt/suppliers", headers=H, json={"name": "供应商甲"})).json()["id"]

        async def buy_and_receive(name, qty, price, code):
            it = (await c.post("/api/purchase-mgmt/items", headers=H, json={
                "supplier_id": sup, "item_name": name, "qty": qty,
                "unit_price": price, "project_code": code})).json()
            await c.put(f"/api/purchase-mgmt/items/{it['id']}/receive", headers=H, json={
                "arrival_date": "2026-08-01", "unit_price": price,
                "received_amount": round(qty * price, 2)})
            return it["id"]

        # 通用料：没有订单编号 → 采购可自由支配
        await buy_and_receive("通用螺母", 30, 2, None)
        # 项目料：挂了订单编号 → 是 P-001 的，采购动不得
        await buy_and_receive("项目轴承", 12, 40, "P-001")

        # ---------- #391 ----------
        from app.database import SessionLocal as SL
        from app.routers.purchase_mgmt_router import _build_stock_by_key
        async with SL() as db:
            sk = await _build_stock_by_key(db)
        free_n, held_n = sk.get(("通用螺母", ""), (0, 0))
        free_p, held_p = sk.get(("项目轴承", ""), (0, 0))
        chk((free_n, held_n) == (30, 0),
            f"#391 通用料 → 全算可用: 可用{free_n} 项目占用{held_n}")
        chk((free_p, held_p) == (0, 12),
            f"#391 挂编号的料 → 全算项目占用，可用为 0: 可用{free_p} 项目占用{held_p}")

        # 建议采购必须按**可用**算：需求 20，项目轴承在库 12 但动不得 → 建议采购 20 而不是 8
        class _Row:
            pass
        from app.routers.purchase_mgmt_router import _stock_key
        need = 20
        free, held = sk.get(_stock_key("项目轴承", None), (0.0, 0.0))
        suggest_new = max(0.0, need - free)
        suggest_old = max(0.0, need - (free + held))
        chk(suggest_new == 20 and suggest_old == 8,
            f"#391 建议采购：新口径 {suggest_new}（对） vs 旧口径 {suggest_old}（少采 12，"
            f"因为把别的项目的料当成自己的了）")

        # ---------- #378 ----------
        # ⚠️ 必须由**采购员本人**下单：明细有行级隔离（_buyer_restricted，只能编辑自己下的单）。
        #    用 admin 建的单去测锁，403 会来自「无权编辑他人明细」而不是 #378 的锁——
        #    断言照样绿，但什么都没验到。
        it = (await c.post("/api/purchase-mgmt/items", headers=HB, json={
            "supplier_id": sup, "item_name": "待锁料", "qty": 5,
            "unit_price": 10, "project_code": "P-001"})).json()
        iid = it["id"]
        chk(not it.get("expected_arrival"), "前提：新建时预计到货是空的")

        # 首次填 —— 普通采购可以
        r = await c.put(f"/api/purchase-mgmt/items/{iid}", headers=HB,
                        json={"expected_arrival": "2026-08-20"})
        chk(r.status_code == 200 and r.json()["expected_arrival"] == "2026-08-20",
            f"#378 首次填预计到货，普通采购可以: {r.status_code} {r.text[:80]}")

        # 首次填**不**推「预计到货变更」留痕。
        # #378 之后普通采购能做的只剩首次填，不滤掉的话每建一条明细就 ping 一次主管+管理层；
        # 历史数据 195 条通知里 84 条是「由 未填 改为」，43% 是这种噪音。
        from sqlalchemy import select as _sel
        from app.database import SessionLocal as _SL
        async with _SL() as _db:
            n = len((await _db.execute(_sel(models.Message).where(
                models.Message.biz_type == "po_expected_changed",
                models.Message.biz_id == iid))).scalars().all())
        chk(n == 0, f"#378 首次填不推留痕通知（那是下单动作不是改期）: 推了 {n} 条")

        # 再改 —— 普通采购不行。
        # ⚠️ 只断言 403 是不够的：行级隔离也返回 403，撞上它断言照样绿。必须验错误文案。
        r = await c.put(f"/api/purchase-mgmt/items/{iid}", headers=HB,
                        json={"expected_arrival": "2026-09-10"})
        chk(r.status_code == 403 and "只能维护一次" in r.text,
            f"#378 填过之后普通采购改不了，且拦的是 #378 的锁不是行级隔离: {r.status_code} {r.text[:110]}")
        cur = (await c.get("/api/purchase-mgmt/items", headers=H)).json()
        got = next((x for x in (cur if isinstance(cur, list) else cur.get("rows", []))
                    if x["id"] == iid), None)
        chk(got and got["expected_arrival"] == "2026-08-20",
            f"#378 被拦下后日期没变: {got['expected_arrival'] if got else None}")

        # 填同样的值不算改（重复提交表单不该报错）
        r = await c.put(f"/api/purchase-mgmt/items/{iid}", headers=HB,
                        json={"expected_arrival": "2026-08-20"})
        chk(r.status_code == 200, f"#378 原值重填放行（重复提交不该报错）: {r.status_code}")

        # 主管/管理层可以改
        r = await c.put(f"/api/purchase-mgmt/items/{iid}", headers=H,
                        json={"expected_arrival": "2026-09-10"})
        chk(r.status_code == 200 and r.json()["expected_arrival"] == "2026-09-10",
            f"#378 管理层可以改（供应商确实改期时的出口）: {r.status_code}")

        # 批量接口同样受锁
        it2 = (await c.post("/api/purchase-mgmt/items", headers=HB, json={
            "supplier_id": sup, "item_name": "待锁料2", "qty": 1,
            "unit_price": 1, "project_code": "P-001"})).json()
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=HB,
                        json={"ids": [it2["id"]], "expected_arrival": "2026-08-22"})
        chk(r.status_code == 200, f"#378 批量首次填放行: {r.status_code} {r.text[:80]}")
        r = await c.put("/api/purchase-mgmt/items/batch-expected-arrival", headers=HB,
                        json={"ids": [it2["id"]], "expected_arrival": "2026-09-30"})
        chk(r.status_code == 403,
            f"#378 批量改期同样被拦（只锁单条接口等于没锁，批量是绕过口）: {r.status_code}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
