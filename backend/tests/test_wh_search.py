"""仓库搜索（2026-08-06 仓库反馈：材料入库后要能搜，物料主数据也要能搜）。

问题有多严重（生产实测）：出入库流水 1083 条，而接口只回最近 200 条、
前端在这 200 条里过滤——**前端能搜到的最早只到昨天**，仓库入完料第二天就搜不着了。

要锁死的：
  1. 搜索必须**先筛后截断**：limit 很小也不影响 total，
     否则又回到"只在前 N 条里找"，改了等于没改。
  2. 跨表也要能搜：物料名/规格在 wh_materials 上、项目编号在 projects 上，
     不 join 进去的话"搜密封圈""搜 2026-071"这两种最常用的搜法直接落空。
  3. 返回 total/shown，前端据此提示"还有 N 条没显示"——
     不提示的话人会以为搜完了，比搜不到更糟。
  4. 物料主数据的 kw 要覆盖 单位/库位/材质（编码全库只有 6/551 有值，
     只按编码搜等于搜不到），并支持按库位精确筛、只看低于安全库存。
  5. 日期范围能筛。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="whsearch")
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

        pr = await c.post("/api/projects", headers=H, json={"code": "2026-S01", "name": "搜索测试机"})
        pid = pr.json()["id"]

        # 物料：两种，库位不同，其中一个设安全库存好测 low_only
        async def mk_mat(**kw):
            r = await c.post("/api/wh/materials", headers=Hw, json=kw)
            assert r.status_code == 200, r.text
            return r.json()["id"]

        m_seal = await mk_mat(name="密封圈", spec="DN50", unit="个", location="A-03",
                              init_stock=0, safety_stock=100, material_grade="丁腈橡胶")
        m_bear = await mk_mat(name="轴承", spec="6205", unit="套", location="B-11", init_stock=0)

        # 30 条流水，跨 3 个月，好验"截断之外也搜得到"
        for i in range(30):
            mon = (i % 3) + 1
            await c.post("/api/wh/txns", headers=Hw, json={
                "material_id": m_seal if i % 2 == 0 else m_bear,
                "biz_date": f"2026-0{mon}-1{i % 9}", "direction": "in", "qty": 5,
                "source": "采购入库", "party": "顺鑫" if i % 2 == 0 else "吉明",
                "project_id": pid if i < 5 else None})

        async def txn(**kw):
            r = await c.get("/api/wh/txns", headers=Hw, params=kw)
            assert r.status_code == 200, r.text
            return r.json()

        d = await txn()
        chk(d.get("total") == 30 and d.get("shown") == 30, f"3) 返回 total/shown: {d.get('total')}/{d.get('shown')}")
        chk(isinstance(d.get("rows"), list), "3) rows 是数组")

        # ===== 1) 先筛后截断 —— 这条是整件事的要害 =====
        d = await txn(kw="密封圈", limit=2)
        chk(d["total"] == 15 and d["shown"] == 2,
            f"1) limit=2 时 total 仍是全量命中数 15: total={d['total']} shown={d['shown']}")
        chk(all("密封圈" == r_["material_name"] for r_ in d["rows"]), "1) 返回的确实是命中的那些")
        # 反证：如果还是"在前 N 条里找"，limit=2 时 total 只可能 ≤2
        chk(d["total"] > d["shown"], "1) total>shown —— 说明不是在截断后的结果里搜的")

        # ===== 2) 跨表搜 =====
        chk((await txn(kw="密封圈"))["total"] == 15, "2) 按物料名搜（join wh_materials）")
        chk((await txn(kw="DN50"))["total"] == 15, "2) 按规格搜")
        chk((await txn(kw="2026-S01"))["total"] == 5, "2) 按项目编号搜（join projects）")
        chk((await txn(kw="顺鑫"))["total"] == 15, "2) 按往来单位搜")
        chk((await txn(kw="采购入库"))["total"] == 30, "2) 按来源搜")
        chk((await txn(kw="RK"))["total"] == 30, "2) 按单号前缀搜")
        chk((await txn(kw="A-03"))["total"] == 15, "2) 按库位搜")
        chk((await txn(kw="压根没有的东西"))["total"] == 0, "2) 搜不存在的返回 0（不是全量）")

        # 大小写不敏感
        chk((await txn(kw="dn50"))["total"] == 15, "2) 大小写不敏感")

        # ===== 5) 日期范围 =====
        d = await txn(date_from="2026-02-01", date_to="2026-02-28")
        chk(d["total"] == 10, f"5) 按月筛: {d['total']}")
        chk(all(r_["biz_date"].startswith("2026-02") for r_ in d["rows"]), "5) 筛出来的都在范围内")
        # 搜索 + 日期叠加
        d = await txn(kw="密封圈", date_from="2026-02-01", date_to="2026-02-28")
        chk(d["total"] == 5, f"5) 关键词+日期叠加: {d['total']}")

        # 方向筛不受影响
        chk((await txn(direction="in"))["total"] == 30, "方向筛仍有效")

        # ===== 4) 物料主数据 =====
        async def mat(**kw):
            r = await c.get("/api/wh/materials", headers=Hw, params=kw)
            assert r.status_code == 200, r.text
            return r.json()

        chk((await mat())["total"] == 2, "4) 不带条件返回全部")
        chk((await mat(kw="密封"))["total"] == 1, "4) 按名称搜")
        chk((await mat(kw="6205"))["total"] == 1, "4) 按规格搜")
        chk((await mat(kw="套"))["total"] == 1, "4) 按单位搜（原来搜不了）")
        chk((await mat(kw="A-03"))["total"] == 1, "4) 按库位搜（原来搜不了）")
        chk((await mat(kw="丁腈"))["total"] == 1, "4) 按材质搜（原来搜不了）")
        chk((await mat(location="B-11"))["total"] == 1, "4) 按库位精确筛")
        chk((await mat(location="不存在"))["total"] == 0, "4) 库位精确筛不命中就是 0")

        # low_only：密封圈安全库存 100、现存 75（15 笔 ×5），应算低
        d = await mat(low_only="true")
        chk(d["total"] == 1 and d["materials"][0]["name"] == "密封圈",
            f"4) 只看低于安全库存: {d['total']} 条 {[m['name'] for m in d['materials']]}")
        # ⚠️ low 是拿实时库存跟安全库存比出来的，必须在算完库存之后过滤
        chk(d["materials"][0]["low"] is True, "4) low_only 返回的确实是 low 的")

        # 组合：关键词 + 库位
        chk((await mat(kw="密封圈", location="B-11"))["total"] == 0, "4) 关键词与库位是「与」的关系")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
