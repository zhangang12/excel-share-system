"""反馈批次 2026-08-18：#401 请购需求时间 / #402 对私付款 / #403 待办完成筛选。

  #401 李新新（采购）：「他们请购的东西，能不能添加一个需求时间，我这边好根据实际
       情况安排（我有时候要凑单）。没有时间我也不知道他们着不着急，耽误事情就不好了」
  #402 李昌奇：「没有对私付款申请一项」
  #403 赵仁辉：管理层待办「下发/监控」里下发过的越堆越多，要能分已完成/未完成

本文件锁的是**后端**这三条（#403 的筛选是纯前端切片，这里只锁它依赖的
done_count/total 口径必须对——前端就是靠这两个数判定"这条办完了"）。
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="fb401")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns, ensure_indexes
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
    await ensure_indexes(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        # ================= #401 请购单需求时间 =================
        wh = (await c.post("/api/admin/users", headers=H, json={
            "username": "wh1", "password": "pass123", "full_name": "仓库王", "role_id": rid["warehouse"]})).json()
        HW = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 'wh1', 'password': 'pass123'})).json()['access_token']}"}

        today = date.today()
        past, soon, far = (today - timedelta(days=3)).isoformat(), (today + timedelta(days=1)).isoformat(), (today + timedelta(days=30)).isoformat()

        made = {}
        for tag, nd in [("过期", past), ("很急", soon), ("不急", far), ("没填", None)]:
            body = {"lines": [{"item_name": f"圆头螺丝-{tag}", "spec": "M5*50", "qty": 200}]}
            if nd:
                body["need_date"] = nd
            rr = await c.post("/api/purchase-mgmt/purchase-requests", headers=HW, json=body)
            chk(rr.status_code == 200, f"#401 提请购单（{tag}）: {rr.status_code} {rr.text[:80]}")
            made[tag] = rr.json()

        chk(made["很急"]["need_date"] == soon, f"#401 需求时间存下来了: {made['很急']['need_date']}")
        chk(made["没填"]["need_date"] is None, "#401 不填也能提（选填，不逼人瞎填一个）")
        # need_days 由服务端算：三个页面各算各的迟早对不上
        chk(made["过期"]["need_days"] == -3, f"#401 已过期 → need_days 为负: {made['过期']['need_days']}")
        chk(made["很急"]["need_days"] == 1, f"#401 明天要 → need_days=1: {made['很急']['need_days']}")
        chk(made["不急"]["need_days"] == 30, f"#401 一个月后 → need_days=30: {made['不急']['need_days']}")
        chk(made["没填"]["need_days"] is None, "#401 没填日期 → need_days 为空（不是 0，0 会被当成'就是今天'）")

        rows = (await c.get("/api/purchase-mgmt/purchase-requests", headers=H)).json()
        by_name = {r["lines"][0]["item_name"]: r for r in rows if r["lines"]}
        chk(by_name["圆头螺丝-很急"]["need_date"] == soon, "#401 列表接口带出需求时间（采购端要按它排序）")
        chk(by_name["圆头螺丝-很急"]["need_days"] == 1, "#401 列表接口也带 need_days")

        # 打印出来的申请单要有这一项（给领导/供应商看的那份不能少）
        pdf = await c.get(f"/api/purchase-mgmt/purchase-requests/{made['很急']['id']}/pdf", headers=H)
        chk(pdf.status_code == 200 and pdf.content[:4] == b"%PDF",
            f"#401 申请单 PDF 能出: {pdf.status_code} {pdf.content[:8]}")

        # ================= #402 对私付款申请 =================
        types = (await c.get("/api/oa/doc-types", headers=H)).json()
        keys = {t["key"]: t for t in types}
        chk("payment_private" in keys, f"#402 单据类型里有对私付款: {sorted(keys)[:6]}…")
        if "payment_private" in keys:
            chk(keys["payment_private"]["label"] == "对私付款申请",
                f"#402 名称: {keys['payment_private']['label']}")
            chk(keys["payment_private"].get("enabled") is not False, "#402 默认启用")

        # ⚠️ 关键：新类型必须**有审批链**，否则一提交就撞「尚未配置审批流程」= 上线即不可用。
        #   全新库里对公自己也没链（没人配过），所以先照生产的样子给对公配一条，
        #   再跑一次迁移——这才是生产上真正会走的那条路（生产实测：对公 20 条 / 7 个部门）。
        dept_id = (await c.get("/api/oa/departments", headers=H)).json()[0]["id"]
        rr = await c.post("/api/oa/chains", headers=H, json={
            "department_id": dept_id, "doc_type": "payment_public", "step_order": 1,
            "approver_role": "manager", "enabled": True})
        chk(rr.status_code == 200, f"#402 先给对公配一条审批链: {rr.status_code} {rr.text[:70]}")
        async with SessionLocal() as db:
            await run_all(db)
        async with SessionLocal() as db:
            n_priv = len((await db.execute(select(models.OaApprovalStep).where(
                models.OaApprovalStep.doc_type == "payment_private"))).scalars().all())
            n_pub = len((await db.execute(select(models.OaApprovalStep).where(
                models.OaApprovalStep.doc_type == "payment_public"))).scalars().all())
        chk(n_priv == n_pub and n_priv > 0,
            f"#402 **审批链已从对公复制过来**（不然一提交就被挡，功能等于没上）: 对私 {n_priv} 条 / 对公 {n_pub} 条")

        # 幂等：再跑一次迁移不该翻倍
        async with SessionLocal() as db:
            await run_all(db)
            n2 = len((await db.execute(select(models.OaApprovalStep).where(
                models.OaApprovalStep.doc_type == "payment_private"))).scalars().all())
        chk(n2 == n_priv, f"#402 迁移幂等，审批链没被复制第二遍: {n_priv} → {n2}")

        # 对私是转账 → 账号/开户行必填；收款人为空时的报错文案要说「收款人」不是「收款单位」
        base = {"category": "business", "doc_type": "payment_private", "department_id": dept_id,
                "title": "付张三劳务费", "amount": 5000, "detail": {"payee": "", "reason": "劳务费"}}
        rr = await c.post("/api/oa/requests", headers=H, json=base)
        chk(rr.status_code == 400 and "收款人" in rr.text,
            f"#402 对私缺收款人时提示「收款人」而不是「收款单位」: {rr.status_code} {rr.text[:70]}")
        rr = await c.post("/api/oa/requests", headers=H, json={
            **base, "detail": {"payee": "张三", "reason": "劳务费"}})
        chk(rr.status_code == 400 and "收款账号" in rr.text,
            f"#402 对私是转账，账号必填: {rr.status_code} {rr.text[:70]}")
        rr = await c.post("/api/oa/requests", headers=H, json={
            **base, "detail": {"payee": "张三", "reason": "劳务费",
                               "payee_account": "6222021234567890", "payee_bank": "工行无锡分行"}})
        chk(rr.status_code == 200, f"#402 填全了能提交（说明审批链是通的）: {rr.status_code} {rr.text[:90]}")

        # ================= #403 下发/监控 完成状态 =================
        u1 = (await c.post("/api/admin/users", headers=H, json={
            "username": "t1", "password": "pass123", "full_name": "收件甲", "role_id": rid["warehouse"]})).json()
        u2 = (await c.post("/api/admin/users", headers=H, json={
            "username": "t2", "password": "pass123", "full_name": "收件乙", "role_id": rid["warehouse"]})).json()
        t = (await c.post("/api/management-todos", headers=H, json={
            "title": "中石化油卡", "content": "不用指定车牌",
            "recipient_ids": [u1["id"], u2["id"]]})).json()

        def find(lst, tid):
            return next((x for x in lst if x["id"] == tid), None)

        sent = find((await c.get("/api/management-todos/sent", headers=H)).json(), t["id"])
        chk(sent["total"] == 2 and sent["done_count"] == 0,
            f"#403 刚下发：完成 0/2（前端据此归入「未完成」）: {sent['done_count']}/{sent['total']}")

        # 甲完成 → 仍算未完成（1/2）；前端不能只看"有人完成了"就当整条办完
        H1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 't1', 'password': 'pass123'})).json()['access_token']}"}
        mine = (await c.get("/api/management-todos/mine", headers=H1)).json()
        tgt1 = next(x for x in mine if x["todo_id"] == t["id"])
        await c.post(f"/api/management-todos/{tgt1['target_id']}/done", headers=H1)
        sent = find((await c.get("/api/management-todos/sent", headers=H)).json(), t["id"])
        chk(sent["done_count"] == 1 and sent["total"] == 2,
            f"#403 只完成一半仍是未完成: {sent['done_count']}/{sent['total']}")

        # 乙也完成 → 整条完成
        H2 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 't2', 'password': 'pass123'})).json()['access_token']}"}
        mine2 = (await c.get("/api/management-todos/mine", headers=H2)).json()
        tgt2 = next(x for x in mine2 if x["todo_id"] == t["id"])
        await c.post(f"/api/management-todos/{tgt2['target_id']}/done", headers=H2)
        sent = find((await c.get("/api/management-todos/sent", headers=H)).json(), t["id"])
        chk(sent["done_count"] == 2 and sent["total"] == 2,
            f"#403 两人都完成 → done_count==total，前端归入「已完成」: {sent['done_count']}/{sent['total']}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
