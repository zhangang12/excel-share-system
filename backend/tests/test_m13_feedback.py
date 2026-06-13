"""M13 生产问题反馈流：装配提交→主管审批→设计师接收/驳回 全状态机。"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="m13test")
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
        ids = {}
        for u, rc, fn in [("s1","sales","赵仁辉"),("dl","design_lead","陈工"),("d1","designer","张工"),
                          ("pm","pm_lead","钱主管"),("a1","assembler","赵师傅"),("a2","assembler","孙师傅")]:
            ids[u] = await mk(u, rc, fn)
        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        Hs1, Hdl, Hd1, Hpm, Ha1, Ha2 = (await login("s1"), await login("dl"), await login("d1"),
                                        await login("pm"), await login("a1"), await login("a2"))

        # 销售下单(设计+生产) → 设计分派给d1，生产分派给a1
        r = await c.post("/api/sales/orders", headers=Hs1, json={
            "name":"乳化机","customer":"x","cust_type":"经销商","contract":"有","amount":100000,
            "tax_rate":"13%","prepay":0,"before_ship":0,"ship_receivable":0,"balance":0,
            "balance_date":"","depts":["design","produce"],"receiver":{"name":"a","phone":"1","addr":"b"}})
        pid, code = r.json()["project_id"], r.json()["code"]
        od = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"]==pid][0]["id"]
        await c.post(f"/api/orders/{od}/assign", headers=Hdl, json={"worker_id":ids["d1"]})
        op = [o for o in (await c.get("/api/orders?dept=produce", headers=Hpm)).json() if o["project_id"]==pid][0]["id"]
        await c.post(f"/api/orders/{op}/assign", headers=Hpm, json={"worker_id":ids["a1"]})

        # 在手项目
        r = await c.get("/api/feedbacks/projects", headers=Ha1)
        chk(any(p["code"]==code for p in r.json()), "装配a1在手项目含该项目")
        r = await c.get("/api/feedbacks/projects", headers=Ha2)
        chk(not any(p["code"]==code for p in r.json()), "未派的a2无该项目")

        # a2 不能对非在手项目提交
        r = await c.post("/api/feedbacks", headers=Ha2, json={"project_id":pid,"content":"测试"})
        chk(r.status_code==403, "非在手项目提交被拒")
        # a1 提交
        r = await c.post("/api/feedbacks", headers=Ha1, json={"project_id":pid,"content":"B02机架孔位偏差2mm，建议核对图纸"})
        chk(r.status_code==200, f"a1提交反馈: {r.text[:120]}")
        msgs = (await c.get("/api/messages", headers=Hpm)).json()
        chk(any("问题反馈待审批" in m["text"] for m in msgs), "生产主管收提交推送")
        fid = (await c.get("/api/feedbacks?mine=true", headers=Hpm)).json()[0]["id"]

        # 设计师此时无待接收
        r = await c.get("/api/feedbacks?mine=true", headers=Hd1)
        chk(len(r.json())==0, "未审批前设计师无待接收")
        # 主管通过 → 设计师收到（按worker_id反查=d1）
        r = await c.post(f"/api/feedbacks/{fid}/pm-approve", headers=Hpm)
        chk(r.status_code==200, "主管通过")
        msgs = (await c.get("/api/messages", headers=Hd1)).json()
        chk(any("问题反馈待接收" in m["text"] for m in msgs), "设计师d1收到回馈(worker反查)")
        r = await c.get("/api/feedbacks?mine=true", headers=Hd1)
        chk(len(r.json())==1, "设计师有1条待接收")

        # 设计师接收 → 存档
        r = await c.post(f"/api/feedbacks/{fid}/design-accept", headers=Hd1)
        chk(r.status_code==200, "设计师接收存档")
        r = await c.get(f"/api/feedbacks?project_id={pid}", headers=H)
        chk(r.json()[0]["status"]=="archived", "状态已存档")

        # 第二条走 设计驳回 路径
        await c.post("/api/feedbacks", headers=Ha1, json={"project_id":pid,"content":"第二个问题"})
        fid2 = [f for f in (await c.get("/api/feedbacks?mine=true", headers=Hpm)).json()][0]["id"]
        await c.post(f"/api/feedbacks/{fid2}/pm-approve", headers=Hpm)
        r = await c.post(f"/api/feedbacks/{fid2}/design-reject", headers=Hd1)
        chk(r.status_code==200, "设计驳回")
        msgs = (await c.get("/api/messages", headers=Hpm)).json()
        chk(any("设计驳回反馈" in m["text"] for m in msgs), "主管收设计驳回推送")

        # 第三条走 主管驳回 路径
        await c.post("/api/feedbacks", headers=Ha1, json={"project_id":pid,"content":"第三个问题"})
        fid3 = [f for f in (await c.get("/api/feedbacks?mine=true", headers=Hpm)).json()][0]["id"]
        r = await c.post(f"/api/feedbacks/{fid3}/pm-reject", headers=Hpm)
        chk(r.status_code==200, "主管驳回")
        msgs = (await c.get("/api/messages", headers=Ha1)).json()
        chk(any("反馈被驳回" in m["text"] for m in msgs), "提交人收主管驳回推送")

        # 协作 tab 存档列表（按项目）
        r = await c.get(f"/api/feedbacks?project_id={pid}", headers=H)
        chk(len(r.json())==3, f"项目反馈存档3条: {len(r.json())}")
        statuses = {f["status"] for f in r.json()}
        chk(statuses=={"archived","rejected_by_design","rejected_by_pm"}, f"三种终态: {statuses}")
        # 🆕 #31 越权修复：收紧角色(销售,无详单权限)不得读项目反馈协作存档
        r = await c.get(f"/api/feedbacks?project_id={pid}", headers=Hs1)
        chk(r.status_code==403, f"销售越权读反馈被拒(应403): {r.status_code}")
        # 设计师(有详单权限)仍可读协作存档，不回归
        r = await c.get(f"/api/feedbacks?project_id={pid}", headers=Hd1)
        chk(r.status_code==200, f"设计师读协作存档仍放行: {r.status_code}")
        # 设计师不能处理别人的（无待接收时）
        r = await c.post(f"/api/feedbacks/{fid}/design-accept", headers=Hd1)
        chk(r.status_code==400, "已存档不可再接收")

        # ===== 🆕 #29 无在岗设计师的死信 → 设计负责人指派 → 设计师接收 =====
        await c.post(f"/api/orders/{od}/void", headers=Hdl)  # 作废设计任务=无在岗设计师
        await c.post("/api/feedbacks", headers=Ha1, json={"project_id":pid,"content":"第四个问题(死信场景)"})
        fid4 = (await c.get("/api/feedbacks?mine=true", headers=Hpm)).json()[0]["id"]
        r = await c.post(f"/api/feedbacks/{fid4}/pm-approve", headers=Hpm)
        chk(r.status_code==200, "#29 主管通过(项目无在岗设计师)")
        dl_mine = (await c.get("/api/feedbacks?mine=true", headers=Hdl)).json()
        chk(any(f["id"]==fid4 for f in dl_mine), f"#29 设计负责人待办看到无人认领反馈: {[f['id'] for f in dl_mine]}")
        msgs = (await c.get("/api/messages", headers=Hdl)).json()
        chk(any("无在岗设计师" in m["text"] for m in msgs), "#29 设计负责人收指派提示")
        r = await c.post(f"/api/feedbacks/{fid4}/assign?worker_id={ids['d1']}", headers=Hdl)
        chk(r.status_code==200, f"#29 指派给设计师: {r.text[:80]}")
        d1_mine = (await c.get("/api/feedbacks?mine=true", headers=Hd1)).json()
        chk(any(f["id"]==fid4 for f in d1_mine), "#29 被指派设计师看到待接收")
        r = await c.post(f"/api/feedbacks/{fid4}/design-accept", headers=Hd1)
        chk(r.status_code==200, f"#29 设计师接收存档: {r.text[:80]}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
