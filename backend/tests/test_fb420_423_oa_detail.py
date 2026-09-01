"""OA 详情/付款相关的一批反馈（#420~#423，2026-09-01 王芹/杨倩）。

本文件锁**后端**这几件事：
  ① 标记已付款要写审计（查 #420 时发现：线上 54 张已付款单谁点的完全无从查证，
     OaRequest 没有 paid_by、端点也不写 write_audit）——内控要能回溯。
  ② 详情接口必须返回 title（#423 前端要把标题显示在抽屉里；线上真实标题里写着
     「他家收款账号已变更，注意最新收款账号」，正要付款的出纳原来看不到）。
  ③ 详情接口必须原样返回 detail 里的 payee / payee_account / payee_bank
     （#422 前端要给这三个字段加复制按钮）；多行文本不能被后端吃掉换行（#423-b 靠 CSS 显示）。

#421（附件预览）纯前端复用已有组件，后端零改动，不在这里测。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb420")
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


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=60) as c:
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

        fin_id = await mkuser("oa_fin", ["finance"])
        Hf = await login("oa_fin", "pass123")

        # 造一张待付款的对公付款单。审批链因环境而异，直接改库把状态推到 pending_payment，
        # 本文件测的是 mark_paid 之后的事，不是审批链本身（那有 test_fb412_oa_paid.py 管）。
        TITLE = "6月份货款，他家收款账号已变更，注意最新收款账号"
        # 多行事由：线上真实形态（开票名称/开户行/账号/行号/税号各一行）
        REASON = "开票名称：苏州市贝得机电有限公司\n开户行：建行苏州市环秀支行\n银行账户：32201989039051501113"
        dept = (await c.get("/api/oa/departments", headers=H)).json()
        dept_id = dept[0]["id"] if dept else None
        async with SessionLocal() as db:
            req = models.OaRequest(
                request_no="OA20260901-001", category="purchase", doc_type="payment_public",
                title=TITLE, department_id=dept_id, requester_id=fin_id,
                amount=850, status="pending_payment",
                detail={"payee": "苏州市贝得机电有限公司", "reason": REASON,
                        "payee_bank": "建行苏州市环秀支行",
                        "payee_account": "32201989039051501113",
                        "expect_pay_date": "2026-08-29"})
            db.add(req)
            await db.flush()
            oid = req.id
            await db.commit()

        # ── ② 详情返回 title（#423 的数据前提）──
        r = await c.get(f"/api/oa/requests/{oid}", headers=Hf)
        chk(r.status_code == 200, f"取详情 -> {r.status_code}")
        d = r.json()
        chk(d.get("title") == TITLE, f"详情返回完整 title（#423）-> {(d.get('title') or '')[:20]}…")

        # ── ③ detail 里三个可复制字段原样返回（#422 的数据前提）──
        det = d.get("detail") or {}
        for k, v in [("payee", "苏州市贝得机电有限公司"),
                     ("payee_account", "32201989039051501113"),
                     ("payee_bank", "建行苏州市环秀支行")]:
            chk(det.get(k) == v, f"detail.{k} 原样返回（#422 复制按钮的取值）")
        # 多行事由的换行不能被后端吃掉（前端靠 white-space:pre-wrap 显示）
        chk("\n" in (det.get("reason") or ""), "多行付款事由的换行保留（#423-b）")
        chk((det.get("reason") or "").count("\n") == 2, "三行事由完整（不是被压成一行）")

        # ── ① 标记已付款要写审计 ──
        async with SessionLocal() as db:
            before = len((await db.execute(select(models.AuditLog).where(
                models.AuditLog.action == "oa_mark_paid"))).scalars().all())

        # 对公不传凭证应被拒（#412 既有口径，顺带保住不被本次改动破坏）
        r = await c.put(f"/api/oa/requests/{oid}/mark-paid", headers=Hf, data={"pay_note": "试试"})
        chk(r.status_code == 400, f"对公不传凭证仍被拒（#412 没被破坏）-> {r.status_code}")

        r = await c.put(f"/api/oa/requests/{oid}/mark-paid", headers=Hf,
                        data={"pay_note": "9/1 网银转账"},
                        files={"file": ("receipt.png", b"\x89PNG\r\n\x1a\n fake", "image/png")})
        chk(r.status_code == 200, f"带凭证标记已付款 -> {r.status_code}")
        chk(r.json().get("status") == "paid", "状态停在 paid（#412 口径）")

        async with SessionLocal() as db:
            rows = (await db.execute(select(models.AuditLog).where(
                models.AuditLog.action == "oa_mark_paid"))).scalars().all()
            chk(len(rows) == before + 1, f"写了 1 条 oa_mark_paid 审计 -> {len(rows) - before}")
            if rows:
                a = rows[-1]
                chk(a.user_id == fin_id, "审计记的是操作人（谁点的付款，从此查得到）")
                chk("OA20260901-001" in (a.detail or ""), f"审计带单号 -> {a.detail}")
                chk("850" in (a.detail or ""), f"审计带金额 -> {a.detail}")

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
