"""采购收货逾期：采购员逐条 + 管理层每日一条汇总（2026-08-12 用户要求）。

原来只推下单采购员本人（2026-07-26 收窄，注释原话「不再推主管/管理层(反馈信息太多)」）。
用户要求把管理层加回来。**管理层走汇总不走逐条**：
生产上当前命中 31 条 × (2 manager + 1 admin) = 每天 93 条、天天重复，
一周就把管理层的消息列表淹了，然后整个系统的通知一起被静音——正是当初收窄的原因。

这个文件锁死的：
  1. 采购员本人仍然逐条收（一条明细一条）
  2. 管理层收到的是**一条汇总**，不是 N 条
  3. 汇总里写清楚共几条、压在谁手上、最久超期几天
  4. 同一天重复扫描不重复推（幂等），跨天才再推一条
  5. 15:00 之前不推（#292 原有时间窗，货车中午才发车）
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="podigest")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from unittest.mock import patch
from datetime import datetime as _dt
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models, overdue

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def msgs(db, biz_type, uid=None):
    q = select(models.Message).where(models.Message.biz_type == biz_type)
    if uid is not None:
        q = q.where(models.Message.to_user_id == uid)
    return list((await db.execute(q)).scalars().all())


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        admin_id = (await c.get("/api/auth/me", headers=H)).json()["id"]
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        uids = {}
        for uname, name, rc in [("b1", "采购甲", "buyer"), ("b2", "采购乙", "buyer"),
                                ("m1", "总经理", "manager")]:
            rr = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": name, "role_id": rid[rc]})
            chk(rr.status_code == 200, f"建用户 {name}: {rr.status_code} {rr.text[:70]}")
            uids[name] = rr.json()["id"]

        sup = (await c.post("/api/purchase-mgmt/suppliers", headers=H,
                            json={"name": "测试供应商"})).json()["id"]
        today = date.today()
        # 采购甲 3 条逾期（最久 10 天）、采购乙 1 条逾期（3 天）、1 条已收货(不该命中)、1 条未到期(不该命中)
        plan = [("甲件A", 10, uids["采购甲"], None), ("甲件B", 5, uids["采购甲"], None),
                ("甲件C", 1, uids["采购甲"], None), ("乙件A", 3, uids["采购乙"], None),
                ("已收货件", 8, uids["采购甲"], (today - timedelta(days=1)).isoformat()),
                ("未到期件", -5, uids["采购乙"], None)]
        async with SessionLocal() as db:
            for name, over, bid, arrived in plan:
                db.add(models.PurchaseItem(
                    supplier_id=sup, item_name=name, qty=1, buyer_id=bid,
                    expected_arrival=(today - timedelta(days=over)).isoformat(),
                    arrival_date=arrived))
            await db.commit()

        # ---- 15:00 之前不推（#292 原有时间窗）----
        class _Morning(_dt):
            @classmethod
            def now(cls, tz=None):
                return _dt(today.year, today.month, today.day, 9, 0, tzinfo=tz)
        with patch.object(overdue, "datetime", _Morning):
            async with SessionLocal() as db:
                res = await overdue.scan_po_arrival_overdue(db)
        chk(res["notified"] == 0, f"15:00 前不推（货车中午才发车，#292）: {res}")

        # ---- 正常扫描 ----
        class _Afternoon(_dt):
            @classmethod
            def now(cls, tz=None):
                return _dt(today.year, today.month, today.day, 16, 0, tzinfo=tz)
        with patch.object(overdue, "datetime", _Afternoon):
            async with SessionLocal() as db:
                res = await overdue.scan_po_arrival_overdue(db)
                await db.commit()
        chk(res["scanned"] == 4,
            f"只扫到 4 条逾期未收货（已收货的、未到期的都不该命中）: {res}")

        async with SessionLocal() as db:
            # 1) 采购员本人逐条
            m_a = await msgs(db, "po_arrival_overdue", uids["采购甲"])
            m_b = await msgs(db, "po_arrival_overdue", uids["采购乙"])
            chk(len(m_a) == 3, f"1) 采购甲逐条收到 3 条: {len(m_a)}")
            chk(len(m_b) == 1, f"1) 采购乙逐条收到 1 条: {len(m_b)}")

            # 2) 管理层收到的是一条汇总而不是 4 条
            d_m1 = await msgs(db, "po_arrival_overdue_digest", uids["总经理"])
            d_adm = await msgs(db, "po_arrival_overdue_digest", admin_id)
            chk(len(d_m1) == 1, f"2) 总经理只收到 1 条汇总（不是 4 条逐条）: {len(d_m1)}")
            chk(len(d_adm) == 1, f"2) admin 只收到 1 条汇总: {len(d_adm)}")
            chk(len(await msgs(db, "po_arrival_overdue", uids["总经理"])) == 0,
                "2) 管理层不再收逐条明细（那是 93 条/天的来源）")

            # 3) 汇总内容要能直接判断严重程度
            t = d_m1[0].text if d_m1 else ""
            chk("共 4 条" in t, f"3) 汇总写明总条数: {t[:90]}")
            chk("采购甲 3 条" in t and "采购乙 1 条" in t, f"3) 汇总写明压在谁手上: {t[:120]}")
            chk("最久超期 10 天" in t, f"3) 汇总写明最久超期天数: {t[:140]}")
            chk(t.index("采购甲") < t.index("采购乙"), "3) 按条数从多到少排，最该催的排最前")

        # ---- 4) 同日重复扫描不重复推 ----
        with patch.object(overdue, "datetime", _Afternoon):
            async with SessionLocal() as db:
                await overdue.scan_po_arrival_overdue(db)
                await db.commit()
        async with SessionLocal() as db:
            chk(len(await msgs(db, "po_arrival_overdue_digest", uids["总经理"])) == 1,
                "4) 同一天重复扫描，汇总不重复推")
            chk(len(await msgs(db, "po_arrival_overdue", uids["采购甲"])) == 3,
                "4) 同一天重复扫描，逐条也不重复推")

        # ---- 5) 跨天再推一条 ----
        tomorrow = today + timedelta(days=1)

        class _Tomorrow(_dt):
            @classmethod
            def now(cls, tz=None):
                return _dt(tomorrow.year, tomorrow.month, tomorrow.day, 16, 0, tzinfo=tz)
        with patch.object(overdue, "datetime", _Tomorrow):
            async with SessionLocal() as db:
                await overdue.scan_po_arrival_overdue(db)
                await db.commit()
        async with SessionLocal() as db:
            chk(len(await msgs(db, "po_arrival_overdue_digest", uids["总经理"])) == 2,
                "5) 第二天再推一条汇总（还没收货就得继续提醒）")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
