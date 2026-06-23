import { http } from './index'

export interface UserFeedbackRow {
  id: number
  kind: 'bug' | 'suggest' | 'other'
  content: string
  page_url?: string | null
  user_agent?: string | null
  status: 'open' | 'done'
  created_at: string
  user_id?: number | null
  user_name?: string | null
  user_role?: string | null
  shot_file_id?: number | null
  shot_file_name?: string | null
  // 🆕 系统回信
  reply?: string | null
  replied_at?: string | null
  replier_name?: string | null
  reply_read?: boolean
}

export const userFeedbackApi = {
  submit: (kind: string, content: string, page_url: string, file?: File | null) => {
    const fd = new FormData()
    fd.append('kind', kind)
    fd.append('content', content)
    fd.append('page_url', page_url)
    if (file) fd.append('file', file)
    return http.post<UserFeedbackRow>('/user-feedback', fd).then((r) => r.data)
  },
  list: (params: { mine?: boolean; kind?: string; status?: string } = {}) =>
    http.get<UserFeedbackRow[]>('/user-feedback', { params }).then((r) => r.data),
  markDone: (id: number) =>
    http.post<{ message: string }>(`/user-feedback/${id}/done`).then((r) => r.data),
  // 🆕 系统回信
  reply: (id: number, reply: string) =>
    http.post<UserFeedbackRow>(`/user-feedback/${id}/reply`, { reply }).then((r) => r.data),
  myUnreadReplies: () =>
    http.get<UserFeedbackRow[]>('/user-feedback/my-unread-replies').then((r) => r.data),
  markRepliesRead: () =>
    http.post<{ message: string }>('/user-feedback/replies/read').then((r) => r.data),
  exportUrl: (params: { kind?: string; status?: string } = {}) => {
    const qs = new URLSearchParams()
    if (params.kind) qs.set('kind', params.kind)
    if (params.status) qs.set('status', params.status)
    const q = qs.toString()
    return `/api/user-feedback/export.html${q ? '?' + q : ''}`
  },
}
