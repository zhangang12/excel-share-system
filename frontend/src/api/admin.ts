import { http } from './index'
import type { User, Role, AuditLog } from '@/types'

export const adminApi = {
  // 角色
  listRoles: () => http.get<Role[]>('/admin/roles').then((r) => r.data),

  // 用户
  listUsers: () => http.get<User[]>('/admin/users').then((r) => r.data),
  createUser: (data: Partial<User> & { username: string; password: string; role_ids: number[] }) =>
    http.post<User>('/admin/users', data).then((r) => r.data),
  updateUser: (id: number, data: Partial<User> & { password?: string }) =>
    http.put<User>(`/admin/users/${id}`, data).then((r) => r.data),
  deleteUser: (id: number) =>
    http.delete<{ message: string }>(`/admin/users/${id}`).then((r) => r.data),

 // 审计
  listAudit: (limit = 200) =>
    http.get<AuditLog[]>('/admin/audit', { params: { limit } }).then((r) => r.data),
}
