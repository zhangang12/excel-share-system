import { http } from './index'

// 🆕 v3 M13 生产问题反馈
export interface Feedback {
  id: number
  project_id: number
  code: string
  name: string
  content: string
  status: string
  created_by_name?: string | null
  designer_name?: string | null
  created_at: string
}

export const FB_STATUS_TXT: Record<string, string> = {
  pending_pm: '待主管审批',
  pending_design: '待设计接收',
  archived: '已存档',
  rejected_by_pm: '主管驳回',
  rejected_by_design: '设计驳回',
}
export const FB_STATUS_TAG: Record<string, any> = {
  pending_pm: 'warning',
  pending_design: 'primary',
  archived: 'success',
  rejected_by_pm: 'info',
  rejected_by_design: 'danger',
}

export const feedbackApi = {
  mine: () => http.get<Feedback[]>('/feedbacks', { params: { mine: true } }).then((r) => r.data),
  byProject: (pid: number) => http.get<Feedback[]>('/feedbacks', { params: { project_id: pid } }).then((r) => r.data),
  myProjects: () => http.get<{ id: number; code: string; name: string }[]>('/feedbacks/projects').then((r) => r.data),
  create: (project_id: number, content: string) =>
    http.post('/feedbacks', { project_id, content }).then((r) => r.data),
  pmApprove: (id: number) => http.post(`/feedbacks/${id}/pm-approve`).then((r) => r.data),
  pmReject: (id: number) => http.post(`/feedbacks/${id}/pm-reject`).then((r) => r.data),
  designAccept: (id: number) => http.post(`/feedbacks/${id}/design-accept`).then((r) => r.data),
  designReject: (id: number) => http.post(`/feedbacks/${id}/design-reject`).then((r) => r.data),
}
