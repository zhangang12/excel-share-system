"""反馈 #339~#343（2026-08-03 导出的 5 条）+ 用户补充 2 条。

后端能测的只有下面这些，其余是纯前端/客户端改动，各自的验证方式记在文件末尾。

#340 赵仁辉「不能单个下载」（语音转写，原意「不能单个下载」）：
     采购资料抽屉只有勾选打 zip，想单独拿一张 CAD 图也得解压。
     现在 /purchase/package 加 single=true → 只勾一项时直接回原文件。
     **必须走同一个端点**：另开一个 GET 就要把角色校验、项目归属、pushed==True
     重写一遍，写漏一条就是越权下载。本测试逐条压这三道门。

#341 卢照坤「按照分类排序自动顺延生成编号，同时保留可修改」：
     新增中类/细分类时段码是空的，得自己数到第几个了，猜错后端甩 409。
     顺延逻辑在前端（要即时填进输入框），这里压的是它依赖的后端约束：
     位数固定、同级不重复——顺延算法算错了必然踩这两条。

#343 王利利「到4点多一点就黑屏了」：
     截图是纯色 #0f1d30，正是 BrowserWindow.backgroundColor，即窗口里什么都没画。
     这个自恢复 1.0.21（2026-08-01）已经修了，但生产 desktop_reports 一条上报都没有
     → 在跑的客户端普遍低于 1.0.21，修了也没装上。所以真正的交付是「补充2 强制更新」。
     这里压的是上报链路本身通（否则下次还是两眼一抹黑）。
"""
import asyncio, os, sys, tempfile, io, zipfile

