"""M01+M15基建 集成测试：临时SQLite库 → 启动逻辑 → 接口断言"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="m01test")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)

async def main():
    # 启动逻辑（同 lifespan）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # 登录 admin
        r = await c.post("/api/auth/login", json={"username":"admin","password":"admin123"})
        chk(r.status_code==200, f"admin login {r.status_code} {r.text[:200]}")
        tok = r.json()["access_token"]; H = {"Authorization": f"Bearer {tok}"}

        # 菜单：admin 全可见
        r = await c.get("/api/auth/menus", headers=H)
        chk(r.status_code==200, "menus endpoint")
        keys = [m["key"] for m in r.json()["menus"]]
        chk("sales" in keys and "aftersales" in keys and "approve" in keys, f"admin sees all menus: {keys}")
        chk(r.json()["can_view_detail"] is True, "admin can_view_detail")

        # 角色列表含新角色
        r = await c.get("/api/admin/roles", headers=H)
        codes = [x["code"] for x in r.json()]
        for need in ["sales","sales_lead","electrician","assembler","pm_lead","sheetmetal","buyer","warehouse_lead","logistics","finance","as_worker","as_lead","design_lead","electric_lead"]:
            chk(need in codes, f"role seeded: {need}")

        # 建一个销售员用户 + 一个电工 + 一个售后
        rid = {x["code"]:x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        for uname, rc in [("s1","sales"),("e1","electrician"),("a1","assembler"),("w1","as_worker"),("d1","designer")]:
            r = await c.post("/api/admin/users", headers=H, json={"username":uname,"password":"pass123","full_name":uname,"role_id":rid[rc]})
            chk(r.status_code==200, f"create user {uname}: {r.status_code} {r.text[:150]}")

        # 建一个项目（admin）
        r = await c.post("/api/projects", headers=H, json={"code":"T-001","name":"测试项目"})
        chk(r.status_code==200, f"create project {r.status_code} {r.text[:200]}")
        pid = r.json()["id"]

        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        # 销售员：菜单=catalog+sales+leads+oa+messages，无 list；详单 403
        Hs = await login("s1")
        r = await c.get("/api/auth/menus", headers=Hs); j = r.json()
        ks = [m["key"] for m in j["menus"]]
        chk(ks==["catalog","sales","leads","oa","messages"], f"sales menus: {ks}")
        chk(j["can_view_detail"] is False, "sales cannot view detail")
        r = await c.get(f"/api/projects/{pid}", headers=Hs)
        chk(r.status_code==403, f"sales project detail 403: {r.status_code}")
        r = await c.get(f"/api/projects/{pid}/datasheets", headers=Hs)
        chk(r.status_code==403, f"sales datasheets 403: {r.status_code}")

        # 电工：catalog+electric+oa+messages，无详单
        He = await login("e1")
        ks = [m["key"] for m in (await c.get("/api/auth/menus", headers=He)).json()["menus"]]
        chk(ks==["catalog","electric","oa","messages"], f"electrician menus: {ks}")
        r = await c.get(f"/api/projects/{pid}", headers=He)
        chk(r.status_code==403, "electrician detail 403")

        # 🆕 #91 详单子端点统一闸门：被收紧角色直打 datasheet/field/record/cell 子端点均 403
        Hd_early = await login("d1")  # 设计师(有详单)做非回归断言
        ds = (await c.get(f"/api/projects/{pid}/datasheets", headers=H)).json()
        did = ds[0]["id"] if isinstance(ds, list) and ds else None
        if did:
            recs = (await c.get(f"/api/datasheets/{did}/records", headers=H)).json()
            rid_any = recs[0]["id"] if isinstance(recs, list) and recs else None
            # GET fields / records — sales/electrician 应 403
            for h, who in [(Hs, "sales"), (He, "electrician")]:
                r = await c.get(f"/api/datasheets/{did}/fields", headers=h)
                chk(r.status_code == 403, f"#91 {who} list_fields 403: {r.status_code}")
                r = await c.get(f"/api/datasheets/{did}/records", headers=h)
                chk(r.status_code == 403, f"#91 {who} list_records 403: {r.status_code}")
                # PUT cell — 写端点也应 403(即使有 rid)
                if rid_any:
                    r = await c.put(f"/api/records/{rid_any}/cell", headers=h,
                                    json={"field_id": 1, "value": "x"})
                    chk(r.status_code == 403, f"#91 {who} update_cell 403: {r.status_code}")
                # POST records — 写端点也应 403
                r = await c.post(f"/api/datasheets/{did}/records", headers=h,
                                 json={"values": {}})
                chk(r.status_code == 403, f"#91 {who} create_record 403: {r.status_code}")
            # 设计师(有详单权限)仍可读 fields/records, 不回归
            r = await c.get(f"/api/datasheets/{did}/fields", headers=Hd_early)
            chk(r.status_code == 200, f"designer 仍可读 fields(不回归): {r.status_code}")

        # 售后：仅 aftersales+oa+messages，无 catalog
        Hw = await login("w1")
        ks = [m["key"] for m in (await c.get("/api/auth/menus", headers=Hw)).json()["menus"]]
        chk(ks==["aftersales","oa","messages"], f"as_worker menus: {ks}")

        # 设计师（老角色）：catalog+list+design+oa+messages，详单可访问
        Hd = await login("d1")
        jd = (await c.get("/api/auth/menus", headers=Hd)).json()
        ks = [m["key"] for m in jd["menus"]]
        chk(ks==["catalog","list","design","oa","messages"], f"designer menus: {ks}")
        chk(jd["can_view_detail"] is True, "designer can view detail")
        r = await c.get(f"/api/projects/{pid}", headers=Hd)
        chk(r.status_code==200, f"designer detail 200: {r.status_code}")

        # 附件：上传/列表/下载/删除
        import io
        r = await c.post("/api/attachments", headers=H,
            files={"file":("合同.pdf", io.BytesIO(b"PDFDATA"), "application/pdf")},
            data={"biz_type":"contract","project_id":str(pid)})
        chk(r.status_code==200, f"attachment upload: {r.status_code} {r.text[:200]}")
        aid = r.json()["id"]
        r = await c.get(f"/api/attachments?biz_type=contract&project_id={pid}", headers=H)
        chk(len(r.json())==1, "attachment list")
        r = await c.get(f"/api/attachments/{aid}/download", headers=H)
        chk(r.status_code==200 and r.content==b"PDFDATA", "attachment download")
        # 越权删除：销售员删别人的 → 403
        r = await c.delete(f"/api/attachments/{aid}", headers=Hs)
        chk(r.status_code==403, f"attachment delete forbidden: {r.status_code}")
        r = await c.delete(f"/api/attachments/{aid}", headers=H)
        chk(r.status_code==200, "attachment delete by admin")
        # 非法类型拒绝
        r = await c.post("/api/attachments", headers=H,
            files={"file":("x.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
            data={"biz_type":"contract"})
        chk(r.status_code==400, f"bad ext rejected: {r.status_code}")
        r = await c.post("/api/attachments", headers=H,
            files={"file":("x.pdf", io.BytesIO(b"1"), "application/pdf")},
            data={"biz_type":"hack"})
        chk(r.status_code==400, f"bad biz_type rejected: {r.status_code}")

        # 消息：角色池推送 + 未读 + 已读
        from app.notify import push_message
        async with SessionLocal() as db:
            n = await push_message(db, to_role="sales", kind="warn", text="测试角色池推送")
            chk(n==1, f"push to role fan-out: {n}")
        r = await c.get("/api/messages/unread-count", headers=Hs)
        chk(r.json()["count"]==1, f"unread count: {r.json()}")
        r = await c.get("/api/messages", headers=Hs)
        chk(len(r.json())==1 and r.json()[0]["text"]=="测试角色池推送", "message list")
        r = await c.post("/api/messages/read-all", headers=Hs)
        r = await c.get("/api/messages/unread-count", headers=Hs)
        chk(r.json()["count"]==0, "read-all clears")
        # 别人看不到
        r = await c.get("/api/messages", headers=He)
        chk(len(r.json())==0, "messages isolated per user")

        # 企微绑定
        users = (await c.get("/api/admin/users", headers=H)).json()
        uid_s1 = [u["id"] for u in users if u["username"]=="s1"][0]
        r = await c.put(f"/api/admin/users/{uid_s1}/wxid", headers=H, json={"wxid":"zhang-san"})
        chk(r.status_code==200, "wxid bind")

        # 幂等：二次跑迁移
        async with SessionLocal() as db:
            await seed(db); await run_all(db)
        await ensure_schema_columns(engine)
        r = await c.get("/api/admin/roles", headers=H)
        chk(len([x for x in r.json() if x["code"]=="buyer"])==1, "seed idempotent (single buyer)")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
