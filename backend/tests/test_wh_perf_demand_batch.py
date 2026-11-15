"""仓库性能：物料需求总览改批量预取（2026-08-15）。

改之前 /api/wh/demand-overview 是 `for p in 项目: await _demand_rows(db, p.id)`，
每个项目 2 条流水聚合 + 1 条清单查询，再对每张清单查 字段/记录/采购项 各一条。
**生产实测 107 个项目、321 张清单 = 1333 条 SQL / 1.0 秒**，而且随项目数线性膨胀
——项目只会越来越多，这条曲线一直往上走。

改成 `_demand_ctx` 一次性批量取回，SQL 条数固定、与项目数无关。
性能改写最怕的是"快了但算错了"，所以本文件盯住的是**口径与条数两件事**：

  1. 总览里每个项目的数，跟单独查 /demand/{pid} 逐项目算出来的**必须一模一样**
  2. 项目数从 3 个涨到 13 个，SQL 条数**不许跟着涨**（这才是这次改动的全部意义）
  3. 单项目 /demand/{pid} 本身不受影响（它现在走的是同一个 ctx，只是只装一个项目）
  4. 清单需求 / 采购入库两个来源、已领用扣减、建议采购(#393 口径) 都还对
  5. ensure_indexes 幂等：跑两遍不炸，第二遍一个都不该再建
"""
import asyncio, os, sys, tempfile, time

tmp = tempfile.mkdtemp(prefix="whperf")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import event
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, ensure_indexes
from app import models

FAIL = []
SQL = {"n": 0, "on": False}


