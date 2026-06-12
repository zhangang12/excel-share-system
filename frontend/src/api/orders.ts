import { http } from './index'

// 🆕 v3 部门任务单
export interface OrderAttachment {
  id: number
  biz_type: string
  kind?: string | null
  name: string
  ext?: string | null
  size: number
  created_at: string
}

export interface DeptOrder {
  id: number
  project_id: number
  project_code: string
  project_name: string
  dept: string
  status: 'pending_assign' | 'assigned' | 'in_progress' | 'done' | 'voided'
  worker_id?: number | null
  worker_name?: string | null
  req_text?: string | null
  start_date?: string | null
  due_date?: string | null
  done_date?: string | null
  notify_user_id?: number | null
  notify_user_name?: string | null
  eff_pct?: number | null
  on_time?: boolean | null
  overdue: boolean
  created_at: string
  input_files: OrderAttachment[]
  start_files: OrderAttachment[]
  output_files: OrderAttachment[]
}

export interface OptionUser { id: number; name: string }

export interface DeptOptions {
  workers: OptionUser[]
  notify_pool: OptionUser[]
  notify_label: string
  dept_name: string
  sheet_check: boolean
  start_outputs: { k: string; label: string; to_role: string }[]
  outputs: { k: string; label: string; to_role: string; required?: boolean }[]
  start_label: string
  end_label: string
  done_label: string
}

export const ordersApi = {
  list: (dept?: string, status?: string) =>
    http.get<DeptOrder[]>('/orders', { params: { dept, status } }).then((r) => r.data),

  options: (dept: string) =>
    http.get<DeptOptions>('/orders/options', { params: { dept } }).then((r) => r.data),

  create: (data: { project_id: number; dept: string; req_text?: string; worker_id?: number }) =>
    http.post<DeptOrder>('/orders', data).then((r) => r.data),

  assign: (id: number, workerId: number) =>
    http.post(`/orders/${id}/assign`, { worker_id: workerId }).then((r) => r.data),

  start: (id: number, startDate: string, dueDate: string) =>
    http.post(`/orders/${id}/start`, { start_date: startDate, due_date: dueDate }).then((r) => r.data),

  startUpload: (id: number, kind: string, files: File[]) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    return http.post<OrderAttachment[]>(`/orders/${id}/start-upload?kind=${kind}`, fd).then((r) => r.data)
  },

  outputUpload: (id: number, kind: string, files: File[]) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    return http.post<OrderAttachment[]>(`/orders/${id}/output-upload?kind=${kind}`, fd).then((r) => r.data)
  },

  inputUpload: (id: number, files: File[]) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    return http.post<OrderAttachment[]>(`/orders/${id}/input-files`, fd).then((r) => r.data)
  },

  removeAttachment: (id: number, attId: number) =>
    http.delete(`/orders/${id}/attachments/${attId}`).then((r) => r.data),

  complete: (id: number, notifyUserId: number) =>
    http.post(`/orders/${id}/complete`, { notify_user_id: notifyUserId }).then((r) => r.data),

  reopen: (id: number) => http.post(`/orders/${id}/reopen`).then((r) => r.data),

  void: (id: number) => http.post(`/orders/${id}/void`).then((r) => r.data),

  reassign: (id: number, workerId: number) =>
    http.post(`/orders/${id}/reassign`, { worker_id: workerId }).then((r) => r.data),
}

// 附件下载（带鉴权的 blob 下载，沿用现有 fetch+blob 模式）
export async function downloadAttachment(att: { id: number; name: string }) {
  const r = await http.get(`/attachments/${att.id}/download`, { responseType: 'blob' })
  const url = URL.createObjectURL(r.data as Blob)
  const a = document.createElement('a')
  a.href = url
  a.download = att.name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

// 状态中文映射（入库英文、展示中文的唯一前端来源）
export const ORDER_STATUS_TEXT: Record<string, string> = {
  pending_assign: '待分派',
  assigned: '待接单',
  in_progress: '进行中',
  done: '已完成',
  voided: '已作废',
}

export const ORDER_STATUS_TAG: Record<string, 'warning' | 'info' | 'primary' | 'success' | 'danger'> = {
  pending_assign: 'warning',
  assigned: 'info',
  in_progress: 'primary',
  done: 'success',
  voided: 'danger',
}
