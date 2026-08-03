"""🆕 回款登记卡 + 销售订单审批卡。

回款登记这张卡的特殊之处：它的动作**有副作用且必须可逆**——
点「已收款」会把 balance 清零、原值存进 balance_contract，催办立即停。
生产上 balance_contract 全库 0 条，说明这条恢复路径从未在有值时执行过，
所以本测试专门锁「批注→清零→删批注→恢复」的完整往返。

其余锁的仍是三原则：卡片白名单、令牌绑定、行级隔离与端点谓词一致。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="settle")
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
from app.agent import cards

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

    tr = ASGITransport(app=app)
    async with AsyncClient(transport=tr, base_url="http://t", timeout=60) as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        for u, r in (("mgr", "manager"), ("s1", "sales"), ("s2", "sales")):
            await c.post("/api/admin/users", headers=H, json={
                "username": u, "password": "pass123", "full_name": u, "role_id": rid[r]})
        async def hdr(u):
            t = (await c.post("/api/auth/login", json={"username": u, "password": "pass123"})).json()
            return {"Authorization": f"Bearer {t['access_token']}"}
        Hm, H1, H2 = await hdr("mgr"), await hdr("s1"), await hdr("s2")

        async with SessionLocal() as db:
            uid = {u.username: u.id for u in (await db.execute(select(models.User))).scalars().all()}
            def mk(code):
                p = models.Project(code=code, name=code); db.add(p); return p
            pa, pb, pc = mk("S-A"), mk("S-B"), mk("S-C")
            await db.flush()
            db.add(models.SalesLedger(project_id=pa.id, customer="甲", sales_uid=uid["s1"],
                                      amount=100000, balance=30000, balance_date=None,
                                      before_ship=50000, ship_receivable=50000))
            db.add(models.SalesLedger(project_id=pb.id, customer="乙", sales_uid=uid["s2"],
                                      amount=80000, ship_receivable=20000))
            db.add(models.SalesLedger(project_id=pc.id, customer="丙", sales_uid=uid["s1"],
                                      amount=60000, order_state="pending"))
            await db.commit()
            aid = (await db.execute(select(models.SalesLedger).where(
                models.SalesLedger.customer == "甲"))).scalar_one().id
            cid = (await db.execute(select(models.SalesLedger).where(
                models.SalesLedger.customer == "丙"))).scalar_one().id

        async def snap(lid):
            async with SessionLocal() as db:
                l = await db.get(models.SalesLedger, lid)
                return {"balance": l.balance, "contract": l.balance_contract,
                        "ship": l.ship_receivable, "order_state": l.order_state}

        print("===== 1. 卡片装配 =====")
        r = await c.get("/api/agent/cards/ledger_settle", headers=Hm)
        chk(r.status_code == 200, f"取卡: {r.status_code}")
        d = r.json()
        chk(d["count"] == 2, f"两条盯不住的应收: {d['count']}")
        card = [x for x in d["cards"] if x["ref"] == aid][0]
        keys = {a["key"] for a in card["actions"]}
        chk(keys == {"settle_ship", "settle_balance"}, f"甲有两个动作: {keys}")
        chk(any(f["code"] == "no_due_date" for f in card["flags"]),
            f"标出没填到期日: {[f['code'] for f in card['flags']]}")

        print("\n===== 2. 未知类型与越界动作 =====")
        chk((await c.get("/api/agent/cards/not_a_type", headers=Hm)).status_code == 400,
            "未知卡片类型 400")
        r = await c.post("/api/agent/cards/verify-action", headers=Hm, json={
            "type": "ledger_settle", "ref": aid, "token": card["token"], "action": "delete"})
        chk(r.status_code == 400, f"越界动作被拒: {r.status_code}")

        print("\n===== 3. 行级隔离：拿不到别人的卡 =====")
        d1 = (await c.get("/api/agent/cards/ledger_settle", headers=H1)).json()
        chk({x["ref"] for x in d1["cards"]} == {aid}, f"s1 只看到自己的: {d1['count']}")
        d2 = (await c.get("/api/agent/cards/ledger_settle", headers=H2)).json()
        chk(aid not in {x["ref"] for x in d2["cards"]}, "s2 看不到 s1 的")
        r = await c.post("/api/agent/cards/verify-action", headers=H2, json={
            "type": "ledger_settle", "ref": aid, "token": card["token"], "action": "settle_ship"})
        chk(r.status_code == 400, f"拿别人令牌用不了: {r.status_code}")

        print("\n===== 4. 回款登记的完整往返（可逆性）=====")
        s0 = await snap(aid)
        chk(s0["balance"] == 30000 and s0["contract"] is None, f"初始: {s0}")
        r = await c.put(f"/api/sales/ledger/{aid}/payment-note", headers=Hm,
                        json={"field": "balance", "note": "【手机端】8/3 到账"})
        s1 = await snap(aid)
        chk(r.status_code == 200 and s1["balance"] == 0 and s1["contract"] == 30000,
            f"尾款登记后清零并存下合同额: {s1}")
        r = await c.put(f"/api/sales/ledger/{aid}/payment-note", headers=Hm,
                        json={"field": "balance", "note": ""})
        s2 = await snap(aid)
        chk(s2["balance"] == 30000 and s2["contract"] is None, f"撤销后原样恢复: {s2}")

        r = await c.put(f"/api/sales/ledger/{aid}/payment-note", headers=Hm,
                        json={"field": "before_ship", "note": "【手机端】发货款到账"})
        s3 = await snap(aid)
        chk(s3["ship"] == 0, f"发货款登记后应收清零: {s3}")
        await c.put(f"/api/sales/ledger/{aid}/payment-note", headers=Hm,
                    json={"field": "before_ship", "note": ""})
        s4 = await snap(aid)
        chk(s4["ship"] == 50000, f"撤销后恢复: {s4}")

        print("\n===== 5. 销售订单审批卡 =====")
        r = await c.get("/api/agent/cards/sales_order_approve", headers=Hm)
        oc = r.json()
        chk(oc["count"] == 1 and oc["cards"][0]["ref"] == cid, f"一笔待审: {oc['count']}")
        ocard = oc["cards"][0]
        chk({a["key"] for a in ocard["actions"]} == {"approve", "reject"}, "两个动作")
        r = await c.post("/api/agent/cards/verify-action", headers=Hm, json={
            "type": "sales_order_approve", "ref": cid,
            "token": ocard["token"], "action": "approve"})
        chk(r.status_code == 200, f"校验通过: {r.status_code} {r.text[:60]}")

        print("\n===== 6. 状态变了之后卡片动作要失效 =====")
        async with SessionLocal() as db:
            led = await db.get(models.SalesLedger, cid)
            led.order_state = None
            await db.commit()
        r = await c.post("/api/agent/cards/verify-action", headers=Hm, json={
            "type": "sales_order_approve", "ref": cid,
            "token": ocard["token"], "action": "approve"})
        chk(r.status_code == 400, f"已不在待办里 → 拒绝: {r.status_code} {r.text[:50]}")

        print("\n===== 7. 三类卡都登记了装配器 =====")
        chk(set(cards.ASSEMBLERS) == set(cards.CARD_TYPES),
            f"装配器与白名单一一对应: {sorted(cards.ASSEMBLERS)}")

    await engine.dispose()
    print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
