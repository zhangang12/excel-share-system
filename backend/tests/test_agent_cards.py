"""🆕 L3 卡片式人审门（阶段一：请款审批卡）。

设计依据 docs/ai-agent-erp-handbook 第 3.4 / 3.5 节。本测试锁四件事：

 1. **原则二 事实源在服务端**——facts 由后端用 current 重查回填。
    防的是「屏幕上写同意 ¥4,800，按钮实际批了 ¥48,000」。
 2. **原则三 能力可枚举**——type/action 双白名单，越界一律拒绝并留痕。
 3. **职责分离不留后门**——自己提交的单，卡片里按钮就是灰的，
    且带原因；不能让人点下去才吃 400（手册 3.5.3）。
 4. **令牌绑死 (user, type, ref)**——换人、换单、换类型、篡改、过期全部拒。

另外覆盖两个本次顺带补的缺口：请款审批写审计、供应商改收款账号写审计。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="cards")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.main import app
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.agent.cards import token as ctok, registry as creg

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        # 造两个人：mgr 是审批人（manager），other 是别人
        for u, role in (("mgr", "manager"), ("other", "finance")):
            await c.post("/api/admin/users", headers=H, json={
                "username": u, "password": "pass123", "full_name": u, "role_id": rid[role]})
        Hm = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'mgr','password':'pass123'})).json()['access_token']}"}

        async with SessionLocal() as db:
            uid = {x.username: x.id for x in (await db.execute(select(models.User))).scalars().all()}
            sup = models.Supplier(name="上海屹上脚轮有限公司", bank_name="农行",
                                  bank_account="6228480199998127")
            db.add(sup); await db.flush()
            # #1 别人提交的 → 可批   #2 mgr 自己提交的 → 职责分离，不可批
            pr_ok = models.PaymentRequest(supplier_id=sup.id, requested_amount=48000,
                                          requester_id=uid["other"], status="pending")
            pr_self = models.PaymentRequest(supplier_id=sup.id, requested_amount=6240,
                                            requester_id=uid["mgr"], status="pending")
            db.add_all([pr_ok, pr_self])
            # 历史 4 笔小额，用来验金额异常判据（48000 / 均值1000 = 48 倍）
            for _ in range(4):
                db.add(models.PaymentRequest(supplier_id=sup.id, requested_amount=1000,
                                             requester_id=uid["other"], status="paid"))
            await db.flush()
            # 🆕 关联采购明细，卡片的「项目/采购内容」就是从这来的。
            #    ⚠️ 关联字段是 PaymentRequestItem.item_id（不是 purchase_item_id），
            #    口径与网页端 purchase_mgmt_router._pr_out 同源。
            #    故意造两条不同项目编号 + 一条空编号，验去重、排序、空值不进结果。
            for code, item in (("2026-072", "蒸汽发生器"),
                               ("2026-059B", "脚轮"),
                               ("", "无编号的杂项")):
                pi = models.PurchaseItem(supplier_id=sup.id, item_name=item,
                                         project_code=code or None)
                db.add(pi); await db.flush()
                db.add(models.PaymentRequestItem(request_id=pr_ok.id, item_id=pi.id,
                                                 allocated_amount=100))
            await db.commit()
            ok_id, self_id = pr_ok.id, pr_self.id

        print("===== 1. 卡片装配：facts 来自后端重查 =====")
        r = await c.get("/api/agent/cards/pending", headers=Hm)
        chk(r.status_code == 200, f"取卡成功: {r.status_code}")
        data = r.json()
        chk(data["count"] == 2, f"两张待审卡: {data['count']}")
        by_ref = {x["ref"]: x for x in data["cards"]}
        card = by_ref[ok_id]
        facts = {f["k"]: f["v"] for f in card["facts"]}
        chk(facts["请款金额"] == "¥48,000.00", f"金额由后端算: {facts['请款金额']}")
        chk(facts["供应商"] == "上海屹上脚轮有限公司", f"供应商名: {facts['供应商']}")
        chk(facts["收款账号"] == "农行 …8127", f"账号只露后 4 位: {facts['收款账号']}")
        chk("6228480199998127" not in str(card), "完整银行账号不出现在卡片任何角落")
        chk(any(f.get("sensitive") for f in card["facts"]), "账号标了 sensitive")
        chk(data["amount_total"] == 54240.0, f"合计 = 48000+6240: {data['amount_total']}")

        # 🆕 用户反馈：审批时看不见「这笔钱花在哪个项目上」，没法判断该不该批。
        #    口径与网页端财务请款审批列表的「项目编号」列同源
        #    （purchase_mgmt_router._pr_out：关联采购明细的 project_code，去重排序）。
        #    ⚠️ 关联字段是 PaymentRequestItem.item_id，不是 purchase_item_id。
        chk("项目" in facts, f"卡片要有「项目」这一项：{sorted(facts)}")
        chk(card["facts"][0]["k"] == "项目",
            f"**项目摆第一**——审批人第一眼要知道钱花在哪：{card['facts'][0]['k']}")
        chk("采购内容" in facts, "还要说清买的是什么，光有金额判断不了")
        chk(facts["项目"] == "2026-059B、2026-072",
            f"多个项目编号去重排序拼接：{facts['项目']!r}")
        chk("无编号" not in facts["项目"] and "None" not in facts["项目"],
            f"空编号的明细不产生空值/None：{facts['项目']!r}")
        chk("蒸汽发生器" in facts["采购内容"] and "脚轮" in facts["采购内容"],
            f"采购内容列出买的什么：{facts['采购内容']!r}")

        print("\n===== 2. 职责分离：自己提交的单按钮置灰且给原因 =====")
        mine = by_ref[self_id]
        codes = {f["code"] for f in mine["flags"]}
        chk("self_submitted" in codes, f"标了 self_submitted: {codes}")
        chk(all(a["disabled_by"] == "self_submitted" for a in mine["actions"]),
            "两个动作都被置灰")
        msg = [f["msg"] for f in mine["flags"] if f["code"] == "self_submitted"][0]
        chk("职责分离" in msg, f"写明原因: {msg}")
        chk(data["blocked"] == 1, f"blocked 计数: {data['blocked']}")
        # 关键：不能因为批不了就不展示——用户会以为单子丢了
        chk(self_id in by_ref, "批不了的单仍然出现在列表里（不静默消失）")

        print("\n===== 3. 金额异常（历史够 3 笔才算）=====")
        acodes = {f["code"] for f in card["flags"]}
        chk("amount_outlier" in acodes, f"48000 vs 均值 1000 标了异常: {acodes}")
        chk(not any(f["code"] == "bank_changed" for f in card["flags"]),
            "无账号变更留痕时不出该标（宁可不出，不能出假象）")

        print("\n===== 4. 动作校验：白名单 + 令牌 =====")
        tok = card["token"]
        async def act(**kw):
            body = {"type": "pay_req_approve", "ref": ok_id, "token": tok, "action": "approve"}
            body.update(kw)
            return await c.post("/api/agent/cards/verify-action", headers=Hm, json=body)

        chk((await act()).status_code == 200, "正常动作通过")
        chk((await act(type="invoice_approve")).status_code == 400, "未登记的卡类型被拒")
        chk((await act(action="delete")).status_code == 400, "该卡不支持的动作被拒")
        chk((await act(ref=999999)).status_code == 400, "令牌与 ref 不匹配被拒")
        chk((await act(token=tok[:-4] + "AAAA")).status_code == 400, "签名被篡改后拒绝")
        r = await act(ref=self_id, token=by_ref[self_id]["token"])
        chk(r.status_code == 400 and "职责分离" in r.text, f"被 block 的卡动作也拒: {r.text[:60]}")

        print("\n===== 5. 越权：别人看不到不属于自己的待办 =====")
        Ho = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'other','password':'pass123'})).json()['access_token']}"}
        r = await c.post("/api/agent/cards/verify-action", headers=Ho, json={
            "type": "pay_req_approve", "ref": ok_id, "token": tok, "action": "approve"})
        chk(r.status_code == 400, f"拿别人的令牌用不了: {r.status_code}")

        print("\n===== 6. 拒绝动作要留痕（审计） =====")
        async with SessionLocal() as db:
            n = len((await db.execute(select(models.AuditLog).where(
                models.AuditLog.action == "card_action_denied"))).scalars().all())
            chk(n >= 5, f"每次拒绝都写审计: {n} 条")

        print("\n===== 7. 顺带补的两个缺口 =====")
        r = await c.put(f"/api/purchase-mgmt/payment-requests/{ok_id}/approve", headers=Hm)
        chk(r.status_code == 200, f"审批成功: {r.status_code} {r.text[:60]}")
        async with SessionLocal() as db:
            a = (await db.execute(select(models.AuditLog).where(
                models.AuditLog.action == "payment_approve"))).scalars().all()
            chk(len(a) == 1, f"请款审批现在写审计了: {len(a)} 条")
            chk("48,000" in (a[0].detail or ""), f"审计带金额: {a[0].detail}")

        r = await c.put(f"/api/purchase-mgmt/suppliers/{1}/", headers=Hm) if False else None
        async with SessionLocal() as db:
            s = (await db.execute(select(models.Supplier))).scalars().first()
            sid = s.id
        r = await c.put(f"/api/purchase-mgmt/suppliers/{sid}", headers=Hm,
                        json={"bank_account": "6228480188887777"})
        chk(r.status_code == 200, f"改收款账号: {r.status_code}")
        async with SessionLocal() as db:
            b = (await db.execute(select(models.AuditLog).where(
                models.AuditLog.action == "update_supplier_bank"))).scalars().all()
            chk(len(b) == 1, f"改账号写审计了: {len(b)} 条")
            chk("6228480188887777" not in (b[0].detail or ""),
                f"审计里不存账号明文: {b[0].detail}")
            chk("7777" in (b[0].detail or ""), "只留尾号便于核对")

        print("\n===== 8. 改完账号后，新卡应带 bank_changed =====")
        async with SessionLocal() as db:
            db.add(models.PaymentRequest(supplier_id=sid, requested_amount=900,
                                         requester_id=uid["other"], status="pending"))
            await db.commit()
        r = await c.get("/api/agent/cards/pending", headers=Hm)
        fresh = [x for x in r.json()["cards"] if x["ref"] not in (ok_id, self_id)]
        chk(fresh and any(f["code"] == "bank_changed" for f in fresh[0]["flags"]),
            f"账号刚改过 → 卡上标出来: {[f['code'] for f in fresh[0]['flags']] if fresh else '无卡'}")

        print("\n===== 9. 白名单本身 =====")
        chk(creg.is_known("pay_req_approve") and not creg.is_known("anything_else"),
            "第一期只放行 pay_req_approve 一类")
        chk(creg.allows("pay_req_approve", "approve") and
            not creg.allows("pay_req_approve", "pay"), "动作也是白名单")

    await engine.dispose()
    print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
