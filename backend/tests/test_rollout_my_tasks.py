"""🆕 2026-09-23 「我手上的活」工具 + 全角色门户默认卡。

这次要把智能体开给全部 26 个账号，其中人数最多的是装配、钣金、封板三个组。
排查时发现他们打开助手基本是空的：

  · 他们的任务派在 `produce_group_tasks` 上，`dept_orders.worker_id` 对他们**永远是 NULL**
    （生产主管先接部门单、再分派到组）。而助手唯一沾边的工具 `overdue_orders`
    查的就是 dept_orders.worker_id —— 装配工问「我今天要干什么」永远查到 0 条，
    助手回「你没有待办 ✅」。**数据是有的，只是没人去取。**
  · 门户默认卡：这些岗位全落到 _FALLBACK（晨报 + 部门逾期 + 采购超期），
    后两张对他们要么恒 0、要么没权限被过滤 —— 第一次打开就是一屏没用的东西。

本文件钉死：装配工能查到自己的活、查不到别人的活、门户第一张卡就是它，
以及「每个真实角色都有一组默认卡」这条不变量（写错角色 code 不会报错，只会静默落兜底）。
"""
import asyncio, os, sys, tempfile
from datetime import date, timedelta

tmp = tempfile.mkdtemp(prefix="rolltask")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed, ROLES
from app.data_migration import run_all, ensure_schema_columns
from app import models
from app.agent import portal, tools_tasks
from app.routers.agent_router import _allowed_tools

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


