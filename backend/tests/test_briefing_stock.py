"""🆕 首页「今天该管的」里的仓库缺料条目：带规格 + 点得动。

依据 2026-08-18 用户发来的手机截图：3 个名额里有 2 个是
「丝攻 库存 0，低于安全线 10」——**一模一样**，¥22 万那条应收差点被挤下去。

查了生产库才知道不是重复数据：`wh_materials` 里「丝攻」有 6 条，
分别是 M6/M8/M10/M12/M18/M22，**是 6 个不同的料**。
简报只显示 `name`、把 `spec` 丢了，所以看起来一样。

同一条截图还暴露：「补货 ›」点了什么也不发生（`card` 是 None，
前端跳 `/chat?card=null`，而 H5ChatView 要求非空串才走卡片通道）。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="briefstock")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from app.database import engine, SessionLocal, Base
from app import models
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.agent import briefing

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
        # 照抄生产：同名不同规格的 6 条丝攻
        for spec in ("M6", "M8", "M10", "M12", "M18", "M22"):
            db.add(models.WhMaterial(code=f"SG-{spec}", name="丝攻", spec=spec,
                                     unit="个", safety_stock=10, init_stock=0))
        # 一条没有规格的，确认不会渲染出多余空格
        db.add(models.WhMaterial(code="X-1", name="扎带", spec=None,
                                 unit="包", safety_stock=5, init_stock=1))
        await db.commit()

    async with SessionLocal() as db:
        me = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalars().first()
        items = await briefing._stock_items(db, me)
        chk(len(items) == 7, f"7 条缺料都识别到了：{len(items)}")

        titles = [i["title"] for i in items]
        chk(len(set(titles)) == len(titles),
            f"**每条标题都不一样**（以前 6 条丝攻长得一模一样）：{titles[:3]}")
        chk(any("丝攻 M6" in t for t in titles), f"带上了规格：{titles[0]}")
        chk(not any("丝攻 库存" in t for t in titles), "不再有没规格的裸「丝攻」")

        zd = next(i for i in items if "扎带" in i["title"])
        chk(zd["title"].startswith("扎带 库存"),
            f"没有规格的料不留多余空格：{zd['title']!r}")

        # ── 点得动 ──
        for i in items:
            chk(bool(i.get("ask")), f"每条都带了点击后要问的话：{i.get('ask')!r}"
                if not i.get("ask") else "每条都带了点击后要问的话")
            break
        chk("丝攻 M6" in items[0]["ask"] or "丝攻" in items[0]["ask"],
            f"问句里带着料名：{items[0]['ask']!r}")
        chk(all(i["card"] is None for i in items),
            "card 仍是 None——缺料没有卡片通道，靠 ask 走对话")

    await engine.dispose()
    print("\n" + ("FAILED: " + "; ".join(FAIL) if FAIL else "ALL PASS"))
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
