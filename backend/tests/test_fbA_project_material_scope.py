"""反馈 A 组（#373/#374/#375/#376/#377/#388/#389/#390）：物料按有没有项目编号一刀切开。

业务要求（超级管理员 2026-08-12）：
  #373 项目材料成本要**包括所有收货的物料成本，包括没有出库的**
  #374/#388 库存总览、库存金额只显示**没有关联项目编号**的物料
  #375 问：合并收货会把不同项目编号的物料分派到对应项目吗？（原答案：不会，还会全覆盖）
  #376 合并收货要逐行带出项目编号并自动分派
  #377 库位存量物料能调到项目物料中转库
  #389/#390 项目材料成本 / 项目毛利要能展开看明细

生产数据（改之前）：收货 ¥421,444，旧口径「领料出库×均价」只认出 ¥163,697——
六成成本在系统里蒸发；同时库存金额 ¥148,099 里有 ¥116,718(79%) 其实是已经名花有主的项目料。

这个文件锁死的是**口径的自洽性**，不是某个数字：
  A. 每一笔入库金额有且只有一个去处（项目成本 / 通用库存 / 未归集），不重算不漏
  B. 项目物料的领料出库**不再产生新成本**——它的钱在收货那一刻已经算过了。
     这条是整个改动最容易写错的地方：顺手把旧的「领料腿」留着，同一批料就算两遍。
  C. 毛利榜的材料腿 == 项目材料成本页的数（两处共用 _project_cost_map）
  D. 展开的明细逐行加总 == 外面那个合计
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fbA")
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
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


def near(a, b, tol=0.02):
    return abs((a or 0) - (b or 0)) <= tol


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db)
        await run_all(db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # ---------- 场景搭建 ----------
        # 两个项目 + 一个供应商
        pid = {}
        for code, name in [("T-001", "甲项目"), ("T-002", "乙项目")]:
            rr = await c.post("/api/projects", headers=H, json={"code": code, "name": name})
            chk(rr.status_code == 200, f"建项目 {code}: {rr.status_code} {rr.text[:80]}")
            pid[code] = rr.json()["id"]
        rr = await c.post("/api/purchase-mgmt/suppliers", headers=H, json={"name": "测试供应商"})
        chk(rr.status_code == 200, f"建供应商: {rr.status_code} {rr.text[:80]}")
        sup = rr.json()["id"]

        async def mk_item(name, qty, price, project_code=None):
            rr = await c.post("/api/purchase-mgmt/items", headers=H, json={
                "supplier_id": sup, "item_name": name, "qty": qty,
                "unit_price": price, "project_code": project_code})
            assert rr.status_code == 200, rr.text
            return rr.json()["id"]

        # 甲项目：收货 10 × 100 = 1000（挂项目编号 → 项目物料）
        i1 = await mk_item("项目料A", 10, 100, "T-001")
        rr = await c.put(f"/api/purchase-mgmt/items/{i1}/receive", headers=H,
                         json={"arrival_date": "2026-08-01", "unit_price": 100,
                               "received_amount": 1000})
        chk(rr.status_code == 200, f"甲项目收货: {rr.status_code} {rr.text[:90]}")
        # 通用料：收货 20 × 50 = 1000（不挂项目 → 通用物料）
        i2 = await mk_item("通用料B", 20, 50, None)
        rr = await c.put(f"/api/purchase-mgmt/items/{i2}/receive", headers=H,
                         json={"arrival_date": "2026-08-01", "unit_price": 50,
                               "received_amount": 1000})
        chk(rr.status_code == 200, f"通用料收货: {rr.status_code} {rr.text[:90]}")

        mats = (await c.get("/api/wh/materials", headers=H)).json()["materials"]
        mid = {m["name"]: m["id"] for m in mats}
        pmat = {m["name"]: m["is_project_material"] for m in mats}

        # ---------- #373/#374：项目物料 vs 通用物料 ----------
        chk(pmat.get("项目料A") is True, f"#374 挂项目收的料 = 项目物料: {pmat.get('项目料A')}")
        chk(pmat.get("通用料B") is False, f"#374 没挂项目收的料 = 通用物料: {pmat.get('通用料B')}")
        gen = (await c.get("/api/wh/materials", headers=H, params={"scope": "general"})).json()
        names = {m["name"] for m in gen["materials"]}
        chk("通用料B" in names and "项目料A" not in names,
            f"#374 scope=general 只剩通用物料: {sorted(names)}")
        allm = (await c.get("/api/wh/materials", headers=H)).json()["materials"]
        chk(any(m["name"] == "项目料A" for m in allm),
            "⚠️ 默认 scope 必须是 all——这个接口还喂着物料主数据和出库选料，"
            "默认过滤掉项目物料会让它们在主数据里凭空消失")

        # ---------- #388：库存金额只算通用物料 ----------
        iv = (await c.get("/api/wh/inventory-value", headers=H)).json()
        ivn = {r["name"] for r in iv["rows"]}
        chk("项目料A" not in ivn, f"#388 库存金额里没有项目物料: {sorted(ivn)}")
        chk(near(iv["total_value"], 1000), f"#388 库存金额=通用料 20×50=1000: {iv['total_value']}")
        chk(near(iv["excluded_value"], 1000),
            f"#388 被排除的项目物料金额要单报出来(不能悄悄消失): {iv.get('excluded_value')}")

        # ---------- #373：项目材料成本 = 收货即计 ----------
        pc = (await c.get("/api/wh/project-cost", headers=H)).json()
        cost = {r["code"]: r["cost"] for r in pc["rows"]}
        chk(near(cost.get("T-001"), 1000),
            f"#373 甲项目一次没领料，成本就该是收货的 1000: {cost.get('T-001')}")

        # ---------- B. 项目物料领料出库不再产生新成本（最易写错的一条） ----------
        rr = await c.post("/api/wh/txns", headers=H, json={
            "material_id": mid["项目料A"], "biz_date": "2026-08-02", "direction": "out",
            "qty": 4, "project_id": pid["T-001"], "source": "领料出库"})
        chk(rr.status_code == 200, f"甲项目领料 4 个: {rr.status_code} {rr.text[:90]}")
        pc2 = (await c.get("/api/wh/project-cost", headers=H)).json()
        cost2 = {r["code"]: r["cost"] for r in pc2["rows"]}
        chk(near(cost2.get("T-001"), 1000),
            f"B. 项目物料领料后成本**不变**(钱在收货时已计,再计就是同一批料算两遍): {cost2.get('T-001')}")

        # ---------- 通用物料领料出库 → 才产生成本 ----------
        rr = await c.post("/api/wh/txns", headers=H, json={
            "material_id": mid["通用料B"], "biz_date": "2026-08-02", "direction": "out",
            "qty": 6, "project_id": pid["T-002"], "source": "领料出库"})
        chk(rr.status_code == 200, f"乙项目从通用库存领 6 个: {rr.status_code} {rr.text[:90]}")
        pc3 = (await c.get("/api/wh/project-cost", headers=H)).json()
        cost3 = {r["code"]: r["cost"] for r in pc3["rows"]}
        chk(near(cost3.get("T-002"), 300),
            f"通用物料领用 6×50=300 才算乙项目成本: {cost3.get('T-002')}")
        iv3 = (await c.get("/api/wh/inventory-value", headers=H)).json()
        chk(near(iv3["total_value"], 700),
            f"领走 6 个后通用库存金额 = 14×50 = 700: {iv3['total_value']}")

        # ---------- A. 配平：入库总额 = 项目成本 + 通用库存 + 未归集 ----------
        total_in = 2000.0
        booked = sum(r["cost"] for r in pc3["rows"]) + iv3["total_value"] + pc3.get("unassigned", 0)
        chk(near(booked, total_in),
            f"A. 每笔入库钱只落一处：项目成本{sum(r['cost'] for r in pc3['rows'])} + "
            f"通用库存{iv3['total_value']} + 未归集{pc3.get('unassigned')} = {booked}，应等于入库总额 {total_in}")

        # ---------- D. #389 明细展开加总 == 合计 ----------
        d = (await c.get(f"/api/wh/project-cost/{pid['T-001']}/detail", headers=H)).json()
        chk(near(d["total"], 1000), f"#389 甲项目明细合计 = 1000: {d['total']}")
        chk([r["leg"] for r in d["rows"]] == ["收货"],
            f"#389 甲项目只有收货腿(领料的是项目物料,不重复列成本): {[r['leg'] for r in d['rows']]}")
        d2 = (await c.get(f"/api/wh/project-cost/{pid['T-002']}/detail", headers=H)).json()
        chk(near(d2["total"], 300) and [r["leg"] for r in d2["rows"]] == ["领料"],
            f"#389 乙项目只有领料腿 300: {d2['total']} {[r['leg'] for r in d2['rows']]}")

        # ---------- C. 毛利榜材料腿 == 项目材料成本页 ----------
        pnl = (await c.get("/api/reports/project-pnl", headers=H)).json()
        pnl_mat = {r["code"]: r["mat_cost"] for r in pnl["rows"]}
        chk(near(pnl_mat.get("T-001"), cost3.get("T-001")) and near(pnl_mat.get("T-002"), cost3.get("T-002")),
            f"C. 毛利榜材料腿与项目材料成本页同数: 榜{pnl_mat} vs 成本页{cost3}")

        # ---------- #390 毛利明细展开 ----------
        pd = (await c.get(f"/api/reports/project-pnl/{pid['T-001']}/detail", headers=H)).json()
        chk(near(pd["by_leg"].get("材料"), 1000),
            f"#390 毛利展开的材料腿 = 1000: {pd.get('by_leg')}")
        chk(near(pd["total"], sum(r["amount"] or 0 for r in pd["rows"])),
            "#390 展开逐行加总 == 展开合计")

        # ---------- #376 合并收货逐行分派 ----------
        a = await mk_item("合并料甲", 1, 10, "T-001")
        b = await mk_item("合并料乙", 1, 20, "T-002")
        cc = await mk_item("合并料丙", 1, 30, None)
        rr = await c.post("/api/purchase-mgmt/items/receive-batch", headers=H, json={
            "item_ids": [a, b, cc], "arrival_date": "2026-08-03",
            "project_code": "T-001",         # 整批兜底：只该填空的那一行
            "lines": [{"item_id": a, "unit_price": 10, "received_amount": 10},
                      {"item_id": b, "unit_price": 20, "received_amount": 20},
                      {"item_id": cc, "unit_price": 30, "received_amount": 30}]})
        chk(rr.status_code == 200, f"#376 合并收货: {rr.status_code} {rr.text[:120]}")
        got = {x["item_name"]: x["project_code"] for x in rr.json()}
        chk(got.get("合并料乙") == "T-002",
            f"#376 各行原有的项目编号**不能被整批编号覆盖**（原来会全抹成一个）: {got.get('合并料乙')}")
        chk(got.get("合并料甲") == "T-001", f"#376 本来就是 T-001 的保持不变: {got.get('合并料甲')}")
        chk(got.get("合并料丙") == "T-001", f"#376 空的那行由整批编号兜底填上: {got.get('合并料丙')}")
        pc4 = (await c.get("/api/wh/project-cost", headers=H)).json()
        cost4 = {r["code"]: r["cost"] for r in pc4["rows"]}
        chk(near(cost4.get("T-002"), 320),
            f"#376 分派对了，乙项目成本 300+20=320（抹平的话这 20 会跑到甲）: {cost4.get('T-002')}")

        # 逐行显式指定，优先级高于整批
        d1 = await mk_item("逐行料", 1, 40, None)
        rr = await c.post("/api/purchase-mgmt/items/receive-batch", headers=H, json={
            "item_ids": [d1], "arrival_date": "2026-08-04", "project_code": "T-001",
            "lines": [{"item_id": d1, "unit_price": 40, "received_amount": 40,
                       "project_code": "T-002"}]})
        chk(rr.status_code == 200 and rr.json()[0]["project_code"] == "T-002",
            f"#376 逐行填的编号优先于整批: {rr.json()[0]['project_code'] if rr.status_code == 200 else rr.text[:80]}")

        # ---------- #377 库位调项目物料 ----------
        i3 = await mk_item("待调料C", 8, 25, None)
        rr = await c.put(f"/api/purchase-mgmt/items/{i3}/receive", headers=H,
                         json={"arrival_date": "2026-08-05", "unit_price": 25,
                               "received_amount": 200})
        chk(rr.status_code == 200, f"待调料收货: {rr.status_code}")
        mats = (await c.get("/api/wh/materials", headers=H)).json()["materials"]
        mC = next(m for m in mats if m["name"] == "待调料C")
        chk(mC["is_project_material"] is False and mC["stock"] == 8, "#377 调之前：通用物料，现存 8")
        iv_before = (await c.get("/api/wh/inventory-value", headers=H)).json()["total_value"]

        rr = await c.post("/api/wh/transfer-to-project", headers=H, json={
            "project_id": pid["T-002"], "biz_date": "2026-08-06",
            "lines": [{"material_id": mC["id"], "qty": 5}], "note": "中转"})
        chk(rr.status_code == 200, f"#377 调至项目物料: {rr.status_code} {rr.text[:110]}")
        mats = (await c.get("/api/wh/materials", headers=H)).json()["materials"]
        mC2 = next(m for m in mats if m["name"] == "待调料C")
        chk(mC2["stock"] == 8, f"#377 净库存不变(一出一进): {mC2['stock']}")
        chk(mC2["is_project_material"] is True, "#377 调完变成项目物料")
        iv_after = (await c.get("/api/wh/inventory-value", headers=H)).json()
        chk("待调料C" not in {r["name"] for r in iv_after["rows"]}, "#377 调完退出库存金额")
        chk(near(iv_before - iv_after["total_value"], 200),
            f"#377 库存金额减掉整个物料 8×25=200: {iv_before} → {iv_after['total_value']}")
        pc5 = (await c.get("/api/wh/project-cost", headers=H)).json()
        cost5 = {r["code"]: r["cost"] for r in pc5["rows"]}
        # 乙项目此前 320（通用料B 领用 300 + 合并料乙 20）+ 逐行料 40 + 本次转入 125
        chk(near(cost5.get("T-002"), 320 + 40 + 125),
            f"#377 转入的 5×25=125 计入乙项目成本: {cost5.get('T-002')}")
        # 未归集 = 待调料C 的无编号收支净额 = 采购入库 200 − 调拨转出 125 = 75。
        # 只加不减的话，调拨的转入腿(+125)会凭空多出一份钱，配平立刻崩。
        chk(near(pc5.get("unassigned"), 75),
            f"#377 未归集要减掉调拨转出腿：200−125=75（只加不减 = 同一批料算两遍）: {pc5.get('unassigned')}")

        # 调拨超现存要拦
        rr = await c.post("/api/wh/transfer-to-project", headers=H, json={
            "project_id": pid["T-002"], "lines": [{"material_id": mC["id"], "qty": 999}]})
        chk(rr.status_code == 400, f"#377 超现存被拦: {rr.status_code}")

        # ---------- #377 回归：调拨**不能倒扣别的项目已经发生的领料成本** ----------
        # 本地实测踩过：「通用螺栓领 120 给甲 → 剩下 380 调给乙」时，如果腿B 的判据是
        # "物料是不是项目物料"，甲会因为这个物料被乙"转正"而凭空少掉 ¥144。
        # 正确判据是**通用池够不够扣**：甲的 120 从池子里扣得出来，就该算甲的。
        i4 = await mk_item("共用料D", 100, 3, None)
        rr = await c.put(f"/api/purchase-mgmt/items/{i4}/receive", headers=H,
                         json={"arrival_date": "2026-08-07", "unit_price": 3, "received_amount": 300})
        chk(rr.status_code == 200, f"共用料D 收货 100×3: {rr.status_code}")
        mD = next(m for m in (await c.get("/api/wh/materials", headers=H)).json()["materials"]
                  if m["name"] == "共用料D")
        rr = await c.post("/api/wh/txns", headers=H, json={
            "material_id": mD["id"], "biz_date": "2026-08-08", "direction": "out",
            "qty": 20, "project_id": pid["T-001"], "source": "领料出库"})
        chk(rr.status_code == 200, f"甲项目领 20 个共用料D: {rr.status_code} {rr.text[:80]}")
        before = {r["code"]: r["cost"] for r in (await c.get("/api/wh/project-cost", headers=H)).json()["rows"]}
        chk(near(before.get("T-001"), 1040 + 60), f"甲项目吃到 20×3=60: {before.get('T-001')}")
        # 把剩下的 80 调给乙——这一步会让共用料D 变成「项目物料」
        rr = await c.post("/api/wh/transfer-to-project", headers=H, json={
            "project_id": pid["T-002"], "biz_date": "2026-08-09",
            "lines": [{"material_id": mD["id"], "qty": 80}]})
        chk(rr.status_code == 200, f"把剩下 80 调给乙: {rr.status_code} {rr.text[:90]}")
        after = {r["code"]: r["cost"] for r in (await c.get("/api/wh/project-cost", headers=H)).json()["rows"]}
        chk(near(after.get("T-001"), before.get("T-001")),
            f"#377 回归：调拨给乙**不能**把甲已经领的 ¥60 倒扣掉: {before.get('T-001')} → {after.get('T-001')}")
        chk(near(after.get("T-002"), (before.get("T-002") or 0) + 240),
            f"#377 乙拿到转入的 80×3=240: {before.get('T-002')} → {after.get('T-002')}")

        # ---------- 反向回归：B 项目领 A 项目的料，不能凭空多算一份 ----------
        # 生产上这种"超领别人的料"有 ¥34,771，占超领总额 99%；见 _project_cost_core 腿B。
        i5 = await mk_item("专用料E", 10, 50, "T-001")
        rr = await c.put(f"/api/purchase-mgmt/items/{i5}/receive", headers=H,
                         json={"arrival_date": "2026-08-10", "unit_price": 50, "received_amount": 500})
        chk(rr.status_code == 200, f"甲专用料E 收货 10×50=500: {rr.status_code}")
        mE = next(m for m in (await c.get("/api/wh/materials", headers=H)).json()["materials"]
                  if m["name"] == "专用料E")
        base = {r["code"]: r["cost"] for r in (await c.get("/api/wh/project-cost", headers=H)).json()["rows"]}
        rr = await c.post("/api/wh/txns", headers=H, json={
            "material_id": mE["id"], "biz_date": "2026-08-11", "direction": "out",
            "qty": 3, "project_id": pid["T-002"], "source": "领料出库"})
        chk(rr.status_code == 200, f"乙项目领走甲的专用料E 3 个: {rr.status_code} {rr.text[:80]}")
        now = {r["code"]: r["cost"] for r in (await c.get("/api/wh/project-cost", headers=H)).json()["rows"]}
        chk(near(now.get("T-002"), base.get("T-002")),
            f"乙领甲的料，通用池是 0 → 不产生新成本（钱已在甲的收货腿里）: "
            f"{base.get('T-002')} → {now.get('T-002')}")
        chk(near(now.get("T-001"), base.get("T-001")),
            f"甲的成本也不动（料是它收的，钱本来就在它头上）: {base.get('T-001')} → {now.get('T-001')}")

        # ---------- 全量配平（收官，整个改动的总闸） ----------
        # 采购入库总额 = 1000(项目料A) + 1000(通用料B) + 60(合并三行) + 40(逐行料)
        #              + 200(待调料C) + 300(共用料D) + 500(专用料E)
        # 两次调拨的转入与转出各自相抵，不改变总额。
        # 恒等式：Σ项目材料成本 + 通用库存金额 + 未归集 = 采购入库总额
        #   （通用料B 领走的 300 已经从库存金额转成 T-002 的成本，仍在等式左边）
        total_purchased = 3100.0
        iv_f = (await c.get("/api/wh/inventory-value", headers=H)).json()
        pc_f = (await c.get("/api/wh/project-cost", headers=H)).json()
        proj_total = sum(r["cost"] for r in pc_f["rows"])
        booked2 = proj_total + iv_f["total_value"] + pc_f.get("unassigned", 0)
        chk(near(booked2, total_purchased),
            f"收官配平：项目成本{proj_total} + 通用库存{iv_f['total_value']} "
            f"+ 未归集{pc_f.get('unassigned')} = {booked2}，应等于采购入库总额 {total_purchased}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
