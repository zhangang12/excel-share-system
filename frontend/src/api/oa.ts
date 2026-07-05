import { http } from './index'

export interface Department {
  id: number; name: string; lead_role?: string | null; sort_order: number; enabled: boolean
}
export interface OaDocType {
  id: number; key: string; category: string; category_label: string; label: string
  sort_order: number; enabled: boolean
}
export interface OaApprovalStep {
  id: number; department_id: number; doc_type: string; step_order: number
  approver_role: string; step_label?: string | null; enabled: boolean
}
export interface OaRequestStep {
  id: number; step_order: number; approver_role: string; step_label?: string | null
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
  created_at: string; updated_at: string
  steps: OaRequestStep[]
  cc_users: OaCcUser[]   // 🆕 抄送人
  can_approve: boolean; can_withdraw: boolean; can_mark_paid: boolean
}
export interface OaSummaryRow {
  department_id: number; department_name: string; doc_type: string; count: number; amount: number
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
  markPaid: (id: number) => http.put<OaRequest>(`/oa/requests/${id}/mark-paid`).then(r => r.data),

  summary: () => http.get<OaSummaryRow[]>('/oa/reports/summary').then(r => r.data),
}
