"""🆕 `find_entity` 的候选要出表格 —— 对准杨坛的真实用法。

依据：2026-08-18 拉的问答日志，杨坛 18 次提问里 **13 次是「编号 ↔ 设备规格」互查**
（「200L的设备有哪几个编号」「5L双行星是哪个编号」「5L的设备有几台」…），
答案其实都答对了，但 **7 条里 6 条 `rendered=false`** —— 全是模型一行行打的文字墙。

根因和 `get_project` 是同一个：返回嵌套对象，渲染层只认 `items`/`columns`。

另外锁一条：`count` 必须给。以前不给，审计日志把**答对了**的问答统统记成
「查到 0 条」，看日志的人会以为这个场景是坏的——差点按坏的去改。
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="findtbl")
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

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


async def main():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    async with SessionLocal() as db:
        u = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()

        # 照抄生产上的真实数据（项目名里带容量的占 95/107）
        rows = [("2026-071A", "200L双行星分散混合机", "进行中", "江苏军航"),
                ("2026-071B", "200L提升式压料机", "进行中", "江苏军航"),
                ("2026-059A", "200L双行星分散混合机", "已完成", "湖北正安"),
                ("2026-059B", "200L提升式压料机", "已完成", "湖北正安"),
                ("2026-068", "5L双行星混合机", "进行中", "江苏军航"),
                ("2026-070", "5L双行星搅拌压料灌装一体机", "进行中", "苏州杜玛")]
        for code, nm, st, cus in rows:
            p = models.Project(code=code, name=nm, status=st,
                               extra={te.DELIVER_KEY: (date.today() + timedelta(7)).isoformat()})
            db.add(p); await db.flush()
            db.add(models.SalesLedger(project_id=p.id, customer=cus, amount=60000))
        db.add(models.Supplier(name="无锡腾丰液压"))
        db.add(models.WhMaterial(code="M-001", name="丝攻", spec="M6", unit="个"))
        await db.commit()

        # ── ① 「200L的设备有哪几个编号」 ──
        r = await te.find_entity(db, u, "200L")
        chk(r["total"] == 4, f"200L 命中 4 个项目：{r['total']}")
        chk(r["count"] == 4 and r["shown"] == 4,
            f"count/shown 有值（以前恒为 0，日志会把答对的记成失败）：{r['count']}/{r['shown']}")
        chk(r["columns"] == ["code", "name", "status", "customer"], f"列声明：{r['columns']}")
        t = rd.table(r, plan=None)
        chk(t.startswith("|"), "渲染成 markdown 表格，不是文字墙")
        chk("2026-071A" in t and "2026-059B" in t, "4 个编号一个不少")
        chk("进行中" in t and "已完成" in t,
            "带出状态——他真正要分的就是在建还是已完成")
        chk("江苏军航" in t, "带出客户")

        # ── ② 「5L双行星是哪个编号」 ──
        r2 = await te.find_entity(db, u, "5L双行星")
        chk(r2["total"] == 2, f"5L双行星命中 2 个：{r2['total']}")
        t2 = rd.table(r2, plan=None)
        chk("2026-068" in t2 and "2026-070" in t2, "两个编号都在表里")

        # ── ③ 一个都没命中时不要硬出空表 ──
        r3 = await te.find_entity(db, u, "浙江宝")
        chk(r3["total"] == 0 and r3["items"] == [], "没命中就没有 items")
        chk(rd.table(r3, plan=None) == "", "没命中不渲染空表")
        chk(r3["hint"], "给了改口的提示")

        # ── ④ 只铺命中最多的那一类，列才对得上 ──
        r4 = await te.find_entity(db, u, "丝攻")
        chk(r4["columns"] == ["item_name", "spec"], f"物料用物料的列：{r4['columns']}")
        t4 = rd.table(r4, plan=None)
        chk("规格" in t4 and "M6" in t4, f"物料表带规格：{t4.splitlines()[0] if t4 else '(空)'}")

        # ── ⑤ 按客户命中 ──
        r5 = await te.find_entity(db, u, "苏州杜玛", kind="customer")
        chk(r5["columns"] == ["customer", "ledger_rows"], f"客户用客户的列：{r5['columns']}")
        t5 = rd.table(r5, plan=None)
        chk("苏州杜玛" in t5, "客户名出来了")

        # ── ⑥ 键名必须对上渲染层的 _NAME_KEYS，否则表头退化成「名称」 ──
        chk("客户" in t5, f"客户表头是「客户」不是「名称」：{t5.splitlines()[0] if t5 else '(空)'}")

    await engine.dispose()
    print("\n" + ("FAILED: " + "; ".join(FAIL) if FAIL else "ALL PASS"))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
