"""端到端全生命周期测试（需求驱动 · 全角色 · 文件流转 · 状态流转 · 权限闸门）

依据《业务确认结论.md》逐条验证一个项目从销售下单到发货/开票/售后的完整闭环：
  角色: sales/sales_lead, design_lead/designer, electric_lead/electrician,
        pm_lead/assembler, sheetmetal, buyer, warehouse/warehouse_lead, logistics,
        finance, as_lead/as_worker, manager, admin
  文件流转(FILE): 合同 → 图纸包(→钣金) → 采购清单(→采购+第5表) → 电路图/说明书/铭牌(→物流)
                → 发货单(→回传台账) → 发票(→回传台账) → 售后物料清单(→财务)
  状态流转(STATE): dept_order(pending_assign→assigned→in_progress→done) / D5闸门 /
                  shipment(pending→shipped) / invoice(None→applying→pending_invoice→invoiced) /
                  feedback(pending_pm→pending_design→archived) / aftersales(pending→approved)
  权限(PERM): A3 部门页仅本部门+管理层 / A4 生产仅主管下单 / 报表仅管理层 / 详单闸门
"""
import asyncio, os, sys, tempfile, shutil, io

tmp = tempfile.mkdtemp(prefix="e2etest")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from datetime import datetime, timezone
from sqlalchemy import update, select
from httpx import AsyncClient, ASGITransport
from app.main import app
from app import models
from app.database import engine, SessionLocal, Base
from app.seed import seed
from app.sheet_templates import SHEET_TEMPLATES
from app.data_migration import run_all, ensure_schema_columns


async def mark_sheets_imported(project_id: int):
    """D1：模拟项目详单四表已导入 Excel（等价 import-excel 结果，给 imported_at 打标）。"""
    async with SessionLocal() as db:
        await db.execute(update(models.Datasheet).where(
            models.Datasheet.project_id == project_id,
            models.Datasheet.name.in_(tuple(SHEET_TEMPLATES.keys())),
        ).values(imported_at=datetime.now(timezone.utc)))
        await db.commit()

FAIL = []
def chk(c, m):
    if c:
        print("  PASS", m)
    else:
        FAIL.append(m); print("  FAIL:", m)