async def _u(uname):
    async with SessionLocal() as db:
        return (await db.execute(select(models.User)
                                 .where(models.User.username == uname))).scalars().unique().one()


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mkuser(name, codes, menus=None):
            rr = await c.post("/api/admin/users", headers=H, json={
                "username": name, "password": "pass123", "full_name": name,
                "role_ids": [rid[x] for x in codes]})
            assert rr.status_code == 200, rr.text
            uid = rr.json()["id"]
            if menus is not None:
                m = await c.put(f"/api/admin/users/{uid}/menus", headers=H, json={"menus": menus})
                assert m.status_code == 200, m.text
            return uid

        PROD = ["catalog", "list", "produce", "messages"]
        asm_id = await mkuser("t_asm", ["assembler"], PROD)
        asm2_id = await mkuser("t_asm2", ["assembler"], PROD)
        des_id = await mkuser("t_des", ["designer"], ["catalog", "list", "design", "messages"])
        u_asm, u_asm2, u_des = await _u("t_asm"), await _u("t_asm2"), await _u("t_des")

        # ── 造活：一个项目 → 生产部门单 → 分派给装配工 t_asm ──
        pid = (await c.post("/api/projects", headers=H,
                            json={"code": "2026-950", "name": "装配活验证台"})).json()["id"]
        y = (date.today() - timedelta(days=3)).isoformat()      # 已超期 3 天
        t = (date.today() + timedelta(days=5)).isoformat()
        async with SessionLocal() as db:
            o = models.DeptOrder(project_id=pid, dept="produce", status="in_progress",
                                 due_date=y)
            db.add(o)
            await db.flush()
            db.add(models.ProduceGroupTask(order_id=o.id, project_id=pid, group="assembly",
                                           status="dispatched", worker_id=asm_id, due_date=y))
            o2 = models.DeptOrder(project_id=pid, dept="design", status="in_progress",
                                  due_date=t, worker_id=des_id)
            db.add(o2)
            await db.commit()

        print("\n=== 1. 装配工查得到自己的活（改之前恒 0 条）===")
        async with SessionLocal() as db:
            got = await tools_tasks.my_tasks(db, u_asm)
        chk(got["count"] == 1,
            f"装配工「我手上的活」查到 1 条 —— 改之前 overdue_orders 查 dept_orders.worker_id，"
            f"对生产组永远是 NULL，答「你没有待办 ✅」→ {got['count']}")
        if got["count"]:
            it = got["items"][0]
            chk(it["project"] == "2026-950" and it["what"] == "装配",
                f"项目编号和干什么都对 → {it['project']} / {it['what']}")
            chk(it["days_left"] == -3, f"超期天数是负数 → {it['days_left']}")
        chk(got["overdue"] == 1, f"超期计数对 → {got['overdue']}")

        print("\n=== 2. 只看自己的，别人的一条都不给 ===")
        async with SessionLocal() as db:
            got2 = await tools_tasks.my_tasks(db, u_asm2)
        chk(got2["count"] == 0, f"另一个装配工：0 条 → {got2['count']}")
        chk(got2.get("hint"), "而且明说「没有派给你的」，不留空让模型自己编一句含糊的话")

        print("\n=== 3. 部门单那一半没被改坏 ===")
        async with SessionLocal() as db:
            gotd = await tools_tasks.my_tasks(db, u_des)
        chk(gotd["count"] == 1 and gotd["items"][0]["what"] == "设计",
            f"设计师查到自己的部门单 → {gotd['count']} / "
            f"{gotd['items'][0]['what'] if gotd['count'] else '-'}")
        chk(gotd["overdue"] == 0, f"没超期就不报超期 → {gotd['overdue']}")

        print("\n=== 4. 完成/作废的不再出现 ===")
        async with SessionLocal() as db:
            t_ = (await db.execute(select(models.ProduceGroupTask))).scalars().first()
            t_.status = "done"
            await db.commit()
        async with SessionLocal() as db:
            chk((await tools_tasks.my_tasks(db, u_asm))["count"] == 0,
                "标记完成后就不在「手上的活」里了")

        print("\n=== 5. 工具不设门控：只有 produce 菜单的人也拿得到 ===")
        chk("my_tasks" in _allowed_tools(u_asm),
            "装配工的工具集里有 my_tasks（他只有 produce 一个菜单，"
            "加菜单门控等于把最需要的人挡在外面）")
        chk("my_tasks" in _allowed_tools(await _u("admin")), "管理层也有")

        print("\n=== 6. 门户默认卡 ===")
        rr = await c.get("/api/agent/portal", headers={
            "Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 't_asm', 'password': 'pass123'})).json()['access_token']}"})
        chk(rr.status_code == 200, f"装配工取门户 {rr.status_code}")
        if rr.status_code == 200:
            tiles = [x["key"] for x in (rr.json().get("tiles") or [])]
            chk(tiles and tiles[0] == "my_tasks",
                f"第一张卡就是「我手上的活」——以前这里是晨报+部门逾期+采购超期，"
                f"后两张对他恒 0 或没权限 → {tiles}")
            chk("po_arrival_overdue" not in tiles, "不给他摆点不动的采购卡")

        print("\n=== 6b. 点这张卡走「直答」，不经 LLM ===")
        # 把活改回未完成（第 4 段把它标成 done 了）
        async with SessionLocal() as db:
            t_ = (await db.execute(select(models.ProduceGroupTask))).scalars().first()
            t_.status = "dispatched"
            await db.commit()
        Ha = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username': 't_asm', 'password': 'pass123'})).json()['access_token']}"}
        rr = await c.post("/api/agent/tool", headers=Ha, json={"tool": "my_tasks"})
        chk(rr.status_code == 200, f"直答端点 200 → {rr.status_code} {rr.text[:80]}")
        if rr.status_code == 200:
            b = rr.json()
            chk(b.get("direct") is True, "走的是直答（门户卡答案是确定性的，让模型再想一遍纯属浪费）")
            chk("2026-950" in b["reply"] and "超期" in b["reply"],
                f"答案里有项目号和超期提示 → {b['reply'][:90]}")
            chk(b.get("duration_ms", 9999) < 1000,
                f"几十毫秒级，不是十几秒 → {b.get('duration_ms')}ms")

        print("\n=== 6c. 规则降级也要认得出来（超日限/并发满都会落到这条路）===")
        for q in ("我今天要干什么", "我手上还有几台", "派给我的活"):
            rr = await c.post("/api/agent/chat", headers=Ha, json={"message": q})
            ok = rr.status_code == 200 and "2026-950" in rr.json().get("reply", "")
            chk(ok, f"问「{q}」能答出自己的活，不是甩一段能力清单 → "
                    f"{rr.json().get('reply', '')[:50] if rr.status_code == 200 else rr.status_code}")
        rr = await c.post("/api/agent/chat", headers=Ha, json={"message": "我有没有超期的活"})
        chk(rr.status_code == 200 and rr.json().get("sources") == ["我手上的活"],
            f"「我有没有超期的」要落到**自己的活**，不是全公司的部门逾期表 → "
            f"{rr.json().get('sources')}")

        print("\n=== 7. 不变量：每个真实角色都要有一组默认卡 ===")
        real = {x[0] for x in ROLES}
        ghost = sorted(set(portal._DEFAULTS) - real)
        chk(not ghost,
            f"默认组里不能出现**不存在的角色 code** —— 写错了不报错，只会静默落兜底 → {ghost}")
        chk(not [x for x in portal._ROLE_ORDER if x not in real],
            f"查找顺序里同样不能有幽灵 code → "
            f"{[x for x in portal._ROLE_ORDER if x not in real]}")
        miss = sorted(real - set(portal._DEFAULTS))
        chk(not miss, f"每个角色都要配到，别再有人落兜底 → {miss}")
        keys = {x["key"] for x in portal.CATALOG}
        bad = sorted({t for v in portal._DEFAULTS.values() for t in v if t not in keys})
        chk(not bad, f"默认组引用的卡都要在目录里 → {bad}")


asyncio.run(main())
print("\n" + ("全部通过 ✅" if not FAIL else f"{len(FAIL)} 条失败 ❌"))
for m in FAIL:
    print(" -", m)
sys.exit(1 if FAIL else 0)
