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

export const desktopApi = {
  clients: () => http.get<DesktopClientsResult>('/admin/desktop-clients').then((r) => r.data),
}
