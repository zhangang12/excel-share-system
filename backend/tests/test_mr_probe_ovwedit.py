"""多角色「一览单元格编辑闸门」并集验证（写端点 PUT /cell，非读 map）。

场景：
  admin 建项目(POST /api/projects {code,name}) → 取一个一览字段 OverviewField；
  PUT /api/permissions/overview-fields/{fid} 设 designer can_view=true,can_edit=false（禁编辑）。
  建用户 d1=[designer]、d2=[designer,finance]（finance 未配置该字段=默认放行）。
  新用户默认是所有活跃项目的 edit 成员（建项目在前 → 用户能过 user_can_edit_project）。
  用 PUT /api/overview/projects/{pid}/cell {field_id, value} 编辑该字段：
    d1 应 403（唯一角色被禁编辑）
    d2 应 200（并集放行：finance 未配置该字段 → 默认放行 → OR 合并放行）

通过 = 脚本退出码 0 且打印 PASSED。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="mrovw")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    # 角色 id 直接查 models.Role（admin/manager 对 /api/admin/roles 隐藏，故走 DB）
    async with SessionLocal() as db:
        rid = {r.code: r.id for r in (await db.execute(select(models.Role))).scalars().all()}
    chk("designer" in rid and "finance" in rid, f"designer/finance 角色存在: {sorted(rid)}")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        async def login(u, p="pass123"):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, f"login {u}: {r.text}"
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")

        async def mk(uname, role_ids):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "role_ids": role_ids})
            assert r.status_code == 200, f"mk {uname}: {r.text}"
            return r.json()["id"]

        # 1) admin 建项目（在建用户之前 → 用户回填为该项目 edit 成员）
        r = await c.post("/api/projects", headers=H, json={"code": "OVW-1", "name": "一览编辑闸门项目"})
        chk(r.status_code == 200, f"建项目 OVW-1: {r.text[:160]}")
        pid = r.json()["id"]

        # 2) 取一个一览字段
        async with SessionLocal() as db:
            of = (await db.execute(
                select(models.OverviewField).order_by(models.OverviewField.id)
            )).scalars().first()
        chk(of is not None, "存在一览字段用于编辑闸门测试")
        if of is None:
            raise SystemExit("no overview field")
        fid = of.id

        # 3) 设 designer can_view=true, can_edit=false（禁编辑该字段）
        r = await c.put(f"/api/permissions/overview-fields/{fid}", headers=H, json={
            "permissions": [{"role_id": rid["designer"], "can_view": True, "can_edit": False}]})
        chk(r.status_code == 200, f"设置 designer 禁编辑该字段: {r.text[:160]}")
        # 校验写入：designer 该字段 can_edit=False
        got = {it["role_id"]: it for it in r.json()}
        chk(got.get(rid["designer"], {}).get("can_edit") is False,
            f"designer 该字段 can_edit 应为 False: {got.get(rid['designer'])}")

        # 4) 建用户 d1=[designer]、d2=[designer,finance]
        await mk("ovw_d1", [rid["designer"]])
        await mk("ovw_d2", [rid["designer"], rid["finance"]])
        Hd1 = await login("ovw_d1")
        Hd2 = await login("ovw_d2")

        # 5) PUT /cell 编辑该字段
        body = {"field_id": fid, "value": "x"}
        r1 = await c.put(f"/api/overview/projects/{pid}/cell", headers=Hd1, json=body)
        chk(r1.status_code == 403,
            f"d1=[designer] 唯一角色被禁编辑 → 应 403，实得 {r1.status_code}: {r1.text[:200]}")

        r2 = await c.put(f"/api/overview/projects/{pid}/cell", headers=Hd2, json=body)
        chk(r2.status_code == 200,
            f"d2=[designer,finance] 并集放行(finance 未配置该字段=默认放行) → 应 200，"
            f"实得 {r2.status_code}: {r2.text[:200]}")

        # 6) 旁证：确认 d1 卡在「字段闸门」而非「项目编辑权」——
        #    d1 编辑另一个未做权限限制的字段应 200（说明它本来是 edit 成员）
        async with SessionLocal() as db:
            other = (await db.execute(
                select(models.OverviewField).where(models.OverviewField.id != fid)
                .order_by(models.OverviewField.id)
            )).scalars().first()
        if other is not None:
            r3 = await c.put(f"/api/overview/projects/{pid}/cell", headers=Hd1,
                             json={"field_id": other.id, "value": "y"})
            chk(r3.status_code == 200,
                f"旁证：d1 对未限制字段可编辑(证明其拥有项目 edit 权,403 来自字段闸门) → "
                f"应 200，实得 {r3.status_code}: {r3.text[:200]}")
        else:
            print("INFO: 只有一个一览字段，跳过旁证")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
