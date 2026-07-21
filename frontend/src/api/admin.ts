import { http } from './index'
import type { User, Role, AuditLog } from '@/types'

export const adminApi = {
  // 角色
  listRoles: () => http.get<Role[]>('/admin/roles').then((r) => r.data),
  // 🆕 #7 可授权的二级菜单(tab) 注册表
  tabRegistry: () => http.get<{ menu_key: string; menu_label: string; tabs: { key: string; label: string }[] }[]>('/admin/tab-registry').then((r) => r.data),

  // 用户
  listUsers: () => http.get<User[]>('/admin/users').then((r) => r.data),
  createUser: (data: Partial<User> & { username: string; password: string; role_ids: number[] }) =>
    http.post<User>('/admin/users', data).then((r) => r.data),
  updateUser: (id: number, data: Partial<User> & { password?: string }) =>
    http.put<User>(`/admin/users/${id}`, data).then((r) => r.data),
  // 🆕 一级菜单按账号配置（整体替换该账号的一级菜单 key 清单）
  setMenus: (id: number, menus: string[]) =>
    http.put<User>(`/admin/users/${id}/menus`, { menus }).then((r) => r.data),
  // 🆕 全量一级菜单定义（business=业务区 / admin=管理组，含中文 label，顺序即侧边栏顺序）
  menuDefs: () =>
    http.get<{ business: { key: string; label: string }[]; admin: { key: string; label: string }[] }>('/admin/menu-defs').then((r) => r.data),
  // 兼容包装（旧桌面端语义靠后端）：对管理组 key 做「含有的加入 menus、不含的移除」
  grantMenus: (id: number, grant_menus: string[]) =>
    http.put<User>(`/admin/users/${id}/grant-menus`, { grant_menus }).then((r) => r.data),
  deleteUser: (id: number) =>
    http.delete<{ message: string }>(`/admin/users/${id}`).then((r) => r.data),

 // 审计
  listAudit: (limit = 200) =>
    http.get<AuditLog[]>('/admin/audit', { params: { limit } }).then((r) => r.data),
}
