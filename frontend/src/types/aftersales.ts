// 售后/安装费用清单的一行（登记弹窗、重传发票弹窗、售后台账、财务部共用）
// 🆕 反馈#445：一行多张发票、几行共用一张发票（同一个附件 id 出现在几行）、没发票的附付款记录、每行备注。

export interface AsFile { id: number; name?: string | null }

export interface CostItem {
  id?: number
  name: string
  amount: number
  note?: string | null
  invoice_files: AsFile[]
  pay_proof_files: AsFile[]
  // 接口仍返回第一张发票（兼容老客户端），新界面不用
  invoice_file_id?: number | null
  invoice_file_name?: string | null
}

export const newCostItem = (): CostItem =>
  ({ name: '', amount: 0, note: '', invoice_files: [], pay_proof_files: [] })

/** 接口返回的一行 → 可编辑的一行。老接口只有 invoice_file_id 时退回它。 */
export function toCostItem(it: any): CostItem {
  const inv: AsFile[] = it?.invoice_files?.length
    ? it.invoice_files
    : (it?.invoice_file_id ? [{ id: it.invoice_file_id, name: it.invoice_file_name }] : [])
  return {
    id: it?.id, name: it?.name || '', amount: Number(it?.amount) || 0, note: it?.note || '',
    invoice_files: inv.map(f => ({ ...f })),
    pay_proof_files: (it?.pay_proof_files || []).map((f: AsFile) => ({ ...f })),
  }
}

/** 提交给后端的一行。invoice_file_id 带上第一张，老后端也认。 */
export function costItemPayload(it: CostItem) {
  return {
    name: it.name.trim(), amount: Number(it.amount) || 0, note: (it.note || '').trim() || null,
    invoice_file_ids: it.invoice_files.map(f => f.id),
    pay_proof_file_ids: it.pay_proof_files.map(f => f.id),
    invoice_file_id: it.invoice_files[0]?.id ?? null,
  }
}

/** 填了东西的行（整行空白的跳过，和后端口径一致） */
export const filledCostRows = (rows: CostItem[]) =>
  rows.filter(it => it.name.trim() || Number(it.amount))

/** 提交前的检查；返回要提示的话，没问题返回 null */
export function costRowsProblem(rows: CostItem[]): string | null {
  const filled = filledCostRows(rows)
  if (!filled.length) return '请至少填一行费用'
  if (filled.some(it => !it.name.trim())) return '有金额没填费用项名称'
  const i = rows.findIndex(it => (it.name.trim() || Number(it.amount))
    && !it.invoice_files.length && !it.pay_proof_files.length)
  if (i >= 0) return `第 ${i + 1} 行「${rows[i].name.trim() || '费用'}」没有发票，要上传付款记录（转账或支付截图）`
  return null
}
