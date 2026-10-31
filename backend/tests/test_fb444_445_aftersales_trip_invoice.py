"""反馈#444 / #445（杨坛）售后、安装登记报销：

#444 出差时间：出发 / 结束必填，结束要晚于出发；存北京时间「YYYY-MM-DD HH:MM」，列表原样带回。
#445 费用清单：
  1. 一行可以传好几张发票；几行可以共用同一张发票（同一个附件 id 出现在几行）
  2. 没有发票的行必须附付款记录，否则拒；只有付款记录的行算「缺发票」但不算「缺凭证」
  3. 每行可填备注
  4. invoice_file_id 仍等于第一张发票（老客户端、按 invoice_file_id 数缺票的 SQL 照旧）
  5. 引用不存在的附件被拒
  6. 重传发票同样要守第 2 条
  7. 老客户端：只传单张 invoice_file_id 仍能用；不传出差时间被拒且提示更新客户端
"""
import asyncio, io, json, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="fb444")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, func
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models

FAIL = []
def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c: FAIL.append(m)

def _f(name="x.pdf"):
    return {"file": (name, io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")}

TRIP = {"trip_start": "2026-10-28 07:30", "trip_end": "2026-10-30 19:00"}


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
        for u, rc in (("asw", "as_worker"), ("asl", "as_lead"), ("fin", "finance")):
            r = await c.post("/api/admin/users", headers=H, json={"username": u, "password": "pass123", "full_name": u, "role_id": rid[rc]})
            assert r.status_code == 200, r.text
        async def login(u):
            return {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': u, 'password': 'pass123'})).json()['access_token']}"}
        Hw, Hl, Hf = await login("asw"), await login("asl"), await login("fin")
        pid = (await c.post("/api/projects", headers=H, json={"code": "2026-T444", "name": "出差测试机"})).json()["id"]

        async def up(biz, name):
            r = await c.post("/api/attachments", headers=Hw, data={"biz_type": biz}, files=_f(name))
            assert r.status_code == 200, r.text
            return r.json()["id"]
        inv1, inv2, inv_shared = await up("aftersales_invoice", "车票1.pdf"), await up("aftersales_invoice", "车票2.pdf"), await up("aftersales_invoice", "增值税票.pdf")
        pp = await up("aftersales_payproof", "微信支付截图.png")

        async def reg(items, extra=None, problem="现场更换机封"):
            data = {"project_id": pid, "problem": problem, "items": json.dumps(items, ensure_ascii=False)}
            data.update(TRIP if extra is None else extra)
            return await c.post("/api/aftersales", headers=Hw, data=data, files=_f("物料清单.pdf"))

        ok_items = [
            {"name": "差旅", "amount": 360, "invoice_file_ids": [inv1, inv2], "note": "往返高铁"},
            {"name": "配件A", "amount": 500, "invoice_file_ids": [inv_shared]},
            {"name": "配件B", "amount": 300, "invoice_file_ids": [inv_shared], "note": "和配件A同一张票"},
            {"name": "打车", "amount": 46, "pay_proof_file_ids": [pp], "note": "司机开不了票"},
        ]

        # ===== #444 出差时间 =====
        r = await reg(ok_items, extra={})
        chk(r.status_code == 400 and "出差" in r.text and "更新客户端" in r.text, f"不传出差时间被拒并提示更新客户端: {r.status_code} {r.text[:80]}")
        r = await reg(ok_items, extra={"trip_start": "2026-10-30 19:00", "trip_end": "2026-10-28 07:30"})
        chk(r.status_code == 400 and "晚于" in r.text, f"结束早于出发被拒: {r.text[:80]}")
        r = await reg(ok_items, extra={"trip_start": "2026-10-30 19:00", "trip_end": "2026-10-30 19:00"})
        chk(r.status_code == 400, f"结束等于出发被拒: {r.status_code}")
        r = await reg(ok_items, extra={"trip_start": "明天早上", "trip_end": "2026-10-30 19:00"})
        chk(r.status_code == 400 and "格式" in r.text, f"格式不对被拒: {r.text[:60]}")

        # ===== #445 没票没付款记录被拒 =====
        bad = ok_items[:3] + [{"name": "打车", "amount": 46}]
        r = await reg(bad)
        chk(r.status_code == 400 and "第 4 行" in r.text and "付款记录" in r.text, f"没票也没付款记录被拒，指到第 4 行: {r.text[:90]}")
        r = await reg([{"name": "配件", "amount": 10, "invoice_file_ids": [999999]}])
        chk(r.status_code == 400 and "找不到" in r.text, f"引用不存在的附件被拒: {r.text[:60]}")

        # ===== 正常登记 =====
        r = await reg(ok_items)
        chk(r.status_code == 200, f"正常登记: {r.status_code} {r.text[:120]}")
        row = (await c.get("/api/aftersales", headers=Hw)).json()["rows"][0]
        aid = row["id"]
        chk(row["trip_start"] == "2026-10-28 07:30" and row["trip_end"] == "2026-10-30 19:00", f"出差时间原样带回: {row['trip_start']} ~ {row['trip_end']}")
        its = row["items"]
        chk(abs(row["cost"] - 1206) < 0.01, f"合计 360+500+300+46=1206: {row['cost']}")
        chk([f["id"] for f in its[0]["invoice_files"]] == [inv1, inv2], f"一行两张发票: {its[0]['invoice_files']}")
        chk(its[0]["invoice_file_id"] == inv1 and its[0]["invoice_file_name"] == "车票1.pdf", "invoice_file_id = 第一张（兼容老客户端）")
        chk([f["id"] for f in its[1]["invoice_files"]] == [inv_shared] == [f["id"] for f in its[2]["invoice_files"]], "两行共用一张发票")
        chk(its[3]["invoice_files"] == [] and [f["id"] for f in its[3]["pay_proof_files"]] == [pp], "没票那行带付款记录")
        chk(its[3]["pay_proof_files"][0]["name"] == "微信支付截图.png", "付款记录带文件名")
        chk(its[0]["note"] == "往返高铁" and its[2]["note"] == "和配件A同一张票" and its[1]["note"] is None, "备注逐行保存")
        chk(row["missing_invoice"] == 1 and row["missing_proof"] == 0, f"缺发票 1 行、缺凭证 0 行: {row['missing_invoice']} / {row['missing_proof']}")
        async with SessionLocal() as db:
            n_null = (await db.execute(select(func.count(models.AfterSalesItem.id)).where(
                models.AfterSalesItem.aftersales_id == aid, models.AfterSalesItem.invoice_file_id.is_(None)))).scalar()
        chk(n_null == 1, f"按 invoice_file_id IS NULL 数缺票的老 SQL 仍然对: {n_null}")

        # ===== 老客户端只传单张 invoice_file_id =====
        r = await reg([{"name": "配件", "amount": 88, "invoice_file_id": inv1}], problem="老客户端")
        chk(r.status_code == 200, f"老客户端单张发票仍能登记: {r.status_code} {r.text[:80]}")
        old = [x for x in (await c.get("/api/aftersales", headers=Hw)).json()["rows"] if x["problem"] == "老客户端"][0]
        chk([f["id"] for f in old["items"][0]["invoice_files"]] == [inv1], "单张发票进了 invoice_files")

        # ===== 存量行（只有 invoice_file_id、没有新列）读出来照样有发票 =====
        async with SessionLocal() as db:
            it = (await db.execute(select(models.AfterSalesItem).where(models.AfterSalesItem.aftersales_id == old["id"]))).scalar_one()
            it.invoice_file_ids = None; it.pay_proof_file_ids = None
            await db.commit()
        old = [x for x in (await c.get("/api/aftersales", headers=Hw)).json()["rows"] if x["problem"] == "老客户端"][0]
        chk([f["id"] for f in old["items"][0]["invoice_files"]] == [inv1] and old["missing_invoice"] == 0, "存量行退回 invoice_file_id，不算缺票")

        # ===== 审批 → 财务退回 → 重传也守规矩 =====
        chk((await c.post(f"/api/aftersales/{aid}/approve", headers=Hl)).status_code == 200, "主管审批")
        msgs = (await c.get("/api/messages", headers=Hf)).json()
        chk(any("附的是付款记录" in m["text"] for m in msgs), "推给财务的消息说明没票那行附的是付款记录")
        chk((await c.post(f"/api/aftersales/{aid}/pay-reject", headers=Hf, data={"reason": "打车那行再补张票"})).status_code == 200, "财务退回")
        r = await c.post(f"/api/aftersales/{aid}/resubmit-invoice", headers=Hw, data={"items": json.dumps(bad, ensure_ascii=False)})
        chk(r.status_code == 400 and "付款记录" in r.text, f"重传时没票没付款记录同样被拒: {r.text[:60]}")
        fixed = ok_items[:3] + [{"name": "打车", "amount": 46, "invoice_file_ids": [inv2], "pay_proof_file_ids": [pp]}]
        r = await c.post(f"/api/aftersales/{aid}/resubmit-invoice", headers=Hw, data={"items": json.dumps(fixed, ensure_ascii=False)})
        chk(r.status_code == 200, f"补票后重传: {r.status_code} {r.text[:80]}")
        row = [x for x in (await c.get("/api/aftersales", headers=Hf)).json()["rows"] if x["id"] == aid][0]
        chk(row["missing_invoice"] == 0 and row["trip_start"] == "2026-10-28 07:30", "重传后发票齐了，出差时间不受影响")
        fin = [x for x in (await c.get("/api/finance/aftersales", headers=Hf)).json()["rows"] if x["id"] == aid][0]
        chk(fin["trip_end"] == "2026-10-30 19:00" and len(fin["items"][0]["invoice_files"]) == 2, "财务部那张表也带出差时间和多张发票")

    print("\n" + ("全部通过" if not FAIL else f"失败 {len(FAIL)} 项"))
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
