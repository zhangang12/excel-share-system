import { http } from './index'
import type { LoginResp, User } from '@/types'

export interface MenuItem { key: string; label: string }
export interface MenusResp { menus: MenuItem[]; can_view_detail: boolean }

export const authApi = {
  login: (username: string, password: string) =>
    http.post<LoginResp>('/auth/login', { username, password }).then((r) => r.data),

  me: () => http.get<User>('/auth/me').then((r) => r.data),

  // 🆕 v3：当前用户可见菜单（侧边栏渲染权威）+ 详单可点性
  menus: () => http.get<MenusResp>('/auth/menus').then((r) => r.data),

  changePassword: (oldPwd: string, newPwd: string) =>
    http
      .post<{ message: string }>('/auth/change-password', {
        old_password: oldPwd,
        new_password: newPwd,
      })
      .then((r) => r.data),

  logout: () => http.post<{ message: string }>('/auth/logout').then((r) => r.data),
}
