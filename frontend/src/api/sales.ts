import { http } from './index'

// 🆕 v3 销售台账 / 下单 / 开票
export interface SalesLedgerRow {
  id: number
  project_id: number
  code: string
  name: string
  status: string
  qty?: number | null
  unit?: string | null
  sales_uid?: number | null
  sales_name?: string | null
  customer?: string | null
  cust_type?: string | null
  sign_date?: string | null
  deliver_date?: string | null
  contract: string
  contract_file_id?: number | null
  contract_file_name?: string | null
  amount: number
  tax_rate?: string | null
  invoice_state?: 'applying' | 'pending_invoice' | 'invoiced' | null
  invoice_batch_id?: number | null   // 🆕 合并开票批次号；同批多行共享，None=单项目
  void_state?: 'applying' | 'voided' | null   // 🆕 订单作废流：None 正常 / applying 待审批 / voided 已作废
  void_reason?: string | null
  order_state?: 'pending' | 'draft' | null    // 🆕 下单审批流：None 已生效 / pending 待审批 / draft 被退回
  order_reject_reason?: string | null
  pending_order?: { depts?: string[]; req_text?: string; receiver?: { name?: string; phone?: string; addr?: string } } | null
  invoice_apply_file_id?: number | null
  invoice_apply_file_name?: string | null
  invoice_file_id?: number | null
  invoice_file_name?: string | null
  prepay: number
  prepay_note?: string | null
  before_ship: number
  before_ship_note?: string | null
  ship_receivable: number
  balance: number
  balance_date?: string | null
  ship_date?: string | null
}

export interface SalesLedgerTotals {
  count: number
  amount: number
  uninvoiced: number
  prepay: number
  before_ship: number
  ship_receivable: number
  balance: number
}

export interface SalesLedgerList {
  rows: SalesLedgerRow[]
  totals?: SalesLedgerTotals | null
  total?: number | null   // 🆕 分页总条数
}

export interface SalesOrderForm {
  code: string
  name: string
  qty: number
  unit: string
  customer: string
  cust_type: string
  contract: string
  amount: number
  tax_rate: string
  prepay: number
  prepay_note: string
  before_ship: number
  before_ship_note: string
  ship_receivable: number
  balance: number
  balance_date: string
  depts: string[]
  req_text: string
  receiver: { name: string; phone: string; addr: string }
}

export const salesApi = {
  ledger: (params?: { kw?: string; cust_type?: string; contract?: string; sales_uid?: number; balance_month?: string; page?: number; page_size?: number }) =>
    http.get<SalesLedgerList>('/sales/ledger', { params }).then((r) => r.data),

  nextCode: (year?: string) =>
    http.get<{ code: string }>('/sales/next-code', { params: year ? { year } : {} }).then((r) => r.data.code),

  // 🆕 可分配销售员名单（拥有 sales/sales_lead 角色的在职用户），台账编辑下拉用
  salespeople: () => http.get<{ id: number; name: string }[]>('/sales/salespeople').then((r) => r.data),

  createOrder: (data: SalesOrderForm) =>
    http.post<{ project_id: number; code: string; order_ids: number[]; ledger_id?: number }>('/sales/orders', data).then((r) => r.data),

  // 🆕 待审批/草稿下单的下单资料暂存（审批通过后自动转挂各部门任务）
  pendingFiles: (lid: number, files: File[]) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    return http.post(`/sales/ledger/${lid}/pending-files`, fd).then((r) => r.data)
  },

  updateLedger: (id: number, data: Partial<SalesLedgerRow>) =>
    http.put(`/sales/ledger/${id}`, data).then((r) => r.data),

  // 🆕 收款批注（预付/发货前付）独立更新，销售本人即可记录
  paymentNote: (id: number, field: 'prepay' | 'before_ship', note: string) =>
    http.put(`/sales/ledger/${id}/payment-note`, { field, note }).then((r) => r.data),

  uploadContract: (id: number, file: File, signDate: string, deliverDate: string) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('sign_date', signDate)
    fd.append('deliver_date', deliverDate)
    return http.post(`/sales/ledger/${id}/contract`, fd).then((r) => r.data)
  },

  invoiceApply: (id: number, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.post(`/sales/ledger/${id}/invoice-apply`, fd).then((r) => r.data)
  },

  invoiceApprovals: () => http.get<SalesLedgerList>('/sales/invoice-approvals').then((r) => r.data),
  invoiceApprove: (id: number) => http.post(`/sales/ledger/${id}/invoice-approve`).then((r) => r.data),
  invoiceReject: (id: number) => http.post(`/sales/ledger/${id}/invoice-reject`).then((r) => r.data),

  // 🆕 合并开票：勾选同客户多个项目(≥2)，上传一份合并开票申请表
  invoiceApplyMerge: (ledgerIds: number[], file: File) => {
    const fd = new FormData()
    fd.append('ledger_ids', ledgerIds.join(','))
    fd.append('file', file)
    return http.post('/sales/invoice-apply-merge', fd).then((r) => r.data)
  },
  invoiceBatchApprove: (batchId: number) =>
    http.post(`/sales/invoice-batch/${batchId}/approve`).then((r) => r.data),
  invoiceBatchReject: (batchId: number) =>
    http.post(`/sales/invoice-batch/${batchId}/reject`).then((r) => r.data),

  // 🆕 订单作废：销售员申请→负责人审批(负责人调用即一键直接作废)
  voidApply: (id: number, reason: string) =>
    http.post<{ message: string }>(`/sales/ledger/${id}/void-apply`, { reason }).then((r) => r.data),
  voidApprovals: () => http.get<SalesLedgerList>('/sales/void-approvals').then((r) => r.data),
  voidApprove: (id: number) => http.post(`/sales/ledger/${id}/void-approve`).then((r) => r.data),
  voidReject: (id: number) => http.post(`/sales/ledger/${id}/void-reject`).then((r) => r.data),

  // 🆕 销售下单审批：仅销售员下单需主管审批(开关 SALES_ORDER_APPROVAL 开启时)
  orderApprovals: () => http.get<SalesLedgerList>('/sales/order-approvals').then((r) => r.data),
  orderApprove: (id: number) => http.post(`/sales/ledger/${id}/order-approve`).then((r) => r.data),
  orderReject: (id: number, reason: string) =>
    http.post(`/sales/ledger/${id}/order-reject`, { reason }).then((r) => r.data),
  // 修改被退回的草稿并重新提交审批
  draftResubmit: (id: number, data: SalesOrderForm) =>
    http.put<{ project_id: number; code: string; ledger_id?: number }>(`/sales/orders/${id}/draft-resubmit`, data).then((r) => r.data),
  orderDiscard: (id: number) => http.post(`/sales/ledger/${id}/order-discard`).then((r) => r.data),
}

export function fmtMoney(n?: number | null): string {
  if (!n) return '—'
  return '¥' + Number(n).toLocaleString('zh-CN')
}
