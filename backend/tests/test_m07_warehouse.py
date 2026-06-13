"""M07 仓库组：物料/出入库(超量拦截/单号)/实时库存/冲红回滚/收发存勾稽/低库存预警/权限。"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="m07test")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

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
        r = await c.post("/api/auth/login", json={"username":"admin","password":"admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        async def mk(u, rc, fn):
            r = await c.post("/api/admin/users", headers=H, json={"username":u,"password":"pass123","full_name":fn,"role_id":rid[rc]})
            return r.json()["id"]
        for u, rc, fn in [("w1","warehouse","孙仓管"),("wl","warehouse_lead","郑主管"),
                          ("d1","designer","张工"),("lo","logistics","马师傅")]:
            await mk(u, rc, fn)
        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        Hw, Hwl, Hd = await login("w1"), await login("wl"), await login("d1")

        # ===== 物料：建 + 同名同规格查重 + 安全库存 =====
        r = await c.post("/api/wh/materials", headers=Hw, json={
            "name":"搅拌桨","spec":"15L","unit":"个","safety_stock":5,"init_stock":10,"category":"搅拌件"})
        chk(r.status_code==200 and r.json()["stock"]==10, f"建物料初始库存: {r.text[:120]}")
        mid = r.json()["id"]
        r = await c.post("/api/wh/materials", headers=Hw, json={"name":"搅拌桨","spec":"15L"})
        chk(r.status_code==409, "同名同规格查重")
        # 设计师只读查库存（全员可读 materials）
        r = await c.get("/api/wh/materials", headers=Hd)
        chk(r.status_code==200 and r.json()["total"]==1, "设计师可只读查库存")
        # 设计师不能建物料
        r = await c.post("/api/wh/materials", headers=Hd, json={"name":"x"})
        chk(r.status_code==403, "设计师不能建物料")

        # ===== 出入库 =====
        # 入库 20 → 库存 30
        r = await c.post("/api/wh/txns", headers=Hw, json={
            "material_id":mid,"biz_date":"2026-06-05","direction":"in","qty":20,"source":"采购入库","party":"顺鑫"})
        chk(r.status_code==200 and r.json()["message"].startswith("已登记 RK"), f"入库单号RK: {r.json()}")
        r = await c.get("/api/wh/materials", headers=Hw)
        chk(r.json()["materials"][0]["stock"]==30, f"入库后库存30: {r.json()['materials'][0]['stock']}")
        # 出库超量拦截
        r = await c.post("/api/wh/txns", headers=Hw, json={"material_id":mid,"biz_date":"2026-06-06","direction":"out","qty":999})
        chk(r.status_code==400 and "超过现存" in r.text, "超量出库拦截")
        # 出库 25 → 库存 5（=安全库存,不低于）
        r = await c.post("/api/wh/txns", headers=Hw, json={"material_id":mid,"biz_date":"2026-06-06","direction":"out","qty":25,"party":"生产领用"})
        chk(r.status_code==200 and "CK" in r.json()["message"], "出库单号CK")
        r = await c.get("/api/wh/materials", headers=Hw)
        chk(r.json()["materials"][0]["stock"]==5, "出库后库存5")
        # 再出 1 → 库存 4 < 安全库存 5 → 预警推仓库主管
        await c.post("/api/wh/txns", headers=Hw, json={"material_id":mid,"biz_date":"2026-06-07","direction":"out","qty":1})
        r = await c.get("/api/wh/materials", headers=Hw)
        chk(r.json()["materials"][0]["stock"]==4 and r.json()["materials"][0]["low"], "低于安全库存标记")
        msgs = (await c.get("/api/messages", headers=Hwl)).json()
        chk(any("低库存预警" in m["text"] for m in msgs), "仓库主管收低库存预警")
        chk(r.json()["low_count"]==1, "low_count统计")

        # ===== 冲红：把出库25冲掉 → 库存回滚 +25 =====
        txns = (await c.get("/api/wh/txns?direction=out", headers=Hw)).json()
        out25 = [t for t in txns if t["qty"]==25][0]
        r = await c.post(f"/api/wh/txns/{out25['id']}/reverse", headers=Hw)
        chk(r.status_code==200, f"冲红: {r.text[:120]}")
        r = await c.get("/api/wh/materials", headers=Hw)
        chk(r.json()["materials"][0]["stock"]==29, f"冲红回滚后库存29: {r.json()['materials'][0]['stock']}")
        # 原单标记 reversed + 生成反向单
        txns = (await c.get("/api/wh/txns", headers=Hw)).json()
        chk(any(t["id"]==out25["id"] and t["reversed"] for t in txns), "原单标记reversed")
        chk(any(t["is_reversal"] and t["source"]=="冲红" for t in txns), "生成冲红反向单")
        # 冲红单不可再冲红
        rev = [t for t in txns if t["is_reversal"]][0]
        r = await c.post(f"/api/wh/txns/{rev['id']}/reverse", headers=Hw)
        chk(r.status_code==400, "冲红单不可再冲红")

        # ===== 🆕 #83 冲红入库单的负库存校验：入10出8后冲红入库会致负库存,应拒 =====
        m2 = (await c.post("/api/wh/materials", headers=Hw, json={"name":"密封圈","spec":"DN50","unit":"个","init_stock":0})).json()["id"]
        await c.post("/api/wh/txns", headers=Hw, json={"material_id":m2,"biz_date":"2026-06-05","direction":"in","qty":10,"source":"采购入库"})
        await c.post("/api/wh/txns", headers=Hw, json={"material_id":m2,"biz_date":"2026-06-06","direction":"out","qty":8,"party":"领用"})
        in10 = [t for t in (await c.get("/api/wh/txns?direction=in", headers=Hw)).json() if t["material_id"]==m2 and t["qty"]==10][0]
        r = await c.post(f"/api/wh/txns/{in10['id']}/reverse", headers=Hw)
        chk(r.status_code==400 and "不足冲红" in r.text, f"#83 冲红入库致负库存被拒: {r.status_code} {r.text[:80]}")
        out8 = [t for t in (await c.get("/api/wh/txns?direction=out", headers=Hw)).json() if t["material_id"]==m2 and t["qty"]==8][0]
        await c.post(f"/api/wh/txns/{out8['id']}/reverse", headers=Hw)  # 先冲出库,库存回到10
        r = await c.post(f"/api/wh/txns/{in10['id']}/reverse", headers=Hw)
        chk(r.status_code==200, f"#83 先冲出库后入库单可冲红: {r.text[:80]}")

        # ===== 收发存汇总勾稽（2026-06）=====
        r = await c.get("/api/wh/summary?period=2026-06", headers=Hw)
        row = [x for x in r.json() if x["material_id"]==mid][0]
        # 期初(6月前)=init10; 本期入=20+25(冲红反向in); 本期出=25+1; 期末=10+45-26=29
        chk(row["opening"]==10, f"期初=10: {row['opening']}")
        chk(row["closing"]==row["opening"]+row["in_qty"]-row["out_qty"], "期初+入-出=期末勾稽")
        chk(row["closing"]==29, f"期末=29: {row['closing']}")

        # ===== 发货清单上传推物流 =====
        r = await c.post("/api/projects", headers=H, json={"code":"WH-1","name":"仓库测试项目"})
        wpid = r.json()["id"]
        # 该项目经迁移补发货待办行（进行中项目；模拟 run_all）
        from app.data_migration import backfill_shipments
        async with SessionLocal() as db:
            await backfill_shipments(db)
        r = await c.post(f"/api/wh/ship-list/{wpid}", headers=Hw,
                         files={"file":("发货清单.xlsx", io.BytesIO(b"X"), "application/vnd.ms-excel")})
        chk(r.status_code==200, f"发货清单上传: {r.text[:120]}")
        msgs = (await c.get("/api/messages", headers=Hd)).json()  # designer 不是物流,应收不到
        Hlo = await login("lo")
        msgs = (await c.get("/api/messages", headers=Hlo)).json()
        chk(any("发货清单" in m["text"] for m in msgs), "物流收发货清单推送")
        # 物流看板该项目仓库清单列出现
        board = (await c.get("/api/logistics/board", headers=Hlo)).json()
        wrow = [x for x in board if x["code"]=="WH-1"][0]
        chk(len(wrow["ship_list_files"])==1, "物流看板出现仓库发货清单")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
