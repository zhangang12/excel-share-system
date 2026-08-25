"""合并开票拦截时要**点名是哪几项**（反馈#407 李新新，2026-08-22）。

原话：「这个怎么不能整单维护，都已经签收，已经填写价格，核对过」
她勾了同一供应商 22 条明细点「维护开票号」，被这条挡住：
    「所选含 2 项尚未收货（收货金额为0），请先全部收货后再合并开票」

查生产：那 2 项（#1038/#1039 脚轮）**到货日期确实填了**，缺的是收货金额——连单价都是空的。
所以她没说错，是提示在胡说。两个毛病缺一不可修：
  ① 「尚未收货」——她签收过，看到这四个字根本不会想到是金额没填；
  ② 只报个数不报是哪几项——22 条跨 6 张采购单、要翻屏，找不出来。

生产体量：「到货了但金额为0」156 条（其中 155 条连单价都没有）、「压根没到货」299 条。
两种该做的事完全不同（补金额 vs 去收货），所以必须分开说。

本文件锁后端这条 400 的内容（前端同口径，但前端拦在先，后端是 H5/旧客户端绕过时的兜底）。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb407")
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        H = {"Authorization": "Bearer " + (await c.post("/api/auth/login", json={
            "username": "admin", "password": "admin123"})).json()["access_token"]}
        sid = (await c.post("/api/purchase-mgmt/suppliers", headers=H,
                            json={"name": "无锡市聚创五金机电有限公司"})).json()["id"]

        async def mk(name, spec, proj, arrival=None, recv=None, price=None):
            body = {"supplier_id": sid, "item_name": name, "spec": spec, "qty": 4,
                    "project_code": proj}
            if price is not None:
                body["unit_price"] = price
            if arrival:
                body["arrival_date"] = arrival
            if recv is not None:
                body["received_amount"] = recv
            r = await c.post("/api/purchase-mgmt/items", headers=H, json=body)
            assert r.status_code == 200, r.text
            return r.json()["id"]

        ok1 = await mk("福马轮", "120F", "2026-077", "2026-08-17", 480, 120)
        ok2 = await mk("日式低重心", None, "2026-077", "2026-08-17", 200, 50)
        # 复刻线上那两条：到货日期有、单价和收货金额都没有。
        # ⚠️ PurchaseItemCreate 里**没有 arrival_date 字段**（到货是收货流程写的），
        #   建单时传了会被静默丢掉——直接改库，别以为传了就生效。
        bad1 = await mk("脚轮", "2.5寸低重心重型脚轮", "2026-068", None, None, None)
        bad2 = await mk("脚轮", "2.5寸低重心重型脚轮", "2026-053", None, None, None)
        async with SessionLocal() as db:
            from sqlalchemy import update as _upd
            await db.execute(_upd(models.PurchaseItem)
                             .where(models.PurchaseItem.id.in_([bad1, bad2]))
                             .values(arrival_date="2026-08-01"))
            await db.commit()
        # 再来一条真·没收货的
        nore = await mk("地脚杯", "M20x120", "2026-072", None, None, None)

        async def try_invoice(ids):
            # ⚠️ 是 POST /items/set-invoice-no，字段叫 item_ids（不是 ids）；
            #   写成 /items/invoice-no 会被 /items/{iid} 抢先匹配，报「iid 必须是整数」
            return await c.post("/api/purchase-mgmt/items/set-invoice-no", headers=H, json={
                "item_ids": ids, "invoice_no": "INV-001", "invoice_date": "2026-08-22"})

        # ① 只含「到货了但没金额」的那两条
        r = await try_invoice([ok1, ok2, bad1, bad2])
        chk(r.status_code == 400, f"① 仍然拦住（发票总额必须=Σ收货金额，0 的行算不进去）: {r.status_code}")
        d = r.json().get("detail", "")
        chk("尚未收货" not in d,
            f"① **不再说「尚未收货」**——她签收过，这四个字让她以为系统在胡说: {d[:120]}")
        chk("已到货但没填收货金额" in d,
            f"① 说清真正缺的是收货金额: {d[:120]}")
        chk("脚轮" in d and "2.5寸低重心重型脚轮" in d,
            f"① **点名到具体零件**（她勾 22 条跨 6 张单，只报个数根本找不出来）: {d[:160]}")
        chk("2026-068" in d and "2026-053" in d,
            f"① 带上项目编号，能直接定位到行: {d[:160]}")
        chk("福马轮" not in d and "日式低重心" not in d,
            f"① 不牵连没问题的行: {d[:160]}")

        # ② 只含「真·没收货」的
        r = await try_invoice([ok1, nore])
        d = r.json().get("detail", "")
        chk(r.status_code == 400 and "还没收货" in d and "地脚杯" in d,
            f"② 真没收货的说「还没收货」并点名: {r.status_code} {d[:120]}")
        chk("采购收货" in d, f"② 并告诉她去哪收货: {d[:120]}")

        # ③ 两种混在一起要分开说，别糊成一句
        r = await try_invoice([ok1, bad1, nore])
        d = r.json().get("detail", "")
        chk("已到货但没填收货金额" in d and "还没收货" in d,
            f"③ 两种情况分开说（一个去补金额、一个去收货，动作完全不同）: {d[:200]}")

        # ④ 都合规时照常放行 —— 别为了改文案把功能拦死
        r = await try_invoice([ok1, ok2])
        chk(r.status_code == 200, f"④ 金额齐全的正常开票: {r.status_code} {r.text[:100]}")
        items = (await c.get("/api/purchase-mgmt/items", headers=H)).json()
        rows = items if isinstance(items, list) else items.get("rows", [])
        got = [x for x in rows if x["id"] in (ok1, ok2)]
        chk(all(x.get("invoice_no") == "INV-001" for x in got),
            f"④ 开票号真的写进去了: {[(x['id'], x.get('invoice_no')) for x in got]}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
