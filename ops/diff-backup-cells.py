#!/usr/bin/env python3
"""比对两份 pg_dump 备份：**哪些列的已填值凭空变少了**。

用来回答「更新版本后数据没了」这类报障——不靠记忆和猜测，直接拿两天的备份
数出每个 (项目/表/列) 的非空格子数，只报变少的。

只读文件，不连数据库、不改任何东西。兼容 Python 3.6（服务器上是 3.6.8）。

用法（服务器上）：
    python3 ops/diff-backup-cells.py /backup/pms-db-A.sql.gz /backup/pms-db-B.sql.gz
    # A 是较早的那份

输出示例：
    🔴 2026-071B / 激光件清单 / 到料日期   11 → 0   (少了 11)
"""
from __future__ import print_function
import gzip
import json
import sys


def _copy_block(path, table):
    """把 `COPY public.<table> (...) FROM stdin;` 到 `\\.` 之间的行吐出来。"""
    head = "COPY public." + table + " "
    inside = False
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not inside:
                if line.startswith(head):
                    inside = True
                continue
            if line.startswith("\\."):
                return
            yield line.rstrip("\n")


def _unesc(s):
    return s.replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


def load(path):
    """→ (fields: {fid: (ds_id, name)}, filled: {(ds_id, fid): 非空数},
           sheets: {ds_id: (project_id, name)}, projects: {pid: code})"""
    fields, sheets, projects = {}, {}, {}

    # projects: id, code 在前两列（pg_dump 按建表列序）
    for line in _copy_block(path, "projects"):
        c = line.split("\t")
        if len(c) >= 2:
            projects[c[0]] = c[1]

    for line in _copy_block(path, "datasheets"):
        c = line.split("\t")
        if len(c) >= 3:
            sheets[c[0]] = (c[1], _unesc(c[2]))

    for line in _copy_block(path, "fields"):
        c = line.split("\t")
        if len(c) >= 3:
            fields[c[0]] = (c[1], _unesc(c[2]))

    filled = {}
    for line in _copy_block(path, "records"):
        c = line.split("\t")
        if len(c) < 4:
            continue
        ds_id, raw = c[1], c[3]
        if raw in ("\\N", ""):
            continue
        try:
            vals = json.loads(_unesc(raw))
        except ValueError:
            continue
        for fid, v in vals.items():
            if str(v).strip():
                key = (ds_id, fid)
                filled[key] = filled.get(key, 0) + 1
    return fields, filled, sheets, projects


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    old_path, new_path = sys.argv[1], sys.argv[2]
    print("旧: %s\n新: %s\n" % (old_path, new_path))

    of, ofill, osheets, oproj = load(old_path)
    nf, nfill, nsheets, nproj = load(new_path)

    drops = []
    for key, n_old in ofill.items():
        n_new = nfill.get(key, 0)
        if n_new < n_old:
            ds_id, fid = key
            # 列可能被删掉重建（fid 变了）——那种情况下列名还在但 fid 不同
            f = of.get(fid) or nf.get(fid) or ("?", "字段#" + fid)
            pid, sheet = osheets.get(ds_id, ("?", "表#" + ds_id))
            drops.append((n_old - n_new, oproj.get(pid, "项目#" + str(pid)),
                          sheet, f[1], n_old, n_new,
                          "列已不存在" if fid not in nf else ""))

    gone_fields = [fid for fid in of if fid not in nf]
    gone_sheets = [d for d in osheets if d not in nsheets]

    if not drops:
        print("✅ 没有任何列的已填值变少。")
    else:
        drops.sort(reverse=True)
        print("⚠️ %d 个 (表/列) 的已填值变少，共少了 %d 个格子：\n"
              % (len(drops), sum(d[0] for d in drops)))
        for d, code, sheet, fname, a, b, note in drops:
            print("  🔴 %-12s / %-14s / %-10s  %3d → %-3d (少 %d) %s"
                  % (code, sheet, fname, a, b, d, note))
    print("\n消失的字段 %d 个，消失的数据表 %d 张。"
          % (len(gone_fields), len(gone_sheets)))
    return 1 if drops else 0


if __name__ == "__main__":
    sys.exit(main())
