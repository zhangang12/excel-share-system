import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi } from '@/api/auth'
import type { User } from '@/types'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(localStorage.getItem('pms_token') || '')
  const user = ref<User | null>(
    JSON.parse(localStorage.getItem('pms_user') || 'null'),
  )

  const isLoggedIn = computed(() => !!token.value)
  const isAdmin = computed(() => ['admin', 'manager'].includes(user.value?.role_code || ''))
  const mustChangePassword = computed(() => !!user.value?.password_must_change)

  async function login(username: string, password: string) {
    const resp = await authApi.login(username, password)
    token.value = resp.access_token
    user.value = resp.user
    localStorage.setItem('pms_token', resp.access_token)
    localStorage.setItem('pms_user', JSON.stringify(resp.user))
  }

  async function fetchMe() {
    if (!token.value) return null
    try {
      const me = await authApi.me()
      user.value = me
      localStorage.setItem('pms_user', JSON.stringify(me))
      return me
    } catch {
      logout()
      return null
    }
  }

  async function changePassword(oldPwd: string, newPwd: string) {
    await authApi.changePassword(oldPwd, newPwd)
    if (user.value) {
      user.value.password_must_change = false
      localStorage.setItem('pms_user', JSON.stringify(user.value))
    }
  }

  function logout() {
    try { authApi.logout() } catch { /* ignore */ }
    token.value = ''
    user.value = null
    localStorage.removeItem('pms_token')
    localStorage.removeItem('pms_user')
  }

  return { token, user, isLoggedIn, isAdmin, mustChangePassword,
           login, fetchMe, changePassword, logout }
})
