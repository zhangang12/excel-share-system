"""🆕 #324/#323 设计部图纸推送按域路由 + 二次上传更新提醒：

#324 推送按采购员分工域(dept_config.BUYER_SHEET_MAP)路由：
1. 外购附图(outsource_img, to_domain=standard) → 只推李新新(lixinxin)，王芹/方步森/普通采购收不到；
2. CAD激光图纸(sheetpkg, to_domain=laser) → 只推王芹(wangqin) + 钣金组(sheetmetal)照旧收到；
3. 域内无匹配活跃用户时回退原 to_role=buyer 池（防没人收到）；
#323 二次/补充推送（该 kind 已有 pushed=1 文件）→ 消息改「【更新】…请以最新为准」口径。
"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="pushdom")
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

        async def mk(uname, rc, full):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": full, "role_id": rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]
        async def login(u):
            r = await c.post("/api/auth/login", json={"username":u,"password":"pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        async def msgs(h):
            return (await c.get("/api/messages", headers=h)).json()

        ids = {}
        for u, rc, fn in [("s1","sales","赵仁辉"),("dl","design_lead","陈工"),("d1","designer","张工"),
                          ("sm","sheetmetal","何师傅"),("bu","buyer","林采购")]:
            ids[u] = await mk(u, rc, fn)
        Hs1, Hdl, Hd1, Hsm, Hbu = (await login("s1"), await login("dl"), await login("d1"),
                                   await login("sm"), await login("bu"))

        async def mk_design_order(name):
            """销售下单(仅设计部) → 设计负责人分派 d1 → d1 接单，返回 (pid, code, oid)。"""
            r = await c.post("/api/sales/orders", headers=Hs1, json={
                "name":name,"customer":"x","cust_type":"经销商","contract":"有","amount":100000,
                "tax_rate":"13%","prepay":0,"before_ship":0,"ship_receivable":0,"balance":0,
                "balance_date":"","depts":["design"],"receiver":{"name":"a","phone":"1","addr":"b"}})
            assert r.status_code == 200, r.text
            pid, code = r.json()["project_id"], r.json()["code"]
            od = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json()
                  if o["project_id"]==pid][0]["id"]
            await c.post(f"/api/orders/{od}/assign", headers=Hdl, json={"worker_id":ids["d1"]})
            await c.post(f"/api/orders/{od}/start", headers=Hd1,
                         json={"start_date":"2026-07-01","due_date":"2026-12-31"})
            return pid, code, od

        async def upload_push(od, kind, fname, content):
            r = await c.post(f"/api/orders/{od}/start-upload?kind={kind}", headers=Hd1,
                             files=[("files", (fname, io.BytesIO(content), "application/pdf"))])
            assert r.status_code == 200, r.text
            r = await c.post(f"/api/orders/{od}/start-push", headers=Hd1, json={"kind":kind})
            assert r.status_code == 200, r.text

        # ===== 1. 域内无匹配用户（lixinxin 不存在）→ 回退 buyer 池 =====
        _, code1, od1 = await mk_design_order("回退机")
        await upload_push(od1, "outsource_img", "外购附图A.pdf", b"F1")
        m_bu = await msgs(Hbu)
        chk(any("外购附图" in m["text"] and code1 in m["text"] for m in m_bu),
            "域内无匹配用户时回退 buyer 池：普通采购(bu)收到外购附图推送")

        # ===== 2. 建三名分工采购员 → 按域路由 =====
        for u, fn in [("lixinxin","李新新"),("wangqin","王芹"),("fangbusen","方步森")]:
            ids[u] = await mk(u, "buyer", fn)
        Hlxx, Hwq, Hfbs = await login("lixinxin"), await login("wangqin"), await login("fangbusen")

        _, code2, od2 = await mk_design_order("路由机")
        # 2a. 外购附图 → standard 域 = 仅李新新
        await upload_push(od2, "outsource_img", "外购附图B.pdf", b"F2")
        def has(h, kw, code):
            return any(kw in m["text"] and code in m["text"] for m in h)
        chk(has(await msgs(Hlxx), "外购附图", code2), "#324 外购附图推给 standard 域(李新新)")
        chk(not has(await msgs(Hwq), "外购附图", code2), "#324 王芹不收外购附图推送")
        chk(not has(await msgs(Hfbs), "外购附图", code2), "#324 方步森不收外购附图推送")
        chk(not any(code2 in m["text"] for m in await msgs(Hbu)),
            "#324 域内有匹配时普通采购(bu)不再收外购附图推送")

        # 2b. CAD激光图纸 → laser 域 = 王芹；钣金组照旧收到
        await upload_push(od2, "sheetpkg", "总装图.pdf", b"P1")
        chk(has(await msgs(Hwq), "CAD激光图纸", code2), "#324 CAD激光图纸推给 laser 域(王芹)")
        chk(not has(await msgs(Hlxx), "CAD激光图纸", code2), "#324 李新新不收CAD激光图纸推送")
        chk(not has(await msgs(Hfbs), "CAD激光图纸", code2), "#324 方步森不收CAD激光图纸推送")
        chk(has(await msgs(Hsm), "CAD激光图纸", code2), "#324 钣金组仍收CAD激光图纸推送")
        # 首次推送为原文案（不含【更新】）
        first = [m["text"] for m in await msgs(Hwq) if "CAD激光图纸" in m["text"] and code2 in m["text"]]
        chk(all("【更新】" not in t for t in first), "#323 首次推送维持原文案")

        # ===== 3. #323 二次上传推送 → 「【更新】…请以最新为准」 =====
        await upload_push(od2, "sheetpkg", "补充图.pdf", b"P2")
        upd = [m["text"] for m in await msgs(Hwq) if code2 in m["text"] and "【更新】" in m["text"]]
        chk(any("CAD激光图纸" in t and "新增 1 个文件" in t and "请以最新为准" in t for t in upd),
            f"#323 二次推送王芹收【更新】文案: {upd}")
        upd_sm = [m["text"] for m in await msgs(Hsm) if code2 in m["text"] and "【更新】" in m["text"]]
        chk(any("CAD激光图纸" in t for t in upd_sm), "#323 钣金组同步收【更新】文案")
        # 外购附图二次推送 → 李新新收【更新】
        await upload_push(od2, "outsource_img", "外购附图C.pdf", b"F3")
        upd_lxx = [m["text"] for m in await msgs(Hlxx) if code2 in m["text"] and "【更新】" in m["text"]]
        chk(any("外购附图" in t and "请以最新为准" in t for t in upd_lxx),
            "#323 外购附图二次推送李新新收【更新】文案")
        chk(not any("【更新】" in m["text"] and code2 in m["text"] for m in await msgs(Hwq)
                    if "外购附图" in m["text"]),
            "#323 王芹仍不收外购附图的更新推送")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
