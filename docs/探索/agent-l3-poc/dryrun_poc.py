"""原型：不改业务接口，原样「演练」一次写操作 —— 外层事务 + 保存点 + 回滚，拦截企微外发，记录字段级改动。"""
import asyncio, contextvars, os, sys, tempfile
tmp = tempfile.mkdtemp(prefix="dryrun")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/t.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
sys.path.insert(0, os.getcwd())

from sqlalchemy import event, select, func, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models, schemas, notify
from app.config import settings
from app.routers import purchase_mgmt_router as pm

# SQLite 的保存点要这个官方写法才可靠（生产 Postgres 原生支持，不需要）
@event.listens_for(engine.sync_engine, "connect")
def _c(dbapi_conn, rec): dbapi_conn.isolation_level = None
@event.listens_for(engine.sync_engine, "begin")
def _b(conn): conn.exec_driver_sql("BEGIN")

OUTBOX: contextvars.ContextVar = contextvars.ContextVar("outbox", default=None)
SENT_FOR_REAL = []
async def fake_send(db, user_ids, text):
    box = OUTBOX.get()
    if box is not None:
        box.append((sorted(user_ids), text[:40]))   # 演练：只记下来
    else:
        SENT_FOR_REAL.append((sorted(user_ids), text[:40]))
notify._send_wecom = fake_send
settings.wecom_corp_id, settings.wecom_secret = "dummy", "dummy"   # 让企微分支真的走到

def watch(session: AsyncSession, out: list):
    @event.listens_for(session.sync_session, "before_flush")
    def _cap(sess, ctx, inst):
        for o in sess.new:
            out.append(("新增", type(o).__tablename__))
        for o in sess.dirty:
            ch = [a.key for a in inspect(o).attrs if a.history.has_changes()]
            if ch: out.append(("修改", type(o).__tablename__, tuple(sorted(ch))))
        for o in sess.deleted:
            out.append(("删除", type(o).__tablename__))

async def counts():
    async with SessionLocal() as db:
        return {t.__tablename__: (await db.execute(select(func.count()).select_from(t))).scalar()
                for t in (models.PaymentRequest, models.Message, models.AuditLog)}

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)
        buyer = (await db.execute(select(models.User).where(models.User.username == "admin"))).scalars().first()
        fl_role = (await db.execute(select(models.Role).where(models.Role.code == "finance_lead"))).scalars().first()
        db.add(models.User(username="fl", full_name="财务主管", password_hash="x", role_id=fl_role.id, wxid="caiwu01"))
        sup = models.Supplier(name="无锡俊帆金属"); db.add(sup); await db.flush()
        it = models.PurchaseItem(supplier_id=sup.id, item_name="钢板 Q235", qty=2, unit_price=500,
                                 buyer_id=buyer.id, received_amount=1000)
        db.add(it); await db.commit()
        ids = (buyer.id, sup.id, it.id)
    body = schemas.PaymentRequestCreate(supplier_id=ids[1], requested_amount=1000,
                                        items=[{"item_id": ids[2], "allocated_amount": 1000}])

    before = await counts()
    # ── 演练 ──
    diffs_dry, box = [], []
    async with engine.connect() as conn:
        outer = await conn.begin()
        s = AsyncSession(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
        watch(s, diffs_dry)
        u = (await s.execute(select(models.User).where(models.User.id == ids[0]))).scalars().first()
        tok = OUTBOX.set(box)
        try:
            res = await pm.create_payment_request(body=body, current=u, db=s)
            print("演练返回：请款单", res.id if hasattr(res, "id") else res, "金额", getattr(res, "requested_amount", "?"))
        finally:
            OUTBOX.reset(tok)
            await s.close()
            await outer.rollback()
    after_dry = await counts()
    print("演练前行数", before, "\n演练后行数", after_dry)
    print("演练截下的企微消息", box, " 真发出去的", SENT_FOR_REAL)
    # ── 真实执行（同样的输入）──
    diffs_real = []
    async with SessionLocal() as s2:
        watch(s2, diffs_real)
        u = (await s2.execute(select(models.User).where(models.User.id == ids[0]))).scalars().first()
        await pm.create_payment_request(body=body, current=u, db=s2)
    after_real = await counts()
    print("真实执行后行数", after_real, " 真发出去的企微", len(SENT_FOR_REAL), "条")
    print("\n演练改动：", diffs_dry)
    print("真实改动：", diffs_real)
    ok = (before == after_dry and sorted(map(str, diffs_dry)) == sorted(map(str, diffs_real)) and box and not SENT_FOR_REAL[:-len(box)])
    print("\n结论：", "演练零落库、零外发，改动与真实执行一致 ✅" if before == after_dry and sorted(map(str, diffs_dry)) == sorted(map(str, diffs_real)) else "不一致 ❌")

asyncio.run(main())
