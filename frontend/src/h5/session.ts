/**
 * H5 会话：只有「有没有登录、是谁」两件事，用 localStorage 就够。
 * 不引桌面端的 pinia store —— 那个带菜单、权限矩阵、详单闸门一整套，
 * H5 一个都用不上（它只有登录和助手两页，没有业务页面可跳）。
 *
 * token 的 key 与桌面端保持一致（pms_token / pms_user）：
 * 同一浏览器先登过网页版再开 H5 时能直接复用，不用再登一次。
 */
import { ref, computed } from 'vue'

export interface H5User { id: number; username: string; full_name?: string | null }

const readUser = (): H5User | null => {
  try { return JSON.parse(localStorage.getItem('pms_user') || 'null') } catch { return null }
}

export const token = ref<string>(localStorage.getItem('pms_token') || '')
export const user = ref<H5User | null>(readUser())
export const isLoggedIn = computed(() => !!token.value)
export const displayName = computed(() =>
  user.value?.full_name || user.value?.username || '')

export function setSession(t: string, u: H5User) {
  token.value = t
  user.value = u
  localStorage.setItem('pms_token', t)
  localStorage.setItem('pms_user', JSON.stringify(u))
}

export function clearSession() {
  token.value = ''
  user.value = null
  localStorage.removeItem('pms_token')
  localStorage.removeItem('pms_user')
}
