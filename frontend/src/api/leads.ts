import { http } from './index'

// 🆕 销售线索跟踪：来源/状态与后端 leads_router 同源
export const LEAD_SOURCES = ['1688', 'Geo', '爱采购', '百度推广', '其他'] as const
export const LEAD_STATUSES = ['潜在需求', '报价', '成交', '丢单'] as const

export interface SalesLeadRow {
  id: number
  source: string
  customer?: string | null
  contact?: string | null
  phone?: string | null
  wechat?: string | null
  requirement?: string | null
  owner_uid?: number | null
  owner_name?: string | null
  status: string
  follow_log?: string | null
  lost_reason?: string | null
  created_by?: number | null
  created_by_name?: string | null
  assigned_at?: string | null
  closed_at?: string | null
  created_at?: string | null
}

export interface SalesLeadList {
  rows: SalesLeadRow[]
  total: number
}

export interface LeadReportItem {
  key: string
  leads: number
  deal: number
  quote: number
  potential: number
  lost: number
  rate: number
}

export interface SalesLeadReport {
  by_source: LeadReportItem[]
  by_owner: LeadReportItem[]
  total_leads: number
  total_deal: number
  total_rate: number
}

export interface LeadForm {
  source: string
  customer?: string
  contact?: string
  phone?: string
  wechat?: string
  requirement?: string
  owner_uid?: number | null
  status?: string
  follow_log?: string
  lost_reason?: string
}

export const leadsApi = {
  list: (params?: { source?: string; owner_uid?: number; status?: string; kw?: string; page?: number; page_size?: number }) =>
    http.get<SalesLeadList>('/sales/leads', { params }).then((r) => r.data),
  create: (data: LeadForm) =>
    http.post<SalesLeadRow>('/sales/leads', data).then((r) => r.data),
  update: (id: number, data: Partial<LeadForm>) =>
    http.put<SalesLeadRow>(`/sales/leads/${id}`, data).then((r) => r.data),
  remove: (id: number) =>
    http.delete<{ message: string }>(`/sales/leads/${id}`).then((r) => r.data),
  report: (params?: { year?: string; month?: string }) =>
    http.get<SalesLeadReport>('/sales/leads/report', { params }).then((r) => r.data),
}
