import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi, type MenuItem } from '@/api/auth'
import type { User } from '@/types'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(localStorage.getItem('pms_token') || '')
  const user = ref<User | null>(
    JSON.parse(localStorage.getItem('pms_user') || 'null'),
  )
  // 🆕 v3：后端下发的可见菜单（null=尚未加载，此时按老默认渲染避免闪烁）
  const menus = ref<MenuItem[] | null>(
    JSON.parse(localStorage.getItem('pms_menus') || 'null'),
  )
  const canViewDetail = ref<boolean>(
    localStorage.getItem('pms_can_view_detail') !== '0',
  )

  const isLoggedIn = computed(() => !!token.value)

  // 🆕 多角色：用户全部角色 code（含锚点，去重）
  function roleCodes(): string[] {
    const u = user.value
    if (!u) return []
    const set = new Set<string>(u.role_codes || [])
    if (u.role_code) set.add(u.role_code)
    return [...set]
  }
  // 🆕 是否拥有 codes 中任一角色（并集判断）——前端按钮显隐统一走它
  function hasRole(...codes: string[]): boolean {
    const mine = new Set(roleCodes())
    return codes.some((c) => mine.has(c))
  }

  const isAdmin = computed(() => hasRole('admin', 'manager'))

  // 🆕 #7 二级菜单(tab)按账号授权：某 tab 是否对当前账号隐藏。管理层不受限(始终可见)。
  //   key 形如 "finance:pay_payment"（menuKey:tabName）。
  function tabHidden(menuKey: string, tabName: string): boolean {
    if (hasRole('admin', 'manager')) return false
    return (user.value?.hidden_tabs || []).includes(`${menuKey}:${tabName}`)
  }
  function tabVisible(menuKey: string, tabName: string): boolean { return !tabHidden(menuKey, tabName) }
  const mustChangePassword = computed(() => !!user.value?.password_must_change)

  // 🆕 是否可见某菜单：菜单未加载时 catalog/list 按老默认放行（老角色无感知）
  function hasMenu(key: string): boolean {
    if (menus.value === null) return key === 'catalog' || key === 'list'
    return menus.value.some((m) => m.key === key)
  }

  // 🆕 业务部门菜单（新增模块；排除老的 catalog/list 与管理组）
  const deptMenus = computed<MenuItem[]>(() => {
    if (!menus.value) return []
    const skip = new Set(['catalog', 'list', 'admin-users', 'admin-perms', 'admin-audit', 'dict-admin'])
    return menus.value.filter((m) => !skip.has(m.key))
  })

  async function fetchMenus() {
    if (!token.value) return
    try {
      const resp = await authApi.menus()
      menus.value = resp.menus
      canViewDetail.value = resp.can_view_detail
      localStorage.setItem('pms_menus', JSON.stringify(resp.menus))
      localStorage.setItem('pms_can_view_detail', resp.can_view_detail ? '1' : '0')
    } catch { /* 接口失败保持现状（老默认），不阻塞页面 */ }
  }

  async function login(username: string, password: string) {
    const resp = await authApi.login(username, password)
    token.value = resp.access_token
    user.value = resp.user
    localStorage.setItem('pms_token', resp.access_token)
    localStorage.setItem('pms_user', JSON.stringify(resp.user))
    menus.value = null  // 🆕 切换账号清菜单缓存，登录后重新拉取
    localStorage.removeItem('pms_menus')
    await fetchMenus()
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
    menus.value = null
    localStorage.removeItem('pms_token')
    localStorage.removeItem('pms_user')
    localStorage.removeItem('pms_menus')
    localStorage.removeItem('pms_can_view_detail')
  }

  return { token, user, isLoggedIn, isAdmin, mustChangePassword,
           menus, canViewDetail, hasMenu, deptMenus, fetchMenus,
           hasRole, roleCodes, tabHidden, tabVisible,
           login, fetchMe, changePassword, logout }
})
