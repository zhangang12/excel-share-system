"""OA 审批链「指定到人」+ 代理人。

要锁死的口径（每一条都对应一个真实会踩的坑）：
  1. 指定到人后，**同角色的其他人不该再看到这张单**——这正是这条改动的目的，
     漏了这条等于白改（待办里照样一堆别人该批的单）。
  2. 被指定的人能批、能在待办里看到，即使他没挂那个 approver_role。
  3. 代理人在 3 天内**不能**批；晾够 3 天才能批，而且本人始终能批（不是转移）。
  4. 计时从「这一步真正轮到」起算，不是建单时刻——否则一张在前面排了很久的单
     一轮到就直接进代理人待办。
  5. 存量在途单的 activated_at 是 NULL，**不能**被当成"很久以前"而立刻放行代理人。
  6. 配置时指定一个停用的人要当场拒绝（配上了就是把单子卡死）。
  7. 改配置不影响在途单（快照口径，原有设计不能被破坏）。
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone

tmp = tempfile.mkdtemp(prefix="oadep")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def _set_activated(rid: int, step_order: int, when):
    """直接改库里的 activated_at —— 没别的办法造"已经晾了 N 天"这个状态。"""
    async with SessionLocal() as db:
        s = (await db.execute(select(models.OaRequestStep).where(
            models.OaRequestStep.request_id == rid,
            models.OaRequestStep.step_order == step_order))).scalar_one()
        s.activated_at = when
        await db.commit()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid_map = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(u, rc, fn):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": u, "password": "pass123", "full_name": fn, "role_id": rid_map[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        async def login(u):
            r = await c.post("/api/auth/login", json={"username": u, "password": "pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        # 两个人挂同一个角色：finance。指定到人之后 f2 不该再看到 f1 的单。
        f1 = await mk("f1", "finance", "钱一")
        f2 = await mk("f2", "finance", "钱二")
        dep = await mk("dep", "finance", "代理人")
        emp = await mk("emp", "sales", "小张")
        Hf1, Hf2, Hdep, Hemp = await login("f1"), await login("f2"), await login("dep"), await login("emp")

        # ===== 部门 + 单据类型 =====
        depts = (await c.get("/api/oa/departments", headers=H)).json()
        dept_id = depts[0]["id"]
        doc = [d for d in (await c.get("/api/oa/doc-types", headers=H)).json() if d["enabled"]][0]
        doc_type, doc_cat = doc["key"], doc["category"]

        # ===== 配置：一步，指定给 f1 =====
        r = await c.post("/api/oa/chains", headers=H, json={
            "department_id": dept_id, "doc_type": doc_type, "step_order": 1,
            "approver_role": "finance", "approver_user_id": f1, "enabled": True})
        chk(r.status_code == 200, f"配置指定到人: {r.text[:120]}")
        step_id = r.json().get("id")
        chk(r.json().get("approver_user_id") == f1, "出参带 approver_user_id")
        chk(r.json().get("approver_name") == "钱一", f"出参带审批人姓名: {r.json().get('approver_name')}")
        chk(r.json().get("step_label") == "钱一", f"展示名默认用人名: {r.json().get('step_label')}")

        # 6. 指定停用的人要当场拒绝
        await c.put(f"/api/admin/users/{f2}", headers=H, json={"is_active": False})
        r = await c.put(f"/api/oa/chains/{step_id}", headers=H, json={
            "department_id": dept_id, "doc_type": doc_type, "step_order": 1,
            "approver_role": "finance", "approver_user_id": f2, "enabled": True})
        chk(r.status_code == 400 and "停用" in r.text, f"指定停用的人被拒: {r.status_code} {r.text[:80]}")
        await c.put(f"/api/admin/users/{f2}", headers=H, json={"is_active": True})
        Hf2 = await login("f2")

        # ===== 提交一张单 =====
        r = await c.post("/api/oa/requests", headers=Hemp, json={
            "category": doc_cat, "doc_type": doc_type, "department_id": dept_id, "title": "指定到人测试", "amount": 100})
        chk(r.status_code == 200, f"提交申请: {r.text[:160]}")
        req = r.json()
        req_id = req["id"]
        chk(req["steps"][0].get("approver_user_id") == f1, "在途单快照了指定人")
        chk(req["steps"][0].get("activated_at"), "第一步 activated_at 已写入")

        # 1. 同角色的另一个人不该在待办里看到
        pend_f2 = (await c.get("/api/oa/requests?scope=pending_me", headers=Hf2)).json()
        ids_f2 = [x["id"] for x in (pend_f2 if isinstance(pend_f2, list) else pend_f2.get("items", []))]
        chk(req_id not in ids_f2, f"同角色的钱二看不到这张单: {ids_f2}")

        # 2. 被指定的人能看到、能批
        pend_f1 = (await c.get("/api/oa/requests?scope=pending_me", headers=Hf1)).json()
        ids_f1 = [x["id"] for x in (pend_f1 if isinstance(pend_f1, list) else pend_f1.get("items", []))]
        chk(req_id in ids_f1, f"被指定的钱一看得到: {ids_f1}")
        chk((await c.get(f"/api/oa/requests/{req_id}", headers=Hf1)).json().get("can_approve") is True,
            "钱一 can_approve=True")
        chk((await c.get(f"/api/oa/requests/{req_id}", headers=Hf2)).json().get("can_approve") is False,
            "钱二 can_approve=False")

        # 同角色的人硬打审批接口也要被拒（不能只靠前端藏按钮）
        r = await c.put(f"/api/oa/requests/{req_id}/approve", headers=Hf2, json={})
        chk(r.status_code == 403, f"钱二直接调审批被拒: {r.status_code}")
        chk("钱一" in r.text, f"403 说清楚是谁该批: {r.text[:120]}")

        # 3. 代理人：还没到 3 天，不能批
        await c.put(f"/api/admin/users/{f1}", headers=H, json={"deputy_uid": dep})
        r = await c.put(f"/api/oa/requests/{req_id}/approve", headers=Hdep, json={})
        chk(r.status_code == 403, f"代理人 3 天内不能批: {r.status_code} {r.text[:100]}")
        pend_dep = (await c.get("/api/oa/requests?scope=pending_me", headers=Hdep)).json()
        ids_dep = [x["id"] for x in (pend_dep if isinstance(pend_dep, list) else pend_dep.get("items", []))]
        chk(req_id not in ids_dep, "3 天内不进代理人待办")

        # 5. activated_at 为 NULL（存量在途单）不能被当成"很久以前"
        await _set_activated(req_id, 1, None)
        r = await c.put(f"/api/oa/requests/{req_id}/approve", headers=Hdep, json={})
        chk(r.status_code == 403, f"activated_at 为空时代理人仍不能批: {r.status_code}")
        pend_dep = (await c.get("/api/oa/requests?scope=pending_me", headers=Hdep)).json()
        ids_dep = [x["id"] for x in (pend_dep if isinstance(pend_dep, list) else pend_dep.get("items", []))]
        chk(req_id not in ids_dep, "activated_at 为空时不进代理人待办")

        # 3b. 晾够 4 天 → 代理人可以批，且进了他的待办
        await _set_activated(req_id, 1, datetime.now(timezone.utc) - timedelta(days=4))
        pend_dep = (await c.get("/api/oa/requests?scope=pending_me", headers=Hdep)).json()
        ids_dep = [x["id"] for x in (pend_dep if isinstance(pend_dep, list) else pend_dep.get("items", []))]
        chk(req_id in ids_dep, f"晾够 3 天后进代理人待办: {ids_dep}")
        d = (await c.get(f"/api/oa/requests/{req_id}", headers=Hdep)).json()
        chk(d.get("can_approve") is True, "代理人 can_approve=True")
        chk(d["steps"][0].get("deputy_ready") is True, "步骤上标了 deputy_ready")
        # 本人仍然能批（不是转移）
        chk((await c.get(f"/api/oa/requests/{req_id}", headers=Hf1)).json().get("can_approve") is True,
            "本人仍然能批（代理不是转移）")
        # 不相干的人还是不能批
        chk((await c.get(f"/api/oa/requests/{req_id}", headers=Hf2)).json().get("can_approve") is False,
            "晾再久，非代理人的同角色也不能批")

        r = await c.put(f"/api/oa/requests/{req_id}/approve", headers=Hdep, json={})
        chk(r.status_code == 200, f"代理人审批成功: {r.status_code} {r.text[:120]}")

        # ===== 4. 多步：第二步的计时从"轮到"起算，不是建单时刻 =====
        await c.put(f"/api/oa/chains/{step_id}", headers=H, json={
            "department_id": dept_id, "doc_type": doc_type, "step_order": 1,
            "approver_role": "sales", "approver_user_id": None, "enabled": True})
        r = await c.post("/api/oa/chains", headers=H, json={
            "department_id": dept_id, "doc_type": doc_type, "step_order": 2,
            "approver_role": "finance", "approver_user_id": f1, "enabled": True})
        chk(r.status_code == 200, f"加第二步(指定给钱一): {r.text[:120]}")

        r = await c.post("/api/oa/requests", headers=Hemp, json={
            "category": doc_cat, "doc_type": doc_type, "department_id": dept_id, "title": "两步测试", "amount": 50})
        req2 = r.json()["id"]
        s2 = [s for s in r.json()["steps"] if s["step_order"] == 2][0]
        chk(s2.get("activated_at") is None, "第二步建单时 activated_at 为空（还没轮到）")

        # 把第一步晾 10 天，然后销售批掉 → 第二步的计时应该从现在开始，不是从建单
        await _set_activated(req2, 1, datetime.now(timezone.utc) - timedelta(days=10))
        Hs = await login("emp")
        r = await c.put(f"/api/oa/requests/{req2}/approve", headers=Hs, json={})
        chk(r.status_code == 200, f"第一步(按角色)审批通过: {r.status_code} {r.text[:120]}")
        d = (await c.get(f"/api/oa/requests/{req2}", headers=H)).json()
        s2 = [s for s in d["steps"] if s["step_order"] == 2][0]
        chk(s2.get("activated_at") is not None, "轮到第二步时写了 activated_at")
        chk(s2.get("deputy_ready") is False, "第二步刚轮到，代理人不该立刻能接手")
        r = await c.put(f"/api/oa/requests/{req2}/approve", headers=Hdep, json={})
        chk(r.status_code == 403, f"前面排了 10 天也不算第二步的等待时间: {r.status_code}")

        # ===== 7. 改配置不影响在途单 =====
        chains = (await c.get(f"/api/oa/chains?department_id={dept_id}&doc_type={doc_type}",
                              headers=H)).json()
        sid2 = [x for x in chains if x["step_order"] == 2][0]["id"]
        await c.put(f"/api/oa/chains/{sid2}", headers=H, json={
            "department_id": dept_id, "doc_type": doc_type, "step_order": 2,
            "approver_role": "finance", "approver_user_id": f2, "enabled": True})
        d = (await c.get(f"/api/oa/requests/{req2}", headers=H)).json()
        s2 = [s for s in d["steps"] if s["step_order"] == 2][0]
        chk(s2.get("approver_user_id") == f1, "改配置不影响在途单（仍是钱一）")

        # ===== 不能把自己设成自己的代理人 / 不能互设 =====
        r = await c.put(f"/api/admin/users/{f1}", headers=H, json={"deputy_uid": f1})
        chk(r.status_code == 400, f"不能把自己设为代理人: {r.status_code}")
        r = await c.put(f"/api/admin/users/{dep}", headers=H, json={"deputy_uid": f1})
        chk(r.status_code == 400 and "互设" in r.text, f"不能互设代理人: {r.status_code} {r.text[:80]}")
        # 清空
        r = await c.put(f"/api/admin/users/{f1}", headers=H, json={"deputy_uid": -1})
        chk(r.status_code == 200 and r.json().get("deputy_uid") is None, "传 -1 可清空代理人")

        # ===== 老行为不能坏：不指定人时仍然按角色，谁在岗谁批 =====
        await c.put(f"/api/oa/chains/{sid2}", headers=H, json={
            "department_id": dept_id, "doc_type": doc_type, "step_order": 2,
            "approver_role": "finance", "approver_user_id": None, "enabled": True})
        r = await c.post("/api/oa/requests", headers=Hemp, json={
            "category": doc_cat, "doc_type": doc_type, "department_id": dept_id, "title": "纯角色链", "amount": 10})
        req3 = r.json()["id"]
        await c.put(f"/api/oa/requests/{req3}/approve", headers=await login("emp"), json={})
        for who, hh in (("钱一", Hf1), ("钱二", Hf2)):
            d = (await c.get(f"/api/oa/requests/{req3}", headers=hh)).json()
            chk(d.get("can_approve") is True, f"没指定人时 {who} 都能批（老行为不变）")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
