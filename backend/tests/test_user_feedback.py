"""用户反馈小助手：任意角色可提交 / 管理层看全部 / 普通用户只看自己 / 导出 HTML / 截图。"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="ufbtest")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

FAIL = []
def chk(c, m):
    if c: print("  PASS", m)
    else: FAIL.append(m); print("  FAIL:", m)


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
            r = await c.post("/api/admin/users", headers=H, json={
                "username": u, "password": "pass123", "full_name": fn, "role_id": rid[rc]})
            return r.json()["id"]
        await mk("s1", "sales", "赵销售")
        await mk("d1", "designer", "张设计")
        await mk("mg", "manager", "管理层B")

        async def login(u):
            r = await c.post("/api/auth/login", json={"username": u, "password": "pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        Hs, Hd, Hmg = await login("s1"), await login("d1"), await login("mg")

        # ===== 1) 任意登录用户均可提交（含截图） =====
        # 1x1 透明 PNG
        png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082")
        r = await c.post("/api/user-feedback", headers=Hs,
                         data={"kind": "bug", "content": "销售台账操作列按钮挤行(已修)", "page_url": "/sales"},
                         files={"file": ("shot.png", io.BytesIO(png), "image/png")})
        chk(r.status_code == 200, f"销售员提交带截图: {r.status_code} {r.text[:150]}")
        fb1 = r.json()
        chk(fb1["kind"] == "bug" and fb1["shot_file_id"] and fb1["status"] == "open",
            f"返回结构含 kind/截图id/状态: {fb1}")

        r = await c.post("/api/user-feedback", headers=Hd,
                         data={"kind": "suggest", "content": "建议增加打印预览", "page_url": "/dept/design"})
        chk(r.status_code == 200, "设计师提交意见(无截图)")
        fb2 = r.json()
        chk(fb2["kind"] == "suggest" and not fb2["shot_file_id"], "无截图允许")

        # 空内容拒绝
        r = await c.post("/api/user-feedback", headers=Hs,
                         data={"kind": "bug", "content": "  "})
        chk(r.status_code == 400, f"空内容被拒: {r.status_code}")

        # ===== 2) 列表权限：管理层看全部, 普通用户只看自己 =====
        all_admin = (await c.get("/api/user-feedback", headers=H)).json()
        chk(len(all_admin) == 2 and {x["id"] for x in all_admin} == {fb1["id"], fb2["id"]},
            f"admin 看到全部 2 条: {len(all_admin)}")
        all_mg = (await c.get("/api/user-feedback", headers=Hmg)).json()
        chk(len(all_mg) == 2, f"manager 也看到全部: {len(all_mg)}")

        s_mine = (await c.get("/api/user-feedback", headers=Hs)).json()
        chk(len(s_mine) == 1 and s_mine[0]["id"] == fb1["id"],
            f"销售员只看到自己 1 条: {len(s_mine)}")
        d_mine = (await c.get("/api/user-feedback", headers=Hd)).json()
        chk(len(d_mine) == 1 and d_mine[0]["id"] == fb2["id"], "设计师只看到自己")

        # 管理层加 mine=true 仅看自己提交的(0条)
        my_admin = (await c.get("/api/user-feedback?mine=true", headers=H)).json()
        chk(len(my_admin) == 0, f"admin mine=true 自己未提交则空: {len(my_admin)}")

        # 类型筛选
        bugs = (await c.get("/api/user-feedback?kind=bug", headers=H)).json()
        chk(len(bugs) == 1 and bugs[0]["kind"] == "bug", "按 kind=bug 筛选")

        # ===== 3) 标记已处理：仅管理层可调 =====
        r = await c.post(f"/api/user-feedback/{fb1['id']}/done", headers=Hs)
        chk(r.status_code == 403, f"销售员调标记已处理被拒: {r.status_code}")
        r = await c.post(f"/api/user-feedback/{fb1['id']}/done", headers=Hmg)
        chk(r.status_code == 200, f"manager 标记已处理: {r.text[:80]}")
        fb1_now = [x for x in (await c.get("/api/user-feedback", headers=H)).json() if x["id"] == fb1["id"]][0]
        chk(fb1_now["status"] == "done", "状态变为 done")
        # 状态筛选
        open_only = (await c.get("/api/user-feedback?status=open", headers=H)).json()
        chk(len(open_only) == 1 and open_only[0]["id"] == fb2["id"], "按 status=open 筛选")

        # ===== 4) 导出 HTML（仅管理层）+ 截图 data URI 内嵌 =====
        r = await c.get("/api/user-feedback/export.html", headers=Hs)
        chk(r.status_code == 403, f"销售员导出被拒: {r.status_code}")
        r = await c.get("/api/user-feedback/export.html", headers=H)
        chk(r.status_code == 200, f"admin 导出 HTML 200: {r.status_code}")
        chk("text/html" in r.headers.get("content-type", ""), f"Content-Type: {r.headers.get('content-type')}")
        body = r.text
        chk("用户反馈导出" in body and "<!DOCTYPE html>" in body, "HTML 含标题与文档头")
        chk("销售台账操作列按钮挤行" in body and "建议增加打印预览" in body, "导出含两条反馈内容")
        chk("data:image/png;base64," in body, "截图以 base64 data URI 内嵌(自包含)")
        chk('Content-Disposition' in r.headers and 'attachment' in r.headers.get('Content-Disposition',''),
            "Content-Disposition=attachment(浏览器下载)")
        # 类型筛选导出
        r = await c.get("/api/user-feedback/export.html?kind=suggest", headers=H)
        chk("建议增加打印预览" in r.text and "销售台账操作列按钮挤行" not in r.text,
            "按 kind 筛选导出只含意见建议")

        # ===== 5) 菜单可见性：管理层菜单含 user-feedback；销售员不含 =====
        m_admin = (await c.get("/api/auth/menus", headers=H)).json()
        m_keys = [x["key"] for x in m_admin["menus"]]
        chk("user-feedback" in m_keys, f"admin 菜单含 user-feedback: {m_keys}")
        m_s = (await c.get("/api/auth/menus", headers=Hs)).json()
        chk("user-feedback" not in [x["key"] for x in m_s["menus"]], "销售员菜单不含 user-feedback")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES\n" + "\n".join("  - " + x for x in FAIL))
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
