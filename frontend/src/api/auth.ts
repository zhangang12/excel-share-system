import { http } from './index'
import type { LoginResp, User } from '@/types'

export interface MenuItem { key: string; label: string }
export interface MenusResp { menus: MenuItem[]; can_view_detail: boolean }

// 🆕 外网登录两步闸门：浏览器+外网 IP+非 admin 时第一步不返 token，改返 gate_required+pre_token，
//   前端转第二步输 6 位验证码（码由管理层企微告知），verifyGate 通过才发 token。
//   免闸路径（桌面客户端/admin/内网 IP/开关关闭）响应不含 gate 字段，类型与原 LoginResp 兼容。
export interface GateFields { gate_required?: boolean; pre_token?: string; message?: string }
export type LoginResult = LoginResp & GateFields

export const authApi = {
  login: (username: string, password: string) =>
    http.post<LoginResult>('/auth/login', { username, password }).then((r) => r.data),

  // 🆕 外网登录第二步：验证码换 token
  verifyGate: (username: string, preToken: string, code: string) =>
    http.post<LoginResp>('/auth/login/verify-gate', {
      username, pre_token: preToken, code,
    }).then((r) => r.data),

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