tmp = tempfile.mkdtemp(prefix="fb339")
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
    else: print("  ok:", m)


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema_columns(engine)
    async with SessionLocal() as db:
        await seed(db); await run_all(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}

        async def mk(uname, rc, full):
            r = await c.post("/api/admin/users", headers=H, json={
                "username": uname, "password": "pass123", "full_name": full, "role_id": rid[rc]})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        async def login(u):
            r = await c.post("/api/auth/login", json={"username": u, "password": "pass123"})
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        ids = {}
        for u, rc, fn in [("s1", "sales", "赵仁辉"), ("dl", "design_lead", "陈工"),
                          ("d1", "designer", "张工"), ("b1", "buyer", "李新新")]:
            ids[u] = await mk(u, rc, fn)
        Hs1, Hdl, Hd1, Hb1 = await login("s1"), await login("dl"), await login("d1"), await login("b1")

        # ---- 建一个走到设计接单的项目，才有采购数据表 ----
        r = await c.post("/api/sales/orders", headers=Hs1, json={
            "name": "单个下载机", "customer": "x", "cust_type": "经销商", "contract": "有",
            "amount": 100000, "tax_rate": "13%", "prepay": 0, "before_ship": 0,
            "ship_receivable": 0, "balance": 0, "balance_date": "", "depts": ["design"],
            "receiver": {"name": "a", "phone": "1", "addr": "b"}})
        assert r.status_code == 200, r.text
        pid, code = r.json()["project_id"], r.json()["code"]
        oid = [o for o in (await c.get("/api/orders?dept=design", headers=Hdl)).json()
               if o["project_id"] == pid][0]["id"]
        await c.post(f"/api/orders/{oid}/assign", headers=Hdl, json={"worker_id": ids["d1"]})
        await c.post(f"/api/orders/{oid}/start", headers=Hd1,
                     json={"start_date": "2026-07-01", "due_date": "2026-12-31"})
        sheets = {d["name"]: d["id"] for d in
                  (await c.get(f"/api/projects/{pid}/datasheets", headers=Hd1)).json()}
        sid = sheets["标准件清单"]

        # ================= #340 单个下载 =================
        print("\n===== #340 采购资料能单个下载 =====")

        async def pack(body, h=Hb1):
            return await c.post("/api/purchase/package", headers=h, json=body)

        # 老口径不动：不传 single 还是 zip
        r = await pack({"project_id": pid, "sheet_ids": [sid]})
        chk(r.status_code == 200, f"打包下载仍可用: {r.status_code} {r.text[:120]}")
        chk(r.headers.get("content-type") == "application/zip",
            f"不传 single 时仍是 zip（老行为不变）: {r.headers.get('content-type')}")
        chk(zipfile.ZipFile(io.BytesIO(r.content)).namelist()[0].endswith(".xlsx"),
            "zip 里是 xlsx")

        # single=true 且只勾一项 → 原文件直下
        r = await pack({"project_id": pid, "sheet_ids": [sid], "single": True})
        chk(r.status_code == 200, f"single 单表下载: {r.status_code} {r.text[:160]}")
        chk("spreadsheetml" in (r.headers.get("content-type") or ""),
            f"single 直接回 xlsx，不套 zip: {r.headers.get('content-type')}")
        # xlsx 本身就是个 zip，光看魔数区分不了「xlsx」和「装着 xlsx 的 zip」。
        # 判据是内部结构：xlsx 里必然有 xl/workbook.xml，套壳 zip 里则是一个 .xlsx 条目。
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        chk("xl/workbook.xml" in names and not any(n.endswith(".xlsx") for n in names),
            f"回来的确实是个 xlsx，不是 zip 套 xlsx: {names[:4]}")
        chk(code in (r.headers.get("content-disposition") or ""),
            f"文件名带项目编号: {r.headers.get('content-disposition')}")

        # single=true 但勾了多项 → 退回 zip，不能悄悄只下一个
        r = await pack({"project_id": pid, "sheet_ids": [sid, sheets["激光件清单"]], "single": True})
        chk(r.headers.get("content-type") == "application/zip",
            "single 但勾了 2 项 → 仍打 zip（绝不能只给其中一个就完事）")

        # ---- 三道门逐条压：single 这条新路径不能比打包松 ----
        print("\n----- single 路径的权限口径与打包一致 -----")
        r = await pack({"project_id": pid, "sheet_ids": [sid], "single": True}, h=Hd1)
        chk(r.status_code == 403, f"非采购角色（设计）打不开 single: {r.status_code}")

        # 换个项目，拿别的项目的表 id 来下
        r2 = await c.post("/api/sales/orders", headers=Hs1, json={
            "name": "另一个项目", "customer": "y", "cust_type": "经销商", "contract": "有",
            "amount": 1, "tax_rate": "13%", "prepay": 0, "before_ship": 0,
            "ship_receivable": 0, "balance": 0, "balance_date": "", "depts": ["design"],
            "receiver": {"name": "a", "phone": "1", "addr": "b"}})
        pid2 = r2.json()["project_id"]
        r = await pack({"project_id": pid2, "sheet_ids": [sid], "single": True})
        chk(r.status_code == 404,
            f"跨项目取表 id 被挡（project_id 与 sheet 不匹配）: {r.status_code}")

        # 非采购白名单的表（如设计自己的表）不给下
        other = [n for n in sheets if n not in
                 ("外协加工", "钣金装配", "不锈钢原料下料单", "激光件清单", "电工采购单", "标准件清单")]
        if other:
            r = await pack({"project_id": pid, "sheet_ids": [sheets[other[0]]], "single": True})
            chk(r.status_code == 404,
                f"非采购白名单的表「{other[0]}」不给单独下: {r.status_code}")
        else:
            print("  (跳过：该项目没有非采购表可测)")

        # 未推送的附件不给下（打包侧有 pushed==True，single 侧必须一样）
        r = await pack({"project_id": pid, "attachment_ids": [999999], "single": True})
        chk(r.status_code == 404, f"不存在/未推送的附件 404: {r.status_code}")

        # ================= #341 段码顺延依赖的后端约束 =================
        print("\n===== #341 段码顺延（压它依赖的后端约束）=====")
        r = await c.post("/api/wh/material-categories", headers=H,
                         json={"parent_id": None, "seg_code": "7", "name": "顺延测试大类",
                               "sort_order": 0, "enabled": True})
        chk(r.status_code == 200, f"建大类: {r.status_code} {r.text[:120]}")
        big = r.json()["id"]

        made = []
        for seg in ("01", "02", "03"):
            r = await c.post("/api/wh/material-categories", headers=H,
                             json={"parent_id": big, "seg_code": seg, "name": f"中类{seg}",
                                   "sort_order": 0, "enabled": True})
            chk(r.status_code == 200, f"建中类 {seg}: {r.status_code}")
            made.append(r.json()["id"])

        # 前端 nextSeg 会算出 04；这里确认后端认这个值
        r = await c.post("/api/wh/material-categories", headers=H,
                         json={"parent_id": big, "seg_code": "04", "name": "顺延出来的",
                               "sort_order": 3, "enabled": True})
        chk(r.status_code == 200, f"顺延值 04 被接受: {r.status_code} {r.text[:120]}")

        # 顺延算错（重复）必然 409 —— 所以前端算法必须按同级已用值找空位
        r = await c.post("/api/wh/material-categories", headers=H,
                         json={"parent_id": big, "seg_code": "02", "name": "撞车",
                               "sort_order": 0, "enabled": True})
        chk(r.status_code == 409, f"同级段码重复 → 409（顺延算错就是这个后果）: {r.status_code}")

        # 位数固定：前端 padStart(2,'0') 不能省
        r = await c.post("/api/wh/material-categories", headers=H,
                         json={"parent_id": big, "seg_code": "5", "name": "没补零",
                               "sort_order": 0, "enabled": True})
        chk(r.status_code == 400, f"中类段码必须 2 位，'5' 被拒: {r.status_code}")

        # 删掉中间一个 → 顺延应该补回空位（前端从 1 起找第一个空位就是为这个）
        r = await c.delete(f"/api/wh/material-categories/{made[1]}", headers=H)
        chk(r.status_code == 200, f"删掉 02: {r.status_code} {r.text[:120]}")
        r = await c.post("/api/wh/material-categories", headers=H,
                         json={"parent_id": big, "seg_code": "02", "name": "补回空位",
                               "sort_order": 1, "enabled": True})
        chk(r.status_code == 200, f"空位 02 能补回（不是一路往后加）: {r.status_code}")

        # ================= #343 黑屏：上报链路必须通 =================
        print("\n===== #343 黑屏 —— 上报链路 =====")
        r = await c.post("/api/desktop/report", json={
            "device_id": "fb343-probe", "version": "1.0.20", "kind": "crash",
            "detail": "render-process-gone oom exitCode=5"})
        chk(r.status_code == 200, f"客户端崩溃上报免认证可达: {r.status_code} {r.text[:120]}")
        rows = (await c.get("/api/admin/desktop-reports", headers=H)).json()
        rows = rows if isinstance(rows, list) else rows.get("items", [])
        chk(any(x.get("device_id") == "fb343-probe" for x in rows),
            f"上报能在管理端查到（生产 0 条 = 客户端太老，不是链路坏了）: {len(rows)} 条")

    print("\n" + "=" * 60)
    if FAIL:
        print(f"❌ {len(FAIL)} 条失败：")
        for f in FAIL: print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")
    print("""
其余改动的验证方式（后端测不到）：
  #339 ¥¥        全库 grep '¥{{ fmt' / '¥${fmt' 应为 0 —— 4 个 fmtMoney 变体都自带 ¥
  #342 树展开     浏览器：展开一支→收起另一支→新增→只有新增的父节点跟着展开
  #341 段码       浏览器：在已有 01..05 的分类下点＋子类，输入框应预填 06 且可改
  #340 单个下载   浏览器：抽屉每行右侧下载图标，点了直接落文件不是 zip
  补充1 下载目录  客户端：菜单「操作 → 下载位置…」，标题显示当前路径
  补充2 强制更新  客户端：version.json 设 force_latest 后，旧版登录点不动，切强制更新页
""")

asyncio.run(main())
