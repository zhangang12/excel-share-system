import { http } from './index'

export type MgmtTodoStatus = 'pending' | 'committed' | 'done'
export type ExtendStatus = 'pending' | 'approved' | 'rejected' | null

export interface MgmtTodoTarget {
  id: number
  user_id: number
  user_name?: string | null
  status: MgmtTodoStatus
  committed_at?: string | null
  progress?: string | null
  reply_at?: string | null
  done_at?: string | null
  overdue: boolean
  extend_status?: ExtendStatus
  extend_to?: string | null
  extend_reason?: string | null
}

export interface MgmtTodo {
  id: number
  title: string
  content?: string | null
  priority: 'normal' | 'urgent'
  created_by: number
  creator_name?: string | null
  created_at: string
  targets: MgmtTodoTarget[]
  total: number
  done_count: number
  overdue_count: number
  pending_reply_count: number
}

export interface MyTodoRow {
  target_id: number
  todo_id: number
  title: string
  content?: string | null
  priority: 'normal' | 'urgent'
  creator_name?: string | null
  created_at: string
  status: MgmtTodoStatus
  committed_at?: string | null
  progress?: string | null
  done_at?: string | null
  overdue: boolean
  extend_status?: ExtendStatus
  extend_to?: string | null
  extend_reason?: string | null
}

export const managementTodoApi = {
  // 管理层
  create: (data: { title: string; content?: string; priority?: string; recipient_ids: number[] }) =>
    http.post<MgmtTodo>('/management-todos', data).then((r) => r.data),
  listSent: () => http.get<MgmtTodo[]>('/management-todos/sent').then((r) => r.data),
  remove: (todoId: number) =>
    http.delete<{ message: string }>(`/management-todos/${todoId}`).then((r) => r.data),
  decideExtend: (targetId: number, approve: boolean, note?: string) =>
    http.post<MgmtTodoTarget>(`/management-todos/${targetId}/extend/decide`, { approve, note }).then((r) => r.data),

  // 收件人
  listMine: () => http.get<MyTodoRow[]>('/management-todos/mine').then((r) => r.data),
  myCount: () => http.get<{ count: number }>('/management-todos/mine/count').then((r) => r.data.count),
  reply: (targetId: number, committed_at: string, progress?: string) =>
    http.post<MyTodoRow>(`/management-todos/${targetId}/reply`, { committed_at, progress }).then((r) => r.data),
  updateProgress: (targetId: number, progress: string) =>
    http.post<MyTodoRow>(`/management-todos/${targetId}/progress`, { progress }).then((r) => r.data),
  markDone: (targetId: number, progress?: string) =>
    http.post<MyTodoRow>(`/management-todos/${targetId}/done`, { progress }).then((r) => r.data),
  requestExtend: (targetId: number, extend_to: string, reason: string) =>
    http.post<MyTodoRow>(`/management-todos/${targetId}/extend`, { extend_to, reason }).then((r) => r.data),
}
