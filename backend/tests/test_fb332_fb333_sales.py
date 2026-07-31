"""反馈 #332 / #333（杨坛，销售主管兼管理层兼财务主管）

#333 销售主管可申请所有销售员的发票
  - 主管(sales+sales_lead)对**别人**的台账行 invoice-apply → 200 且直接进 pending_invoice
    （原来 `_is_sales(current)` 不排除主管，兼着 sales 角色的主管恒被拦；同函数里
      「管理层/销售主管提交直接进待开票」那段对他是死代码）
  - 同 bug 的上传合同端点一并放开
  - 普通销售员改不了别人的行 → 仍 403（隔离不破）

#332 尾款插入到款时间 → 尾款金额自动清零（用户 2026-08-01 选定「直接清零」口径）
  - 写尾款到账批注 → balance=0，合同额存进 balance_contract
  - 清零后 scan_balance_overdue 不再每周催办（它只认 balance>0）
  - 删批注 → balance 原样还原、balance_contract 清空（可逆）
  - 对照：预付批注不动任何金额
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb332")
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
from app.overdue import scan_balance_overdue
from app import models

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def _led(lid):
    async with SessionLocal() as db:
        return (await db.execute(select(models.SalesLedger)
                                 .where(models.SalesLedger.id == lid))).scalar_one()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)
    async with SessionLocal() as db:
        rid = {r.code: r.id for r in (await db.execute(select(models.Role))).scalars().all()}

    tr = ASGITransport(app=app)
    async with AsyncClient(transport=tr, base_url="http://test", timeout=60) as c:
        async def login(u, p):
            r = await c.post("/api/auth/login", json={"username": u, "password": p})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        H = await login("admin", "admin123")

        async def mkuser(name, codes):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        s1 = await mkuser("fb_s1", ["sales"])
        await mkuser("fb_s2", ["sales"])
        # 杨坛：销售员角色 + 销售主管（这正是触发 bug 的组合）
        await mkuser("fb_lead", ["sales", "sales_lead"])

        await c.post("/api/projects", headers=H, json={"code": "FB-332", "name": "尾款测试项目"})
        async with SessionLocal() as db:
            pid = (await db.execute(select(models.Project.id)
                                    .where(models.Project.code == "FB-332"))).scalar_one()
            db.add(models.SalesLedger(project_id=pid, sales_uid=s1, contract="有",
                                      amount=100000, tax_rate="13%",
                                      balance=50000, balance_date="2026-01-31"))
            await db.commit()
            lid = (await db.execute(select(models.SalesLedger.id)
                                    .where(models.SalesLedger.project_id == pid))).scalar_one()

        Hs1 = await login("fb_s1", "pass123")
        Hs2 = await login("fb_s2", "pass123")
        Hlead = await login("fb_lead", "pass123")

        # ==================== #333 ====================
        doc = {"file": ("开票申请表.docx", b"fake-docx", "application/octet-stream")}
        # 普通销售员动别人的行 → 仍 403
        r = await c.post(f"/api/sales/ledger/{lid}/invoice-apply", headers=Hs2, files=doc)
        chk(r.status_code == 403, f"他人开票申请应403: {r.status_code} {r.text[:120]}")

        # ★ 主管代销售员申请开票 → 200，直接进待开票
        r = await c.post(f"/api/sales/ledger/{lid}/invoice-apply", headers=Hlead, files=doc)
        chk(r.status_code == 200, f"★主管代申请开票: {r.status_code} {r.text[:200]}")
        if r.status_code == 200:
            chk("财务" in r.json().get("message", ""),
                f"★主管提交应直接同步财务: {r.json().get('message')}")
        led = await _led(lid)
        chk(led.invoice_state == "pending_invoice", f"★状态=待开票: {led.invoice_state}")

        # 主管代传合同（同一个漏配）
        async with SessionLocal() as db:
            l2 = (await db.execute(select(models.SalesLedger)
                                   .where(models.SalesLedger.id == lid))).scalar_one()
            l2.invoice_state = None
            await db.commit()
        r = await c.post(f"/api/sales/ledger/{lid}/contract", headers=Hlead,
                         data={"sign_date": "2026-01-05", "deliver_date": "2026-03-05"},
                         files={"file": ("合同.pdf", b"fake-pdf", "application/pdf")})
        chk(r.status_code == 200, f"★主管代传合同: {r.status_code} {r.text[:150]}")
        r = await c.post(f"/api/sales/ledger/{lid}/contract", headers=Hs2,
                         data={"sign_date": "2026-01-05", "deliver_date": "2026-03-05"},
                         files={"file": ("合同.pdf", b"fake-pdf", "application/pdf")})
        chk(r.status_code == 403, f"他人传合同应403: {r.status_code}")

        # ==================== #332 ====================
        # 清零前：逾期尾款催办会扫到它（balance=50000 且 balance_date 已过期）
        async with SessionLocal() as db:
            before = await scan_balance_overdue(db)
        chk(before.get("scanned", 0) >= 1, f"清零前应被催办扫到: {before}")

        # 插入到款时间 → 尾款清零、合同额留存
        r = await c.put(f"/api/sales/ledger/{lid}/payment-note", headers=Hs1,
                        json={"field": "balance", "note": "【2026-08-01 09:30】尾款到账"})
        chk(r.status_code == 200, f"尾款批注保存: {r.status_code} {r.text[:120]}")
        led = await _led(lid)
        chk(led.balance == 0, f"★插到款时间后尾款清零: {led.balance}")
        chk(led.balance_contract == 50000, f"★合同尾款额留存: {led.balance_contract}")
        chk(led.balance_note and "2026-08-01" in led.balance_note, f"批注已存: {led.balance_note}")

        # 清零后不再催办（幂等键会挡一次，所以直接验扫描命中数为 0）
        async with SessionLocal() as db:
            after = await scan_balance_overdue(db)
        chk(after.get("scanned", 0) == 0, f"★清零后不再扫到该行: {after}")

        # 序列化把 balance_contract 带出去（页面要显示"合同尾款 ¥X 已到账"）
        rows = (await c.get("/api/sales/ledger", headers=Hlead)).json()["rows"]
        row = next((x for x in rows if x["id"] == lid), None)
        chk(row is not None and row.get("balance_contract") == 50000,
            f"接口返回 balance_contract: {row and row.get('balance_contract')}")

        # 删批注 → 原样还原（把"不可逆"这条兜住）
        r = await c.put(f"/api/sales/ledger/{lid}/payment-note", headers=Hs1,
                        json={"field": "balance", "note": ""})
        chk(r.status_code == 200, f"删批注: {r.status_code} {r.text[:120]}")
        led = await _led(lid)
        chk(led.balance == 50000, f"★删批注后尾款还原: {led.balance}")
        chk(led.balance_contract is None, f"★还原后备份清空: {led.balance_contract}")
        chk(led.balance_note is None, f"批注已清: {led.balance_note}")

        # 对照：预付批注不动任何金额
        r = await c.put(f"/api/sales/ledger/{lid}/payment-note", headers=Hs1,
                        json={"field": "prepay", "note": "【8-01】预付到账"})
        led = await _led(lid)
        chk(led.balance == 50000 and led.balance_contract is None,
            f"预付批注不应动尾款: balance={led.balance} contract={led.balance_contract}")

    print("PASSED" if not FAIL else f"FAILED {len(FAIL)}")

asyncio.run(main())
