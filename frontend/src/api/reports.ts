import { http } from './index'

// 🆕 v3 M14 报表
export interface WorkerStat {
  dept: string; dept_name: string; worker_name: string
  total: number; done: number; ontime: number; over: number
  rate?: number | null; avg_eff?: number | null
}
export interface OverdueItem {
  dept_name: string; worker_name: string; code: string
  due_date?: string | null; done_date?: string | null; over_days: number; eff?: number | null
}
export interface MonthlyReport {
  month: string; total: number; done: number; overdue: number
  ontime_rate?: number | null; avg_eff?: number | null
  dept_cards: any[]; workers: WorkerStat[]; overdue_items: OverdueItem[]
  sales_order_count: number; wh_txn_count: number
}
export interface DeptReport {
  dept: string; dept_name: string; total: number; done: number; overdue: number
  ontime_rate?: number | null; avg_eff?: number | null
  workers: WorkerStat[]; overdue_items: OverdueItem[]
}
export interface SalesReport {
  project_count: number; total_amount: number; invoiced_amount: number; uninvoiced_amount: number
  shipped_count: number; contract_count: number; contract_rate?: number | null; invoice_rate?: number | null
  by_salesperson: any[]; by_cust_type: any[]; by_invoice_state: any[]; receivables: Record<string, number>
}

export const reportsApi = {
  monthly: (month?: string) => http.get<MonthlyReport>('/reports/monthly', { params: { month } }).then((r) => r.data),
  dept: (dept: string, year?: string) =>
    http.get<DeptReport>(`/reports/dept/${dept}`, { params: year ? { year } : {} }).then((r) => r.data),
  sales: () => http.get<SalesReport>('/reports/sales').then((r) => r.data),
}
