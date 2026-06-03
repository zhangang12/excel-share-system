import { http } from './index'
import type { Project, ProjectMember } from '@/types'

export const projectsApi = {
  list: (q?: string, status?: string) => {
    const params: Record<string, string> = {}
    if (q) params.q = q
    if (status) params.status = status
    return http.get<Project[]>('/projects', { params }).then(r => r.data)
  },
  get: (id: number) => http.get<Project>(`/projects/${id}`).then(r => r.data),
  create: (data: { code: string; name: string; description?: string; status?: string; manager_id?: number | null }) =>
    http.post<Project>('/projects', data).then(r => r.data),
  update: (id: number, data: Partial<Project>) =>
    http.put<Project>(`/projects/${id}`, data).then(r => r.data),
  remove: (id: number) =>
    http.delete<{ message: string }>(`/projects/${id}`).then(r => r.data),

  // 更新项目头表单个字段（数量 / 销售 / 设计师 / 电器 / 下单日期 / 交货日期 / 制表日期）
  updateHeaderCell: (id: number, key: string, value: string | null) =>
    http.put<{ message: string }>(`/projects/${id}/header-cell`, { key, value })
      .then(r => r.data),

  // 成员
  listMembers: (id: number) =>
    http.get<ProjectMember[]>(`/projects/${id}/members`).then(r => r.data),
  addMember: (id: number, user_id: number, permission: 'edit'|'view') =>
    http.post<ProjectMember>(`/projects/${id}/members`, { user_id, permission }).then(r => r.data),
  addMembersBatch: (id: number, user_ids: number[], permission: 'edit'|'view') =>
    http.post<ProjectMember[]>(`/projects/${id}/members/batch`,
      { user_ids, permission }).then(r => r.data),
  updateMember: (id: number, mid: number, permission: 'edit'|'view') =>
    http.put<ProjectMember>(`/projects/${id}/members/${mid}`, { user_id: 0, permission }).then(r => r.data),
  removeMember: (id: number, mid: number) =>
    http.delete<{ message: string }>(`/projects/${id}/members/${mid}`).then(r => r.data),
}
