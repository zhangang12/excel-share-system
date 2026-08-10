"""🆕 `get_project` 的明细要能渲染成表格，而且带得上负责人。

为什么要有这个测试（来自杨坛 2026-08-10 的真实使用记录）：

 1. **最高频的「查某个项目」反而享受不到表格**。`get_project` 以前返回的是
    嵌套对象（dept_orders / produce_groups / purchase_pending 各一层），
    而渲染层只认 `items` / `suppliers` / `rows` 这几个**列表**字段——
    结果 5 次问答里 3 次 `rendered=false`，答案退回成一堆文字。
 2. **「037的电是谁做的」问了两遍都答不上来**。138 条部门单里有 worker_id，
    但工具压根没把人名带出来。
 3. 状态和部门必须是**中文**。给业务看 `dispatched` / `electric` 等于没答。
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="projdetail")
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


def d(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


async def main():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    async with SessionLocal() as db:
        u = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        for un, fn in (("songpu", "宋朴"), ("zhourui", "周瑞")):
            db.add(models.User(username=un, full_name=fn, password_hash="x", role_id=1))
        await db.flush()
        who = {x.username: x.id
               for x in (await db.execute(select(models.User))).scalars().all()}
        sup = models.Supplier(name="无锡腾丰液压"); db.add(sup); await db.flush()

        # 复刻 2026-060A / 060B：同一个「060」命中两个项目
        for code, nm in (("2026-060A", "30L双行星混合机"), ("2026-060B", "30L提升式压料机")):
            p = models.Project(code=code, name=nm, status="进行中",
                               extra={te.DELIVER_KEY: d(5)})
            db.add(p); await db.flush()
            db.add(models.SalesLedger(project_id=p.id, customer="湖北正安新材料", amount=56000))
            db.add(models.DeptOrder(project_id=p.id, dept="design", status="done"))
            db.add(models.DeptOrder(project_id=p.id, dept="electric", status="in_progress",
                                    due_date=d(-25), worker_id=who["songpu"]))
            po = models.DeptOrder(project_id=p.id, dept="produce", status="in_progress",
                                  worker_id=who["zhourui"])
            db.add(po); await db.flush()
            db.add(models.ProduceGroupTask(order_id=po.id, project_id=p.id,
                                           group="sheetmetal", status="dispatched",
                                           worker_id=who["zhourui"]))
            db.add(models.Shipment(project_id=p.id, status="pending"))
        for i in range(3):
            db.add(models.PurchaseItem(supplier_id=sup.id, project_code="2026-060A",
                                       item_name=f"液压件{i}", expected_arrival=d(-3)))
        await db.commit()

        # ── ① 卡点清单能渲染成表格 ──
        r = await te.get_project(db, u, "060")
        chk(r.get("matched_count") == 2, "060 同时命中 A 和 B")
        chk(isinstance(r.get("items"), list) and r["items"], "返回了可渲染的 items 列表")
        chk(r.get("columns") == ["stage", "worker", "status"], "工具自己声明了列")
        tbl = rd.table(r, plan=None)
        chk(tbl.startswith("|"), "渲染出的是 markdown 表格，不是一堆文字")
        chk("| 环节 | 负责人 | 状态 |" in tbl, f"表头是中文的三列，实际：{tbl.splitlines()[0]}")

        # ── ② 负责人带出来了（「037的电是谁做的」） ──
        chk("宋朴" in tbl, "电工那行带出了负责人姓名")
        chk("2026-060A" in tbl and "2026-060B" in tbl, "两个项目都在表里，一个都不少")

        # ── ③ 中文化：不许把 dispatched / electric 甩给业务 ──
        for raw in ("dispatched", "electric", "produce", "sheetmetal", "in_progress"):
            chk(raw not in tbl, f"表里没有英文原值 {raw}")
        chk("已派工" in tbl, "dispatched 译成了「已派工」")
        chk("逾期未完成" in tbl, "超期的电工单标成了「逾期未完成」")

        # ── ④ 生产拆到组之后不再重复报父单 ──
        stages = [i["stage"] for i in r["items"]]
        chk(not any(s.endswith(" 生产") for s in stages),
            f"生产已拆到组就不报父单，实际 stages={stages}")
        chk(any("生产·钣金" in s for s in stages), "报的是生产组")

        # ── ⑤ 已完成的环节不进卡点清单 ──
        chk(not any("设计" in s for s in stages), "已完成的设计不算卡点")

        # ── ⑥ detail=purchase 换成采购明细，且供应商没被挤掉 ──
        r2 = await te.get_project(db, u, "060", detail="purchase")
        chk(r2["columns"] == ["item_name", "supplier", "over_days"], "采购明细列正确")
        t2 = rd.table(r2, plan=None)
        chk("| 物料 | 供应商 | 超期 |" in t2, f"采购表头带供应商，实际：{t2.splitlines()[0]}")
        chk("无锡腾丰液压" in t2, "供应商名字出来了（不是 supplier_id）")
        chk("2026-060A 液压件0" in t2, "多项目时项目号并进物料名，不额外占一列")

        # ── ⑦ 单个项目精确查也要有明细 ──
        r3 = await te.get_project(db, u, "2026-060A")
        chk("matched_count" not in r3, "精确编号仍然只出一个项目")
        chk(isinstance(r3.get("items"), list) and r3["items"], "单项目一样带 items")
        t3 = rd.table(r3, plan=None)
        chk(t3.startswith("|"), "单项目也渲染成表格")
        chk("2026-060A" not in t3, "只有一个项目时不用每行重复项目号，省一列宽度")

    await engine.dispose()
    print("\n" + ("FAILED: " + "; ".join(FAIL) if FAIL else "ALL PASS"))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
