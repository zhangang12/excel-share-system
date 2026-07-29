"""🆕 #325 批量出库（POST /api/wh/txns/batch-out）测试：
1. 未登录 401；无仓库写权限角色 403。
2. 多行成功：每行各生成一条 CK 出库流水，库存扣减正确，单价随物料参考单价自动算金额。
3. 任一行超现存 → 400 报行号+物料名，整体回滚（库存/流水不变）。
4. 同一物料多行累计超现存 → 400 报行号（按剩余量递减校验）。
5. 非项目校验口径同单条：无项目未勾非项目 → 400；勾「非项目领用」+原因 → 200，
   source 默认「非项目领用」、party 带原因。
6. 行校验：qty<=0 → 400 报行号；物料不存在 → 400 报行号；空 lines → 422。
"""
import asyncio, os, sys, tempfile, shutil

tmp = tempfile.mkdtemp(prefix="wh_batch_out")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.data_migration import run_all, ensure_schema_columns

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        H = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'admin','password':'admin123'})).json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        for uname, role in [("w1", "warehouse"), ("s1", "sales")]:
            r = await c.post("/api/admin/users", headers=H,
                             json={"username": uname, "password": "pass123", "full_name": uname, "role_id": rid[role]})
            assert r.status_code == 200, r.text
        Hw1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'w1','password':'pass123'})).json()['access_token']}"}
        Hs1 = {"Authorization": f"Bearer {(await c.post('/api/auth/login', json={'username':'s1','password':'pass123'})).json()['access_token']}"}

        # ===== 建物料：A(10, 单价2) / B(5) / C(3) =====
        mids = {}
        for name, init, up in [("批量料A", 10, 2), ("批量料B", 5, None), ("批量料C", 3, None)]:
            body = {"name": name, "init_stock": init}
            if up is not None: body["unit_price"] = up
            r = await c.post("/api/wh/materials", headers=H, json=body)
            chk(r.status_code == 200, f"建物料 {name}: {r.status_code} {r.text[:120]}")
            mids[name] = r.json()["id"]
        A, B, C = mids["批量料A"], mids["批量料B"], mids["批量料C"]

        async def stock_of(mid):
            r = await c.get("/api/wh/materials", headers=H, params={"kw": "批量料"})
            for m in r.json()["materials"]:
                if m["id"] == mid: return m["stock"]
            return None

        async def out_txns():
            r = await c.get("/api/wh/txns", headers=H, params={"direction": "out"})
            return [t for t in r.json() if t["material_id"] in (A, B, C)]

        # ===== 1. 未登录 401 / 无写权限 403 =====
        body_np = {"biz_date": "2026-07-29", "non_project": True, "non_project_reason": "车间耗材",
                   "lines": [{"material_id": A, "qty": 1}]}
        r = await c.post("/api/wh/txns/batch-out", json=body_np)
        chk(r.status_code == 401, f"未登录 401: {r.status_code}")
        r = await c.post("/api/wh/txns/batch-out", headers=Hs1, json=body_np)
        chk(r.status_code == 403, f"sales 角色 403: {r.status_code}")

        # ===== 2. 多行成功：各生成流水 + 库存扣减 + 金额自动算 =====
        r = await c.post("/api/wh/txns/batch-out", headers=Hw1, json={
            "biz_date": "2026-07-29", "source": "领料出库", "party": "装配一组",
            "non_project": True, "non_project_reason": "车间耗材",
            "lines": [{"material_id": A, "qty": 3}, {"material_id": B, "qty": 2}]})
        chk(r.status_code == 200, f"批量出库 200: {r.status_code} {r.text[:200]}")
        chk(r.status_code == 200 and "2" in r.json().get("message", ""), f"提示含条数: {r.text[:120]}")
        chk(await stock_of(A) == 7, f"A 库存 10-3=7: {await stock_of(A)}")
        chk(await stock_of(B) == 3, f"B 库存 5-2=3: {await stock_of(B)}")
        ts = await out_txns()
        chk(len(ts) == 2, f"生成 2 条出库流水: {len(ts)}")
        if len(ts) == 2:
            refs = sorted(t["ref_no"] for t in ts)
            chk(all(x.startswith("CK20260729-") for x in refs) and len(set(refs)) == 2,
                f"单号 CK 前缀且互不相同: {refs}")
            ta = next(t for t in ts if t["material_id"] == A)
            chk(ta["unit_price"] == 2 and ta["amount"] == 6, f"A 单价/金额自动算: {ta['unit_price']}/{ta['amount']}")
            chk(ta["party"] and "装配一组" in ta["party"] and "非项目:车间耗材" in ta["party"],
                f"party 领用方+非项目原因: {ta['party']}")
            chk(ta["source"] == "领料出库", f"source 用途: {ta['source']}")
            chk(all(t["biz_date"] == "2026-07-29" for t in ts), "共用业务日期")

        # ===== 3. 任一行超现存 → 400 报行号+物料名，整体回滚 =====
        n0 = len(await out_txns())
        r = await c.post("/api/wh/txns/batch-out", headers=Hw1, json={
            "biz_date": "2026-07-29", "non_project": True, "non_project_reason": "车间耗材",
            "lines": [{"material_id": A, "qty": 2}, {"material_id": B, "qty": 99}]})
        chk(r.status_code == 400, f"超现存 400: {r.status_code} {r.text[:200]}")
        d = r.json().get("detail", "")
        chk("第2行" in d and "批量料B" in d and "99" in d and "3" in d, f"报错指明行+物料+数量+现存: {d}")
        chk(await stock_of(A) == 7 and await stock_of(B) == 3, "整体回滚：库存不变")
        chk(len(await out_txns()) == n0, "整体回滚：流水不新增")

        # ===== 4. 同一物料多行累计超现存 → 400 报行号 =====
        r = await c.post("/api/wh/txns/batch-out", headers=Hw1, json={
            "biz_date": "2026-07-29", "non_project": True, "non_project_reason": "车间耗材",
            "lines": [{"material_id": C, "qty": 2}, {"material_id": C, "qty": 2}]})
        chk(r.status_code == 400, f"同物料累计超 400: {r.status_code} {r.text[:200]}")
        d = r.json().get("detail", "")
        chk("第2行" in d and "批量料C" in d, f"累计校验报第2行: {d}")
        chk(await stock_of(C) == 3, "累计超回滚：C 库存不变")

        # ===== 5. 非项目校验口径同单条 =====
        r = await c.post("/api/wh/txns/batch-out", headers=Hw1, json={
            "biz_date": "2026-07-29", "lines": [{"material_id": A, "qty": 1}]})
        chk(r.status_code == 400 and "领用项目" in r.json().get("detail", ""),
            f"无项目未勾非项目 400: {r.status_code} {r.text[:160]}")
        r = await c.post("/api/wh/txns/batch-out", headers=Hw1, json={
            "biz_date": "2026-07-29", "non_project": True, "non_project_reason": "工具磨损",
            "lines": [{"material_id": C, "qty": 1}]})
        chk(r.status_code == 200, f"勾非项目+原因 200: {r.status_code} {r.text[:160]}")
        ts = await out_txns()
        tc = next((t for t in ts if t["material_id"] == C), None)
        chk(tc and tc["source"] == "非项目领用", f"source 默认非项目领用: {tc and tc['source']}")
        chk(tc and "非项目:工具磨损" in (tc["party"] or ""), f"party 带原因: {tc and tc['party']}")

        # ===== 6. 行校验 =====
        r = await c.post("/api/wh/txns/batch-out", headers=Hw1, json={
            "biz_date": "2026-07-29", "non_project": True, "non_project_reason": "x",
            "lines": [{"material_id": A, "qty": 1}, {"material_id": B, "qty": 0}]})
        chk(r.status_code == 400 and "第2行" in r.json().get("detail", "") and "数量" in r.json().get("detail", ""),
            f"qty=0 报第2行: {r.status_code} {r.text[:160]}")
        r = await c.post("/api/wh/txns/batch-out", headers=Hw1, json={
            "biz_date": "2026-07-29", "non_project": True, "non_project_reason": "x",
            "lines": [{"material_id": A, "qty": 1}, {"material_id": 999999, "qty": 1}]})
        chk(r.status_code == 400 and "第2行" in r.json().get("detail", "") and "不存在" in r.json().get("detail", ""),
            f"物料不存在报第2行: {r.status_code} {r.text[:160]}")
        r = await c.post("/api/wh/txns/batch-out", headers=Hw1, json={
            "biz_date": "2026-07-29", "non_project": True, "non_project_reason": "x", "lines": []})
        chk(r.status_code == 422, f"空 lines 422: {r.status_code}")
        chk(await stock_of(A) == 7 and await stock_of(B) == 3, "行校验失败均回滚：库存不变")

    await engine.dispose()
    print("PASSED" if not FAIL else f"{len(FAIL)} FAILURES")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
