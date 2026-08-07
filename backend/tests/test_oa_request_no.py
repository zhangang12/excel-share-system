"""OA 单号生成 —— 2026-08-07 生产事故：当天所有 OA 申请都提不了。

现场：当天有 OA20260807-001 / -003 / -004（002 被删过）。
原来的算法是 `count(distinct request_no) + 1` = 3+1 = 004 → **004 已经存在**
→ 唯一约束冲突 → 前端弹「数据已存在，不能重复」。
不是付款申请的问题，是**当天任何人、任何单据类型都提不了**。

要锁死的：
  1. 号段中间缺号（删过单）时，新号要接在**最大号之后**，不能去填空缺
  2. 空缺处的号绝不能被重新分配——历史单据的号必须唯一且不复用
  3. 跨天重新从 001 开始
  4. 序号超过 999 后仍然递增（字符串 max 会把 "1000" 排在 "999" 前面）
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="oano")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from datetime import date
from sqlalchemy import select
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app.routers.oa_router import _next_oa_no
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

    today = date.today().strftime("%Y%m%d")
    pre = f"OA{today}-"

    async with SessionLocal() as db:
        chk(await _next_oa_no(db) == f"{pre}001", "3) 当天第一张是 001")

        # 复现生产现场：001 / 003 / 004（002 被删过）
        dept = (await db.execute(select(models.Department))).scalars().first()
        for n in ("001", "003", "004"):
            db.add(models.OaRequest(request_no=f"{pre}{n}", category="business",
                                    doc_type="other_biz", department_id=dept.id,
                                    requester_id=1, status="pending"))
        await db.commit()

        nxt = await _next_oa_no(db)
        chk(nxt == f"{pre}005", f"1) 缺号时接在最大号之后（005，不是 004）: {nxt}")
        exist = {r for (r,) in (await db.execute(select(models.OaRequest.request_no))).all()}
        chk(nxt not in exist, "2) 生成的号没跟已有的撞")
        chk(f"{pre}002" not in exist and nxt != f"{pre}002",
            "2) 不去填 002 那个空缺（号不复用）")

        # 4) 超过 999
        db.add(models.OaRequest(request_no=f"{pre}999", category="business",
                                doc_type="other_biz", department_id=dept.id,
                                requester_id=1, status="pending"))
        await db.commit()
        chk(await _next_oa_no(db) == f"{pre}1000", f"4) 999 之后是 1000: {await _next_oa_no(db)}")

        db.add(models.OaRequest(request_no=f"{pre}1000", category="business",
                                doc_type="other_biz", department_id=dept.id,
                                requester_id=1, status="pending"))
        await db.commit()
        n2 = await _next_oa_no(db)
        chk(n2 == f"{pre}1001",
            f"4) 1000 之后是 1001（字符串比较会错判成 999 更大）: {n2}")

        # 3) 别的日期的号不干扰今天
        db.add(models.OaRequest(request_no="OA20250101-888", category="business",
                                doc_type="other_biz", department_id=dept.id,
                                requester_id=1, status="pending"))
        await db.commit()
        chk(await _next_oa_no(db) == f"{pre}1001", "3) 其它日期的号不参与当天计算")

        # 手工改过的怪号不能把生成卡死
        db.add(models.OaRequest(request_no=f"{pre}手工补单", category="business",
                                doc_type="other_biz", department_id=dept.id,
                                requester_id=1, status="pending"))
        await db.commit()
        try:
            n3 = await _next_oa_no(db)
            chk(n3 == f"{pre}1001", f"怪号被跳过，不影响生成: {n3}")
        except Exception as e:
            chk(False, f"怪号把生成搞崩了: {e}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
