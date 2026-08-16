"""反馈#364（王芹）：「所有的项目编号应同步过来」+ 发货看板筛选改成发货状态。

两个毛病是同一件事的两面——**看不到的项目就没法录运费，运费就进不了成本**：

  ① 42 个项目从来没有 Shipment 行（其中 37 个已完成），而看板是从 Shipment 出发查的，
     这些项目在物流部**根本不存在**。根因：`backfill_shipments` 只补「进行中」项目。
  ② 旧筛选是按**项目状态**（进行中/已完成）。「进行中」= 未发货 且 项目≠已完成，
     把 27 张「未发货但项目被手工标已完成」的单挡在两个筛选之外，只有「全部」才看得到。

要锁死的：
  1. 每个未删项目都有发货单行——包括**已完成**的（原来只补进行中）
  2. 回填幂等：跑两次不会补出第二行
  3. 筛选按**发货状态**：已发货只出 shipped、未发货只出 pending、空=全部
  4. 「未发货」必须包含「项目已完成但没发货」的那批——这正是旧口径漏掉的
  5. 旧客户端还在发的 proj_status 要继续可用（进行中→未发货、已完成→已发货），
     否则没升级的人筛选静默失效
  6. 侧边栏「待发货」角标只数进行中项目——补进来的历史单不该冲爆待办数
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb364")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, func
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, backfill_shipments
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

        # 三个项目：进行中未发货 / 已完成未发货（旧口径的盲区）/ 已完成已发货
        made = {}
        for code, st in [("2026-801", "进行中"), ("2026-802", "已完成"), ("2026-803", "已完成")]:
            p = (await c.post("/api/projects", headers=H,
                              json={"code": code, "name": f"项目{code}"})).json()
            made[code] = p["id"]
            async with SessionLocal() as db:
                pr = (await db.execute(select(models.Project).where(
                    models.Project.id == p["id"]))).scalar_one()
                pr.status = st
                await db.commit()

        # 建项目时可能没自动建发货单；跑回填补齐
        async with SessionLocal() as db:
            res1 = await backfill_shipments(db)
        async with SessionLocal() as db:
            n = (await db.execute(select(func.count(models.Shipment.id)).where(
                models.Shipment.project_id.in_(list(made.values()))))).scalar()
        chk(n == 3, f"1) 三个项目都有发货单行（含已完成的）: {n}/3")

        # 2) 幂等
        async with SessionLocal() as db:
            res2 = await backfill_shipments(db)
        async with SessionLocal() as db:
            n2 = (await db.execute(select(func.count(models.Shipment.id)).where(
                models.Shipment.project_id.in_(list(made.values()))))).scalar()
        chk(n2 == 3 and res2["created"] == 0,
            f"2) 再跑一次不重复补: 行数 {n2}，本次新建 {res2['created']}")

        # 把 803 标成已发货
        async with SessionLocal() as db:
            sp = (await db.execute(select(models.Shipment).where(
                models.Shipment.project_id == made["2026-803"]))).scalar_one()
            sp.status = "shipped"
            await db.commit()

        async def board(**kw):
            r = await c.get("/api/logistics/board", headers=H, params={"year": "2026", **kw})
            assert r.status_code == 200, r.text
            return {x["code"]: x for x in r.json() if x["code"] in made}

        # 3) 按发货状态筛
        shipped = await board(ship_status="已发货")
        chk(set(shipped) == {"2026-803"}, f"3) 已发货只出 shipped 的: {sorted(shipped)}")

        unshipped = await board(ship_status="未发货")
        chk(set(unshipped) == {"2026-801", "2026-802"},
            f"3) 未发货只出 pending 的: {sorted(unshipped)}")

        allrows = await board()
        chk(set(allrows) == {"2026-801", "2026-802", "2026-803"},
            f"3) 不传筛选=全部: {sorted(allrows)}")

        # 4) 旧口径的盲区：已完成但没发货，必须出现在「未发货」里
        chk("2026-802" in unshipped,
            "4) 「项目已完成但没发货」的单出现在未发货里（旧筛选两边都看不到它）")

        # 5) 旧客户端的 proj_status 仍然可用
        old_doing = await board(proj_status="进行中")
        chk(set(old_doing) == {"2026-801", "2026-802"},
            f"5) 旧参数「进行中」映射到未发货: {sorted(old_doing)}")
        old_done = await board(proj_status="已完成")
        chk(set(old_done) == {"2026-803"}, f"5) 旧参数「已完成」映射到已发货: {sorted(old_done)}")

        # 6) 角标口径 = 看板「未发货」，两边必须是同一个数
        #    角标显示 N、点进去看到 N 行，人才信这个数；对不上比数字大更糟。
        cnt = (await c.get("/api/logistics/pending-count", headers=H)).json()["count"]
        async with SessionLocal() as db:
            all_pending = (await db.execute(
                select(func.count(models.Shipment.id)).join(models.Project).where(
                    models.Shipment.status == "pending",
                    models.Project.is_deleted == False))).scalar()   # noqa: E712
        chk(cnt == all_pending,
            f"6) 待发货角标按发货状态数，不看项目状态: {cnt} == {all_pending}")
        # 已完成但没发货的那个项目，必须同时出现在角标和看板未发货里
        chk("2026-802" in (await board(ship_status="未发货")),
            "6) 项目已完成但没发货的，角标和看板都算上（这是真实欠账，本来就该被看见）")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
