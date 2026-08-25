"""采购下单时要能联想仓库已有物料（反馈#411 李新新，2026-08-25）。

原话：「这两个地方的物料名称，规格型号，能不能在我输入物料名称型号关键词的时候
跳出仓库已有物料名称，规格型号来供我这边选择。这样就不会造成仓库物料名称重复。
因为目前仓库入库那边有重复的名称。」

她和 #410（王利利）说的是**同一件事的两头**：
收货入库时是按「名称完全相等 + 规格完全相等」去找物料的
（purchase_mgmt_router 收货过账那段），找不到就**新建一条**。
所以采购手打的写法只要和仓库里差一个字、或者规格一个填了一个空着，就多出一条重复物料。

线上实测（2026-08-25）：
  · 采购明细 1962 条里有 200 条的「名称+规格」在物料主数据里对不上 → 收货就会建新料
  · 仓库已攒出 10 组「同名、一条规格空一条有」的重复，其中 9 组带着流水或库存

本文件锁的是联想接口的口径：**名称和规格都要能搜到**（原来只按名称搜，
她在「规格型号」列敲型号什么也搜不出来，只能凭记忆手打）。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb411")
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        H = {"Authorization": "Bearer " + (await c.post("/api/auth/login", json={
            "username": "admin", "password": "admin123"})).json()["access_token"]}

        for name, spec in [("福马轮", "120F"), ("福马脚轮", "100F"),
                           ("不锈钢黑色把手", "黑色铝合金拉手孔距120mm"),
                           ("耳锅把手", None), ("深沟球轴承", "6205")]:
            r = await c.post("/api/wh/materials", headers=H, json={
                "name": name, "spec": spec, "unit": "个", "init_stock": 0})
            assert r.status_code == 200, r.text

        async def sug(q):
            r = await c.get("/api/wh/materials/suggest", headers=H, params={"q": q})
            assert r.status_code == 200, r.text
            return r.json()

        # ===== 1) 按名称搜（原来就有的能力，别改坏）=====
        by_name = await sug("把手")
        names = {x["name"] for x in by_name}
        chk(names == {"不锈钢黑色把手", "耳锅把手"},
            f"1) 按名称搜还能用: {sorted(names)}")

        # ===== 2) 按规格搜（#411 新加的；原来这里什么都搜不到）=====
        by_spec = await sug("120F")
        chk(any(x["spec"] == "120F" for x in by_spec),
            f"2) **按规格搜得到**——她在「规格型号」列敲型号，原来一条都不出: {by_spec}")
        chk(any(x["name"] == "福马轮" for x in by_spec),
            f"2) 而且把名称一起带出来（选中要两个字段一起补，收货才认得是同一个料）: {by_spec}")

        # ===== 3) 规格里的片段也要能搜到（她记得"孔距120"但记不全）=====
        frag = await sug("孔距120")
        chk(any(x["name"] == "不锈钢黑色把手" for x in frag),
            f"3) 规格中间的片段也能命中: {frag}")

        # ===== 4) 排序：名称前缀命中的排在规格命中的前面 =====
        mixed = await sug("福马")
        chk(mixed and mixed[0]["name"].startswith("福马"),
            f"4) 名称前缀命中的排最前: {[x['name'] for x in mixed]}")

        # ===== 5) 空关键字不返回全库（下拉一打开就糊一屏）=====
        chk(await sug("") == [], "5) 空关键字返回空，不是把全库倒出来")

        # ===== 6) 联想给出的名称+规格，正是收货匹配用的那一对 =====
        #   —— 只要她照着选，收货时就能对上现有物料，不会再建重复的
        one = next(x for x in by_spec if x["spec"] == "120F")
        r = await c.get("/api/wh/materials", headers=H, params={"kw": one["name"]})
        got = [m for m in r.json()["materials"]
               if m["name"] == one["name"] and (m["spec"] or None) == (one["spec"] or None)]
        chk(len(got) == 1,
            f"6) 联想返回的「名称+规格」在物料主数据里能精确命中唯一一条"
            f"（收货就是按这一对找料的，对不上才会新建）: {len(got)}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
