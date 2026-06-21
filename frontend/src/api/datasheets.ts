import { http } from './index'
import type { Datasheet, DataField, DataRecord, FieldType, FieldConfig } from '@/types'

export const datasheetsApi = {
  // 数据表
  list: (projectId: number) =>
    http.get<Datasheet[]>(`/projects/${projectId}/datasheets`).then(r => r.data),
  create: (projectId: number, name: string) =>
    http.post<Datasheet>(`/projects/${projectId}/datasheets`, { name }).then(r => r.data),
  rename: (id: number, name: string) =>
    http.put<Datasheet>(`/datasheets/${id}`, { name }).then(r => r.data),
  remove: (id: number) =>
    http.delete<{ message: string }>(`/datasheets/${id}`).then(r => r.data),

  // 字段
  listFields: (datasheetId: number) =>
    http.get<DataField[]>(`/datasheets/${datasheetId}/fields`).then(r => r.data),
  createField: (datasheetId: number, data: { name: string; type: FieldType; config?: FieldConfig }) =>
    http.post<DataField>(`/datasheets/${datasheetId}/fields`, data).then(r => r.data),
  updateField: (id: number, data: { name?: string; type?: FieldType; sort_order?: number; config?: FieldConfig | null }) =>
    http.put<DataField>(`/fields/${id}`, data).then(r => r.data),
  deleteField: (id: number) =>
    http.delete<{ message: string }>(`/fields/${id}`).then(r => r.data),

  // 行
  listRecords: (datasheetId: number) =>
    http.get<DataRecord[]>(`/datasheets/${datasheetId}/records`).then(r => r.data),
  createRecord: (datasheetId: number, values: Record<string, unknown> = {}) =>
    http.post<DataRecord>(`/datasheets/${datasheetId}/records`, { values }).then(r => r.data),
  updateRecord: (id: number, values: Record<string, unknown>) =>
    http.put<DataRecord>(`/records/${id}`, { values }).then(r => r.data),
  updateCell: (recordId: number, field_id: number, value: unknown) =>
    http.put<DataRecord>(`/records/${recordId}/cell`, { field_id, value }).then(r => r.data),
  deleteRecord: (id: number) =>
    http.delete<{ message: string }>(`/records/${id}`).then(r => r.data),

  // ── 生产组专用：钣金/装配角色编辑「钣金装配」表（绕开详单闸门）──
  produceUpdateCell: (datasheetId: number, recordId: number, field_id: number, value: unknown) =>
    http.put(`/datasheets/${datasheetId}/produce-edit/records/${recordId}/cell`, { field_id, value }).then(r => r.data),
  produceCreateRecord: (datasheetId: number) =>
    http.post(`/datasheets/${datasheetId}/produce-edit/records`, { values: {} }).then(r => r.data),
  produceDeleteRecord: (datasheetId: number, recordId: number) =>
    http.delete(`/datasheets/${datasheetId}/produce-edit/records/${recordId}`).then(r => r.data),
}
