import { http } from './index'

// ===== 🆕 采购模块 API（供应商 / 采购明细 / 账目一览 / 汇总报表） =====

export interface SupplierRow {
  id: number
  name: string
  category?: string | null
  contact?: string | null
  phone?: string | null
  address?: string | null
  tax_no?: string | null
  bank_name?: string | null
  bank_account?: string | null
  settle_type?: string | null
  settle_days?: number | null
  note?: string | null
  status: string
  // 账目一览口径
  recv_total: number
  invoiced: number
  to_invoice: number
  paid: number
  owed: number
  item_count: number
}

export interface SupplierForm {
  name: string
  category?: string | null
  contact?: string | null
  phone?: string | null
  address?: string | null
  tax_no?: string | null
  bank_name?: string | null
  bank_account?: string | null
  settle_type?: string | null
  settle_days?: number | null
  note?: string | null
  status?: string | null
}

export interface SupplierOption {
  id: number
  name: string
  category?: string | null
  settle_type?: string | null
  settle_days?: number | null
}

export interface PurchaseItemRow {
  id: number
  recon_status: string
  delivery_date?: string | null
  supplier_id: number
  supplier_name?: string | null
  contract_no?: string | null
  project_no?: string | null
  delivery_no?: string | null
  item_name?: string | null
  spec?: string | null
  qty?: number | null
  unit_price?: number | null
  recv_amount: number
  invoice_date?: string | null
  tax_rate?: string | null
  invoice_amount: number
  to_invoice: number
  pay_date?: string | null
  pay_amount: number
  owed: number
  buyer_uid?: number | null
  buyer_name?: string | null
  note?: string | null
  created_at?: string | null
}

export interface PurchaseItemForm {
  supplier_id: number
  delivery_date?: string | null
  contract_no?: string | null
  project_no?: string | null
  delivery_no?: string | null
  item_name?: string | null
  spec?: string | null
  qty?: number | null
  unit_price?: number | null
  recv_amount?: number | null
  invoice_date?: string | null
  tax_rate?: string | null
  invoice_amount?: number | null
  pay_date?: string | null
  pay_amount?: number | null
  buyer_uid?: number | null
  recon_status?: string | null
  note?: string | null
}

export interface PurchaseItemList {
  rows: PurchaseItemRow[]
  total: number
  recv_total: number
  invoiced: number
  to_invoice: number
  paid: number
  owed: number
}

export interface TrendBucket {
  key: string
  recv: number
  invoiced: number
  paid: number
  owed: number
}
export interface BuyerStat { key: string; recv: number; paid: number; owed: number; count: number }
export interface SupplierStat { key: string; recv: number; owed: number }

export interface ProcureSummary {
  month_total: number
  quarter_total: number
  year_total: number
  recv_total: number
  owed_total: number
  monthly: TrendBucket[]
  by_buyer: BuyerStat[]
  top_suppliers: SupplierStat[]
}

export const SUPPLIER_CATEGORIES = ['外协加工', '标准件', '不锈钢原料', '激光件', '电气件', '运输', '其他']
export const SETTLE_TYPES = ['现金', '月结', '无账期']
export const RECON_STATUSES = ['待对账', '已对账']

export const procureApi = {
  // 供应商
  listSuppliers: (params?: { category?: string; status?: string; kw?: string }) =>
    http.get<{ rows: SupplierRow[]; total: number }>('/procure/suppliers', { params }).then(r => r.data),
  supplierOptions: () =>
    http.get<SupplierOption[]>('/procure/suppliers/options').then(r => r.data),
  createSupplier: (data: SupplierForm) =>
    http.post<SupplierRow>('/procure/suppliers', data).then(r => r.data),
  updateSupplier: (id: number, data: SupplierForm) =>
    http.put<{ message: string }>(`/procure/suppliers/${id}`, data).then(r => r.data),
  deleteSupplier: (id: number) =>
    http.delete<{ message: string }>(`/procure/suppliers/${id}`).then(r => r.data),
  importSuppliers: (names: string[], category?: string) =>
    http.post<{ message: string }>('/procure/suppliers/import', { names, category }).then(r => r.data),

  // 采购明细
  listItems: (params?: {
    supplier_id?: number; project_no?: string; buyer_uid?: number; recon_status?: string
    month?: string; kw?: string; page?: number; page_size?: number
  }) => http.get<PurchaseItemList>('/procure/items', { params }).then(r => r.data),
  createItem: (data: PurchaseItemForm) =>
    http.post<PurchaseItemRow>('/procure/items', data).then(r => r.data),
  updateItem: (id: number, data: PurchaseItemForm) =>
    http.put<PurchaseItemRow>(`/procure/items/${id}`, data).then(r => r.data),
  deleteItem: (id: number) =>
    http.delete<{ message: string }>(`/procure/items/${id}`).then(r => r.data),
  batchInvoice: (ids: number[], date?: string) =>
    http.post<{ message: string }>('/procure/items/batch-invoice', { ids, date }).then(r => r.data),
  batchPay: (ids: number[], date?: string) =>
    http.post<{ message: string }>('/procure/items/batch-pay', { ids, date }).then(r => r.data),
  batchReconcile: (ids: number[]) =>
    http.post<{ message: string }>('/procure/items/batch-reconcile', { ids }).then(r => r.data),

  // 汇总报表
  summary: (year?: string) =>
    http.get<ProcureSummary>('/procure/summary', { params: { year } }).then(r => r.data),
}
