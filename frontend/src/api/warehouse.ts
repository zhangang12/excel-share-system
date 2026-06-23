import { http } from './index'

// 🆕 v3 M07 仓库组
export interface WhMaterial {
  id: number; code?: string | null; name: string; spec?: string | null
  category?: string | null; unit: string; location?: string | null
  safety_stock: number; init_stock: number; status: string; stock: number; low: boolean
}
export interface WhTxn {
  id: number; material_id: number; material_name: string; spec?: string | null
  biz_date: string; direction: string; qty: number; source?: string | null; party?: string | null
  project_id?: number | null; project_code?: string | null; ref_no: string
  is_reversal: boolean; reversed: boolean; created_at: string
}
export interface WhSummaryRow {
  material_id: number; name: string; spec?: string | null; unit: string
  opening: number; in_qty: number; out_qty: number; closing: number
}

// 🆕 #9 发货清单项
export interface ShipListItem { id: number; name: string; created_at: string; uploaded_by?: number | null }

export const whApi = {
  shipLists: (pid: number) => http.get<ShipListItem[]>(`/wh/ship-list/${pid}`).then((r) => r.data),
  deleteShipList: (aid: number) => http.delete<{ message: string }>(`/wh/ship-list/item/${aid}`).then((r) => r.data),
  materials: (kw?: string) =>
    http.get<{ materials: WhMaterial[]; total: number; low_count: number }>('/wh/materials', { params: { kw } }).then((r) => r.data),
  createMaterial: (data: Partial<WhMaterial>) => http.post('/wh/materials', data).then((r) => r.data),
  updateMaterial: (id: number, data: Partial<WhMaterial>) => http.put(`/wh/materials/${id}`, data).then((r) => r.data),
  txns: (params?: { direction?: string; material_id?: number }) =>
    http.get<WhTxn[]>('/wh/txns', { params }).then((r) => r.data),
  createTxn: (data: any) => http.post('/wh/txns', data).then((r) => r.data),
  reverse: (id: number) => http.post(`/wh/txns/${id}/reverse`).then((r) => r.data),
  summary: (period: string) => http.get<WhSummaryRow[]>('/wh/summary', { params: { period } }).then((r) => r.data),
}
