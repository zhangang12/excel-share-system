"""🆕 管理层待办进智能体·第一批：盯 + 批顺延。

依据是生产库全量 35 条待办 / 38 条收件记录：
  · **7 条顺延申请挂着一条没批**——收件人以为报上去了，管理层压根没看见
  · 5 条承诺日已过还没完成（最久的超 30 天）
  · 3 条发出去根本没人回

本测试锁三类事：

 1. **权限**。`GET /agent/cards/{type}` 只做登录校验，权限一律由各 assembler
    自己按 current 过滤。顺延申请没有「归属人」的概念，不在装配里加
    `require_admin_or_manager` 的同款闸，任何登录用户都能读到谁在拖、
    事项标题、他写的说明——批不了但看得见，照样是越权。
 2. **口径对齐端点**。装配只取 `extend_status='pending'` 且 `extend_to` 有值的，
    与 `decide_extend` 的前置校验一致；否则卡片上有按钮、点下去 400。
 3. **ref 是 target_id 不是 todo_id**。一条待办发给多人时每人一行，
    各自独立申请顺延；用 todo_id 会批错人。
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="mtodo")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.agent import tools_entity as te, render as rd
from app.agent import cards as _cards

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


def d(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


async def main():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        # 另一位管理层：用来验「只看自己发的」
        other_role = boss.role_id
        other = models.User(username="yangtan", full_name="杨坛",
                            password_hash="x", role_id=other_role)
        # 普通人：用来验越权
        worker = models.User(username="xiakun", full_name="夏锟",
                             password_hash="x", role_id=other_role)
        plain = models.User(username="plain", full_name="路人",
                            password_hash="x", role_id=other_role)
        db.add_all([other, worker, plain]); await db.flush()

        def mk(title, creator, prio="normal", due=None):
            t = models.ManagementTodo(title=title, created_by=creator.id,
                                      priority=prio, due_date=due)
            db.add(t)
            return t

        t1 = mk("升降接近开关处理", boss, due=d(-10))
        t2 = mk("车位处理", boss, due=d(-30))
        t3 = mk("库位重排", boss, "urgent", due=d(3))
        t4 = mk("已经做完的事", boss, due=d(-5))
        t5 = mk("杨坛发的，不该出现在我的列表里", other, due=d(-2))
        await db.flush()

        # 夏锟：承诺 -4 天（超期）且申请顺延到 +10
        g1 = models.ManagementTodoTarget(todo_id=t1.id, user_id=worker.id,
                                         status="committed", committed_at=d(-4),
                                         extend_status="pending", extend_to=d(10),
                                         extend_reason="配件还没到",
                                         progress="拆了一半")
        # 方步森：承诺 -30 天，纯超期没申请顺延
        g2 = models.ManagementTodoTarget(todo_id=t2.id, user_id=plain.id,
                                         status="committed", committed_at=d(-30))
        # 还没回复承诺时间
        g3 = models.ManagementTodoTarget(todo_id=t3.id, user_id=worker.id,
                                         status="pending")
        # 已完成：不该出现
        g4 = models.ManagementTodoTarget(todo_id=t4.id, user_id=worker.id,
                                         status="done", committed_at=d(-6))
        # 别人发的
        g5 = models.ManagementTodoTarget(todo_id=t5.id, user_id=worker.id,
                                         status="committed", committed_at=d(-2))
        db.add_all([g1, g2, g3, g4, g5])
        await db.commit()
        g1_id, g2_id = g1.id, g2.id

    # ───────── ① 盯：mgmt_todo_watch ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        r = await te.mgmt_todo_watch(db, boss)

        chk(r["count"] == 3, f"只算自己发的、且没做完的 3 条：{r['count']}")
        names = [i["todo"] for i in r["items"]]
        chk("已经做完的事" not in names, "已完成的不占屏")
        chk(not any("杨坛发的" in n for n in names), "别人发的不混进来")

        s = r["summary"]
        chk(s["pending_extend"] == 1, f"顺延申请计数：{s['pending_extend']}")
        chk(s["overdue"] == 1, f"纯超期计数：{s['overdue']}")
        chk(s["no_reply"] == 1, f"没回复计数：{s['no_reply']}")

        chk("顺延" in r["items"][0]["status"],
            f"要批的顺延排最前：{r['items'][0]['status']}")
        chk(r["items"][1]["over_days"] == 30,
            f"其次按超期天数降序：{r['items'][1].get('over_days')}")
        chk(r["hint"] and "顺延" in r["hint"], f"提示点出顺延：{r['hint']}")

        # ⚠️ 承诺日优先于截止日：t1 的 due_date 是 -10 天，但他承诺的是 -4 天
        it = next(i for i in r["items"] if "升降" in i["todo"])
        chk(it["due_date"] == d(-4), f"逾期看承诺日不看原截止日：{it['due_date']}")

        chk(r["columns"] == ["worker", "todo", "status"], f"列声明：{r['columns']}")
        tbl = rd.table(r, plan=None)
        chk(tbl.startswith("|"), "渲染成表格，不是文字墙")
        chk(tbl.splitlines()[0].startswith("| 负责人 | 事项 | 状态"),
            f"**人排第一列**（催人先看谁）：{tbl.splitlines()[0] if tbl else '(空)'}")
        chk("夏锟" in tbl, "人名在表里")

    # ───────── ② 批顺延：卡片 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        cards = await _cards.assemble_extend_cards(db, boss)
        chk(len(cards) == 1, f"只有 1 条待批顺延：{len(cards)}")
        c = cards[0]
        chk(c["type"] == "mgmt_todo_extend", "卡片类型")
        chk(c["ref"] == g1_id, f"**ref 是 target_id 不是 todo_id**：{c['ref']} vs {g1_id}")
        chk(c["ref"] != g2_id, "没串到别人那行")
        facts = {f["k"]: f["v"] for f in c["facts"]}
        chk(facts.get("谁") == "夏锟", f"带出是谁：{facts.get('谁')}")
        chk(facts.get("申请改到") == d(10), f"新日期：{facts.get('申请改到')}")
        chk(facts.get("原承诺已过") == "4 天", f"原承诺超了多久：{facts.get('原承诺已过')}")
        chk(facts.get("申请理由") == "配件还没到",
            f"**申请理由取 extend_reason 不是 progress**：{facts.get('申请理由')}")
        chk("拆了一半" not in str(facts), "别把平时报的进展当成申请理由")
        chk({a["key"] for a in c["actions"]} == {"approve", "reject"}, "两个动作")
        chk(all(a["disabled_by"] is None for a in c["actions"]), "没被 block")
        chk(_cards.allows("mgmt_todo_extend", "approve"), "白名单登记了 approve")
        chk(not _cards.allows("mgmt_todo_extend", "delete"), "白名单外的动作不放行")
        chk("mgmt_todo_extend" in _cards.ASSEMBLERS, "装配表登记了（否则动作永远校验不过）")

    # ───────── ③ 越权：非管理层一条都看不到 ─────────
    async with SessionLocal() as db:
        p = (await db.execute(select(models.User).where(
            models.User.username == "plain"))).scalars().first()
        # 把角色降成普通职员
        role = (await db.execute(select(models.Role).where(
            models.Role.code == "sales"))).scalars().first()
        if role:
            p.role_id = role.id
            await db.commit()
            await db.refresh(p)
        chk(not p.has_role("admin", "manager"), "这个人确实不是管理层")
        leaked = await _cards.assemble_extend_cards(db, p)
        chk(leaked == [], f"**非管理层一条都读不到**（批不了但看得见也是越权）：{len(leaked)}")

    # ───────── ④ 口径对齐端点：撤回申请后卡片就该消失 ─────────
    async with SessionLocal() as db:
        boss = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        g = await db.get(models.ManagementTodoTarget, g1_id)
        g.extend_status = "approved"          # 已经批过了
        await db.commit()
        chk(await _cards.assemble_extend_cards(db, boss) == [],
            "批过的不再出卡（否则点下去 400）")

        g.extend_status = "pending"
        g.extend_to = None                     # 状态在但没填新日期
        await db.commit()
        chk(await _cards.assemble_extend_cards(db, boss) == [],
            "没填新日期的不出卡（端点也会拒）")

    await engine.dispose()
    print("\n" + ("FAILED: " + "; ".join(FAIL) if FAIL else "ALL PASS"))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
