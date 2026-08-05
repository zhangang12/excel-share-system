"""🆕 明细渲染的两条底线：**绝不漏内部结构**、**不瞎分组**。

生产事故（2026-08-06）：问「项目进度跟进」，回答里出现了这样的行——

    **其他**（26）
    - {'dept': 'electric', 'dept_name': '电工部', 'project_code': '2026-057', ...}

**裸 Python dict 直接甩给了用户。** 成因链：
 1. 模型这一轮调了**两个**工具（project_progress + overdue_orders）；
 2. `last_result` 留的是**最后一个**（overdue_orders）；
 3. 而模型写的编排块用的是**前一个**工具的字段名；
 4. `row()` 按那份 fields 一个都没命中 → 走到 `str(item)` 兜底 → 漏结构；
 5. `group:"urgency"` 在这批数据上也不存在 → 全落进「其他」，标题毫无信息量。

⚠️ 这与 `apply_render` 里「模型给坏 JSON 就只删块不渲染」是同一条纪律：
   **宁可少一段明细，也不能把内部结构甩出去。**
"""
import os, sys, tempfile

tmp = tempfile.mkdtemp(prefix="render")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/test.db"
os.environ["FILES_DIR"] = f"{tmp}/files"
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from app.agent import render as rd

FAIL = []
def chk(c, m):
    if not c: FAIL.append(m); print("FAIL:", m)
    else: print("  ok:", m)


# 生产上那批真实数据的形状（overdue_orders 的行）
OVERDUE = {"count": 26, "shown": 8, "items": [
    {"dept": "electric", "dept_name": "电工部", "project_code": f"2026-0{50+i}",
     "worker": "宋朴", "due_date": "2026-06-23", "over_days": 44 - i}
    for i in range(8)]}

# 交期看板的行（字段完全不同）
BOARD = {"count": 3, "shown": 3, "items": [
    {"project": "2026-045B", "name": "中转罐", "deliver_date": "2026-06-10",
     "days_left": -55, "urgency": "已过交货日", "blocked_at": "电工未完成"},
    {"project": "2026-071A", "name": "混合机", "deliver_date": "2026-08-29",
     "days_left": 25, "urgency": "30 天内交货", "blocked_at": "采购未到货 5 项"},
    {"project": "2026-008", "name": "封尾机", "deliver_date": "2026-02-04",
     "days_left": -181, "urgency": "已发货 · 只差收尾", "blocked_at": "已发货，待收尾"},
]}

print("===== 1. 字段清单与数据对不上：忽略它，别漏结构 =====")
bad_plan = {"group": "urgency",
            "fields": ["project", "name", "deliver_date", "days_left", "blocked_at"]}
out = rd.table(OVERDUE, plan=bad_plan)
chk("{'dept'" not in out and "'project_code':" not in out,
    "**没有裸 dict 漏出去**（这是本测试最主要的目标）")
chk("**其他**" not in out, "分组字段不存在时不分组（全落「其他」比不分组还糟）")
chk("2026-050" in out and "超 44 天" in out, "退回按默认顺序自己挑字段，明细照样出得来")
chk("宋朴" in out, "人名等信息没丢")

print("\n===== 2. 一个字段都渲染不出来的行：跳过，不是打印 dict =====")
junk = {"count": 2, "shown": 2, "items": [
    {"foo": "bar", "baz": 1}, {"project_code": "2026-001", "over_days": 3}]}
out2 = rd.table(junk, plan={"fields": ["nope"]})
chk("'foo'" not in out2 and "bar" not in out2, "渲染不出的行被跳过，没有打印原始 dict")
chk("2026-001" in out2, "能渲染的行照常出")
chk(rd.row({"foo": "bar"}) is None, "row() 对渲染不出的行返回 None")

print("\n===== 3. 分组字段确实存在时才分组 =====")
out3 = rd.table(BOARD, plan={"group": "urgency"})
chk("**已过交货日**（1）" in out3, "按 urgency 正常分组")
chk(out3.index("已过交货日") < out3.index("已发货 · 只差收尾"),
    "保持工具排好的顺序，不重排")

print("\n===== 4. 每组有条数上限：手机上不铺满屏 =====")
many = {"count": 30, "shown": 30, "items": [
    {"project": f"2026-{i:03d}", "days_left": -i, "urgency": "已过交货日"}
    for i in range(1, 31)]}
out4 = rd.table(many, plan={"group": "urgency"})
body = [l for l in out4.split("\n") if l.startswith("- ") and "另有" not in l]
chk(len(body) <= rd._GROUP_MAX,
    f"单组最多 {rd._GROUP_MAX} 行（实际 {len(body)} 行）")
chk("本组另有 24 条" in out4, "剩下的说清有多少，不是悄悄吞掉")

print("\n===== 5. 单行字段数受控（手机一行放得下）=====")
wide = {"count": 1, "shown": 1, "items": [{
    "supplier": "某供应商", "item_name": "件", "project_code": "2026-001",
    "spec": "规格", "amount": 12345, "over_days": 3, "due_date": "2026-01-01",
    "worker": "张三", "dept_name": "电工部", "po_no": "PO-1"}]}
line = rd.table(wide, plan=None).split("\n")[0]
chk(line.count("·") <= 4, f"一行最多 5 个字段（实际 {line.count('·')+1} 个）")

print("\n===== 6. 项目编号必须出现在明细里（人是按编号认单的）=====")
out6 = rd.table(BOARD, plan={"group": "urgency"})
for code in ("2026-045B", "2026-071A", "2026-008"):
    chk(code in out6, f"{code} 出现在明细里")
chk(out6.index("2026-045B") < out6.index("中转罐"),
    "编号排在设备名前面 —— 只给「300L平台式中转罐」对不上号")

print("\n===== 7. 分组时 sort 只在组内生效，不跨组重排 =====")
# ⚠️ 模型给 sort:days_left,desc:false 时，「已发货·只差收尾」（过期 181 天）
#    会被顶到最前 —— 而那恰恰是最不该占首位的一类。组的先后由工具定。
out7 = rd.table(BOARD, plan={"group": "urgency", "sort": "days_left", "desc": False})
chk(out7.index("已过交货日") < out7.index("已发货 · 只差收尾"),
    "组序仍按工具给的来，没有被模型的 sort 推翻")
grouped = {"count": 4, "shown": 4, "items": [
    {"project": "A", "days_left": -5, "urgency": "已过交货日"},
    {"project": "B", "days_left": -50, "urgency": "已过交货日"},
    {"project": "C", "days_left": 3, "urgency": "7 天内交货"}]}
out7b = rd.table(grouped, plan={"group": "urgency", "sort": "days_left", "desc": False})
chk(out7b.index("- B") < out7b.index("- A"), "组内按 sort 排（-50 在 -5 前面）")

print("\nPASSED" if not FAIL else f"\n{len(FAIL)} FAILURES")
sys.exit(1 if FAIL else 0)
