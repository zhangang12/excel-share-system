# 智能体越权回放审计（只读）：以每个在用非管理层账号的身份，把他能用的工具全跑一遍，逐行核对数据归属。
# 2026-09-30 首次使用，审出 shipment_receiver 不分人、find_entity 项目命中带客户名两处（见交接文档第六节）。
#
# 用法（生产，只读）：
#   ssh root@<服务器> 'docker exec -i pms2_backend python -' < ops/agent-perm-replay.py
# 输出按「工具 × 问题类型」分组，末尾 0 条 = 干净。加新工具时在下面补一段对应的归属核对。
#
# 不写库：跳过唯一会写东西的 mgmt_todo_send（它会建草稿）。
import asyncio, json
from sqlalchemy import select
from app.database import SessionLocal
from app import models
from app.agent import perm, tools_entity as te, tools_sales as ts, tools_tasks as tt
from app.routers import agent_router as ar
from app.routers.purchase_mgmt_router import _buyer_restricted
from app.deps import restricted_dir_pids

ISSUES = []


def issue(u, tool, msg):
    ISSUES.append(f"{u.full_name or u.username}（{','.join(sorted(u.role_codes))}） | {tool} | {msg}")


async def main():
    async with SessionLocal() as db:
        users = (await db.execute(select(models.User).where(models.User.is_active == True))).scalars().unique().all()  # noqa
        # 归属对照表
        led = {p_code: (suid, cust) for p_code, suid, cust in (await db.execute(
            select(models.Project.code, models.SalesLedger.sales_uid, models.SalesLedger.customer)
            .join(models.SalesLedger, models.SalesLedger.project_id == models.Project.id))).all()}
        pid_by_code = {c: i for i, c in (await db.execute(select(models.Project.id, models.Project.code))).all()}
        name_by_uid = {u.id: (u.full_name or u.username) for u in users}
        all_codes = [c for c in pid_by_code if c.startswith("2026-0")]
        ran = 0
        for u in users:
            if u.has_role("admin", "manager"):
                continue
            allowed = ar._allowed_tools(u)
            me = u.full_name or u.username
            restricted_buyer = _buyer_restricted(u)
            scope = perm.money_scope(u)
            vis = await perm.visible_pids(db, u)

            def own_ledger(code):
                s = led.get(code)
                return s is not None and s[0] == u.id

            # 隐藏页签：资金面板+项目毛利都藏了（又不是销售），钱类工具就不该给（独立于 perm 的判断，免得自证）
            hidden = set(u.hidden_tabs or [])
            if {"finance:fund", "finance:pnl"} <= hidden and not u.has_role("sales", "sales_lead"):
                leak = allowed & {"balance_due", "receivable_blind", "get_customer", "sales_summary", "ledger_incomplete"}
                if leak:
                    issue(u, "隐藏页签", f"藏了资金面板/项目毛利却仍给钱类工具 {sorted(leak)}")
            if not u.has_role("sales", "sales_lead"):
                for t in allowed & {"leads_followup", "order_pending"}:
                    issue(u, t, "非销售部拿到了线索/待审订单工具（网页上只给销售部）")

            for tool in sorted(allowed):
                if tool in ("mgmt_todo_send",):
                    continue
                args = {}
                if tool == "project_status":
                    for code in [c for c in all_codes if c in led and led[c][0] and led[c][0] != u.id][:5]:
                        r = await ar._run_tool_inner(tool, {"code": code}, db, u); ran += 1
                        if isinstance(r, dict) and r.get("ledger") and scope != "all":
                            issue(u, tool, f"台账段（客户/合同额）没按归属脱敏 {code}")
                    continue
                if tool == "leads_followup":
                    r = await ar._run_tool_inner(tool, {"limit": 200}, db, u); ran += 1
                    for it in (r.get("items") or []):
                        ld = await db.get(models.SalesLead, it["id"])
                        if ld and ld.owner_uid != u.id and not u.has_role("sales_lead"):
                            issue(u, tool, f"看到别人的线索 {it.get('customer')}")
                    continue
                if tool == "get_project":
                    # 拿一个他**看不到**的项目去问（受限岗位），再拿一个有台账的去问（看钱有没有漏）
                    targets = []
                    if vis is not None:
                        hidden = [c for c in all_codes if pid_by_code[c] not in vis
                                  and u.id not in (((await db.get(models.Project, pid_by_code[c])).extra or {}).get("__viz_uids__") or [])]
                        targets += hidden[:3]
                    targets += [c for c in all_codes if c in led and led[c][0] and led[c][0] != u.id][:3]
                    for code in targets:
                        r = await ar._run_tool_inner("get_project", {"code": code}, db, u)
                        ran += 1
                        if not isinstance(r, dict) or r.get("error"):
                            continue
                        snaps = r.get("projects") or ([r] if r.get("found") else [])
                        for sn in snaps:
                            pid = pid_by_code.get(sn.get("project"))
                            if vis is not None and pid not in vis:
                                p = await db.get(models.Project, pid)
                                if u.id not in ((p.extra or {}).get("__viz_uids__") or []):
                                    issue(u, tool, f"看到了不归他的项目 {sn.get('project')}")
                            if sn.get("ledger") and not (scope == "all" or (scope == "own" and own_ledger(sn.get("project")))):
                                issue(u, tool, f"{sn.get('project')} 的台账（客户/合同额）没脱敏")
                            if sn.get("shipment_receiver") and not u.has_role("logistics") and not (
                                    scope == "all" or (scope == "own" and own_ledger(sn.get("project")))):
                                issue(u, tool, f"{sn.get('project')} 的收货人姓名没脱敏")
                    continue
                if tool == "get_customer":
                    for cust in ["英菲尼蒂", "泰威尔", "米创"]:
                        r = await ar._run_tool_inner(tool, {"name": cust}, db, u); ran += 1
                        for it in (r.get("items") or []):
                            if scope != "all" and not own_ledger(it.get("project_code")):
                                issue(u, tool, f"客户全景里有别人的台账 {it.get('project_code')}")
                    continue
                if tool == "get_supplier":
                    for sup in ["立旸", "中砂", "众邦", "东翔"]:
                        r = await ar._run_tool_inner(tool, {"name": sup}, db, u); ran += 1
                        if restricted_buyer and isinstance(r, dict) and r.get("found"):
                            n_all = (await db.execute(select(models.PurchaseItem).join(models.Supplier)
                                     .where(models.Supplier.name == r["supplier"]))).scalars().all()
                            mine = [i for i in n_all if i.buyer_id == u.id]
                            if r.get("purchase_items", 0) > len(mine):
                                issue(u, tool, f"{r['supplier']} 统计了别人下的单：{r['purchase_items']} 条 > 自己的 {len(mine)} 条")
                    continue
                if tool == "get_material":
                    args = {"q": "轴承"}
                if tool == "find_entity":
                    for q in ["英菲尼蒂", "2026-0", "立旸"]:
                        r = await ar._run_tool_inner(tool, {"q": q}, db, u); ran += 1
                        m = r.get("matches") or {}
                        if m.get("customer") and scope == "none":
                            issue(u, tool, f"没有看钱权限却搜到了客户：{[x['customer'] for x in m['customer']][:3]}")
                        if m.get("customer") and scope == "own":
                            mycust = {c for code, (s, c) in led.items() if s == u.id}
                            extra = [x["customer"] for x in m["customer"] if x["customer"] not in mycust]
                            if extra:
                                issue(u, tool, f"销售搜到了不归他的客户：{extra[:3]}")
                        for p in (m.get("project") or []):
                            if vis is not None and p["id"] not in vis:
                                pp = await db.get(models.Project, p["id"])
                                if u.id not in ((pp.extra or {}).get("__viz_uids__") or []):
                                    issue(u, tool, f"搜到了不归他的项目 {p['code']}")
                            if p.get("customer") and (scope == "none" or (scope == "own" and not own_ledger(p["code"]))):
                                issue(u, tool, f"项目搜索结果带出了客户名 {p['code']}·{p['customer']}")
                    continue

                r = await ar._run_tool_inner(tool, args, db, u)
                ran += 1
                if not isinstance(r, dict):
                    continue
                rows = r.get("items") or r.get("rows") or []
                # 晨报：拆开各段一起查
                if tool == "morning_report":
                    sec = r
                    for k in ("po_arrival_overdue", "po_overdue", "purchase"):
                        if isinstance(sec.get(k), dict):
                            for it in sec[k].get("top") or sec[k].get("items") or []:
                                if restricted_buyer and it.get("buyer") and it["buyer"] != me:
                                    issue(u, "morning_report·采购段", f"看到别人（{it['buyer']}）的采购 {it.get('po_no')}")
                    for k in ("balance_due", "balance"):
                        if isinstance(sec.get(k), dict):
                            for it in sec[k].get("top") or sec[k].get("items") or []:
                                code = it.get("project_code")
                                if scope == "none" or (scope == "own" and not own_ledger(code)):
                                    issue(u, "morning_report·尾款段", f"看到不归他的尾款 {code} {it.get('customer')}")
                    for k in ("overdue_orders", "orders"):
                        if isinstance(sec.get(k), dict):
                            for it in sec[k].get("top") or sec[k].get("items") or []:
                                if it.get("worker") and it["worker"] != me and not any(
                                        c.endswith("_lead") for c in u.role_codes):
                                    issue(u, "morning_report·逾期段", f"工人看到别人（{it['worker']}）的逾期任务 {it.get('project_code')}")
                    continue
                for it in rows:
                    if tool.startswith("po_") and restricted_buyer and it.get("buyer") and it["buyer"] != me:
                        issue(u, tool, f"看到别人（{it['buyer']}）的采购 {it.get('po_no')}")
                    if tool.startswith("po_") and restricted_buyer and tool == "po_arriving":
                        # po_arriving 的行里没有 buyer 字段，按单号回库核对
                        its = (await db.execute(select(models.PurchaseItem).where(
                            models.PurchaseItem.po_no == it.get("po_no"),
                            models.PurchaseItem.item_name == it.get("item_name")))).scalars().all()
                        if its and all(i.buyer_id != u.id for i in its):
                            issue(u, tool, f"看到别人下的单 {it.get('po_no')}·{it.get('item_name')}（{name_by_uid.get(its[0].buyer_id)}）")
                    if tool in ("balance_due", "receivable_blind", "ledger_incomplete", "invoice_pending",
                                "order_pending", "shipment_receiver", "sales_summary"):
                        code = it.get("project_code") or it.get("code") or it.get("project")
                        if code and scope == "own" and not own_ledger(code) and not (
                                tool == "shipment_receiver" and u.has_role("logistics")):
                            issue(u, tool, f"看到不归他的台账 {code}")
                        if code and scope == "none" and tool != "shipment_receiver":
                            issue(u, tool, f"没有看钱权限却拿到台账 {code}")
                    if tool == "project_progress":
                        pid = pid_by_code.get(it.get("project"))
                        if vis is not None and pid not in vis:
                            pp = await db.get(models.Project, pid)
                            if u.id not in ((pp.extra or {}).get("__viz_uids__") or []):
                                issue(u, tool, f"交期看板里有不归他的项目 {it.get('project')}")
                        if ("contract" in it or "customer" in it) and not (
                                scope == "all" or (scope == "own" and own_ledger(it.get("project")))):
                            issue(u, tool, f"交期看板带出了 {it.get('project')} 的客户/合同额")
                    if tool == "overdue_orders" and it.get("worker") and it["worker"] != me and not any(
                            c.endswith("_lead") for c in u.role_codes):
                        issue(u, tool, f"工人看到别人（{it['worker']}）的逾期任务 {it.get('project_code')}")
        print(f"回放账号 {sum(1 for u in users if not u.has_role('admin','manager'))} 个，工具调用 {ran} 次")
        print(f"发现问题 {len(ISSUES)} 条")
        import re
        grp = {}
        for x in ISSUES:
            who, tool, msg = x.split(" | ", 2)
            kind = re.split(r"[ ：（]", msg, 1)[0]
            g = grp.setdefault((tool, kind), {"n": 0, "who": {}, "eg": msg})
            g["n"] += 1
            g["who"][who] = g["who"].get(who, 0) + 1
        for (tool, kind), g in sorted(grp.items(), key=lambda kv: -kv[1]["n"]):
            print(f"\n【{tool}】{kind} —— 共 {g['n']} 条，涉及 {len(g['who'])} 个账号")
            print("   例：", g["eg"])
            print("   账号：", "；".join(f"{w}×{n}" for w, n in sorted(g["who"].items(), key=lambda kv: -kv[1])))

asyncio.run(main())
