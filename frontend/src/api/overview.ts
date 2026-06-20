import { http } from './index'
import type { OverviewBundle, OverviewField, OverviewRow, FieldType, FieldConfig } from '@/types'

export const overviewApi = {
  get: (year?: string) => http.get<OverviewBundle>('/overview', { params: year ? { year } : {} }).then(r => r.data),

  listFields: () => http.get<OverviewField[]>('/overview/fields').then(r => r.data),
  createField: (data: { name: string; type: FieldType; config?: FieldConfig }) =>
    http.post<OverviewField>('/overview/fields', data).then(r => r.data),
  updateField: (id: number, data: { name?: string; type?: FieldType; sort_order?: number; config?: FieldConfig | null }) =>
    http.put<OverviewField>(`/overview/fields/${id}`, data).then(r => r.data),
  deleteField: (id: number) =>
    http.delete<{ message: string }>(`/overview/fields/${id}`).then(r => r.data),

  updateCell: (projectId: number, field_id: number, value: unknown) =>
    http.put<OverviewRow>(`/overview/projects/${projectId}/cell`, { field_id, value }).then(r => r.data),
}
