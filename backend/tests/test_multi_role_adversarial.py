"""多角色「对抗性」验证（临时 SQLite）：专攻并集语义的边界与回收。

覆盖：
A. 字段权限并集(OR)：某角色被禁看字段 + 另一未配置角色 → 仍可见（最宽松，
   正是「加未配置角色会恢复可见」的口径）。纯被禁角色 → 不可见。
B. 行级可见性并集：sales(受限,仅本人) + design_lead(部门负责人,看全部) → 看到全部项目。
   纯 sales(无台账) → 看不到。
C. 销售台账 all-view 并集：sales + sales_lead → 看全部台账；纯 sales → 仅本人。
D. 去角色即回收：[sales_lead] 改为 [sales] → 重新登录后只看本人台账。
E. 越权不放行：纯 warehouse 调管理端 → 403（has_role 不误放行）。
F. backfill_user_roles 幂等：重复跑不产生重复关联。
G. 删多角色用户：user_roles 关联随之清空。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="mradv")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select, func
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

    # 所有角色 id（含 admin/manager 等对 /roles 隐藏的）
    async with SessionLocal() as db:
        rid = {r.code: r.id for r in (await db.execute(select(models.Role))).scalars().all()}

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

        # ================= A. 字段权限并集(OR) =================
        # 取一个一览字段，禁 designer 看；finance 不配置
        async with SessionLocal() as db:
            of = (await db.execute(select(models.OverviewField).order_by(models.OverviewField.id))).scalars().first()
        chk(of is not None, "存在一览字段用于权限测试")
        if of is not None:
            r = await c.put(f"/api/permissions/overview-fields/{of.id}", headers=H, json={
                "permissions": [{"role_id": rid["designer"], "can_view": False, "can_edit": False}]})
            chk(r.status_code == 200, f"设置 designer 禁看字段: {r.text[:120]}")

            await mk("d_only", [rid["designer"]])
            await mk("d_plus_fin", [rid["designer"], rid["finance"]])
            Hd = await login("d_only")
            Hdf = await login("d_plus_fin")

            p1 = (await c.get("/api/permissions/me/overview", headers=Hd)).json()
            chk(p1.get(str(of.id), {}).get("can_view") is False,
                f"纯 designer 该字段不可见: {p1.get(str(of.id))}")

            p2 = (await c.get("/api/permissions/me/overview", headers=Hdf)).json()
            chk(p2.get(str(of.id), {}).get("can_view") is True,
                f"designer+finance 该字段并集可见(finance 未配置=放行): {p2.get(str(of.id))}")

        # ================= B. 行级可见性并集 =================
        for i in (1, 2):
            r = await c.post("/api/projects", headers=H, json={"code": f"ADV-{i}", "name": f"对抗项目{i}"})
            assert r.status_code == 200, f"建项目: {r.text}"

        await mk("s_only", [rid["sales"]])
        await mk("s_plus_dlead", [rid["sales"], rid["design_lead"]])
        Hs = await login("s_only")
        Hsd = await login("s_plus_dlead")

        ps_only = (await c.get("/api/projects", headers=Hs)).json()
        chk(all(p["code"] not in ("ADV-1", "ADV-2") for p in ps_only),
            f"纯 sales(无台账) 看不到这些项目: {[p['code'] for p in ps_only]}")
        ps_sd = (await c.get("/api/projects", headers=Hsd)).json()
        codes_sd = {p["code"] for p in ps_sd}
        chk("ADV-1" in codes_sd and "ADV-2" in codes_sd,
            f"sales+design_lead 看到全部项目(并集,负责人放行): {codes_sd}")

        # ================= C/D. 销售台账 all-view 并集 + 去角色回收 =================
        owner_uid = await mk("s_owner", [rid["sales"]])      # 台账归属人
        await mk("s_other", [rid["sales"]])                  # 另一个纯销售
        await mk("s_dual", [rid["sales"], rid["sales_lead"]])# 销售+主管(看全部)

        # 给 ADV-1 建一条台账，归属 s_owner
        async with SessionLocal() as db:
            pid1 = (await db.execute(select(models.Project.id).where(models.Project.code == "ADV-1"))).scalar_one()
            db.add(models.SalesLedger(project_id=pid1, sales_uid=owner_uid, contract="有"))
            await db.commit()

        Howner = await login("s_owner")
        Hother = await login("s_other")
        Hdual = await login("s_dual")
        chk(len((await c.get("/api/sales/ledger", headers=Howner)).json()["rows"]) == 1, "归属人看到本人台账")
        chk(len((await c.get("/api/sales/ledger", headers=Hother)).json()["rows"]) == 0, "其他纯销售看不到他人台账")
        dual_rows = (await c.get("/api/sales/ledger", headers=Hdual)).json()["rows"]
        chk(len(dual_rows) >= 1, f"sales+sales_lead 看全部台账(并集): {len(dual_rows)}")

        # 去角色回收：s_dual 改为仅 sales → 只看本人(0)
        async with SessionLocal() as db:
            duid = (await db.execute(select(models.User.id).where(models.User.username == "s_dual"))).scalar_one()
        r = await c.put(f"/api/admin/users/{duid}", headers=H, json={"role_ids": [rid["sales"]]})
        chk(r.status_code == 200 and r.json()["role_codes"] == ["sales"], f"降为纯 sales: {r.text[:120]}")
        Hdual2 = await login("s_dual")
        chk(len((await c.get("/api/sales/ledger", headers=Hdual2)).json()["rows"]) == 0,
            "去掉 sales_lead 后立即回收 → 只看本人(0)")

        # ================= E. 越权不放行 =================
        await mk("wh_only", [rid["warehouse"]])
        Hwh = await login("wh_only")
        chk((await c.get("/api/admin/users", headers=Hwh)).status_code == 403,
            "纯 warehouse 调管理端用户列表 → 403")
        chk((await c.post("/api/projects", headers=Hwh, json={"code": "X", "name": "x"})).status_code == 403,
            "纯 warehouse 建项目 → 403")

        # ================= H. 越权防护：不可经 API 提权 =================
        r = await c.post("/api/admin/users", headers=H, json={
            "username": "esc1", "password": "pass123", "role_ids": [rid["admin"]]})
        chk(r.status_code == 403, f"admin 角色不可经 API 分配(连 admin 操作也禁): {r.status_code} {r.text[:80]}")
        Hmgr = await login("manager", "manager123")
        # 🆕 口径 2026-06-17: 管理层可为自己/他人调整角色, 含分配 manager 角色
        r = await c.post("/api/admin/users", headers=Hmgr, json={
            "username": "esc2", "password": "pass123", "role_ids": [rid["manager"]]})
        chk(r.status_code == 200, f"manager 现可分配 manager 角色: {r.status_code} {r.text[:80]}")
        # 但 admin 仍是系统级, 任何人(含 manager)都不可经 API 夹带分配
        r = await c.post("/api/admin/users", headers=Hmgr, json={
            "username": "esc3", "password": "pass123", "role_ids": [rid["sales"], rid["admin"]]})
        chk(r.status_code == 403, f"manager 仍不可夹带 admin(系统级): {r.status_code}")
        # 对照：admin 可分配 manager
        r = await c.post("/api/admin/users", headers=H, json={
            "username": "mgr_ok", "password": "pass123", "role_ids": [rid["manager"]]})
        chk(r.status_code == 200, f"admin 分配 manager 应成功: {r.text[:100]}")

        # ===== 🆕 H2. 管理层可为他人/自己调整角色, 但不可篡改超级管理员 =====
        async with SessionLocal() as db:
            mgr_uid = (await db.execute(select(models.User).where(models.User.username == "manager"))).scalar_one().id
            admin_uid = (await db.execute(select(models.User).where(models.User.username == "admin"))).scalar_one().id
        target = await mk("mgr_target", [rid["sales"]])
        # manager 改他人角色 → 200
        r = await c.put(f"/api/admin/users/{target}", headers=Hmgr, json={"role_ids": [rid["designer"]]})
        chk(r.status_code == 200 and r.json()["role_codes"] == ["designer"], f"manager 改他人角色: {r.text[:100]}")
        # manager 给自己调整角色(在 manager 基础上加 sales) → 200
        r = await c.put(f"/api/admin/users/{mgr_uid}", headers=Hmgr, json={"role_ids": [rid["manager"], rid["sales"]]})
        chk(r.status_code == 200 and set(r.json()["role_codes"]) == {"manager", "sales"}, f"manager 给自己调整角色: {r.text[:100]}")
        # manager 不可篡改超级管理员账号 → 403
        r = await c.put(f"/api/admin/users/{admin_uid}", headers=Hmgr, json={"role_ids": [rid["sales"]]})
        chk(r.status_code == 403, f"manager 不可改 admin 账号: {r.status_code}")
        # 🆕 口径 2026-06-17: manager=admin 权限, manager 可删用户
        mdel = await mk("mgr_can_delete", [rid["logistics"]])
        r = await c.delete(f"/api/admin/users/{mdel}", headers=Hmgr)
        chk(r.status_code == 200, f"manager 可删普通用户: {r.status_code} {r.text[:80]}")
        # 但 manager 不可删除超级管理员账号 → 403
        r = await c.delete(f"/api/admin/users/{admin_uid}", headers=Hmgr)
        chk(r.status_code == 403, f"manager 不可删 admin 账号: {r.status_code}")

        # ================= I. 降级后项目可见性回填 =================
        # manager 用户无 ProjectMember；降为普通角色(finance)后应被回填为全项目成员 → 一览可见
        mid = (await c.post("/api/admin/users", headers=H, json={
            "username": "demoteme", "password": "pass123", "role_ids": [rid["manager"]]})).json()["id"]
        r = await c.put(f"/api/admin/users/{mid}", headers=H, json={"role_ids": [rid["finance"]]})
        chk(r.status_code == 200 and r.json()["role_codes"] == ["finance"], f"降级为 finance: {r.text[:100]}")
        Hdem = await login("demoteme")
        ov = (await c.get("/api/overview", headers=Hdem)).json()
        chk(len(ov.get("rows", [])) >= 2, f"降级后回填成员→一览可见项目: rows={len(ov.get('rows', []))}")

        # ================= J. salespeople 含「sales 仅为副角色」的用户 =================
        sec = await mk("sec_sales", [rid["finance"], rid["sales"]])  # 锚点 finance，sales 为副
        people = (await c.get("/api/sales/salespeople", headers=H)).json()
        chk(sec in {p["id"] for p in people}, "锚点非 sales 但副角色含 sales 的用户也应在销售员名单")

        # ================= K. 按角色推送命中「副角色」用户(notify 多角色修复) =================
        # 锚点 designer + 副角色 design_lead：push_message(to_role=design_lead) 必须能推到 ta
        dl = await mk("sec_dlead", [rid["designer"], rid["design_lead"]])
        from app.notify import push_message
        async with SessionLocal() as db:
            n = await push_message(db, to_role="design_lead", kind="info", text="【测试】设计部待办")
            got = (await db.execute(select(func.count()).select_from(models.Message)
                                    .where(models.Message.to_user_id == dl,
                                           models.Message.text == "【测试】设计部待办"))).scalar()
        chk(got == 1, f"K 按角色推送应命中副角色用户(设计部负责人为副角色): 推送{n}条, 该用户收到{got}")

        # ================= G. 删多角色用户清关联 =================
        del_uid = await mk("to_delete", [rid["warehouse"], rid["logistics"], rid["finance"]])
        async with SessionLocal() as db:
            n_before = (await db.execute(select(func.count()).select_from(models.UserRole)
                                         .where(models.UserRole.user_id == del_uid))).scalar()
        chk(n_before == 3, f"删前该用户有 3 条关联: {n_before}")
        r = await c.delete(f"/api/admin/users/{del_uid}", headers=H)
        chk(r.status_code == 200, f"删用户: {r.text[:120]}")
        async with SessionLocal() as db:
            n_after = (await db.execute(select(func.count()).select_from(models.UserRole)
                                        .where(models.UserRole.user_id == del_uid))).scalar()
        chk(n_after == 0, f"删后关联清空: {n_after}")

    # ================= F. backfill 幂等 =================
    async with SessionLocal() as db:
        before = (await db.execute(select(func.count()).select_from(models.UserRole))).scalar()
        await run_all(db)
        await run_all(db)
        after = (await db.execute(select(func.count()).select_from(models.UserRole))).scalar()
    chk(before == after, f"backfill_user_roles 幂等(再跑两遍不增): {before} -> {after}")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
