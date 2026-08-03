"""每日简报（app/agent/briefing.py + daily.py）。

这个模块的每一条设计都是拿生产数据跑出来才定的，测试把这些结论钉住，
免得以后有人「顺手优化」又退回去：

  1. 金额必须**线性**主导 —— 早先用 log10，¥22 万 13.11 分、¥4.2 万 11.34 分，
     差 5 倍金额只差 14% 分，排序退化。
  2. 「发货单还是 pending」必须单独说 —— 生产 35 张发货单 32 张是 pending，
     无脑推「去催款」会让人去追一笔可能还没发货的钱。
  3. 冷却期内推过的**不再推，且不回退成重推** —— 重复推同一批正是催办失败的原因
     （生产催办推了 83 条没人理）。这条曾经被我自己写的兜底破坏过。
  4. 审批要有固定配额 —— 纯按分数排，前三名会被大额应收包圆，
     而审批是卡着别人干活的。
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone

tmp = tempfile.mkdtemp(prefix="brief")
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
from app.agent import briefing, daily

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

    # ================= 纯函数：评分 =================
    print("\n===== 评分 =====")
    s_big = briefing._score(220000, 48, blind=True)
    s_small = briefing._score(42000, 48, blind=True)
    chk(s_big / s_small > 4.5,
        f"5 倍金额要拉开 4.5 倍以上分差（log10 版只有 1.16 倍）：{s_big/s_small:.2f}")
    chk(briefing._score(100000, 0, blind=True) > briefing._score(100000, 0, blind=False),
        "同额同龄，无人管的排前面")
    chk(briefing._score(100000, 90, False) == briefing._score(100000, 300, False),
        "账龄 90 天封顶，老单不能永远霸榜")
    chk(briefing._score(100000, 0, False) * 2 == briefing._score(100000, 90, False),
        "时间系数满格正好 2 倍")

    # ================= 落库数据 =================
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}

        async with SessionLocal() as db:
            me = (await db.execute(select(models.User).where(
                models.User.username == "admin"))).scalar_one()
            now = datetime.now(timezone.utc)
            # 三种发货状态各一条，外加一条尾款
            spec = [("大额待发货", 220000, 0, 48, "pending"),
                    ("中额无发货单", 60000, 0, 30, None),
                    ("已发货未收款", 80000, 0, 20, "shipped"),
                    ("尾款没到期日", 0, 90000, 14, "shipped")]
            for i, (cust, sr, bal, days, st) in enumerate(spec):
                p = models.Project(code=f"BRF-{i}", name=cust, is_deleted=False)
                db.add(p); await db.flush()
                db.add(models.SalesLedger(
                    project_id=p.id, sales_uid=me.id, customer=cust, amount=500000,
                    ship_receivable=sr, balance=bal, balance_date="", order_state="approved",
                    created_at=now - timedelta(days=days)))
                if st:
                    db.add(models.Shipment(project_id=p.id, status=st,
                                           receiver_name="x", receiver_phone="1", receiver_addr="y"))
            # 一条待审销售订单：金额远小于上面的应收，考验配额
            p = models.Project(code="BRF-APR", name="待审", is_deleted=False)
            db.add(p); await db.flush()
            db.add(models.SalesLedger(
                project_id=p.id, sales_uid=me.id, customer="待审客户", amount=17000,
                ship_receivable=0, balance=0, balance_date="", order_state="pending",
                created_at=now - timedelta(days=17)))
            await db.commit()

            print("\n===== 理由必须能区分 =====")
            items = await briefing._ledger_items(db, me)
            whys = {i["title"].split()[0]: i["why"] for i in items}
            chk(any("待发货" in w for w in whys.values()),
                f"pending 发货单要提示先核对：{[w for w in whys.values() if '待发货' in w][:1]}")
            chk(any("没有发货单" in w for w in whys.values()), "没有发货单要单独说")
            chk(any("没填到期日" in w for w in whys.values()), "尾款没到期日要说明催办扫不到")
            chk(all("逾期" not in w and "欠了" not in w for w in whys.values()),
                "**不能**说「逾期/欠了 N 天」——ship_date 和 shipped_at 生产上几乎全空，数据不支持")
            chk(all("台账建了" in w or "今天建的" in w for w in whys.values()),
                "账龄只说「台账建了 N 天」这个站得住的事实")

            print("\n===== 配额：审批不能被大额应收挤掉 =====")
            b = await briefing.build(db, me, top=3)
            cats = [i["cat"] for i in b["items"]]
            chk(cats.count("approve") == 1, f"3 条里固定留 1 条审批：{cats}")
            chk(cats.count("recv") == 2, f"另外 2 条是应收：{cats}")
            amt_recv = [i["amount"] for i in b["items"] if i["cat"] == "recv"]
            chk(amt_recv == sorted(amt_recv, reverse=True), "应收之间按金额从大到小")

            print("\n===== 冷却去重 =====")
            first = {(i["card"], i["ref"]) for i in b["items"]}
            b2 = await briefing.build(db, me, top=3, skip_refs=first)
            second = {(i["card"], i["ref"]) for i in b2["items"]}
            chk(not (first & second), f"第二次不再出现推过的：{first & second}")

            # 全部推过 → 必须静默，不能回退成重推（曾经被自己写的兜底破坏）
            allrefs = {(i["card"], i["ref"]) for i in
                       (await briefing._ledger_items(db, me))
                       + (await briefing._order_items(db, me))}
            b3 = await briefing.build(db, me, top=3, skip_refs=allrefs)
            chk(b3["items"] == [], "冷却期内全推过 → 一条都不推（不是再推一遍）")
            chk(briefing.render(b3, "某人") == "", "没内容时渲染成空串，daily 据此不发消息")

            print("\n===== 渲染 =====")
            txt = briefing.render(b, "杨坛")
            chk(txt.startswith("杨坛，今天这 3 件事该管："), f"抬头：{txt.splitlines()[0]}")
            chk(txt.count("→ 可「") == 3, "每条都带一个可执行动作")
            chk("另有" in txt, "剩余项要说清楚，否则他以为只有这 3 件")

        # ================= 端点 =================
        print("\n===== 端点 =====")
        r = await c.get("/api/agent/briefing/config", headers=H)
        chk(r.status_code == 200 and r.json()["usernames"] == [],
            f"默认无收件人（不会一上线全员轰炸）：{r.status_code} {r.text[:80]}")
        chk(0 < r.json()["next_run_in_seconds"] <= 86400, "下次推送时间在 24 小时内")

        r = await c.put("/api/agent/briefing/config", headers=H,
                        json={"usernames": ["查无此人"]})
        chk(r.status_code == 400, f"收件人不存在要挡下：{r.status_code}")

        r = await c.put("/api/agent/briefing/config", headers=H, json={"usernames": ["admin"]})
        chk(r.status_code == 200, f"设置收件人：{r.status_code} {r.text[:80]}")

        r = await c.post("/api/agent/briefing/preview?dry=true", headers=H)
        chk(r.status_code == 200 and r.json()["sent"] == 0,
            f"dry-run 不真发：{r.json().get('sent')}")
        chk("text" in (r.json()["detail"][0]), "dry-run 要能看到将发的正文")

        r = await c.post("/api/agent/briefing/preview?dry=false", headers=H)
        chk(r.json()["sent"] == 1, f"真发一次：{r.json()}")

        async with SessionLocal() as db:
            n = len((await db.execute(select(models.Message).where(
                models.Message.biz_type == "agent_briefing"))).scalars().all())
            chk(n == 1, f"站内消息落库 1 条：{n}")
            sent = await daily._get_setting(db, daily._SETTING_SENT, {})
            chk(len(sent.get("admin", {})) == 3, f"已推清单记了 3 条：{sent}")

        # 共 5 项，首推 3 条 → 第二轮该推**剩下的**，不是重推（这就是轮换）
        r = await c.post("/api/agent/briefing/preview?dry=false", headers=H)
        second_txt = r.json()["detail"][0].get("text", "")
        chk(r.json()["sent"] == 1 and "已发货未收款" in second_txt,
            f"第二轮换成没推过的那批：{second_txt.splitlines()[1] if second_txt else '(空)'}")
        chk("大额待发货" not in second_txt, "第二轮不再出现首轮推过的")

        # 5 项全推完 → 第三轮必须静默
        r = await c.post("/api/agent/briefing/preview?dry=true", headers=H)
        chk(r.json()["detail"][0].get("skip") is not None,
            f"全部推完后 → 冷却期内不打扰：{r.json()['detail'][0].get('skip') or r.json()['detail'][0]}")

        r = await c.get("/api/agent/briefing/me", headers=H)
        chk(r.status_code == 200 and r.json()["total_count"] > 0,
            f"/briefing/me 给本人的简报（H5 首页用）：{r.json().get('total_count')}")

        # 关掉
        r = await c.put("/api/agent/briefing/config", headers=H, json={"usernames": []})
        chk(r.status_code == 200, "空数组 = 关掉推送")
        r = await c.post("/api/agent/briefing/preview?dry=true", headers=H)
        chk(r.json()["recipients"] == 0, "关掉后不推任何人")

    print("\n" + "=" * 56)
    if FAIL:
        print(f"❌ {len(FAIL)} 条失败：")
        for f in FAIL: print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")

asyncio.run(main())
