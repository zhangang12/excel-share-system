"""🆕 2026-09-28 反馈#438（赵仁辉）：财务「库存 / 成本」→ 项目材料成本「没按顺序排序」。

这是他第二次提同一件事。8/24 的 #408（「未排序」）当时只加了「点表头排序」，默认仍按
材料成本从大到小 —— 他要的是**一打开就按项目编号**，跟销售部台账一个顺序，好对着找。

本文件钉死：
  · /wh/project-cost 默认按项目编号的**自然顺序**返回（年 → 序号 → 字母后缀，非标准编号排最后）；
  · 这个顺序与销售台账用的是**同一个排序键**（sales_router.code_sort_key），两边不会各排各的；
  · 071A/071B、041M补、备06 这些生产上真实存在的编号不排乱。

成本本身怎么算不在这里测（有别的测试管），这里把 _project_cost_map 换成固定数据，只看顺序。
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb438")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns
from app import models
from app.routers import warehouse_router as wr
from app.routers.sales_router import code_sort_key, _ledger_sort_key

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


# 故意打乱录入顺序；成本也故意跟编号反着来 —— 按金额排和按编号排结果一定不同，
# 这样「还在按金额排」会被当场抓出来
CODES_COST = [
    ("2026-071B", 31861.62),
    ("2026-备06", 18000.00),
    ("2026-059A", 85556.75),
    ("2026-063", 37004.47),
    ("2026-041M补", 21474.69),
    ("2026-071A", 77799.18),
    ("2025-120", 5000.00),
    ("2026-059B", 25463.30),
    ("2026-9", 1000.00),        # 序号是数值比：9 要排在 041 前面，字符串比会排到最后
]
EXPECT = ["2025-120", "2026-9", "2026-059A", "2026-059B", "2026-063",
          "2026-071A", "2026-071B", "2026-041M补", "2026-备06"]


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)
        pids = {}
        for code, _ in CODES_COST:
            p = models.Project(code=code, name=f"{code} 设备", status="进行中")
            db.add(p)
            await db.flush()
            pids[p.id] = code
        await db.commit()
    cost_by_code = dict(CODES_COST)

    async def fake_map(db):
        return {pid: cost_by_code[code] for pid, code in pids.items()}, {"unassigned": 0}

    wr._project_cost_map = fake_map

    print("\n=== 1. 排序键本身 ===")
    got = sorted([c for c, _ in CODES_COST], key=code_sort_key)
    chk(got == EXPECT, f"自然顺序：年 → 序号(数值) → 后缀，非标准编号在最后 → {got}")

    class _P:
        def __init__(self, code): self.code = code

    class _L:
        def __init__(self, code): self.project = _P(code)

    chk([_ledger_sort_key(_L(c)) for c in EXPECT] == [code_sort_key(c) for c in EXPECT],
        "销售台账的排序键就是它（抽公共函数时没把台账的顺序改掉）")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print("\n=== 2. 接口默认顺序 = 按项目编号 ===")
        r = await c.get("/api/wh/project-cost", headers=H)
        chk(r.status_code == 200, f"/wh/project-cost → {r.status_code} {r.text[:80]}")
        codes = [x["code"] for x in r.json()["rows"]]
        chk(codes == EXPECT,
            f"一打开就是编号顺序（改之前是按材料成本从大到小，059A 排第一）→ {codes}")
        by_cost = [c for c, _ in sorted(CODES_COST, key=lambda x: -x[1])]
        chk(codes != by_cost, "确实不再按金额排")
        costs = {x["code"]: x["cost"] for x in r.json()["rows"]}
        chk(costs == {k: round(v, 2) for k, v in cost_by_code.items()},
            "只改了顺序，每个项目的金额一分没动")


asyncio.run(main())
print("\n" + ("全部通过 ✅" if not FAIL else f"{len(FAIL)} 条失败 ❌"))
for m in FAIL:
    print(" -", m)
sys.exit(1 if FAIL else 0)