@event.listens_for(engine.sync_engine, "after_cursor_execute")
def _count(conn, cursor, statement, params, context, executemany):
    if SQL["on"]:
        SQL["n"] += 1


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    n1 = await ensure_indexes(engine)
    n2 = await ensure_indexes(engine)
    chk(n2 == 0, f"5) ensure_indexes 幂等：第二遍不该再建（第一遍 {n1} 个，第二遍 {n2} 个）")
    # 模拟生产的真实处境：表早就存在、后来才给某列加 index=True，create_all 永远不会补它
    from sqlalchemy import text as _text
    async with engine.begin() as conn:
        await conn.execute(_text("DROP INDEX ix_purchase_items_source_sheet_id"))
    n3 = await ensure_indexes(engine)
    async with engine.connect() as conn:
        got = await conn.run_sync(lambda sc: {i["name"] for i in
                                              __import__("sqlalchemy").inspect(sc).get_indexes("purchase_items")})
    chk(n3 == 1 and "ix_purchase_items_source_sheet_id" in got,
        f"5) 存量表缺的索引会被补回来（生产上这样缺了 23 个）: 补了 {n3} 个，purchase_items 现有 {len(got)} 个索引")
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    from app.routers.purchase_mgmt_router import _PURCHASABLE_SHEETS
    sheet_name, item_col, spec_col, qty_col = _PURCHASABLE_SHEETS["standard"][:4]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # ---- 造 12 个项目：每个一张标准件清单(3 行) + 一批挂项目的入库/出库 ----
        mats = {}
        for name, spec in [("方矩管", "75*45*2.4"), ("角钢", "50*50*5"), ("圆钢", "Φ20"),
                           ("轴承", "6205"), ("密封圈", "Φ30")]:
            m = (await c.post("/api/wh/materials", headers=H, json={
                "name": name, "spec": spec, "unit": "个", "init_stock": 0})).json()
            mats[name] = m["id"]

        pids = []
        for i in range(12):
            pj = (await c.post("/api/projects", headers=H,
                               json={"code": f"P-{i:03d}", "name": f"批量测试{i}"})).json()
            pids.append(pj["id"])
            async with SessionLocal() as db:
                ds = models.Datasheet(project_id=pj["id"], name=sheet_name)
                db.add(ds)
                await db.flush()
                fmap = {}
                for j, cn in enumerate([item_col, spec_col, qty_col]):
                    f = models.Field(datasheet_id=ds.id, name=cn, type="text", sort_order=j)
                    db.add(f)
                    await db.flush()
                    fmap[cn] = str(f.id)
                for j, (nm, sp) in enumerate([("方矩管", "75*45*2.4"), ("角钢", "50*50*5"),
                                              ("圆钢", "Φ20")]):
                    db.add(models.Record(datasheet_id=ds.id, sort_order=j, values={
                        fmap[item_col]: nm, fmap[spec_col]: sp, fmap[qty_col]: str(10 + j)}))
                await db.commit()
            # 挂本项目的入库：方矩管(在清单里) + 轴承(不在清单里 → 走"采购"那条腿)
            for nm, qty in [("方矩管", 8), ("轴承", 4)]:
                rr = await c.post("/api/wh/txns", headers=H, json={
                    "material_id": mats[nm], "direction": "in", "qty": qty, "unit_price": 10,
                    "biz_date": "2026-08-01", "source": "采购收货", "project_id": pj["id"]})
                chk(rr.status_code == 200, f"入库 {nm}: {rr.status_code} {rr.text[:60]}") if i == 0 else None
            # 领用出库一部分（验 #393 建议采购要扣掉已领）
            await c.post("/api/wh/txns", headers=H, json={
                "material_id": mats["方矩管"], "direction": "out", "qty": 3,
                "biz_date": "2026-08-02", "source": "生产领用", "project_id": pj["id"]})

        # 再来一个只属于自己的项目，专门验 #393 的建议采购口径（上面 12 个项目共用物料，
        # 全局现存被叠成 60，压不出 (需求−已领)−现存 那个减法）
        pj393 = (await c.post("/api/projects", headers=H,
                              json={"code": "P-393", "name": "建议采购口径"})).json()
        pids.append(pj393["id"])
        async with SessionLocal() as db:
            ds = models.Datasheet(project_id=pj393["id"], name=sheet_name)
            db.add(ds)
            await db.flush()
            fmap = {}
            for j, cn in enumerate([item_col, spec_col, qty_col]):
                f = models.Field(datasheet_id=ds.id, name=cn, type="text", sort_order=j)
                db.add(f)
                await db.flush()
                fmap[cn] = str(f.id)
            db.add(models.Record(datasheet_id=ds.id, sort_order=0, values={
                fmap[item_col]: "密封圈", fmap[spec_col]: "Φ30", fmap[qty_col]: "10"}))
            await db.commit()
        await c.post("/api/wh/txns", headers=H, json={
            "material_id": mats["密封圈"], "direction": "in", "qty": 4, "unit_price": 5,
            "biz_date": "2026-08-01", "source": "采购收货", "project_id": pj393["id"]})
        await c.post("/api/wh/txns", headers=H, json={
            "material_id": mats["密封圈"], "direction": "out", "qty": 3,
            "biz_date": "2026-08-02", "source": "生产领用", "project_id": pj393["id"]})

        # ================= 1) 总览 vs 逐项目明细，数必须对得上 =================
        ov = (await c.get("/api/wh/demand-overview", headers=H)).json()
        chk(len(ov) == 13, f"1) 13 个项目都在总览里: {len(ov)}")
        bad = []
        for row in ov:
            rows = (await c.get(f"/api/wh/demand/{row['project_id']}", headers=H)).json()
            pending = sum(1 for x in rows
                          if x["in_stock"] and (x["demand_qty"] or 0) - (x["issued_qty"] or 0) > 0)
            issued = sum(1 for x in rows if (x["issued_qty"] or 0) > 0)
            if (row["total_lines"], row["pending_out"], row["issued_out"]) != (len(rows), pending, issued):
                bad.append((row["code"], (row["total_lines"], row["pending_out"], row["issued_out"]),
                            (len(rows), pending, issued)))
        chk(not bad, f"1) **总览的汇总数 = 逐项目明细自己数出来的**（批量口径没跑偏）: {bad[:3]}")

        # ================= 4) 两个来源 / 已领扣减 / 建议采购口径 =================
        rows = (await c.get(f"/api/wh/demand/{pids[0]}", headers=H)).json()
        by_name = {x["item_name"]: x for x in rows}
        chk(len(rows) == 4, f"4) 3 行清单 + 1 行只走采购入库的轴承 = 4 行: {len(rows)}")
        chk(by_name["方矩管"]["source"] == "清单" and by_name["轴承"]["source"] == "采购",
            f"4) 来源列分得清: 方矩管={by_name['方矩管']['source']} 轴承={by_name['轴承']['source']}")
        chk(by_name["方矩管"]["issued_qty"] == 3, f"4) 已领用带出来了(只算本项目): {by_name['方矩管']['issued_qty']}")
        # ⚠️ stock 是**全库现存**，不是"本项目的现存"：12 个项目各入 8 领 3 → 12×5=60。
        #    这条特意锁一下，免得哪天有人看着这列以为是本项目的量，把口径改了。
        chk(by_name["方矩管"]["stock"] == 60,
            f"4) 现存列是全库现存(不是本项目的): {by_name['方矩管']['stock']}")
        chk("密封圈" not in by_name, "4) 跟本项目无关的物料不进需求表")
        # 密封圈只在 P-393：需求 10、已领 3、全库现存 4−3=1
        #   → 还没领的 7 − 现存 1 = 建议采购 6（#393 口径：先扣已领，再扣现存）
        r393 = {x["item_name"]: x for x in
                (await c.get(f"/api/wh/demand/{pj393['id']}", headers=H)).json()}["密封圈"]
        chk((r393["demand_qty"], r393["issued_qty"], r393["stock"], r393["suggest_purchase"])
            == (10, 3, 1, 6),
            f"4) 建议采购 = (需求−已领)−现存（#393）: 需求={r393['demand_qty']} "
            f"已领={r393['issued_qty']} 现存={r393['stock']} 建议={r393['suggest_purchase']}")

        # ================= 2) SQL 条数不许跟项目数一起涨 =================
        async def sql_of(n_projects: int) -> tuple:
            # 只留前 n 个项目参与总览（其余标删除）
            async with SessionLocal() as db:
                for k, pid in enumerate(pids):
                    p = await db.get(models.Project, pid)
                    p.is_deleted = k >= n_projects
                await db.commit()
            await c.get("/api/wh/demand-overview", headers=H)   # 预热
            SQL["n"] = 0; SQL["on"] = True
            t0 = time.perf_counter()
            r = await c.get("/api/wh/demand-overview", headers=H)
            el = (time.perf_counter() - t0) * 1000
            SQL["on"] = False
            return SQL["n"], len(r.json()), el

        q3, n3, t3 = await sql_of(3)
        q12, n12, t12 = await sql_of(13)
        chk(n3 == 3 and n12 == 13, f"2) 参与总览的项目数对: {n3} / {n12}")
        chk(q12 == q3,
            f"2) **项目 3→13，SQL 条数不变**（改之前是每项目 ~12 条，会涨到 4 倍）: "
            f"3 个项目 {q3} 条 / 13 个项目 {q12} 条")
        chk(q12 <= 15, f"2) 总览总共就十来条 SQL: {q12} 条（{t12:.0f}ms）")

        # ================= 3) 单项目查询本身没被拖累 =================
        async with SessionLocal() as db:
            for pid in pids:
                (await db.get(models.Project, pid)).is_deleted = False
            await db.commit()
        SQL["n"] = 0; SQL["on"] = True
        r = await c.get(f"/api/wh/demand/{pids[0]}", headers=H)
        SQL["on"] = False
        chk(r.status_code == 200 and SQL["n"] <= 15,
            f"3) 单项目 /demand 也只有十来条 SQL（没有为了总览把单查拖慢）: {SQL['n']} 条")
        chk(len(r.json()) == 4, f"3) 单项目明细还是 4 行: {len(r.json())}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
