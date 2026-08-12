import { http } from './index'

// 🆕 反馈#363/#381/#382 个人待办：自己给自己记的事，只有自己看得见。
// 和管理层待办是两套东西（后者要回承诺时间、要留痕，删不得），后端也是两张表。
export interface PersonalTodo {
  id: number
  title: string
  note?: string | null
  due_date?: string | null
  priority: 'normal' | 'urgent'
  project_id?: number | null
  project_code?: string | null
  done: boolean
  done_at?: string | null
  sort_order: number
  overdue: boolean          // 未完成且已过截止日（到期只推一次企微，之后就靠这个标红）
  created_at: string
}

export const personalTodoApi = {
  list: (done?: boolean) =>
    http.get<PersonalTodo[]>('/personal-todos', { params: done === undefined ? {} : { done } })
      .then((r) => r.data),
  count: () => http.get<{ count: number }>('/personal-todos/count').then((r) => r.data.count),
  create: (data: { title: string; note?: string; due_date?: string | null; priority?: string; project_id?: number | null }) =>
    http.post<PersonalTodo>('/personal-todos', data).then((r) => r.data),
  update: (id: number, data: Partial<{ title: string; note: string; due_date: string | null; priority: string; project_id: number | null }>) =>
    http.put<PersonalTodo>(`/personal-todos/${id}`, data).then((r) => r.data),
  toggle: (id: number) =>
    http.post<PersonalTodo>(`/personal-todos/${id}/toggle`).then((r) => r.data),
  remove: (id: number) =>
    http.delete<{ message: string }>(`/personal-todos/${id}`).then((r) => r.data),
  reorder: (ids: number[]) =>
    http.put<{ message: string }>('/personal-todos/reorder', { ids }).then((r) => r.data),
}
