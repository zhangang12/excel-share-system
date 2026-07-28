"""🆕 #311 管理层待办支持上传图片（多张）。

链路（照 OA #264 oa_request 附件的既有模式）：
1. 管理层 POST /management-todos 创建待办 → 前端拿到 todo.id 后逐张
   POST /attachments（biz_type=management_todo, biz_id=todo.id）；
2. 附件与待办关联：GET /attachments?biz_type=management_todo&biz_id= 可查；
   待办响应体（MgmtTodoOut / MyTodoRow）内嵌 attachments，收件人/管理层视图直接可见
   （权限与待办可见口径一致：能看到待办的人即可看附件）；
3. 附件可下载（GET /attachments/{id}/download）；
4. 撤销待办：附件记录+磁盘文件一并删除；
5. 权限：非 admin/manager 不能建待办（403），biz_type 白名单外仍拒（400）。
"""
import asyncio, os, sys, tempfile, shutil
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="todomgmt")
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

PNG1 = b"\x89PNG\r\n\x1a\n" + b"A" * 40   # 假图片字节（附件校验只看扩展名）
PNG2 = b"\x89PNG\r\n\x1a\n" + b"B" * 60


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        async def login(u, p):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': u, 'password': p})).json()['access_token']}"}
        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "w1", "password": "pass123", "full_name": "收件人甲", "role_id": rid["warehouse"]})
        assert r.status_code == 200, r.text
        w1_id = r.json()["id"]
        Hw = await login("w1", "pass123")

        # ===== 1. 权限：普通角色不能建待办 =====
        r = await c.post("/api/management-todos", headers=Hw,
                         json={"title": "越权待办", "recipient_ids": [w1_id]})
        chk(r.status_code == 403, f"普通角色建待办 403: {r.status_code}")

        # ===== 2. 管理层建待办 → 响应带空 attachments =====
        r = await c.post("/api/management-todos", headers=H,
                         json={"title": "排查车间安全隐患", "content": "见附图",
                               "recipient_ids": [w1_id]})
        chk(r.status_code == 200, f"建待办 200: {r.status_code} {r.text[:200]}")
        todo = r.json()
        tid = todo["id"]
        chk(todo.get("attachments") == [], f"新建待办 attachments 为空: {todo.get('attachments')}")

        # ===== 3. 逐张上传图片（biz_type=management_todo, biz_id=待办ID） =====
        up_ids = []
        for fname, data in (("现场照片1.png", PNG1), ("现场照片2.png", PNG2)):
            r = await c.post("/api/attachments", headers=H,
                             files={"file": (fname, data, "image/png")},
                             data={"biz_type": "management_todo", "biz_id": str(tid)})
            chk(r.status_code == 200, f"上传 {fname} 200: {r.status_code} {r.text[:150]}")
            if r.status_code == 200:
                chk(r.json()["biz_type"] == "management_todo" and r.json()["biz_id"] == tid,
                    f"附件关联待办: {r.json()}")
                up_ids.append(r.json()["id"])
        chk(len(up_ids) == 2, f"两张都传上: {len(up_ids)}")

        # 白名单外 biz_type 仍拒
        r = await c.post("/api/attachments", headers=H,
                         files={"file": ("x.png", PNG1, "image/png")},
                         data={"biz_type": "not_a_biz"})
        chk(r.status_code == 400, f"未知 biz_type 400: {r.status_code}")

        # ===== 4. 附件与待办关联：通用列表 + 待办响应内嵌（两个视角） =====
        r = await c.get("/api/attachments", headers=Hw,
                        params={"biz_type": "management_todo", "biz_id": tid})
        chk(r.status_code == 200 and len(r.json()) == 2, f"附件列表 2 条: {r.status_code} {len(r.json())}")

        r = await c.get("/api/management-todos/mine", headers=Hw)
        mine = [x for x in r.json() if x["todo_id"] == tid]
        chk(len(mine) == 1, f"收件人「我收到的」含该待办: {len(mine)}")
        if mine:
            names = sorted(a["name"] for a in mine[0].get("attachments", []))
            chk(names == ["现场照片1.png", "现场照片2.png"], f"收件人视图内嵌附件: {names}")

        r = await c.get("/api/management-todos/sent", headers=H)
        sent = [x for x in r.json() if x["id"] == tid]
        chk(len(sent) == 1 and len(sent[0].get("attachments", [])) == 2,
            f"管理层监控视图内嵌附件: {[len(x.get('attachments', [])) for x in sent]}")

        # ===== 5. 可下载（内容与上传一致） =====
        r = await c.get(f"/api/attachments/{up_ids[0]}/download", headers=Hw)
        chk(r.status_code == 200 and r.content == PNG1, f"下载 200 且内容一致: {r.status_code} {len(r.content)}")

        # ===== 6. 回复链路不受影响（收件人回承诺时间） =====
        if mine:
            r = await c.post(f"/api/management-todos/{mine[0]['target_id']}/reply", headers=Hw,
                             json={"committed_at": "2026-08-01", "progress": "收到，看图处理"})
            chk(r.status_code == 200 and len(r.json().get("attachments", [])) == 2,
                f"回复后 MyTodoRow 仍带附件: {r.status_code}")

        # ===== 7. 撤销待办 → 附件记录+磁盘文件一并删除 =====
        r = await c.delete(f"/api/management-todos/{tid}", headers=H)
        chk(r.status_code == 200, f"撤销待办 200: {r.status_code}")
        r = await c.get("/api/attachments", headers=H,
                        params={"biz_type": "management_todo", "biz_id": tid})
        chk(r.status_code == 200 and r.json() == [], f"撤销后附件列表为空: {r.json()}")
        r = await c.get(f"/api/attachments/{up_ids[0]}/download", headers=H)
        chk(r.status_code == 404, f"撤销后下载 404: {r.status_code}")
        files_left = [p for p in Path(os.environ["FILES_DIR"]).rglob("*") if p.is_file()]
        chk(files_left == [], f"磁盘文件已清理: {files_left}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
