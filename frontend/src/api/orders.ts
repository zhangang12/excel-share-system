import { ElMessage } from 'element-plus'
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
  design_done_flag: boolean
  electric_done_flag: boolean
  ship_prep_done: boolean
  packlist_status?: string | null   // 🆕 发货清单：none/requested/ready
  input_files: OrderAttachment[]
  start_files: OrderAttachment[]
  output_files: OrderAttachment[]
  produce_groups?: { group: string; name: string; due_date?: string | null; done_date?: string | null }[] | null
  standard_datasheet_id?: number | null   // 🆕 #6 电工部只读引用标准件清单
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
  list: (dept?: string, status?: string, year?: string, proj_status?: string, month?: string, worker_id?: number) =>
    http.get<DeptOrder[]>('/orders', { params: { dept, status, year, proj_status, month, worker_id } }).then((r) => r.data),

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

  // 🆕 #5 设计部「发货准备完成」（设计完成后标记说明书/铭牌备齐 → 通知物流）
  shipPrepDone: (id: number) =>
    http.post<{ message: string }>(`/orders/${id}/ship-prep-done`).then((r) => r.data),

  // 🆕 发货清单：设计部推送仓库准备（仓库备货完成后通知物流）
  shipListRequest: (id: number) =>
    http.post<{ message: string }>(`/orders/${id}/ship-list-request`).then((r) => r.data),

  // 🆕 #1 对销售下发的合同技术资料提修订意见 → 推送对应销售员
  revisionRequest: (id: number, reason: string) =>
    http.post<{ message: string }>(`/orders/${id}/revision-request`, { reason }).then((r) => r.data),

  complete: (id: number, notifyUserId: number) =>
    http.post(`/orders/${id}/complete`, { notify_user_id: notifyUserId }).then((r) => r.data),

  reopen: (id: number) => http.post(`/orders/${id}/reopen`).then((r) => r.data),

  void: (id: number) => http.post(`/orders/${id}/void`).then((r) => r.data),

  del: (id: number) => http.delete(`/orders/${id}`).then((r) => r.data),

  reassign: (id: number, workerId: number) =>
    http.post(`/orders/${id}/reassign`, { worker_id: workerId }).then((r) => r.data),

  // 🆕 管理层改预计完成时间(任意状态可改,不受本人锁定)
  editDue: (id: number, dueDate: string) =>
    http.post(`/orders/${id}/edit-due`, { due_date: dueDate }).then((r) => r.data),

  // 🆕 备机下单（设计部负责人/管理层）：建项目+派各部门，不建销售台账
  spareOrder: (data: { code: string; name: string; qty: number; unit: string; depts: string[]; req_text: string }) =>
    http.post<{ project_id: number; code: string; order_ids: number[] }>('/orders/spare', data).then((r) => r.data),

  // 🆕 设计完成第一步：CAD图纸+外购附图+四表齐才可点
  markDesignDone: (id: number) => http.post(`/orders/${id}/design_done`).then((r) => r.data),

  // 🆕 接线完成第一步：采购清单上传后才可点
  markElectricDone: (id: number) => http.post(`/orders/${id}/electric_done`).then((r) => r.data),
}

// 🆕 2026-06-19 生产部分组派发（钣金组/装配组）
export interface GroupProjectRow {
  project_id: number
  code: string
  name: string
  designer?: string | null
  task_id: number
  worker_name?: string | null        // 派给谁（主管视角）
  group_done: boolean
  start_date?: string | null         // 生产开始(派发日)
  due_date?: string | null           // 本组预计完成(组员填，填后锁定)
  done_date?: string | null          // 本组完成日期
  sheetmetal_datasheet_id?: number | null
  sheetmetal_done: boolean
  standard_ready?: boolean | null    // 仅装配组
  outsource_ready?: boolean | null   // 仅装配组
}

export interface DispatchOptions {
  sheetmetal: OptionUser[]
  assembly: OptionUser[]
}

export const produceApi = {
  dispatchOptions: () =>
    http.get<DispatchOptions>('/produce/dispatch-options').then((r) => r.data),
  dispatch: (orderId: number, sheetmetalWorkerId: number | null, assemblyWorkerId: number | null) =>
    http.post(`/produce/dispatch/${orderId}`,
      { sheetmetal_worker_id: sheetmetalWorkerId, assembly_worker_id: assemblyWorkerId }).then((r) => r.data),
  groupDone: (taskId: number, done: boolean) =>
    http.post(`/produce/group/${taskId}/done`, { done }).then((r) => r.data),
  setGroupDue: (taskId: number, dueDate: string) =>
    http.post(`/produce/group/${taskId}/due`, { due_date: dueDate }).then((r) => r.data),
  sheetmetalProjects: (year?: string, proj_status?: string) =>
    http.get<GroupProjectRow[]>('/produce/sheetmetal-projects', { params: { year, proj_status } }).then((r) => r.data),
  assemblyProjects: (year?: string, proj_status?: string) =>
    http.get<GroupProjectRow[]>('/produce/assembly-projects', { params: { year, proj_status } }).then((r) => r.data),
}

// 附件下载（带鉴权的 blob 下载，沿用现有 fetch+blob 模式）
export async function downloadAttachment(att: { id: number; name: string }) {
  // #113 失败不再静默：404(已被上游撤回)/401(登录过期)/网络异常给出明确提示
  try {
    const r = await http.get(`/attachments/${att.id}/download`, { responseType: 'blob' })
    const url = URL.createObjectURL(r.data as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = att.name
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e: any) {
    const code = e?.response?.status
    ElMessage.error(code === 404 ? '文件不存在或已被上游撤回，请刷新后重试'
      : code === 401 ? '登录已过期，请重新登录后再下载'
      : '文件下载失败，请稍后重试')
  }
}

// 状态中文映射（入库英文、展示中文的唯一前端来源）
export const ORDER_STATUS_TEXT: Record<string, string> = {
  pending_assign: '待分派',
  assigned: '待接单',
  in_progress: '进行中',
  done: '已完成',
  voided: '已作废',
}

// 🆕 颜色区分加大：待分派=蓝 / 待接单=橙 / 进行中=靛 / 已完成=绿 / 作废=红（避免待接单与进行中撞色）
export const ORDER_STATUS_TAG: Record<string, 'warning' | 'info' | 'primary' | 'success' | 'danger'> = {
  pending_assign: 'info',
  assigned: 'warning',
  in_progress: 'primary',
  done: 'success',
  voided: 'danger',
}
