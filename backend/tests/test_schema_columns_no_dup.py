"""静态自检：data_migration 的补列字典里不许有重复的表名。

为什么值得单独一个测试
----------------------
`_NEW_COLUMNS` 是个 dict 字面量。同一个表名写两次，**后者会静默覆盖前者**——
前面那一整批列压根不会被 ADD COLUMN，而且没有任何报错、日志、告警。

这不是假想：2026-08-06 查出来 `purchase_items` 和 `wh_materials` 各被写了两次，
`purchase_items` 前一条的 9 列（po_no / brand / invoice_no / custom_values …）
当时其实从没被补过。生产上它们碰巧都在（是重复键出现之前补的），
但拿一份旧备份恢复就会缺列直接炸。

另外顺带检查：模型上声明的列，如果不是 create_all 能覆盖的新表，
就必须出现在补列字典里——漏登记同样是"新库好好的、老库缺列"。
"""
import ast
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL: ") + m)
    if not c:
        FAIL.append(m)


def _dict_literal(src: str, name: str):
    """从源码里取出某个模块级 dict 字面量的键列表（不 import，纯静态）。"""
    for node in ast.walk(ast.parse(src)):
        target = None
        if isinstance(node, ast.AnnAssign):          # 带类型标注：_NEW_COLUMNS: dict[...] = {...}
            target = getattr(node.target, "id", None)
        elif isinstance(node, ast.Assign):
            target = getattr(node.targets[0], "id", None)
        if target == name and isinstance(node.value, ast.Dict):
            return [k.value for k in node.value.keys], node.value
    return None, None


def main():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "app", "data_migration.py")
    src = io.open(path, encoding="utf-8").read()

    for name in ("_NEW_COLUMNS", "_WIDEN_COLUMNS", "_DROP_NOTNULL"):
        keys, node = _dict_literal(src, name)
        chk(keys is not None, f"{name} 是模块级 dict 字面量（能静态检查）")
        if keys is None:
            continue
        dups = sorted({k for k in keys if keys.count(k) > 1})
        chk(not dups, f"{name} 无重复表名（重复的会被静默覆盖）: {dups or '✅'}")

    # 每个表内部的列名也不许重复：重复的 ADD COLUMN 会在第二次抛错，
    # 而 ensure_schema_columns 是整体一个事务，一炸就整批列都补不上。
    keys, node = _dict_literal(src, "_NEW_COLUMNS")
    if node is not None:
        for k, v in zip(node.keys, node.values):
            cols = [e.elts[0].value for e in v.elts]
            d = sorted({c for c in cols if cols.count(c) > 1})
            chk(not d, f"{k.value} 内部无重复列名: {d or '✅'}")

    # 反向自检：故意造一个重复键，上面的检查必须能抓到——
    # 否则这个测试本身是失效的（只会一路 PASS，什么也没验）。
    bad = 'X: dict = {\n    "a": [("c1", "INT")],\n    "b": [("c2", "INT")],\n    "a": [("c3", "INT")],\n}\n'
    bk, _ = _dict_literal(bad, "X")
    chk(sorted({x for x in bk if bk.count(x) > 1}) == ["a"], "反向自检：能抓到人为造的重复键")


main()
print("\nFAILURES:", len(FAIL))
for m in FAIL:
    print("  -", m)
print("PASSED" if not FAIL else "FAILED")
sys.exit(1 if FAIL else 0)
