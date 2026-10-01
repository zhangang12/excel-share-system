"""原型 2：任意写接口经完整 HTTP 链路演练（鉴权、角色检查、参数校验全照常），零改造。"""
import asyncio, contextvars, os, sys, tempfile
tmp = tempfile.mkdtemp(prefix="gdry")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/t.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
sys.path.insert(0, os.getcwd())
from sqlalchemy import event, select, func, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base, get_db
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models, notify
from app.config import settings

@event.listens_for(engine.sync_engine, "connect")
def _c(dbapi_conn, rec): dbapi_conn.isolation_level = None
@event.listens_for(engine.sync_engine, "begin")
def _b(conn): conn.exec_driver_sql("BEGIN")

DRY = contextvars.ContextVar("dry", default=None)       # 演练上下文：{"conn":..., "diff":[...], "outbox":[...]}
SENT = []
async def send(db, uids, text):
    d = DRY.get()
    (d["outbox"] if d else SENT).append((sorted(uids), text[:30]))
notify._send_wecom = send
settings.wecom_corp_id = settings.wecom_secret = "dummy"

async def dry_aware_get_db():
    d = DRY.get()
    if not d:                       # 平时：和原来一模一样
        async with SessionLocal() as s:
            yield s
        return
    s = AsyncSession(bind=d["conn"], join_transaction_mode="create_savepoint", expire_on_commit=False)
    @event.listens_for(s.sync_session, "before_flush")
    def _cap(sess, ctx, inst):
        for o in sess.new: d["diff"].append(("新增", o.__tablename__))
        for o in sess.dirty:
            ch = sorted(a.key for a in inspect(o).attrs if a.history.has_changes())
            if ch: d["diff"].append(("修改", o.__tablename__, tuple(ch)))
        for o in sess.deleted: d["diff"].append(("删除", o.__tablename__))
    try:
        yield s
    finally:
        await s.close()
app.dependency_overrides[get_db] = dry_aware_get_db

async def call(c, H, method, url, body=None, dry=True):
    if not dry:
        r = await c.request(method, url, headers=H, json=body); return r.status_code, None
    async with engine.connect() as conn:
        outer = await conn.begin()
        d = {"conn": conn, "diff": [], "outbox": []}
        tok = DRY.set(d)
        try:
            r = await c.request(method, url, headers=H, json=body)
        finally:
            DRY.reset(tok); await outer.rollback()
    return r.status_code, d

async def snapshot():
    async with SessionLocal() as db:
        out = {}
        for t in (models.PaymentRequest, models.Message, models.Shipment, models.Supplier, models.AuditLog):
            out[t.__tablename__] = (await db.execute(select(func.count()).select_from(t))).scalar()
        sh = (await db.execute(select(models.Shipment))).scalars().first()
        out["收货人"] = sh.receiver_name if sh else None
        return out

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)
        fl = (await db.execute(select(models.Role).where(models.Role.code == "finance_lead"))).scalars().first()
        db.add(models.User(username="fl", full_name="财务主管", password_hash="x", role_id=fl.id))
        p = models.Project(code="2026-999", name="演练台", status="进行中"); db.add(p); await db.flush()
        db.add(models.Shipment(project_id=p.id))
        await db.commit(); sid = (await db.execute(select(models.Shipment.id))).scalar()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        tk = (await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})).json()["access_token"]
        H = {"Authorization": f"Bearer {tk}"}
        sup = (await c.post("/api/purchase-mgmt/suppliers", headers=H, json={"name": "无锡俊帆金属"})).json()["id"]
        it = (await c.post("/api/purchase-mgmt/items", headers=H, json={"supplier_id": sup, "item_name": "钢板", "qty": 2, "unit_price": 500})).json()["id"]
        cases = [
            ("采购·提交请款", "POST", "/api/purchase-mgmt/payment-requests",
             {"supplier_id": sup, "requested_amount": 1000, "items": [{"item_id": it, "allocated_amount": 1000}]}),
            ("物流·填收货人", "PUT", f"/api/logistics/{sid}/receiver",
             {"name": "王建国", "company": "苏州恒达", "phone": "13900001111", "addr": "苏州工业园"}),
            ("采购·新建供应商", "POST", "/api/purchase-mgmt/suppliers", {"name": "常州新锐五金"}),
            ("采购·删除采购明细", "DELETE", f"/api/purchase-mgmt/items/{it}", None),
        ]
        for name, m, url, body in cases:
            before = await snapshot()
            code, d = await call(c, H, m, url, body, dry=True)
            after = await snapshot()
            print(f"\n【{name}】{m} {url}\n  演练 HTTP {code}；数据库{'没变 ✅' if before == after else '变了 ❌ ' + str((before, after))}")
            print(f"  预览改动: {d['diff']}\n  截下的企微: {d['outbox']}")
        # 权限也照常生效：普通销售演练「提交请款」应被原接口的角色检查拒绝
        await c.post("/api/admin/users", headers=H, json={"username": "s1", "password": "pass123", "full_name": "销售", "role_ids": [
            next(x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json() if x["code"] == "sales")]})
        tk2 = (await c.post("/api/auth/login", json={"username": "s1", "password": "pass123"})).json().get("access_token")
        code, d = await call(c, {"Authorization": f"Bearer {tk2}"}, *cases[0][1:])
        print(f"\n【权限】销售演练「提交请款」→ HTTP {code}（原接口的角色检查照常拦截）")
        print("\n真实外发企微（应为 0）:", SENT)

asyncio.run(main())
