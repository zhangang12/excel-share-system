"""🆕 H5 门户按用户定制。

门户配置是用户提交的数据，会被回喂到渲染层，所以它是一个攻击面。
本测试锁四件事：

 1. **能摆什么由服务端说了算**（原则三 能力可枚举）——伪造一个目录里没有的
    key，或伪造一个自己无权用的工具卡，保存后一律消失；
 2. **自定义卡不是新能力**——它只留 label/q 两个字段，其余(url/tool/type…)全丢；
 3. **默认值按角色给，不是空门户**；脏数据也回落默认，不让人看到一片空白；
 4. **一个人的配置不影响另一个人**。
"""
import asyncio, json, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="portal")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.agent import portal

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        for u, role in (("mgr", "manager"), ("buyer", "buyer"), ("dsg", "designer")):
            await c.post("/api/admin/users", headers=H, json={
                "username": u, "password": "pass123", "full_name": u, "role_id": rid[role]})
        async def hdr(u):
            t = (await c.post("/api/auth/login", json={"username": u, "password": "pass123"})).json()
            return {"Authorization": f"Bearer {t['access_token']}"}
        Hm, Hb, Hd = await hdr("mgr"), await hdr("buyer"), await hdr("dsg")
        async with SessionLocal() as db:
            mgr_id = (await db.execute(select(models.User).where(
                models.User.username == "mgr"))).scalar_one().id

        print("===== 1. 默认门户按角色给，不是空的 =====")
        m = (await c.get("/api/agent/portal", headers=Hm)).json()
        keys = [t["key"] for t in m["tiles"]]
        chk(len(keys) > 0, f"manager 有默认门户: {keys}")
        chk("approvals" in keys and "balance_due" in keys,
            f"管理层默认含请款审批与尾款到期: {keys}")
        chk("po_arrival_overdue" not in keys,
            f"采购卡不进管理层默认（他两个月没碰过采购）: {keys}")

        b = (await c.get("/api/agent/portal", headers=Hb)).json()
        bkeys = [t["key"] for t in b["tiles"]]
        chk("po_arrival_overdue" in bkeys, f"采购角色默认含采购卡: {bkeys}")

        print("\n===== 2. 目录按权限过滤（能力可枚举）=====")
        dcat = {x["key"] for x in (await c.get("/api/agent/portal", headers=Hd)).json()["catalog"]}
        mcat = {x["key"] for x in m["catalog"]}
        chk("balance_due" in mcat, "管理层目录里有尾款到期")
        chk("balance_due" not in dcat, f"设计师目录里没有尾款到期（无权）: {sorted(dcat)}")

        print("\n===== 3. 伪造 key 一律被丢掉 =====")
        r = await c.put("/api/agent/portal", headers=Hd, json={"tiles": [
            {"key": "balance_due"},                      # 有权限的人才有；设计师无权
            {"key": "__proto__"},
            {"key": "../../etc/passwd"},
            {"key": "morning_report"},                   # 这个他有
        ]})
        got = [t["key"] for t in r.json()["tiles"]]
        chk(got == ["morning_report"], f"越权与伪造 key 全被丢: {got}")

        print("\n===== 4. 自定义卡只留 label/q，不能凭配置造能力 =====")
        r = await c.put("/api/agent/portal", headers=Hm, json={"tiles": [
            {"key": "approvals"},
            {"key": "custom:x1", "label": "我的常问", "q": "本月哪些项目在做",
             # ↓ 这些字段都该被丢掉：它们试图给卡片安一个新的执行路径
             "url": "/api/admin/users", "method": "DELETE", "tool": "anything",
             "type": "pay_req_approve", "actions": [{"key": "approve"}]},
        ]})
        tiles = r.json()["tiles"]
        cus = [t for t in tiles if t.get("custom")][0]
        chk(set(cus) <= {"key", "label", "q", "custom", "glyph", "tone", "desc"},
            f"自定义卡只剩白名单字段: {sorted(cus)}")
        chk("url" not in cus and "actions" not in cus and "tool" not in cus,
            "url/actions/tool 全被丢掉")
        chk(cus["q"] == "本月哪些项目在做", "提问原样保留")

        print("\n===== 5. 限长与去控制字符 =====")
        r = await c.put("/api/agent/portal", headers=Hm, json={"tiles": [
            {"key": "custom:x2", "label": "很长很长很长很长很长很长的标题", "q": "x" * 500},
            {"key": "custom:x3", "label": "带\x00控制\x1b字符", "q": "正常提问"},
        ]})
        t = r.json()["tiles"]
        chk(len(t[0]["label"]) <= portal.MAX_LABEL, f"标题截断到 {portal.MAX_LABEL}: {len(t[0]['label'])}")
        chk(len(t[0]["q"]) <= portal.MAX_QUESTION, f"提问截断到 {portal.MAX_QUESTION}: {len(t[0]['q'])}")
        chk("\x00" not in t[1]["label"] and "\x1b" not in t[1]["label"],
            f"控制字符被剥掉: {t[1]['label']!r}")

        print("\n===== 6. 数量上限 =====")
        r = await c.put("/api/agent/portal", headers=Hm, json={"tiles": [
            {"key": f"custom:n{i}", "label": f"卡{i}", "q": f"问题{i}"} for i in range(40)]})
        chk(len(r.json()["tiles"]) == portal.MAX_TILES,
            f"最多 {portal.MAX_TILES} 张: {len(r.json()['tiles'])}")

        print("\n===== 7. 一个人的配置不串到别人 =====")
        await c.put("/api/agent/portal", headers=Hm, json={"tiles": [{"key": "morning_report"}]})
        mine = [t["key"] for t in (await c.get("/api/agent/portal", headers=Hm)).json()["tiles"]]
        other = [t["key"] for t in (await c.get("/api/agent/portal", headers=Hb)).json()["tiles"]]
        chk(mine == ["morning_report"], f"我的配置生效: {mine}")
        chk(len(other) > 1 and "po_arrival_overdue" in other, f"别人不受影响: {other}")

        print("\n===== 8. 脏数据回落默认，不给空白门户 =====")
        async with SessionLocal() as db:
            row = (await db.execute(select(models.UserSetting).where(
                models.UserSetting.user_id == mgr_id,
                models.UserSetting.key == portal.PORTAL_KEY))).scalar_one()
            row.value = "{不是JSON"
            await db.commit()
        back = [t["key"] for t in (await c.get("/api/agent/portal", headers=Hm)).json()["tiles"]]
        chk(len(back) > 0, f"脏数据 → 回落角色默认而非空门户: {back}")

        print("\n===== 9. 恢复默认 =====")
        r = await c.delete("/api/agent/portal", headers=Hm)
        chk([t["key"] for t in r.json()["tiles"]][:1] == ["approvals"],
            f"恢复成管理层默认: {[t['key'] for t in r.json()['tiles']]}")

        print("\n===== 10. 存的是 key 不是副本（改目录文案能全员生效）=====")
        await c.put("/api/agent/portal", headers=Hm, json={"tiles": [{"key": "morning_report"}]})
        async with SessionLocal() as db:
            row = (await db.execute(select(models.UserSetting).where(
                models.UserSetting.user_id == mgr_id,
                models.UserSetting.key == portal.PORTAL_KEY))).scalar_one()
            stored = json.loads(row.value)
        chk(stored == [{"key": "morning_report"}],
            f"库里只存 key，不存 label/desc 副本: {stored}")

    await engine.dispose()
    print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