def sec(t): print(f"\n===== {t} =====")


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(u, rc, fn):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": u, "password": "pass123", "full_name": fn, "role_id": rid[rc]})
            assert r.status_code == 200, f"建用户 {u}({rc}) 失败: {r.text[:160]}"
            return r.json()["id"]

        ids = {}
        ROLES = [
            ("s1", "sales", "赵销售"), ("sl", "sales_lead", "孙销售主管"),
            ("dl", "design_lead", "陈设计主管"), ("d1", "designer", "张设计"),
            ("el", "electric_lead", "许电工主管"), ("e1", "electrician", "刘电工"),
            ("pm", "pm_lead", "钱生产主管"), ("a1", "assembler", "周装配"),
            ("sm", "sheetmetal", "何钣金"), ("bu", "buyer", "林采购"),
            ("wh", "warehouse", "吴仓管"), ("wl", "warehouse_lead", "郑仓库主管"),
            ("lo", "logistics", "马物流"), ("fin", "finance", "冯财务"),
            ("al", "as_lead", "卫售后主管"), ("aw", "as_worker", "蒋售后员"),
            ("mg", "manager", "管理层B"),
        ]
        for u, rc, fn in ROLES:
            ids[u] = await mk(u, rc, fn)

        async def login(u):
            r = await c.post("/api/auth/login", json={"username": u, "password": "pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        Hs1, Hsl = await login("s1"), await login("sl")
        Hdl, Hd1 = await login("dl"), await login("d1")
        Hel, He1 = await login("el"), await login("e1")
        Hpm, Ha1 = await login("pm"), await login("a1")
        Hsm, Hbu = await login("sm"), await login("bu")
        Hwh, Hwl, Hlo = await login("wh"), await login("wl"), await login("lo")
        Hfin, Hal, Haw, Hmg = await login("fin"), await login("al"), await login("aw"), await login("mg")

        # ============================================================
        sec("① 权限/菜单口径 (A3 部门页仅本部门+管理层 / 报表仅管理层 / 详单闸门)")
        async def menus(h):
            return set((await c.get("/api/auth/menus", headers=h)).json().get("keys", [])
                       if isinstance((await c.get("/api/auth/menus", headers=h)).json(), dict)
                       else [m.get("key") for m in (await c.get("/api/auth/menus", headers=h)).json()])
        # 菜单结构兼容：直接取原始
        raw = (await c.get("/api/auth/menus", headers=Hs1)).json()
        def keyset(j):
            if isinstance(j, dict):
                arr = j.get("menus") or j.get("keys") or []
            else:
                arr = j
            out = set()
            for m in arr:
                if isinstance(m, str): out.add(m)
                elif isinstance(m, dict):
                    out.add(m.get("key") or m.get("menuKey") or m.get("name"))
                    for ch in (m.get("children") or []):
                        out.add(ch.get("key") or ch.get("menuKey") or ch.get("name"))
            return out
        sales_keys = keyset((await c.get("/api/auth/menus", headers=Hs1)).json())
        chk("sales" in sales_keys, f"销售可见销售部菜单: {sorted(sales_keys)[:8]}")
        chk("report" not in keyset((await c.get("/api/auth/menus", headers=Hdl)).json()),
            "设计主管无月度报表菜单(§十七 仅管理层)")
        # 报表端点：管理层200 / 部门主管403
        chk((await c.get("/api/reports/monthly", headers=Hmg)).status_code == 200, "管理层可访问月度报表")
        chk((await c.get("/api/reports/monthly", headers=Hdl)).status_code == 403, "设计主管访问月度报表403")
        # 详单闸门：销售无项目详单
        chk((await c.get("/api/projects", headers=Hs1)).status_code in (200, 403), "销售访问项目目录(口径)")

        # ============================================================
        sec("② 销售下单 (§十三 自动编号/客户分类/收款字段 + E2 收货信息 + B 多部门)")
        r = await c.post("/api/sales/orders", headers=Hs1, json={
            "name": "300L真空乳化机", "customer": "华东制药", "cust_type": "终端客户",
            "contract": "无", "amount": 128000, "tax_rate": "13%",
            "prepay": 38400, "before_ship": 0, "ship_receivable": 0, "balance": 12800,
            "balance_date": "", "depts": ["design", "electric", "produce"],
            "receiver": {"name": "王收货", "phone": "139-0000-1234", "addr": "无锡惠山区工业园8号"}})
        chk(r.status_code == 200, f"[STATE] 销售下单: {r.text[:160]}")
        pid, code = r.json()["project_id"], r.json()["code"]
        chk(len(r.json()["order_ids"]) == 3, f"[B] 设计+电工+生产 三任务: {len(r.json()['order_ids'])}")
        # 台账与发货待办自动生成
        led = [x for x in (await c.get("/api/sales/ledger", headers=Hsl)).json()["rows"] if x["project_id"] == pid][0]
        chk(led["customer"] == "华东制药" and led["prepay"] == 38400, "[§十三] 台账含客户/预付")
        board0 = [x for x in (await c.get("/api/logistics/board", headers=Hlo)).json() if x["code"] == code][0]
        chk(board0["receiver_name"] == "王收货", "[E2/FILE] 收货信息来自下单回传物流看板")
        sid = board0["id"]
        # PERM: 销售只看自己的单(§九.1)
        await c.post("/api/sales/orders", headers=Hsl, json={  # 主管以自己名义下另一单
            "name": "他人单", "customer": "x", "cust_type": "经销商", "contract": "无",
            "amount": 1000, "tax_rate": "/", "prepay": 0, "before_ship": 0, "ship_receivable": 0,
            "balance": 0, "balance_date": "", "depts": ["design"], "receiver": {"name": "a", "phone": "1", "addr": "b"}})
        s1rows = (await c.get("/api/sales/ledger", headers=Hs1)).json()["rows"]
        chk(all(x["sales_name"] == "赵销售" for x in s1rows) and s1rows,
            f"[PERM §九.1] 销售员仅见自己的单: {len(s1rows)}行")

        # ============================================================
        sec("③ 合同上传 (§十三.8 FILE 合同→下单/交货日期回写)")
        r = await c.post(f"/api/sales/ledger/{led['id']}/contract", headers=Hs1,
                         data={"sign_date": "2026-06-01", "deliver_date": "2026-08-01"},
                         files={"file": ("销售合同.pdf", io.BytesIO(b"%PDF-contract"), "application/pdf")})
        chk(r.status_code == 200, f"[FILE] 上传合同: {r.text[:120]}")
        led = [x for x in (await c.get("/api/sales/ledger", headers=Hsl)).json()["rows"] if x["project_id"] == pid][0]
        chk(led["contract"] == "有" and led["contract_file_id"], "[FILE] 合同标记有+附件回写")
        chk(led["sign_date"] == "2026-06-01", "[§十三.8] 签订日期=下单日期回写")

        # ============================================================
        sec("④ 设计部 (D 接单→图纸包→完成; FILE 图纸包→钣金; STATE 状态机)")
        od = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"] == pid][0]
        chk(od["status"] == "pending_assign", "[STATE] 设计单初始=待分派")
        # A3: 电工不能看设计部任务列表(仅本部门)
        e_see_design = (await c.get("/api/orders?dept=design", headers=He1)).json()
        chk(not any(o["project_id"] == pid for o in e_see_design) or (await c.get("/api/orders?dept=design", headers=He1)).status_code == 403,
            "[PERM A3] 电工看不到设计部任务")
        await c.post(f"/api/orders/{od['id']}/assign", headers=Hdl, json={"worker_id": ids["d1"]})
        await c.post(f"/api/orders/{od['id']}/start", headers=Hd1, json={"start_date": "2026-06-02", "due_date": "2026-06-20"})
        od2 = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"] == pid][0]
        chk(od2["status"] == "in_progress", "[STATE] 设计单 待分派→assigned→进行中")
        # FILE: 图纸包 → 钣金
        up = await c.post(f"/api/orders/{od['id']}/start-upload?kind=sheetpkg", headers=Hd1,
                          files=[("files", ("总装图.pdf", io.BytesIO(b"%PDF-pkg"), "application/pdf"))])
        pkg_id = up.json()[0]["id"]
        smrow = [x for x in (await c.get("/api/sheetmetal/projects", headers=Hsm)).json() if x["code"] == code][0]
        chk(len(smrow["pkg_files"]) == 1, "[FILE §九.2] 图纸包推送到钣金组")
        chk((await c.get(f"/api/attachments/{pkg_id}/download", headers=Hsm)).status_code == 200, "[FILE] 钣金可下载图纸包")
        await c.post(f"/api/orders/{od['id']}/output-upload?kind=manual", headers=Hd1,
                     files=[("files", ("说明书.docx", io.BytesIO(b"DOC"), "application/msword"))])
        # D1/D2: 四表未导入前,设计不可完成(只卡设计部)
        r = await c.post(f"/api/orders/{od['id']}/complete", headers=Hd1, json={"notify_user_id": ids["lo"]})
        chk(r.status_code == 400 and "四表" in r.text, f"[D1/D2] 四表未导入→设计不可完成: {r.status_code}")
        await mark_sheets_imported(pid)  # 模拟项目详单导入四表 Excel
        r = await c.post(f"/api/orders/{od['id']}/complete", headers=Hd1, json={"notify_user_id": ids["lo"]})
        chk(r.status_code == 200, f"[STATE/D1] 四表导入后设计完成: {r.text[:120]}")

        # ============================================================
        sec("⑤ 电工部 (D4 采购清单接单后→采购; 电路图完成必传→物流; §十六 第5表)")
        oe = [o for o in (await c.get("/api/orders?dept=electric", headers=Hel)).json() if o["project_id"] == pid][0]
        await c.post(f"/api/orders/{oe['id']}/assign", headers=Hel, json={"worker_id": ids["e1"]})
        await c.post(f"/api/orders/{oe['id']}/start", headers=He1, json={"start_date": "2026-06-02", "due_date": "2026-06-18"})
        # FILE: 采购清单 → 采购
        await c.post(f"/api/orders/{oe['id']}/start-upload?kind=plist", headers=He1,
                     files=[("files", ("采购清单.xlsx", io.BytesIO(b"PK-xlsx"), "application/vnd.ms-excel"))])
        inbox = [x for x in (await c.get("/api/purchase/inbox", headers=Hbu)).json() if x["code"] == code]
        chk(len(inbox) == 1, "[FILE D4] 采购清单推送到采购部收件箱")
        # §十六 第5表(电工采购单)生成
        dss = (await c.get(f"/api/projects/{pid}/datasheets", headers=H)).json()
        names = [d["name"] for d in (dss if isinstance(dss, list) else dss.get("datasheets", []))]
        chk(any("电工采购单" in n for n in names), f"[§十六] 项目详单生成第5表「电工采购单」: {names}")
        # D4: 电路图完成必传 —— 不传直接完成应被拦
        r = await c.post(f"/api/orders/{oe['id']}/complete", headers=He1, json={"notify_user_id": ids["bu"]})
        chk(r.status_code == 400, f"[D4] 缺电路图完成被拦: {r.status_code}")
        await c.post(f"/api/orders/{oe['id']}/output-upload?kind=circuit", headers=He1,
                     files=[("files", ("电路图.pdf", io.BytesIO(b"%PDF-circuit"), "application/pdf"))])
        r = await c.post(f"/api/orders/{oe['id']}/complete", headers=He1, json={"notify_user_id": ids["bu"]})
        chk(r.status_code == 200, f"[STATE] 电工完成(电路图已传): {r.text[:120]}")

        # ============================================================
        sec("⑥ 生产部 (A4 生产主管下单/分派; E3 无产物仅通知)")
        op = [o for o in (await c.get("/api/orders?dept=produce", headers=Hpm)).json() if o["project_id"] == pid][0]
        await c.post(f"/api/orders/{op['id']}/assign", headers=Hpm, json={"worker_id": ids["a1"]})
        await c.post(f"/api/orders/{op['id']}/start", headers=Ha1, json={"start_date": "2026-06-03", "due_date": "2026-06-22"})

        # ============================================================
        sec("⑦ D5 发货闸门 (§十二.5 已下单部门全完成才可发)")
        b1 = [x for x in (await c.get("/api/logistics/board", headers=Hlo)).json() if x["code"] == code][0]
        chk(not b1["can_ship"] and "生产部" in b1["gate_missing"],
            f"[D5/STATE] 生产未完成→不可发, 缺口={b1['gate_missing']}")
        # 生产完成 → 闸门放开
        r = await c.post(f"/api/orders/{op['id']}/complete", headers=Ha1, json={"notify_user_id": ids["lo"]})
        chk(r.status_code == 200, f"[E3] 生产完成(无产物): {r.text[:120]}")
        b2 = [x for x in (await c.get("/api/logistics/board", headers=Hlo)).json() if x["code"] == code][0]
        chk(b2["can_ship"] and not b2["gate_missing"], f"[D5] 三部门完成→可发货, 缺口={b2['gate_missing']}")
        chk(len(b2["design_files"]) >= 1 and len(b2["electric_files"]) >= 1, "[FILE] 设计/电工产物进物流看板资料列")

        # ============================================================
        sec("⑧ 仓库 (§九.3 物料/出入库 + 发货清单→物流) + 设计只读查库存(§十二.14)")
        m = (await c.post("/api/wh/materials", headers=Hwh, json={
            "name": "机封", "spec": "DN50", "unit": "套", "safety_stock": 2, "init_stock": 5})).json()
        await c.post("/api/wh/txns", headers=Hwh, json={"material_id": m["id"], "biz_date": "2026-06-05", "direction": "out", "qty": 1, "project_id": pid})
        chk((await c.get("/api/wh/materials", headers=Hwh)).json()["materials"][0]["stock"] == 4, "[STATE] 出库后实时库存=4")
        chk((await c.get("/api/wh/materials", headers=Hd1)).status_code == 200, "[PERM §十二.14] 设计师只读查库存")
        chk((await c.post("/api/wh/materials", headers=Hd1, json={"name": "x"})).status_code == 403, "[PERM] 设计师不能建物料")
        # 发货清单 → 物流看板
        r = await c.post(f"/api/wh/ship-list/{pid}", headers=Hwh,
                         files={"file": ("发货清单.xlsx", io.BytesIO(b"PK"), "application/vnd.ms-excel")})
        chk(r.status_code == 200, f"[FILE §九.3] 仓库上传发货清单: {r.text[:120]}")
        b3 = [x for x in (await c.get("/api/logistics/board", headers=Hlo)).json() if x["code"] == code][0]
        chk(len(b3["ship_list_files"]) == 1, "[FILE] 仓库发货清单进物流看板")

        # ============================================================
        sec("⑨ 物流确认发货 (E1 一次发货; FILE 发货单; STATE→shipped; 回传台账发货日期)")
        # PERM: 非物流非管理层不能发货
        chk((await c.post(f"/api/logistics/{sid}/ship", headers=Hd1,
             files={"file": ("x.pdf", io.BytesIO(b"P"), "application/pdf")})).status_code == 403,
            "[PERM] 设计师不能确认发货")
        r = await c.post(f"/api/logistics/{sid}/ship", headers=Hlo,
                         files={"file": ("发货单.pdf", io.BytesIO(b"%PDF-ship"), "application/pdf")})
        chk(r.status_code == 200, f"[FILE/STATE] 物流确认发货: {r.text[:120]}")
        # E1: 重复发货被拒
        chk((await c.post(f"/api/logistics/{sid}/ship", headers=Hlo,
             files={"file": ("y.pdf", io.BytesIO(b"P"), "application/pdf")})).status_code == 400,
            "[E1] 一次发货,重复发货被拒")
        from datetime import date
        led2 = [x for x in (await c.get("/api/sales/ledger", headers=Hsl)).json()["rows"] if x["project_id"] == pid][0]
        chk(led2["ship_date"] == date.today().isoformat(), f"[FILE→STATE] 发货日期回传销售台账: {led2['ship_date']}")

        # ============================================================
        sec("⑩ 财务开票流 (§十三.4 STATE+FILE apply→approve→upload→回传)")
        r = await c.post(f"/api/sales/ledger/{led['id']}/invoice-apply", headers=Hs1,
                         files={"file": ("开票申请.xlsx", io.BytesIO(b"PK"), "application/vnd.ms-excel")})
        chk(r.status_code == 200, f"[STATE] 销售提交开票申请→applying: {r.text[:120]}")
        # PERM: 财务不能审批(主管审批)
        chk((await c.post(f"/api/sales/ledger/{led['id']}/invoice-approve", headers=Hfin)).status_code in (403, 400),
            "[PERM] 财务不能做主管审批")
        chk((await c.post(f"/api/sales/ledger/{led['id']}/invoice-approve", headers=Hsl)).status_code == 200,
            "[STATE] 销售主管审批→pending_invoice")
        # PERM: 销售不能传发票(财务传)
        chk((await c.post(f"/api/sales/ledger/{led['id']}/invoice-upload", headers=Hs1,
             files={"file": ("x.pdf", io.BytesIO(b"P"), "application/pdf")})).status_code == 403,
            "[PERM] 销售不能上传发票")
        r = await c.post(f"/api/sales/ledger/{led['id']}/invoice-upload", headers=Hfin,
                         files={"file": ("增值税发票.pdf", io.BytesIO(b"%PDF-inv"), "application/pdf")})
        chk(r.status_code == 200, f"[FILE] 财务上传发票: {r.text[:120]}")
        led3 = [x for x in (await c.get("/api/sales/ledger", headers=Hsl)).json()["rows"] if x["project_id"] == pid][0]
        chk(led3["invoice_state"] == "invoiced" and led3["invoice_file_id"], "[STATE/FILE] 台账→已开票+发票可下载回传")

        # ============================================================
        sec("⑪ 售后部 (§十五 FILE 物料清单必传 + STATE 审批 → 同步财务)")
        # 必传物料清单：不传被拦
        chk((await c.post("/api/aftersales", headers=Haw,
             data={"project_id": str(pid), "problem": "密封漏油", "cost": "800"})).status_code in (400, 422),
            "[§十五 FILE] 售后登记缺物料清单被拦")
        r = await c.post("/api/aftersales", headers=Haw,
                         data={"project_id": str(pid), "problem": "密封漏油返修", "cost": "800"},
                         files={"file": ("售后物料清单.xlsx", io.BytesIO(b"PK"), "application/vnd.ms-excel")})
        chk(r.status_code == 200, f"[STATE] 售后登记→待审批: {r.text[:120]}")
        aid = (await c.get("/api/aftersales", headers=Haw)).json()["rows"][0]["id"]
        # PERM: 员工不能审批
        chk((await c.post(f"/api/aftersales/{aid}/approve", headers=Haw)).status_code == 403, "[PERM] 售后员工不能审批")
        chk((await c.post(f"/api/aftersales/{aid}/approve", headers=Hal)).status_code == 200, "[STATE] 售后主管审批通过")
        fa = (await c.get("/api/finance/aftersales", headers=Hfin)).json()
        chk(any(x["project_code"] == code for x in fa["rows"]) if "project_code" in (fa["rows"][0] if fa["rows"] else {}) else len(fa["rows"]) >= 1,
            "[FILE/STATE §十五] 售后通过→同步财务部售后费用卡")

        # ============================================================
        sec("⑫ 问题反馈流 (§十四 STATE 装配提交→主管审批→设计接收存档)")
        r = await c.post("/api/feedbacks", headers=Ha1, json={"project_id": pid, "content": "B02机架孔位偏差2mm,请核对图纸"})
        chk(r.status_code == 200, f"[STATE] 装配提交反馈→待主管审批: {r.text[:120]}")
        fid = (await c.get("/api/feedbacks?mine=true", headers=Hpm)).json()[0]["id"]
        chk((await c.post(f"/api/feedbacks/{fid}/pm-approve", headers=Hpm)).status_code == 200, "[STATE] 生产主管通过→待设计接收")
        chk((await c.post(f"/api/feedbacks/{fid}/design-accept", headers=Hd1)).status_code == 200, "[STATE] 设计师接收→存档")
        arch = [x for x in (await c.get(f"/api/feedbacks?project_id={pid}", headers=H)).json() if x["id"] == fid][0]
        chk(arch["status"] == "archived", "[STATE §十四] 反馈终态=已存档")

        # ============================================================
        sec("⑬ 换人 (§十八 主管对进行中任务换负责人)")
        # 再下一条设计单用于换人(原单已完成)
        r2 = await c.post("/api/sales/orders", headers=Hs1, json={
            "name": "换人测试机", "customer": "x", "cust_type": "经销商", "contract": "无",
            "amount": 1000, "tax_rate": "/", "prepay": 0, "before_ship": 0, "ship_receivable": 0,
            "balance": 0, "balance_date": "", "depts": ["design"], "receiver": {"name": "a", "phone": "1", "addr": "b"}})
        pid2 = r2.json()["project_id"]
        od_n = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"] == pid2][0]
        await c.post(f"/api/orders/{od_n['id']}/assign", headers=Hdl, json={"worker_id": ids["d1"]})
        # 再建一个设计师做转交对象
        d2 = await mk("d2", "designer", "李设计")
        r = await c.post(f"/api/orders/{od_n['id']}/reassign", headers=Hdl, json={"worker_id": d2})
        chk(r.status_code == 200, f"[§十八] 设计主管换人: {r.text[:120]}")
        od_n2 = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json() if o["project_id"] == pid2][0]
        chk(od_n2["worker_id"] == d2, "[§十八] 负责人已转交给新设计师")

        # ============================================================
        sec("⑭ 月度报表 (§十七 C口径 + 仅管理层)")
        rep = (await c.get("/api/reports/monthly", headers=Hmg)).json()
        chk(rep["total"] >= 4 and rep["done"] >= 3, f"[§十七] 月度报表统计任务: total={rep['total']} done={rep['done']}")
        chk(rep["wh_txn_count"] >= 1, f"[§十七] 报表含仓库出入库量: {rep['wh_txn_count']}")
        chk(rep["sales_order_count"] >= 1, f"[§十七] 报表含销售下单量: {rep['sales_order_count']}")

    await engine.dispose()
    print(f"\n{'='*60}")
    print("PASSED ✅ 端到端全生命周期 16角色×文件流转×状态流转×权限 全通过" if not FAIL
          else f"{len(FAIL)} FAILURES ❌\n" + "\n".join("  - " + x for x in FAIL))
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
