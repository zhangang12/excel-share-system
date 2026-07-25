"""🆕 #292 采购未到货提醒时间窗测试（货车中午才发车，上午提醒是误报）：
1. 业务时区(UTC+8) 15:00 前扫描（10:30 / 14:59）→ 不推任何消息，scanned/notified 均为 0；
2. 15:00 整起推送（边界含整点）：预计今天到货且未收货 → 采购员收到 1 条；
3. 15 点后当日重扫仍按日级幂等键去重，不重复推。
（monkeypatch app.overdue.datetime 固定当前时间；临时库跑完即删。）
"""
import asyncio, os, sys, tempfile, shutil
from datetime import datetime as RealDT, timezone, timedelta

tmp = tempfile.mkdtemp(prefix="poarrwin")
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
import app.overdue as ov

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)

CN = timezone(timedelta(hours=8))
TODAY = RealDT.now(CN).date().isoformat()


class FakeDT(RealDT):
    """替换 app.overdue.datetime，now() 返回固定的业务时区时间。"""
    fixed = None
    @classmethod
    def now(cls, tz=None):
        return cls.fixed


def at(h, mi=0):
    """业务(中国)今天 h:mi 的 aware 时间。"""
    return RealDT.now(CN).replace(hour=h, minute=mi, second=0, microsecond=0)


async def msgs_for(db, uid, biz_type=None, biz_id=None):
    q = select(models.Message).where(models.Message.to_user_id == uid)
    if biz_type:
        q = q.where(models.Message.biz_type == biz_type)
    if biz_id:
        q = q.where(models.Message.biz_id == biz_id)
    return list((await db.execute(q)).scalars().all())


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        r = await c.post("/api/admin/users", headers=H,
                         json={"username": "b1", "password": "pass123", "full_name": "b1", "role_id": rid["buyer"]})
        assert r.status_code == 200, r.text
        b1 = r.json()["id"]
        Hb1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'b1','password':'pass123'})).json()['access_token']}"}

        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb1, json={"name": "时间窗供应商"})
        chk(r.status_code == 200, f"建供应商: {r.text[:120]}")
        sid = r.json()["id"]
        r = await c.post("/api/purchase-mgmt/items", headers=Hb1, json={
            "supplier_id": sid, "item_name": "轴承", "qty": 2, "unit_price": 10,
            "expected_arrival": TODAY})
        chk(r.status_code == 200, f"建采购明细(预计今天到货): {r.text[:150]}")
        it1 = r.json()["id"]

        ov.datetime = FakeDT
        try:
            async with SessionLocal() as db:
                # ===== 1. 15:00 前扫描 → 不推 =====
                FakeDT.fixed = at(10, 30)
                res = await ov.scan_po_arrival_overdue(db)
                chk(res["notified"] == 0 and res["scanned"] == 0, f"上午10:30 扫描不推: {res}")
                chk(len(await msgs_for(db, b1, "po_arrival_overdue", it1)) == 0, "上午不产生消息")
                FakeDT.fixed = at(14, 59)
                res = await ov.scan_po_arrival_overdue(db)
                chk(res["notified"] == 0, f"14:59 仍不推: {res}")
                chk(len(await msgs_for(db, b1, "po_arrival_overdue", it1)) == 0, "14:59 仍无消息")

                # ===== 2. 15:00 整起推送（边界含整点） =====
                FakeDT.fixed = at(15, 0)
                res = await ov.scan_po_arrival_overdue(db)
                chk(res["notified"] == 1, f"15:00 首发推送: {res}")
                chk(len(await msgs_for(db, b1, "po_arrival_overdue", it1)) == 1, "采购员15点收到提醒")

                # ===== 3. 15 点后当日重扫仍幂等 =====
                FakeDT.fixed = at(16, 30)
                res = await ov.scan_po_arrival_overdue(db)
                chk(res["notified"] == 0, f"16:30 当日重扫幂等: {res}")
                chk(len(await msgs_for(db, b1, "po_arrival_overdue", it1)) == 1, "当日仍只一条")
        finally:
            ov.datetime = RealDT

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
