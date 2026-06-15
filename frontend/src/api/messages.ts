import { http } from './index'

// 🆕 v3 站内消息
export interface Message {
  id: number
  kind: 'wx' | 'warn' | 'info'
  text: string
  read: boolean
  biz_type?: string | null
  biz_id?: number | null
  created_at: string
}

export const messagesApi = {
  list: (limit = 50, offset = 0) =>
    http.get<Message[]>('/messages', { params: { limit, offset } }).then((r) => r.data),

  unreadCount: () =>
    http.get<{ count: number }>('/messages/unread-count').then((r) => r.data.count),

  readAll: () => http.post<{ message: string }>('/messages/read-all').then((r) => r.data),
}
