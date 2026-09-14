"""反馈#426（李新新，2026-09-14）：「如果一个公司有两个收款账号的怎么录入公司信息」——供应商多个收款账号。

口径（老板选方案 1）：
  · 供应商可以有多组「开户行 + 账号 + 备注」，其中一个默认；Supplier.bank_name/bank_account 永远 = 默认账号镜像
  · 老客户端（只传一对 bank_name/bank_account）照常能用：维护的是默认账号，其它账号不动
  · 请款时指定打哪个账号（不指定=默认）；财务付款弹窗只显示本单的账号，并告诉出纳「共 N 个」
  · 付款那一刻快照账号：事后账号被改，已付款记录仍显示当时打的账号
  · 在途请款单指定的账号不能删；驳回重提可以换账号
  · 改收款账号照旧写 update_supplier_bank 审计（请款审批卡据此提示「N 天前改过」）
  · 存量：列上有账号的供应商迁成一条默认账号，迁移幂等
"""
import asyncio, io, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb426")
os.environ["DATABASE_URL"] = os.environ.get("AUDIT_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, func
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, backfill_supplier_bank_accounts
from app import models

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def _audits(sid):
    async with SessionLocal() as db:
        return (await db.execute(select(func.count(models.AuditLog.id)).where(
            models.AuditLog.action == "update_supplier_bank", models.AuditLog.target_id == sid))).scalar()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(u, p="pass123"):
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

        await mkuser("ba_buyer", ["buyer"])
        await mkuser("ba_lead", ["finance", "finance_lead"])
        await mkuser("ba_cash", ["finance"])
        Hb, Hl, Hc = await login("ba_buyer"), await login("ba_lead"), await login("ba_cash")

        def accts_of(sup_json):
            return [(a["bank_name"], a["bank_account"], a["is_default"], a.get("notes")) for a in sup_json["bank_accounts"]]

        async def sup(sid):
            rows = (await c.get("/api/purchase-mgmt/suppliers", headers=Hb)).json()
            return next(x for x in rows if x["id"] == sid)

        # ══════════════ ① 老客户端建档 ══════════════
        print("\n① 老客户端（只传一对账号）")
        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb, json={
            "name": "老客户端供应商", "bank_name": "工行常州支行", "bank_account": "6222 0000 1111"})
        chk(r.status_code == 200, f"老格式建档 -> {r.status_code}")
        s_old = r.json()
        chk(accts_of(s_old) == [("工行常州支行", "622200001111", True, None)],
            f"自动建成一条默认账号（去空格）-> {accts_of(s_old)}")

        # ══════════════ ② 新客户端：多个账号 ══════════════
        print("\n② 多个收款账号")
        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb, json={
            "name": "常州市泰明电子电器有限公司", "bank_accounts": [
                {"bank_name": "农行常州分行", "bank_account": "111", "notes": "开票户"},
                {"bank_name": "建行常州分行", "bank_account": "222", "is_default": True, "notes": "货款户"}]})
        chk(r.status_code == 200, f"两个账号建档 -> {r.status_code} {r.text[:80]}")
        s = r.json()
        sid = s["id"]
        chk(accts_of(s)[0] == ("建行常州分行", "222", True, "货款户") and len(accts_of(s)) == 2,
            f"默认账号排第一 -> {accts_of(s)}")
        chk(s["bank_name"] == "建行常州分行" and s["bank_account"] == "222", "供应商 bank_* 两列 = 默认账号镜像")
        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb, json={
            "name": "重复账号", "bank_accounts": [{"bank_account": "333"}, {"bank_account": "3 33"}]})
        chk(r.status_code == 400 and "两次" in r.text, f"同一账号填两次被拒 -> {r.status_code}")
        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb, json={
            "name": "两个默认", "bank_accounts": [{"bank_account": "444", "is_default": True},
                                             {"bank_account": "555", "is_default": True}]})
        chk(r.status_code == 400 and "默认" in r.text, f"两个默认被拒 -> {r.status_code}")
        r = await c.post("/api/purchase-mgmt/suppliers", headers=Hb, json={
            "name": "都没勾默认", "bank_accounts": [{"bank_account": "666"}, {"bank_account": "777"}]})
        chk(r.status_code == 200 and accts_of(r.json())[0][1:3] == ("666", True), "都没勾时第一个为默认")

        # ══════════════ ③ 编辑：整表保存 ══════════════
        print("\n③ 编辑账号")
        a_nong = next(a for a in s["bank_accounts"] if a["bank_account"] == "111")
        a_jian = next(a for a in s["bank_accounts"] if a["bank_account"] == "222")
        n0 = await _audits(sid)
        r = await c.put(f"/api/purchase-mgmt/suppliers/{sid}", headers=Hb, json={"bank_accounts": [
            {"id": a_nong["id"], "bank_name": "农行常州分行", "bank_account": "111", "is_default": True, "notes": "开票户"},
            {"id": a_jian["id"], "bank_name": "建行常州分行", "bank_account": "222", "notes": "货款户"},
            {"bank_name": "招行", "bank_account": "888"}]})
        s = r.json()
        chk(r.status_code == 200 and len(s["bank_accounts"]) == 3 and s["bank_account"] == "111",
            f"加一个、改默认为农行 → 镜像跟着变 -> {r.status_code} {s.get('bank_account')}")
        chk(await _audits(sid) == n0 + 1, "改收款账号写了 update_supplier_bank 审计")
        r = await c.put(f"/api/purchase-mgmt/suppliers/{sid}", headers=Hb, json={"contact": "张三"})
        chk(r.status_code == 200 and len(r.json()["bank_accounts"]) == 3 and await _audits(sid) == n0 + 1,
            "只改联系人：账号不动、不写账号审计")
        a_zhao = next(a for a in s["bank_accounts"] if a["bank_account"] == "888")
        other = (await c.post("/api/purchase-mgmt/suppliers", headers=Hb, json={
            "name": "别家", "bank_accounts": [{"bank_account": "999"}]})).json()
        r = await c.put(f"/api/purchase-mgmt/suppliers/{sid}", headers=Hb, json={"bank_accounts": [
            {"id": other["bank_accounts"][0]["id"], "bank_account": "999"}]})
        chk(r.status_code == 400, f"拿别家的账号 id 来改被拒 -> {r.status_code}")

        # 老客户端编辑：只动默认账号
        r = await c.put(f"/api/purchase-mgmt/suppliers/{sid}", headers=Hb, json={
            "name": "常州市泰明电子电器有限公司", "bank_name": "农行常州分行", "bank_account": "111"})
        chk(r.status_code == 200 and len(r.json()["bank_accounts"]) == 3 and await _audits(sid) == n0 + 1,
            "老客户端原样提交默认账号：什么都不变、不写审计")
        r = await c.put(f"/api/purchase-mgmt/suppliers/{sid}", headers=Hb, json={
            "bank_name": "农行常州分行", "bank_account": "111-new"})
        s = r.json()
        accts = {a["bank_account"]: a for a in s["bank_accounts"]}
        chk(r.status_code == 200 and "111-new" in accts and accts["111-new"]["is_default"]
            and "222" in accts and "888" in accts and "111" not in accts,
            f"老客户端改账号 = 改默认账号，其它两个保留 -> {list(accts)}")
        chk(accts["111-new"]["id"] == a_nong["id"], "是在原默认账号上改，不是删了重建（在途请款单的指定不丢）")

        # ══════════════ ④ 请款指定账号 ══════════════
        print("\n④ 请款单指定账号")
        async def new_item():
            r = await c.post("/api/purchase-mgmt/orders", headers=Hb, json={
                "supplier_id": sid, "lines": [{"item_name": "继电器", "qty": 1, "unit_price": 100}]})
            return r.json()[0]["id"]

        async def new_pr(bank_account_id=None):
            iid = await new_item()
            body = {"supplier_id": sid, "requested_amount": 100, "items": [{"item_id": iid, "allocated_amount": 100}]}
            if bank_account_id is not None:
                body["bank_account_id"] = bank_account_id
            return await c.post("/api/purchase-mgmt/payment-requests", headers=Hb, json=body)

        r = await new_pr(other["bank_accounts"][0]["id"])
        chk(r.status_code == 400 and "不属于" in r.text, f"指定别家账号被拒 -> {r.status_code}")
        r = await new_pr()
        p_def = r.json()
        chk(r.status_code == 200 and p_def["supplier_bank_account"] == "111-new" and p_def["bank_account_is_default"]
            and p_def["supplier_account_count"] == 3, f"不指定 → 默认账号，共 3 个 -> {p_def.get('supplier_bank_account')}")
        r = await new_pr(a_jian["id"])
        p = r.json()
        chk(r.status_code == 200 and p["supplier_bank_account"] == "222" and p["bank_account_note"] == "货款户"
            and p["bank_account_is_default"] is False, f"指定建行货款户 → 请款单显示这个账号 -> {p.get('supplier_bank_account')}")
        fin = (await c.get("/api/finance/payment-requests", headers=Hc, params={"status": "all"})).json()
        frow = next(x for x in (fin if isinstance(fin, list) else fin.get("rows", [])) if x["id"] == p["id"])
        chk(frow["supplier_bank_account"] == "222" and frow["supplier_account_count"] == 3,
            "财务付款列表看到的也是本单指定的账号")

        # 在途单引用的账号不能删
        cur = (await sup(sid))["bank_accounts"]
        r = await c.put(f"/api/purchase-mgmt/suppliers/{sid}", headers=Hb, json={"bank_accounts": [
            {"id": a["id"], "bank_name": a["bank_name"], "bank_account": a["bank_account"],
             "is_default": a["is_default"], "notes": a["notes"]} for a in cur if a["bank_account"] != "222"]})
        chk(r.status_code == 400 and "在途请款单" in r.text, f"删在途单指定的账号被拒 -> {r.status_code}")
        r = await c.put(f"/api/purchase-mgmt/suppliers/{sid}", headers=Hb, json={"bank_accounts": [
            {"id": a["id"], "bank_name": a["bank_name"], "bank_account": a["bank_account"],
             "is_default": a["is_default"], "notes": a["notes"]} for a in cur if a["bank_account"] != "888"]})
        chk(r.status_code == 200 and len(r.json()["bank_accounts"]) == 2, "没被引用的账号可以删")

        # ══════════════ ⑤ 付款快照 ══════════════
        print("\n⑤ 付款快照")
        await c.put(f"/api/purchase-mgmt/payment-requests/{p['id']}/approve", headers=Hl)
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{p['id']}/pay", headers=Hc,
                        data={"paid_amount": 100, "paid_date": "2026-09-14"})
        chk(r.status_code == 200, f"出纳付款 -> {r.status_code} {r.text[:80]}")
        async with SessionLocal() as db:
            pr = (await db.execute(select(models.PaymentRequest).where(models.PaymentRequest.id == p["id"]))).scalar_one()
        chk(pr.paid_bank_account == "222" and pr.paid_bank_name == "建行常州分行", f"付款时快照了实际账号 -> {pr.paid_bank_account}")
        cur = (await sup(sid))["bank_accounts"]
        await c.put(f"/api/purchase-mgmt/suppliers/{sid}", headers=Hb, json={"bank_accounts": [
            {"id": a["id"], "bank_name": a["bank_name"], "bank_account": ("222-changed" if a["bank_account"] == "222" else a["bank_account"]),
             "is_default": a["is_default"], "notes": a["notes"]} for a in cur]})
        rows = (await c.get("/api/purchase-mgmt/payment-requests", headers=Hb)).json()
        prow = next(x for x in rows if x["id"] == p["id"])
        chk(prow["supplier_bank_account"] == "222", f"事后改了账号，已付款记录仍显示当时打的 222 -> {prow['supplier_bank_account']}")

        # ══════════════ ⑥ 驳回重提换账号 ══════════════
        print("\n⑥ 驳回重提换账号")
        pid2 = p_def["id"]
        await c.put(f"/api/purchase-mgmt/payment-requests/{pid2}/approve", headers=Hl)
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{pid2}/pay-reject", headers=Hc, json={"reason": "应打货款户"})
        chk(r.status_code == 200, f"出纳以账户不对退回 -> {r.status_code}")
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{pid2}/resubmit", headers=Hb,
                        json={"bank_account_id": other["bank_accounts"][0]["id"]})
        chk(r.status_code == 400, f"重提换成别家账号被拒 -> {r.status_code}")
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{pid2}/resubmit", headers=Hb,
                        json={"bank_account_id": a_jian["id"]})
        rows = (await c.get("/api/purchase-mgmt/payment-requests", headers=Hb)).json()
        prow = next(x for x in rows if x["id"] == pid2)
        chk(r.status_code == 200 and prow["status"] == "pending" and prow["supplier_bank_account"] == "222-changed",
            f"重提时换成货款户 → 待审批、显示新账号 -> {r.status_code} {prow.get('supplier_bank_account')}")
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{pid2}/reject", headers=Hl, json={"reason": "再退"})
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{pid2}/resubmit", headers=Hb)
        chk(r.status_code == 200, f"老客户端不带 body 重提照常可用 -> {r.status_code}")

        # ══════════════ ⑦ 存量迁移 / 导入 / 删除 ══════════════
        print("\n⑦ 存量迁移、导入、删除")
        async with SessionLocal() as db:
            legacy = models.Supplier(name="存量老供应商", status="active", bank_name="交行", bank_account="7777")
            db.add(legacy)
            await db.commit()
            lid = legacy.id
            n1 = await backfill_supplier_bank_accounts(db)
            n2 = await backfill_supplier_bank_accounts(db)
            cnt = (await db.execute(select(func.count(models.SupplierBankAccount.id)).where(
                models.SupplierBankAccount.supplier_id == lid))).scalar()
        chk(n1 >= 1 and n2 == 0 and cnt == 1, f"存量迁移成一条默认账号，再跑不重复 -> {n1}/{n2}/{cnt}")

        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active
        ws.append(["供应商名称", "开户行", "银行账号"])
        ws.append(["导入来的供应商", "中行", "5555"])
        bio = io.BytesIO(); wb.save(bio); bio.seek(0)
        r = await c.post("/api/purchase-mgmt/suppliers/import", headers=Hb,
                         files={"file": ("s.xlsx", bio, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        chk(r.status_code == 200, f"Excel 导入供应商 -> {r.status_code} {r.text[:80]}")
        imp = next((x for x in (await c.get("/api/purchase-mgmt/suppliers", headers=Hb)).json() if x["name"] == "导入来的供应商"), None)
        chk(imp and accts_of(imp) == [("中行", "5555", True, None)], f"导入的账号建成默认账号 -> {imp and accts_of(imp)}")

        r = await c.delete(f"/api/purchase-mgmt/suppliers/{imp['id']}", headers=Hb)
        async with SessionLocal() as db:
            left = (await db.execute(select(func.count(models.SupplierBankAccount.id)).where(
                models.SupplierBankAccount.supplier_id == imp["id"]))).scalar()
        chk(r.status_code == 200 and left == 0, f"删供应商连带删账号 -> {r.status_code} 剩 {left}")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
