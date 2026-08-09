"""智能体 v2 阶段一+二：find→get 递进、明细代码渲染。

设计见 docs/agent-architecture-v2.md。这个测试钉住两件事：

**阶段一（工具层）**：v1 的 13 个工具里 12 个是「列一类」，所以 `for _ in range(4)`
的多轮循环实际永远只跑 1 轮 —— 第一轮列完就没下一步。没有 find→get→agg 的递进，
ReAct 无事可做。本阶段补 `find_entity` 与 4 个 `get_*`。

服务对象是拿生产 30 天审计日志定的：
  杨坛   188 次：销售台账+订单 32% → get_customer / get_project
  赵仁辉 635 次：**仓库 36%**、物料字典 170 次 → get_material（v1 对他覆盖率仅 5%）

**阶段二（渲染层）**：实测 46 条明细模型自己打要 ~1700 字 / 35.9 秒。
改成模型只给「结论 + ```render 编排块」，明细由代码渲染 —— 生产数据实测
模型输出从 2621 字降到 70 字。数字不再经模型的手，幻觉与截断误报归零。
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone

tmp = tempfile.mkdtemp(prefix="v2")
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
from app.routers.agent_router import (TOOL_SCHEMAS, TOOL_LABELS, TOOL_DESC,
                                      apply_render, _allowed_tools, _run_tool)

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

    # ================= 注册完整性 =================
    print("\n===== 工具注册三处对齐 =====")
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    v2 = {"find_entity", "get_customer", "get_project", "get_supplier", "get_material"}
    chk(v2 <= names, f"5 个 v2 工具都在 schema 里：{sorted(v2 - names) or '齐'}")
    chk(not (names - set(TOOL_LABELS)), f"schema 里的都有标签：{sorted(names - set(TOOL_LABELS))}")
    chk(not (set(TOOL_LABELS) - set(TOOL_DESC)), "标签的都有小字描述（门户要显示）")
    for n in v2:
        sch = next(t for t in TOOL_SCHEMAS if t["function"]["name"] == n)
        chk(bool(sch["function"]["description"]), f"{n} 有 description（模型据此选型）")

    async with SessionLocal() as db:
        me = (await db.execute(select(models.User).where(
            models.User.username == "admin"))).scalar_one()

        # 造数据：一个客户两单 + 一个供应商两批采购 + 一个物料
        p1 = models.Project(code="V2-001", name="南京双行星搅拌机", is_deleted=False)
        p2 = models.Project(code="V2-002", name="南京配套模温机", is_deleted=False)
        pdel = models.Project(code="V2-DEL", name="已删项目", is_deleted=True)
        db.add_all([p1, p2, pdel]); await db.flush()
        now = datetime.now(timezone.utc)
        db.add_all([
            models.SalesLedger(project_id=p1.id, sales_uid=me.id, customer="南京鸿远机械有限公司",
                               amount=300000, prepay=90000, prepay_note="已收",
                               before_ship=0, ship_receivable=120000, balance=0,
                               balance_date="", order_state="approved",
                               created_at=now - timedelta(days=20)),
            models.SalesLedger(project_id=p2.id, sales_uid=me.id, customer="南京鸿远机械有限公司",
                               amount=100000, ship_receivable=0, balance=40000,
                               balance_date="", order_state="approved",
                               created_at=now - timedelta(days=5)),
            # 软删项目上的钱一分都不能算进来
            models.SalesLedger(project_id=pdel.id, sales_uid=me.id, customer="南京鸿远机械有限公司",
                               amount=999999, ship_receivable=888888, balance=0,
                               balance_date="", order_state="approved", created_at=now),
        ])
        db.add(models.Shipment(project_id=p1.id, status="pending",
                               receiver_name="", receiver_phone="", receiver_addr=""))
        sup = models.Supplier(name="南京精工机械配件有限公司")
        db.add(sup); await db.flush()
        today = datetime.now(timezone.utc).date()
        db.add_all([
            models.PurchaseItem(supplier_id=sup.id, item_name="减速机", spec="X-1",
                                po_no="PO-1", project_code="V2-001",
                                expected_arrival=(today - timedelta(days=6)).isoformat(),
                                arrival_date=None),
            models.PurchaseItem(supplier_id=sup.id, item_name="联轴器", spec="L-2",
                                po_no="PO-2", project_code="V2-001",
                                expected_arrival=(today - timedelta(days=10)).isoformat(),
                                arrival_date=(today - timedelta(days=2)).isoformat()),
        ])
        mat = models.WhMaterial(code="M-001", name="不锈钢焊丝", spec="ø1.2",
                                unit="kg", safety_stock=50, init_stock=10, location="A-01")
        db.add(mat); await db.flush()
        db.add(models.WhTxn(material_id=mat.id, biz_date=today.isoformat(), direction="in",
                            qty=5, party="某供应商", ref_no="RK-1", is_reversal=False))
        await db.commit()

        # ================= find_entity =================
        print("\n===== find_entity：模糊词 → 实体 =====")
        r = await te.find_entity(db, me, "南京")
        chk(r["total"] > 0, f"「南京」能找到东西：{r['total']} 个")
        kinds = set(r["matches"])
        chk("project" in kinds and "customer" in kinds and "supplier" in kinds,
            f"跨类都能找到（这才叫「南京那个」能被理解）：{sorted(kinds)}")
        chk(all(p["code"] != "V2-DEL" for p in r["matches"].get("project", [])),
            "软删项目不出现在候选里")

        r = await te.find_entity(db, me, "南京", kind="supplier")
        chk(set(r["matches"]) == {"supplier"}, f"kind 能限定类别：{sorted(r['matches'])}")

        r = await te.find_entity(db, me, "查无此物")
        chk(r["total"] == 0 and r["hint"], f"找不到时给提示而不是空结果：{r['hint']}")

        # ================= get_customer =================
        print("\n===== get_customer：杨坛主战场（台账占其操作 32%）=====")
        c = await te.get_customer(db, me, "南京鸿远")
        chk(c["found"] and c["ledger_count"] == 2, f"两单（软删那单不算）：{c['ledger_count']}")
        chk(c["contract_total"] == 400000.0, f"合同额只含正常项目 300000+100000：{c['contract_total']}")
        chk(c["unpaid_total"] == 160000.0, f"未收 120000+40000：{c['unpaid_total']}")
        chk("888888" not in str(c), "软删项目的幽灵金额一分都没混进来")
        chk(c["balance_without_due_date"] == 1,
            f"点出「1 笔尾款没填到期日」——催办按到期日扫，扫不到：{c['balance_without_due_date']}")
        chk(c["ship_receivable_but_not_shipped"] == 1,
            f"点出「1 笔发货款其实还没发货」——别直接催客户：{c['ship_receivable_but_not_shipped']}")
        chk(abs(c["paid_ratio"] - 0.6) < 0.001, f"回款率 (40-16)/40：{c['paid_ratio']}")

        c2 = await te.get_customer(db, me, "查无此客户")
        chk(not c2["found"] and c2["hint"], "查不到时给下一步提示，不是空手而归")

        # ================= get_project =================
        print("\n===== get_project：一次给全 =====")
        pj = await te.get_project(db, me, "V2-001")
        chk(pj["found"] and pj["ledger"]["customer"] == "南京鸿远机械有限公司", "带出台账客户")
        chk(pj["ledger"]["ship_receivable"] == 120000.0, "带出收款分解")
        chk(pj["shipment_status"] == "pending", f"带出发货状态：{pj['shipment_status']}")
        chk(pj["purchase_overdue_count"] == 1,
            f"采购未到货只算 arrival_date 为空的（PO-2 已到货不算）：{pj['purchase_overdue_count']}")
        # ⚠️ 键名就叫 item_name（和表字段一致）。原来叫 name，渲染层的 _NAME_KEYS
        #    按 name 匹配不到「物料」这个表头，采购明细的列名会退化成「名称」。
        chk(len(pj["purchase_pending"]) == 1
            and pj["purchase_pending"][0]["item_name"] == "减速机",
            f"未到货明细用 item_name 不是 name：{pj['purchase_pending']}")

        chk(not (await te.get_project(db, me, "V2-DEL"))["found"], "软删项目查不到")

        # ================= get_supplier =================
        print("\n===== get_supplier：准时率是可比的数，不是感觉 =====")
        sp = await te.get_supplier(db, me, "南京精工")
        chk(sp["found"] and sp["purchase_items"] == 2, f"两批采购：{sp['purchase_items']}")
        chk(sp["overdue"] == 2, f"一批未到货已超期 + 一批到货晚了 8 天，都算迟：{sp['overdue']}")
        chk(sp["max_overdue_days"] >= 6, f"最大超期天数：{sp['max_overdue_days']}")
        chk(sp["count"] == 1, f"当前仍未到货的只有 1 批：{sp['count']}")

        # ================= get_material =================
        print("\n===== get_material：赵仁辉主战场（仓库占其操作 36%，v1 覆盖率 5%）=====")
        mt = await te.get_material(db, me, "焊丝")
        chk(mt["found"] and mt["stock"] == 15.0, f"库存 = 期初 10 + 入库 5：{mt['stock']}")
        chk(mt["below_safety"] and mt["shortfall"] == 35.0,
            f"低于安全库存 50，缺口 35——这才是「要不要补货」的依据：{mt['shortfall']}")
        chk(mt["location"] == "A-01" and mt["txn_count"] == 1, "带出库位与流水条数")

        # ================= 权限门控 =================
        print("\n===== 门控 =====")
        allowed = _allowed_tools(me)
        chk(v2 <= allowed, f"admin 拿得到全部 v2 工具：{sorted(v2 - allowed) or '齐'}")
        # ⚠️ 直接 db.add 建的 User 拿不到 role_codes（懒加载关系，会 MissingGreenlet）。
        #    走接口建，再用 selectinload 重新取一次带关系的对象。
        from sqlalchemy.orm import selectinload
        rid = (await db.execute(select(models.Role).where(
            models.Role.code == "designer"))).scalar_one().id
        db.add(models.User(username="v2des", full_name="设计", password_hash="x", role_id=rid))
        await db.commit()
    async with SessionLocal() as db:
        des = (await db.execute(select(models.User)
                                .options(selectinload(models.User.role),
                                         selectinload(models.User.roles))
                                .where(models.User.username == "v2des"))).scalar_one()
        da = _allowed_tools(des)
        chk("get_customer" not in da, f"设计师拿不到客户全景：{sorted(da)}")
        r = await _run_tool("get_customer", {"name": "南京鸿远"}, db, des)
        chk("error" in r, f"直接调也被拒：{r}")

    # ================= 渲染层 =================
    print("\n===== 渲染：模型只出结论，明细代码渲染 =====")
    result = {"count": 46, "shown": 3, "items": [
        {"supplier": "甲公司", "item_name": "钢丝软管", "over_days": 9,
         "expected_arrival": "2026-07-25", "po_no": "PO-A"},
        {"supplier": "乙公司", "item_name": "浇筑桨", "over_days": 3,
         "expected_arrival": "2026-07-31", "po_no": "PO-B"},
        {"supplier": "丙公司", "item_name": "轴承", "over_days": 0,
         "expected_arrival": "2026-08-04", "po_no": "PO-C"},
    ]}
    plan = {"sort": "over_days", "desc": True, "highlight": ["PO-A"],
            "fields": ["supplier", "item_name", "over_days", "expected_arrival"]}
    body = rd.table(result, plan=plan)
    lines = body.split("\n")
    # 🆕 明细现在渲染成 **markdown 表格**（手机上文字墙没人看，见 render.py 顶部说明）
    body_rows = [l for l in lines if l.startswith("| ") and "---" not in l][1:]
    chk(lines[0].startswith("| ") and "|---|" in lines[1],
        f"明细是表格不是文字列表：{lines[0]}")
    chk(body_rows and body_rows[0].startswith("| **"),
        f"按 over_days 降序，最狠那条（模型 highlight 点名的）加粗：{body_rows[0] if body_rows else lines}")
    chk(body_rows and "甲公司" in body_rows[0], "高亮的那一行确实是甲公司那条")
    chk("9 天" in body_rows[0], "天数带单位")
    chk(any("今日到期" in l for l in body_rows),
        f"over_days=0 说「今日到期」不说「超 0 天」：{body_rows}")
    chk("另有 43 条未列（共 46 条）" in lines[-1],
        f"**截断声明由代码写**——代码知道真实总数，模型只知道它收到几条：{lines[-1]}")

    chk("¥" in rd.money(220000) and "220,000" in rd.money(220000),
        f"金额写原始数值不写「22 万」（对账时没法核）：{rd.money(220000)}")

    print("\n===== apply_render：把编排块换成明细 =====")
    reply = '**结论一句话。**\n\n```render\n{"sort":"over_days","desc":true}\n```'
    out = apply_render(reply, result)
    chk("```" not in out and "{" not in out, f"模型给的 JSON 绝不能漏给用户：{out[:60]}")
    chk(out.startswith("**结论一句话。**"), "结论保留在最前")
    chk("甲公司" in out, "明细被渲染出来了")

    chk(apply_render(reply, None) == "**结论一句话。**",
        "没有工具结果时干净地去掉编排块，不露内部结构")
    bad = '结论。\n\n```render\n{这不是合法JSON}\n```'
    chk(apply_render(bad, result) == "结论。", "模型给了坏 JSON 也不能把它漏出去")
    chk(apply_render("没有编排块的普通回答", result) == "没有编排块的普通回答",
        "没有编排块时原样返回")

    print("\n===== 模型忘了给编排块时，代码自己补 =====")
    # 实测：模型经常只写结论就收尾（被「务必简短」和「不要自己打明细」两条同时约束，
    # 容易两头都不做）。指望它主动 opt-in 不可靠，所以由代码决定要不要出明细。
    auto = apply_render("**共 46 条。**", result, want_list=True)
    chk("甲公司" in auto and auto.startswith("**共 46 条。**"),
        f"用户要清单 + 模型只写结论 → 代码补明细：{auto[:50]}")
    chk("9 天" in auto and "|---|" in auto,
        "默认按 over_days 降序（default_plan 挑的），且渲染成表格")
    chk(apply_render("**共 46 条。**", result, want_list=False) == "**共 46 条。**",
        "用户没要清单就别硬塞明细")
    chk(apply_render("结论\n- 甲\n- 乙", result, want_list=True) == "结论\n- 甲\n- 乙",
        "模型自己已经写了明细就不重复补")
    chk(rd.default_plan(result)["sort"] == "over_days",
        f"default_plan 挑「越大越紧迫」的字段：{rd.default_plan(result)}")
    chk(rd.default_plan({"items": [{"x": "a"}]}) == {},
        "没有可排序的数值字段时返回空编排，不硬排")

    print("\n===== 提示词要引导模型别自己打明细 =====")
    from app.routers.agent_router import _SYSTEM_PROMPT
    t = _SYSTEM_PROMPT.format(today="2026-08-04", user_name="杨坛", roles="管理层")
    chk("```render" in t, "提示词里有 render 块的用法")
    # ⚠️ 提示词里放 JSON 例子时，花括号必须写成 {{ }}。
    #    否则 str.format 把 {"sort":...} 当成占位符 → KeyError('"sort"') →
    #    被外层 except 吞掉 → **每一次请求都静默降级**。踩过一次。
    import re as _re
    ph = set(_re.findall(r"(?<!\{)\{(\w+)\}(?!\})", _SYSTEM_PROMPT))
    chk(ph == {"today", "user_name", "roles"},
        f"提示词里只能有这三个占位符，多出来的说明花括号忘了转义：{sorted(ph)}")
    chk('{"sort"' in t, "转义后 render 例子里的 JSON 要能正常显示给模型")
    chk("不要自己一行一行写数据" in t, "明确要求不要自己打明细")
    chk("find_entity" in str(TOOL_SCHEMAS), "模型知道有 find_entity 可用")

    print("\n===== 两条回复路径必须都完整接上渲染 =====")
    # ⚠️ 血的教训：last_result 只在非流式那条路加了赋值，流式声明了却从不赋值，
    #    结果 H5（走流式）永远只有结论没有明细 —— 而且不报错，测不出来。
    #    所以这里静态扫两条路径的三个环节，缺一个就红。
    import ast as _ast, inspect as _in
    from app.routers import agent_router as _ar
    _src = _in.getsource(_ar)
    _lines = _src.split("\n")
    _tree = _ast.parse(_src)
    for _name in ("_chat_with_llm", "_chat_stream"):
        _fn = [n for n in _ast.walk(_tree)
               if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and n.name == _name][0]
        _t = "\n".join(_lines[_fn.lineno - 1:_fn.end_lineno])
        chk("last_result: dict" in _t, f"{_name} 声明了 last_result")
        chk("last_result = result" in _t, f"{_name} **真的给 last_result 赋了值**（漏过一次）")
        chk("apply_render" in _t and "last_result" in _t, f"{_name} 把它传给了 apply_render")
        chk("want_list" in _t, f"{_name} 算了 want_list（决定要不要自动补明细）")

    print("\n" + "=" * 58)
    if FAIL:
        print(f"❌ {len(FAIL)} 条失败：")
        for f in FAIL: print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")

asyncio.run(main())
