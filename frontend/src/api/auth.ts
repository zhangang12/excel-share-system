import { http } from './index'
import type { LoginResp, User } from '@/types'

export const authApi = {
  login: (username: string, password: string) =>
    http.post<LoginResp>('/auth/login', { username, password }).then((r) => r.data),

  me: () => http.get<User>('/auth/me').then((r) => r.data),

  changePassword: (oldPwd: string, newPwd: string) =>
    http
      .post<{ message: string }>('/auth/change-password', {
        old_password: oldPwd,
        new_password: newPwd,
      })
      .then((r) => r.data),

  logout: () => http.post<{ message: string }>('/auth/logout').then((r) => r.data),
}
