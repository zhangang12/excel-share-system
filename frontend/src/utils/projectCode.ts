/**
 * 项目编号的自然排序。
 *
 * 🆕 2026-09-28 反馈#438（赵仁辉）：财务「项目材料成本」点「项目」表头排出来的顺序
 * 跟销售部台账不一样——el-table 默认是按字符串比，而编号里有 071A/071B、041M补、备06 这种，
 * 字符串比会排得乱。
 *
 * ⚠️ 规则必须与后端 `sales_router.code_sort_key` 一字不差：
 *   标准编号 YYYY-NNN[字母后缀] 在前，按 年 → 序号（数值）→ 后缀；
 *   其余（带中文的、备机编号等）排在后面，按字符串。
 * 两边不一致的话，「打开时的顺序」（后端排的）和「点表头后的顺序」（这里排的）会对不上。
 */
const STD = /^(\d{4})-0*(\d+)([A-Za-z]*)$/

function key(code: string | null | undefined): [number, number, number, string, string] {
  const c = code || ''
  const m = STD.exec(c)
  if (m) return [0, Number(m[1]), Number(m[2]), m[3], '']
  return [1, 0, 0, '', c]
}

export function compareProjectCode(a: string | null | undefined, b: string | null | undefined): number {
  const ka = key(a)
  const kb = key(b)
  for (let i = 0; i < ka.length; i++) {
    if (ka[i] < kb[i]) return -1
    if (ka[i] > kb[i]) return 1
  }
  return 0
}
