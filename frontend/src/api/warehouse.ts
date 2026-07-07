import { http } from './index'

// 🆕 v3 M07 仓库组
export interface WhMaterial {
  id: number; code?: string | null; name: string; spec?: string | null
  category?: string | null; material_grade?: string | null; unit: string; location?: string | null
  unit_price?: number | null            // 🆕 需求三：参考单价
  stock_value?: number | null           // 🆕 需求三：库存总价=现存×单价
  safety_stock: number; init_stock: number; status: string; stock: number; low: boolean
  custom_values?: Record<string, any>   // 🆕 自定义字段值
}
// 🆕 仓库物料自定义字段定义（跟采购 R6 同一套）
export interface WhCustomField {
  id: number; label: string; ftype: string; options: string[]
  required: boolean; show_in_list: boolean; sort_order: number; enabled: boolean
}
export interface WhTxn {
  id: number; material_id: number; material_name: string; spec?: string | null
  biz_date: string; direction: string; qty: number
  unit_price?: number | null; amount?: number | null
  source?: string | null; party?: string | null
  project_id?: number | null; project_code?: string | null; ref_no: string
  is_reversal: boolean; reversed: boolean; created_at: string
}
export interface WhSummaryRow {
  material_id: number; name: string; spec?: string | null; unit: string
  opening: number; in_qty: number; out_qty: number; closing: number
}

// 🆕 #9 发货清单项
export interface ShipListItem { id: number; name: string; created_at: string; uploaded_by?: number | null }
export interface ShipListFile { id: number; name: string }

// 🆕 发货清单目录行：设计部推送（含文件），仓库据此备货 → 点「已备齐」通知物流
export interface ShipListPendingRow {
  project_id: number; code: string; name: string
  requested_at?: string | null; requested_by_name?: string | null
  packlist_status: 'requested' | 'ready'
  ready_at?: string | null; ready_by_name?: string | null
  files: ShipListFile[]
}

export const whApi = {
  shipLists: (pid: number) => http.get<ShipListItem[]>(`/wh/ship-list/${pid}`).then((r) => r.data),
  deleteShipList: (aid: number) => http.delete<{ message: string }>(`/wh/ship-list/item/${aid}`).then((r) => r.data),
  shipListPending: (status: 'requested' | 'ready' | 'all' = 'requested') =>
    http.get<ShipListPendingRow[]>('/wh/ship-list/pending', { params: { status } }).then((r) => r.data),
  shipListReady: (projectId: number) =>
    http.post<{ message: string }>(`/wh/ship-list/${projectId}/ready`).then((r) => r.data),
  materials: (kw?: string) =>
    http.get<{ materials: WhMaterial[]; total: number; low_count: number }>('/wh/materials', { params: { kw } }).then((r) => r.data),
  createMaterial: (data: Partial<WhMaterial>) => http.post('/wh/materials', data).then((r) => r.data),
  updateMaterial: (id: number, data: Partial<WhMaterial>) => http.put(`/wh/materials/${id}`, data).then((r) => r.data),
  deleteMaterial: (id: number) => http.delete<{ message: string }>(`/wh/materials/${id}`).then((r) => r.data),
  // 🆕 物料自定义字段
  customFields: () => http.get<WhCustomField[]>('/wh/material-custom-fields').then((r) => r.data),
  createCustomField: (data: Partial<WhCustomField>) => http.post('/wh/material-custom-fields', data).then((r) => r.data),
  updateCustomField: (id: number, data: Partial<WhCustomField>) => http.put(`/wh/material-custom-fields/${id}`, data).then((r) => r.data),
  deleteCustomField: (id: number) => http.delete(`/wh/material-custom-fields/${id}`).then((r) => r.data),
  txns: (params?: { direction?: string; material_id?: number }) =>
    http.get<WhTxn[]>('/wh/txns', { params }).then((r) => r.data),
  createTxn: (data: any) => http.post('/wh/txns', data).then((r) => r.data),
  reverse: (id: number) => http.post(`/wh/txns/${id}/reverse`).then((r) => r.data),
  summary: (period: string) => http.get<WhSummaryRow[]>('/wh/summary', { params: { period } }).then((r) => r.data),
  // 🆕 需求二：物料需求一键领用出库
  issueDemand: (projectId: number, lines: { material_id: number; qty: number }[]) =>
    http.post<{ message: string }>(`/wh/demand/${projectId}/issue`, { lines }).then((r) => r.data),
  // 🆕 需求十五：一键清空（试运行数据清理）
  clearAll: (confirm: string) => http.post<{ message: string }>('/wh/clear-all', { confirm }).then((r) => r.data),
}
