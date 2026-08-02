import { http } from './index'

// 🆕 桌面客户端在线统计（admin/manager 专属，只读；数据由后端中间件按统计头收集）
export interface DesktopClientItem {
  device_id: string
  version: string
  username?: string | null
  last_seen?: string | null
}
export interface DesktopVersionDist { version: string; count: number }
export interface DesktopClientsResult {
  distribution: DesktopVersionDist[]
  items: DesktopClientItem[]
}

// 🆕 客户端故障自动上报（升级失败 / 崩溃 / 更新器报错）
// 由客户端 POST /api/desktop/report 写入（那个入口不需认证——升级失败发生在登录之前）
export interface DesktopReportItem {
  id: number
  device_id: string
  version: string
  kind: 'update_failed' | 'crash' | 'error' | string
  detail?: string | null
  extra?: Record<string, any> | null
  username?: string | null
  handled: boolean
  created_at?: string | null
}
export interface DesktopReportsResult {
  open_count: number
  items: DesktopReportItem[]
}

export const desktopApi = {
  clients: () => http.get<DesktopClientsResult>('/admin/desktop-clients').then((r) => r.data),
  reports: (params?: { kind?: string; only_open?: boolean }) =>
    http.get<DesktopReportsResult>('/admin/desktop-reports', { params }).then((r) => r.data),
  markHandled: (id: number, handled = true) =>
    http.post(`/admin/desktop-reports/${id}/handled`, null, { params: { handled } }).then((r) => r.data),
}
