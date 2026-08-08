"""反馈#356（李新新）：「这个备注怎么填写…有些外购的他们只是附图纸，因为仓库要对料，
我这里需要把详细尺寸给他们，就想着，下单的时候顺手写上，或者这个可以在整单维护的时候写明」

采购下单时本来就有「备注」列，但**采购收货那张表从来不显示它**——填了也传不到仓库，
所以她根本不知道这个框是干嘛用的。真正要保证的是这条链路通着：
采购写 → 仓库收货时看得到 → 采购单 PDF 上也印着（仓库习惯拿纸质单对料）。

要锁死的：
  1. 下单时逐行写的备注存得下来
  2. **仓库**（warehouse 角色）拉收货清单时拿得到备注 —— 这是整条需求的落点
  3. 事后在整单维护里补写/改写也能生效（她明说下单时未必来得及写全）
  4. 清空要能真的清空（写错了得能删，不能只能越写越多）
  5. 采购单 PDF 里带着备注
"""
import asyncio, os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="fb356")
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


SIZE_NOTE = "外径φ120±0.05，内孔φ80H7，厚 25，倒角 C1（只有图纸，按此对料）"


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
        rid = {x["code"]: x["id"] for x in (await c.get("/api/admin/roles", headers=H)).json()}
        await c.post("/api/admin/users", headers=H, json={
            "username": "wh1", "password": "pass123", "full_name": "孙仓管", "role_id": rid["warehouse"]})
        r = await c.post("/api/auth/login", json={"username": "wh1", "password": "pass123"})
        Hw = {"Authorization": f"Bearer {r.json()['access_token']}"}

        sup = (await c.post("/api/purchase-mgmt/suppliers", headers=H, json={"name": "永强机加工"})).json()

        # 1) 下单时逐行写备注
        r = await c.post("/api/purchase-mgmt/orders", headers=H, json={
            "supplier_id": sup["id"], "delivery_date": "2026-08-08", "lines": [
                {"item_name": "法兰盘", "qty": 2, "unit_price": 120, "received_amount": 240,
                 "notes": SIZE_NOTE},
                {"item_name": "定位销", "qty": 10, "unit_price": 3, "received_amount": 30},
            ]})
        chk(r.status_code == 200, f"建单: {r.status_code} {r.text[:100]}")
        rows = r.json()
        po = rows[0]["po_no"]
        got = {x["item_name"]: x.get("notes") for x in rows}
        chk(got.get("法兰盘") == SIZE_NOTE, f"1) 下单写的备注存下来了: {got.get('法兰盘')!r}")

        # 2) 仓库拉收货清单能看到备注 —— 这是这条反馈的落点
        r = await c.get("/api/purchase-mgmt/receiving", headers=Hw, params={"po_no": po})
        chk(r.status_code == 200, f"仓库能拉收货清单: {r.status_code}")
        recv = {x["item_name"]: x.get("notes") for x in r.json()}
        chk(recv.get("法兰盘") == SIZE_NOTE, f"2) 仓库收货清单里带着备注: {recv.get('法兰盘')!r}")
        chk("定位销" in recv, "2) 没写备注的行照常返回（不能因为没备注就漏行）")

        # 3) 事后在整单维护里补写（前端整单维护逐条走 PUT /items/{id}）
        pin = [x for x in rows if x["item_name"] == "定位销"][0]
        r = await c.put(f"/api/purchase-mgmt/items/{pin['id']}", headers=H,
                        json={"notes": "φ8×30 圆柱销，GB119"})
        chk(r.status_code == 200 and r.json().get("notes") == "φ8×30 圆柱销，GB119",
            f"3) 整单维护补写备注生效: {r.status_code} {r.json().get('notes')!r}")
        recv2 = {x["item_name"]: x.get("notes")
                 for x in (await c.get("/api/purchase-mgmt/receiving", headers=Hw,
                                       params={"po_no": po})).json()}
        chk(recv2.get("定位销") == "φ8×30 圆柱销，GB119", f"3) 补写的备注仓库也看得到: {recv2.get('定位销')!r}")

        # 3b) 改写不会串到别的行
        chk(recv2.get("法兰盘") == SIZE_NOTE, "3) 改一行的备注不会动到同单其它行")

        # 4) 清空
        r = await c.put(f"/api/purchase-mgmt/items/{pin['id']}", headers=H, json={"notes": None})
        chk(r.status_code == 200 and not r.json().get("notes"),
            f"4) 备注能清空（写错了得能删）: {r.json().get('notes')!r}")

        # 5) 采购单 PDF 里带备注（仓库常拿纸质单对料）
        r = await c.get(f"/api/purchase-mgmt/orders/{po}/pdf", headers=Hw)
        chk(r.status_code == 200 and r.content[:4] == b"%PDF",
            f"5) 仓库能下采购单 PDF: {r.status_code}")
        # PDF 里中文是子集内嵌的，抠不出明文；退一步核对渲染入口拿到的就是带备注的数据。
        # ⚠️ 用采购的身份查：/orders/{po_no} 的 JSON 只放行采购/财务，仓库走的是上面的 /pdf。
        detail = {x["item_name"]: x.get("notes")
                  for x in (await c.get(f"/api/purchase-mgmt/orders/{po}", headers=H)).json()}
        chk(detail.get("法兰盘") == SIZE_NOTE, f"5) 打印用的整单接口带备注: {detail.get('法兰盘')!r}")


asyncio.run(main())
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
