"""采购申请明细行的改/删（反馈#418 王利利，2026-08-29）。

原话：「这个地方能不能加一个修改和删除」——截图指着采购申请展开的明细行。
整单删除本来就有；这次补**行级**：数量写错/规格要补充，不用整单删了重提。

口径：
  ① 只许本人（或管理层）；② 只许 pending——采购已处理/驳回的单改了对方也看不见；
  ③ 删行删到只剩一行时拒绝——那等于删整单，走整单删除更明确。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb418")
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
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        await mkuser("preq_wh", ["warehouse"])        # 王利利
        await mkuser("preq_wh2", ["warehouse"])       # 别的仓库同事
        await mkuser("preq_buyer", ["buyer"])         # 采购
        Hw, Hw2, Hb = (await login("preq_wh", "pass123"), await login("preq_wh2", "pass123"),
                       await login("preq_buyer", "pass123"))

        # 建一张 3 行的申请
        r = await c.post("/api/purchase-mgmt/purchase-requests", headers=Hw, json={"lines": [
            {"item_name": "快装接头", "spec": "4分（Ø19）", "qty": 50},
            {"item_name": "快装接头", "spec": "6分（Ø25）", "qty": 50},
            {"item_name": "焊接三通", "spec": "6分（Ø25）", "qty": 30},
        ]})
        chk(r.status_code == 200, f"建申请 -> {r.status_code}")
        pr = r.json()
        prid, lines = pr["id"], pr["lines"]

        # ① 本人改行：数量 50→80，补备注
        l0 = lines[0]
        r = await c.put(f"/api/purchase-mgmt/purchase-requests/{prid}/lines/{l0['id']}", headers=Hw, json={
            "item_name": "快装接头", "spec": "4分（Ø19）", "qty": 80, "notes": "改：多备30个"})
        chk(r.status_code == 200, f"本人改行 -> {r.status_code}")
        got = next(x for x in r.json()["lines"] if x["id"] == l0["id"])
        chk(got["qty"] == 80 and got["notes"] == "改：多备30个", "改动落库（数量80+备注）")

        # ② 别人改 -> 403
        r = await c.put(f"/api/purchase-mgmt/purchase-requests/{prid}/lines/{l0['id']}", headers=Hw2, json={
            "item_name": "偷改", "qty": 1})
        chk(r.status_code == 403, f"非本人改被拒 -> {r.status_code}")

        # ③ 本人删一行
        r = await c.delete(f"/api/purchase-mgmt/purchase-requests/{prid}/lines/{lines[2]['id']}", headers=Hw)
        chk(r.status_code == 200, f"本人删行 -> {r.status_code}")
        r = await c.get("/api/purchase-mgmt/purchase-requests", headers=Hw)
        me = next(x for x in r.json() if x["id"] == prid)
        chk(len(me["lines"]) == 2, f"删后剩 {len(me['lines'])} 行")

        # ④ 行号不属于这张单 -> 404
        r = await c.delete(f"/api/purchase-mgmt/purchase-requests/{prid}/lines/999999", headers=Hw)
        chk(r.status_code == 404, f"不存在的行 -> {r.status_code}")

        # ⑤ 采购处理完 -> 不能再改/删
        r = await c.put(f"/api/purchase-mgmt/purchase-requests/{prid}/handle", headers=Hb)
        chk(r.status_code == 200, f"采购标记已处理 -> {r.status_code}")
        r = await c.put(f"/api/purchase-mgmt/purchase-requests/{prid}/lines/{l0['id']}", headers=Hw, json={
            "item_name": "快装接头", "qty": 99})
        chk(r.status_code == 400, f"已处理后改行被拒 -> {r.status_code}")
        r = await c.delete(f"/api/purchase-mgmt/purchase-requests/{prid}/lines/{l0['id']}", headers=Hw)
        chk(r.status_code == 400, f"已处理后删行被拒 -> {r.status_code}")

        # ⑥ 只剩一行不让删（新建一张单行的验证）
        r = await c.post("/api/purchase-mgmt/purchase-requests", headers=Hw, json={"lines": [
            {"item_name": "加热管", "qty": 2}]})
        one = r.json()
        r = await c.delete(f"/api/purchase-mgmt/purchase-requests/{one['id']}/lines/{one['lines'][0]['id']}", headers=Hw)
        chk(r.status_code == 400, f"最后一行不让删 -> {r.status_code}")
        chk("整单" in r.json().get("detail", ""), "提示指去整单删除")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
