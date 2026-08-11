"""反馈#367（赵仁辉）：强制发货放给物流负责人。

原来只有管理层能在 D5 闸门未通过时强制发货。物流部就一个人（王芹），
每次都得找管理层，实际上是把发货卡在了流程外面。

⚠️ 这次放开的实际后果：本接口 require_roles("logistics")，能调它的本来就只有
   物流 + 管理层，所以 force 放给 logistics 之后 **D5 就不再拦任何人了**，
   它从「硬闸」变成「提醒 + 留痕」。是有意为之，但留痕必须真的留下来——
   否则等于把闸门静悄悄关掉、还没人知道。

要锁死的：
  1. 闸门没过时，物流也能强制发出去（这是这次要的）
  2. 闸门没过、又**没勾强制**时仍然要拦——不能变成随便点点就发了
  3. 强制发货必须**通知管理层**：没人看得见的旁路等于没有旁路
  4. 审计要写清**缺了哪几道工序**，只记一个 FORCE 事后查不出漏了什么
  5. 闸门正常通过时不算强制：不推警告、审计里不留 FORCE（否则天天误报，很快没人看）
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb367")
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


async def audits(action="ship"):
    async with SessionLocal() as db:
        r = await db.execute(select(models.AuditLog).where(
            models.AuditLog.action == action).order_by(models.AuditLog.id))
        return [a.detail or "" for a in r.scalars().all()]


async def warn_msgs():
    async with SessionLocal() as db:
        r = await db.execute(select(models.Message).where(
            models.Message.biz_type == "shipment"))
        return [m.text for m in r.scalars().all() if "强制发货" in (m.text or "")]


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
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        await c.post("/api/admin/users", headers=H, json={
            "username": "wq", "password": "pass123", "full_name": "王芹", "role_id": rid["logistics"]})
        r = await c.post("/api/auth/login", json={"username": "wq", "password": "pass123"})
        Hl = {"Authorization": f"Bearer {r.json()['access_token']}"}

        async def new_shipment(code, with_open_task=True):
            """建项目 + 发货单。
            with_open_task=True  → 留一个「进行中」的设计任务，闸门不通过
            with_open_task=False → 放一个**已完成**的任务，闸门通过
              ⚠️ 不能一个任务都不放：`_gate` 里 active 为空时返回「未下任何任务单」，
                 照样不通过（这是给存量零任务单项目留的口子），那样就测不到"正常发货"。
            """
            p = (await c.post("/api/projects", headers=H,
                              json={"code": code, "name": f"项目{code}"})).json()
            async with SessionLocal() as db:
                db.add(models.DeptOrder(project_id=p["id"], dept="design",
                                        status="进行中" if with_open_task else "done"))
                sp = (await db.execute(select(models.Shipment).where(
                    models.Shipment.project_id == p["id"]))).scalars().first()
                if not sp:
                    sp = models.Shipment(project_id=p["id"], status="pending")
                    db.add(sp)
                await db.commit()
                await db.refresh(sp)
                return p, sp.id

        def doc():
            return {"file": ("发货单.pdf", b"%PDF-1.4 x", "application/pdf")}

        # ---- 2) 闸门没过 + 没勾强制 → 仍然要拦 ----
        _, sid = await new_shipment("2026-701")
        r = await c.post(f"/api/logistics/{sid}/ship", headers=Hl,
                         files=doc(), data={"force": "false"})
        chk(r.status_code == 400 and "闸门未通过" in r.text,
            f"2) 没勾强制时照样拦住: {r.status_code} {r.text[:80]}")

        # ---- 1) 物流勾了强制 → 能发出去（这次要的）----
        r = await c.post(f"/api/logistics/{sid}/ship", headers=Hl,
                         files=doc(), data={"force": "true"})
        chk(r.status_code == 200, f"1) 物流负责人能强制发货: {r.status_code} {r.text[:100]}")

        # ---- 3) 必须通知管理层 ----
        w = await warn_msgs()
        chk(len(w) >= 1, f"3) 强制发货推了管理层告警: {len(w)} 条")
        chk(any("王芹" in x for x in w), f"3) 告警里写明是谁发的: {w[:1]}")
        chk(any("设计" in x for x in w), f"3) 告警里写明缺了什么: {w[:1]}")

        # ---- 4) 审计要写清缺了哪几道工序 ----
        a = await audits()
        chk(any("FORCE" in x for x in a), f"4) 审计标了 FORCE: {a}")
        chk(any("FORCE" in x and "设计" in x for x in a),
            f"4) 审计里写了缺哪道工序（光一个 FORCE 事后查不出漏了什么）: {a}")

        # ---- 5) 闸门正常通过的不算强制：不推告警、审计不留 FORCE ----
        w_before = len(await warn_msgs())
        _, sid2 = await new_shipment("2026-702", with_open_task=False)
        r = await c.post(f"/api/logistics/{sid2}/ship", headers=Hl,
                         files=doc(), data={"force": "true"})   # 前端总是带 force，闸门过了就该忽略它
        chk(r.status_code == 200, f"闸门通过时正常发货: {r.status_code} {r.text[:80]}")
        chk(len(await warn_msgs()) == w_before,
            "5) 闸门通过 → 不推强制告警（天天误报的话很快就没人看了）")
        a2 = [x for x in await audits() if "2026-702" in x]
        chk(a2 and "FORCE" not in a2[0], f"5) 闸门通过 → 审计不留 FORCE: {a2}")

        # 管理层自己也还能强制（原有能力不能因为这次改动丢掉）
        _, sid3 = await new_shipment("2026-703")
        r = await c.post(f"/api/logistics/{sid3}/ship", headers=H,
                         files=doc(), data={"force": "true"})
        chk(r.status_code == 200, f"管理层仍可强制发货: {r.status_code} {r.text[:80]}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
