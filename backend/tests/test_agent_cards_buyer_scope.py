"""受限采购员拿 agent 待办卡片会 500（2026-08-22 查请款反馈时发现的线上真 bug）。

`app/agent/cards/pay_req.py` 的行级隔离分支写成了
`models.PaymentRequestItem.purchase_item_id` —— 这个属性**根本不存在**
（PaymentRequestItem 只有 id/request_id/item_id/allocated_amount；
purchase_item_id 是 WhTxn 上的列，models.py:421）。

为什么一直没被发现：
  · 导入时不报错，SQLAlchemy 只在真正取属性时抛 AttributeError；
  · 这一整段包在 `if _buyer_restricted(current):` 里，**管理员走不到**，
    所有用管理员账号跑的测试和手工验证全绿；
  · 同文件 133 行早就写了「关联字段是 item_id，不是 purchase_item_id」的警告，
    下面那段也改对了，唯独 59 行漏了。

2026-08-22 生产实测：王芹、李新新（都是受限采购员，也正是提这轮反馈的人）
调用即 AttributeError；超级管理员正常返回 1 张待审卡。

本文件用**受限采购员**的身份走一遍，锁住这条路不再回归。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="agcard")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models
from app.routers.purchase_mgmt_router import _buyer_restricted

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    # ⚠️ raise_app_exceptions=False：本文件要断言的就是"接口不能 500"。
    #   用默认值的话，未捕获异常会被 httpx 原样抛出，整个测试文件当场崩掉、
    #   看不到是哪条断言挂的——报错还得靠读 traceback 猜。
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        buyer_id = await mkuser("card_buyer", ["buyer"])          # 受限采购员（王芹/李新新这类）
        await mkuser("card_lead", ["finance", "finance_lead"])
        Hb = await login("card_buyer", "pass123")
        Hlead = await login("card_lead", "pass123")

        # 先确认这个账号确实是「受限采购员」——否则这条测试等于没测到那个分支
        from sqlalchemy import select as _sel
        async with SessionLocal() as db:
            uo = (await db.execute(_sel(models.User).where(models.User.id == buyer_id))).scalars().unique().one()
            restricted = _buyer_restricted(uo)
        chk(restricted, "前提：card_buyer 是**受限采购员**（不受限的话这条测试根本走不到出错的分支）")

        # 造一张待审请款单（本人下的单 → 行级隔离应当认它是"我的"）
        sid = (await c.post("/api/purchase-mgmt/suppliers", headers=Hb,
                            json={"name": "无锡市俊帆金属科技制造有限公司"})).json()["id"]
        iid = (await c.post("/api/purchase-mgmt/items", headers=Hb,
                            json={"supplier_id": sid, "item_name": "钢板", "qty": 2,
                                  "unit_price": 500})).json()["id"]
        r = await c.post("/api/purchase-mgmt/payment-requests", headers=Hb,
                         json={"supplier_id": sid, "requested_amount": 1000,
                               "items": [{"item_id": iid, "allocated_amount": 1000}]})
        chk(r.status_code in (200, 201), f"造一张待审请款单: {r.status_code} {r.text[:70]}")

        # ===== 核心：受限采购员拿卡片，必须 200，不能 500 =====
        r = await c.get("/api/agent/cards/pending", headers=Hb)
        chk(r.status_code == 200,
            f"**受限采购员拿待办卡片必须 200**（改之前这里是 AttributeError→500）: "
            f"{r.status_code} {r.text[:110]}")
        if r.status_code == 200:
            body = r.json()
            chk(body.get("count", 0) >= 1,
                f"而且能看到自己那张（行级隔离要真的生效，不是靠返回空蒙混过关）: {body.get('count')}")

        # 财务主管（不受限）也要正常——别为了修受限分支把不受限的搞坏
        r2 = await c.get("/api/agent/cards/pending", headers=Hlead)
        chk(r2.status_code == 200, f"不受限账号照常 200: {r2.status_code} {r2.text[:80]}")

        # 简报走的是同一个 pending_pay_reqs，一并锁住
        from app.agent.cards import pay_req
        async with SessionLocal() as db:
            uo = (await db.execute(_sel(models.User).where(models.User.id == buyer_id))).scalars().unique().one()
            try:
                rows = await pay_req.pending_pay_reqs(db, uo)
                ok, why = True, f"{len(rows)} 张"
            except Exception as e:  # noqa: BLE001
                ok, why = False, f"{type(e).__name__}: {e}"
        chk(ok, f"每日简报也走同一个 pending_pay_reqs，同样不能炸: {why}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
