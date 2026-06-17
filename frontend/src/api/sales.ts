import { http } from './index'

// 🆕 v3 销售台账 / 下单 / 开票
export interface SalesLedgerRow {
  id: number
  project_id: number
  code: string
  name: string
  status: string
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
}

export interface SalesOrderForm {
  code: string
  name: string
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
  ledger: (params?: { kw?: string; cust_type?: string; contract?: string; sales_uid?: number; balance_month?: string }) =>
    http.get<SalesLedgerList>('/sales/ledger', { params }).then((r) => r.data),

  nextCode: () => http.get<{ code: string }>('/sales/next-code').then((r) => r.data.code),

  // 🆕 可分配销售员名单（拥有 sales/sales_lead 角色的在职用户），台账编辑下拉用
  salespeople: () => http.get<{ id: number; name: string }[]>('/sales/salespeople').then((r) => r.data),

  createOrder: (data: SalesOrderForm) =>
    http.post<{ project_id: number; code: string; order_ids: number[] }>('/sales/orders', data).then((r) => r.data),

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
}

export function fmtMoney(n?: number | null): string {
  if (!n) return '—'
  return '¥' + Number(n).toLocaleString('zh-CN')
}
