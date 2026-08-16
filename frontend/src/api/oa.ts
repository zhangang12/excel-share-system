import { http } from './index'

export interface Department {
  id: number; name: string; lead_role?: string | null
  // 🆕 这个部门的报销费用计入哪个成本科目；空 = 不计入
  cost_center?: string | null
  sort_order: number; enabled: boolean
}
// 🆕 成本归集报表
export interface CostRow {
  cost_center: string; source: string; source_label: string; count: number; amount: number
}
export interface CostSummary {
  period: string; rows: CostRow[]; by_center: Record<string, number>
  total: number; notes: string[]
}
export interface OaDocType {
  id: number; key: string; category: string; category_label: string; label: string
  sort_order: number; enabled: boolean
}
export interface OaApprovalStep {
  id: number; department_id: number; doc_type: string; step_order: number
  approver_role: string
  // 🆕 指定到人：填了就只有这个人(及其代理人)能批
  approver_user_id?: number | null; approver_name?: string | null
  step_label?: string | null; enabled: boolean
}
export interface OaRequestStep {
  id: number; step_order: number; approver_role: string
  approver_user_id?: number | null; approver_name?: string | null
  // 这一步什么时候轮到的；deputy_ready=代理人已经可以接手了
  activated_at?: string | null; deputy_ready?: boolean
  step_label?: string | null
  status: string; acted_by?: number | null; actor_name?: string | null
  acted_at?: string | null; note?: string | null
}
export interface OaCcUser { id: number; name: string }
export interface OaRequest {
  id: number; request_no: string; category: string; doc_type: string
  department_id: number; department_name: string
  requester_id: number; requester_name: string
  title?: string | null; amount?: number | null; detail: Record<string, any>
  related_request_id?: number | null; related_request_no?: string | null
  status: string; current_step_order?: number | null
  settle_amount?: number | null; settle_note?: string | null; reject_reason?: string | null
  // 🆕 #395 财务付款备注/时间（回单在附件里，kind=pay_receipt）
  pay_note?: string | null; pay_at?: string | null
  created_at: string; updated_at: string
  steps: OaRequestStep[]
  cc_users: OaCcUser[]   // 🆕 抄送人
  can_approve: boolean; can_withdraw: boolean; can_mark_paid: boolean
}
export interface OaSummaryRow {
  department_id: number; department_name: string; doc_type: string; count: number; amount: number
}
// 🆕 #247 汇总报表下钻明细
export interface OaSummaryDetailRow {
  id: number; request_no: string; requester_name?: string | null; title?: string | null
  amount: number; settled: boolean; created_at: string; updated_at: string
}
// 🆕 已配置审批流程一览
export interface OaChainOverviewStep {
  step_order: number; approver_role: string; role_name: string
  approver_user_id?: number | null; approver_name?: string | null
  step_label?: string | null; enabled: boolean
}
export interface OaChainOverviewRow {
  department_id: number; department_name: string; doc_type: string; doc_label: string
  steps: OaChainOverviewStep[]
}

export const oaApi = {
  docTypes: () => http.get<OaDocType[]>('/oa/doc-types').then(r => r.data),
  createDocType: (body: { key: string; category: string; label: string; sort_order: number; enabled: boolean }) =>
    http.post<OaDocType>('/oa/doc-types', body).then(r => r.data),
  updateDocType: (id: number, body: { key: string; category: string; label: string; sort_order: number; enabled: boolean }) =>
    http.put<OaDocType>(`/oa/doc-types/${id}`, body).then(r => r.data),
  deleteDocType: (id: number) => http.delete<{ message: string }>(`/oa/doc-types/${id}`).then(r => r.data),

  departments: (enabledOnly = false) =>
    http.get<Department[]>('/oa/departments', { params: { enabled_only: enabledOnly } }).then(r => r.data),
  createDepartment: (body: Partial<Department>) => http.post<Department>('/oa/departments', body).then(r => r.data),
  updateDepartment: (id: number, body: Partial<Department>) =>
    http.put<Department>(`/oa/departments/${id}`, body).then(r => r.data),
  deleteDepartment: (id: number) => http.delete<{ message: string }>(`/oa/departments/${id}`).then(r => r.data),

  chainSteps: (departmentId: number, docType: string) =>
    http.get<OaApprovalStep[]>('/oa/chains', { params: { department_id: departmentId, doc_type: docType } }).then(r => r.data),
  costSummary: (period?: string) =>
    http.get<CostSummary>('/oa/reports/cost', { params: period ? { period } : {} }).then(r => r.data),
  chainsOverview: () => http.get<OaChainOverviewRow[]>('/oa/chains/overview').then(r => r.data),
  // 🆕 #200 流程级固定抄送(角色)
  flowCc: (departmentId: number, docType: string) =>
    http.get<{ roles: string[] }>('/oa/flow-cc', { params: { department_id: departmentId, doc_type: docType } }).then(r => r.data),
  saveFlowCc: (departmentId: number, docType: string, roles: string[]) =>
    http.put('/oa/flow-cc', { department_id: departmentId, doc_type: docType, roles }).then(r => r.data),
  // 🆕 #199 管理层删除申请单
  deleteRequest: (id: number) => http.delete(`/oa/requests/${id}`).then(r => r.data),
  createChainStep: (body: Partial<OaApprovalStep>) => http.post<OaApprovalStep>('/oa/chains', body).then(r => r.data),
  updateChainStep: (id: number, body: Partial<OaApprovalStep>) =>
    http.put<OaApprovalStep>(`/oa/chains/${id}`, body).then(r => r.data),
  deleteChainStep: (id: number) => http.delete<{ message: string }>(`/oa/chains/${id}`).then(r => r.data),

  // 🆕 抄送人可选名单（在职用户）
  ccCandidates: () => http.get<OaCcUser[]>('/oa/cc-candidates').then(r => r.data),

  createRequest: (body: {
    category: string; doc_type: string; department_id: number
    title?: string; amount?: number | null; detail?: Record<string, any>; related_request_id?: number | null
    cc_user_ids?: number[]
  }) => http.post<OaRequest>('/oa/requests', body).then(r => r.data),
  listRequests: (params: { scope?: string; department_id?: number; doc_type?: string; status?: string }) =>
    http.get<OaRequest[]>('/oa/requests', { params }).then(r => r.data),
  getRequest: (id: number) => http.get<OaRequest>(`/oa/requests/${id}`).then(r => r.data),
  approve: (id: number, body: { note?: string; settle_amount?: number | null }) =>
    http.put<OaRequest>(`/oa/requests/${id}/approve`, body).then(r => r.data),
  reject: (id: number, reason: string) =>
    http.put<OaRequest>(`/oa/requests/${id}/reject`, { reason }).then(r => r.data),
  withdraw: (id: number) => http.put<{ message: string }>(`/oa/requests/${id}/withdraw`).then(r => r.data),
  // 🆕 #395：标记已付款时可带备注 + 付款回单（回单进申请的附件列表，kind=pay_receipt）
  markPaid: (id: number, payNote?: string, receipt?: File | null) => {
    const fd = new FormData()
    if (payNote) fd.append('pay_note', payNote)
    if (receipt) fd.append('file', receipt)
    return http.put<OaRequest>(`/oa/requests/${id}/mark-paid`, fd).then(r => r.data)
  },

  summary: () => http.get<OaSummaryRow[]>('/oa/reports/summary').then(r => r.data),
  summaryDetail: (department_id: number, doc_type: string) =>
    http.get<OaSummaryDetailRow[]>('/oa/reports/summary/detail', { params: { department_id, doc_type } }).then(r => r.data),
}
