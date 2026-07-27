"""🆕 从清单下单「跨项目模糊搜索未下单零件」(GET /api/purchase-mgmt/purchasable-cross)：
1. 不传 project_id：遍历所有进行中项目的该类型清单，只出 status=未下单 的行
   （已下单/已到货不出；已完成项目的行不出）；
2. q 模糊匹配：名称/规格 包含式、大小写不敏感、空格分隔多关键字全部命中(AND)、规格可命中；
3. 行带 project_id/project_code/project_name（弹窗项目编号列靠它）；
4. sheet_key 权限：采购员按 _BUYER_SHEET_MAP 分工——fangbusen(外协) 搜 standard 被拒 403，
   搜自己负责的 outsource 放行；未在分工表里的普通采购员不限；
5. sheet 必传且须合法（缺省 422 / 未知类型 400）。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="crosssearch")
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
    if not c: FAIL.append(m); print("FAIL:", m)

URL = "/api/purchase-mgmt/purchasable-cross"


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(u, rc):
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": u, "password": "pass123", "full_name": u, "role_id": rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]
        async def login(u):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': u, 'password': 'pass123'})).json()['access_token']}"}

        b1 = await mk("b1", "buyer")                 # 普通采购员（不在按人分表清单里 → 不限清单类型）
        await mk("fangbusen", "buyer_outsource")     # 🆕 分工表里的外协采购员：只能 outsource
        Hb1, Hfbs = await login("b1"), await login("fangbusen")

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1, json={"name": "跨搜供应商"})
        chk(r.status_code == 200, f"建供应商: {r.text[:120]}")
        sid = r.json()["id"]

        # ===== 造数据：两个进行中项目(P1/P2) + 一个已完成项目(P3)，各一张标准件清单 =====
        async with SessionLocal() as db:
            async def mkproj(code, name, status):
                p = models.Project(code=code, name=name, status=status)
                db.add(p); await db.flush()
                ds = models.Datasheet(project_id=p.id, name="标准件清单")
                db.add(ds); await db.flush()
                fn = models.Field(datasheet_id=ds.id, name="项目", type="text", sort_order=1)
                fs = models.Field(datasheet_id=ds.id, name="规格型号", type="text", sort_order=2)
                fq = models.Field(datasheet_id=ds.id, name="数量", type="number", sort_order=3)
                db.add_all([fn, fs, fq]); await db.flush()
                async def rec(nm, sp, q):
                    r_ = models.Record(datasheet_id=ds.id, values={str(fn.id): nm, str(fs.id): sp, str(fq.id): q})
                    db.add(r_); await db.flush()
                    return r_
                return p, ds, rec
            p1, ds1, rec1 = await mkproj("T-CS1", "跨搜项目甲", "进行中")
            p2, ds2, rec2 = await mkproj("T-CS2", "跨搜项目乙", "进行中")
            p3, ds3, rec3 = await mkproj("T-CS3", "跨搜项目丙(已完成)", "已完成")
            r1a = await rec1("六角螺栓", "M8×20", 10)
            await rec1("不锈钢垫圈", "M8", 50)
            r2a = await rec2("六角螺栓", "M10×30", 5)
            r2b = await rec2("导轨滑块", "HGR15", 2)
            await rec3("六角螺栓", "M12", 8)   # 已完成项目，不应出
            # r2b 已下单（有 PurchaseItem 引用且无到货日期）→ 不应出
            db.add(models.PurchaseItem(supplier_id=sid, item_name="导轨滑块", buyer_id=b1,
                                       source_sheet_id=ds2.id, source_record_id=r2b.id))
            # r2c 已到货（引用行 arrival_date 已填）→ 不应出
            r2c = await rec2("轴承座", "UCFL204", 4)
            db.add(models.PurchaseItem(supplier_id=sid, item_name="轴承座", buyer_id=b1,
                                       arrival_date="2026-07-01",
                                       source_sheet_id=ds2.id, source_record_id=r2c.id))
            await db.commit()
            p1id, p2id = p1.id, p2.id

        async def search(h, **params):
            r = await c.get(URL, headers=h, params=params)
            return r

        # ===== 1. 不带 q：只出进行中项目的未下单行；已下单/已到货/已完成项目均不出 =====
        r = await search(Hb1, sheet="standard")
        chk(r.status_code == 200, f"跨搜 200: {r.status_code} {r.text[:150]}")
        rows = r.json()
        names = sorted(x["item_name"] for x in rows)
        chk(names == ["不锈钢垫圈", "六角螺栓", "六角螺栓"], f"只出未下单行(2项目): {names}")
        chk(all(x["status"] == "未下单" for x in rows), "全部 status=未下单")
        # ===== 3. 行带项目编号 =====
        codes = sorted({x["project_code"] for x in rows})
        chk(codes == ["T-CS1", "T-CS2"], f"行带项目编号: {codes}")
        chk(all(x["project_id"] in (p1id, p2id) and x["project_name"] for x in rows),
            "行带 project_id/project_name")
        chk(all(x["sheet_key"] == "standard" and x["sheet_id"] and x["record_id"] for x in rows),
            "行带 sheet_key/sheet_id/record_id(下单要用)")

        # ===== 2a. 子串模糊（名称） =====
        rows = (await search(Hb1, sheet="standard", q="螺栓")).json()
        chk(len(rows) == 2 and all("螺栓" in x["item_name"] for x in rows),
            f"子串命中2行: {[x['item_name'] for x in rows]}")
        # ===== 2b. 大小写不敏感 + 规格命中 =====
        rows = (await search(Hb1, sheet="standard", q="m8")).json()   # 小写 q 命中大写规格 M8
        specs = sorted((x["item_name"], x["spec"]) for x in rows)
        chk(specs == [("不锈钢垫圈", "M8"), ("六角螺栓", "M8×20")], f"规格命中+大小写不敏感: {specs}")
        # ===== 2c. 空格分隔多关键字 AND =====
        rows = (await search(Hb1, sheet="standard", q="六角 m8")).json()
        chk(len(rows) == 1 and rows[0]["spec"] == "M8×20", f"多关键字AND只命中一行: {rows}")
        # 多关键字无命中 → 空
        rows = (await search(Hb1, sheet="standard", q="六角 HGR")).json()
        chk(rows == [], f"多关键字AND无命中: {rows}")
        # 无命中关键字 → 空
        rows = (await search(Hb1, sheet="standard", q="不存在的零件")).json()
        chk(rows == [], f"无命中为空: {rows}")

        # ===== 4. sheet_key 权限：fangbusen(外协) 搜 standard → 403；搜 outsource → 200 =====
        r = await search(Hfbs, sheet="standard", q="螺栓")
        chk(r.status_code == 403, f"外协采购员搜标准件 403: {r.status_code}")
        r = await search(Hfbs, sheet="outsource", q="螺栓")
        chk(r.status_code == 200 and r.json() == [], f"外协采购员搜外协放行(空): {r.status_code} {r.text[:80]}")

        # ===== 5. sheet 必传/合法 =====
        r = await c.get(URL, headers=Hb1, params={"q": "螺栓"})
        chk(r.status_code == 422, f"缺 sheet 422: {r.status_code}")
        r = await search(Hb1, sheet="nosuch")
        chk(r.status_code == 400, f"未知清单类型 400: {r.status_code}")

        # ===== 回归：单项目端点不受影响，仍出全部状态行 =====
        r = await c.get(f"/api/purchase-mgmt/purchasable/{p2id}", headers=Hb1, params={"sheet": "standard"})
        chk(r.status_code == 200, f"单项目端点 200: {r.status_code}")
        sts = sorted(x["status"] for x in r.json())
        chk(sts == ["已下单", "已到货", "未下单"], f"单项目端点仍出全部状态: {sts}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
